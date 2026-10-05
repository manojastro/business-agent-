"""Worker process: bounded-concurrency job loop with leases and heartbeats.

    python -m app.workers.main

Kill the process at any time: an unfinished job's lease expires, another worker (or this one
after restart) claims it again, and the investigation graph resumes from its last checkpoint
under the same investigation ID.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert

from app.agents.graph import advance
from app.agents.nodes import TransientNodeError
from app.agents.runtime import Runtime, reset_runtime, set_runtime
from app.config import get_settings
from app.db.models import Investigation, WorkerHeartbeat
from app.db.session import new_session, session_scope
from app.providers.factory import get_provider
from app.telemetry import setup_tracing, span
from app.workers import queue

log = logging.getLogger("metric_investigator.worker")


class Worker:
    def __init__(self, worker_id: str | None = None, concurrency: int | None = None) -> None:
        self.settings = get_settings()
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.concurrency = concurrency or self.settings.worker_concurrency
        self.stop = threading.Event()
        self.slots = threading.Semaphore(self.concurrency)

    # --------------------------------------------------------------------------- lifecycle
    def run_forever(self) -> None:
        log.info("worker %s starting with concurrency %d", self.worker_id, self.concurrency)
        threading.Thread(target=self._beat_loop, daemon=True).start()
        while not self.stop.is_set():
            if not self.slots.acquire(timeout=1):
                continue
            job = self._claim()
            if job is None:
                self.slots.release()
                self.stop.wait(1.0)
                continue
            threading.Thread(target=self._run_guarded, args=(job,), daemon=True).start()
        log.info("worker %s stopping", self.worker_id)

    def run_until_idle(self, max_jobs: int = 100) -> int:
        """Process jobs synchronously until none are claimable (tests, CLI, evaluation)."""
        n = 0
        while n < max_jobs:
            job = self._claim()
            if job is None:
                return n
            self._run(job)
            n += 1
        return n

    def _claim(self) -> queue.ClaimedJob | None:
        s = new_session()
        try:
            return queue.claim(s, self.worker_id, self.settings.worker_lease_seconds)
        finally:
            s.close()

    def _beat_loop(self) -> None:
        while not self.stop.is_set():
            try:
                with session_scope() as s:
                    s.execute(
                        insert(WorkerHeartbeat)
                        .values(worker_id=self.worker_id, last_seen_at=datetime.now(UTC), detail={"concurrency": self.concurrency})
                        .on_conflict_do_update(index_elements=["worker_id"], set_={"last_seen_at": datetime.now(UTC)})
                    )
            except Exception:  # noqa: BLE001 - health beacons must not kill the worker
                log.exception("heartbeat beacon failed")
            self.stop.wait(5)

    def _run_guarded(self, job: queue.ClaimedJob) -> None:
        try:
            self._run(job)
        finally:
            self.slots.release()

    # --------------------------------------------------------------------------- one job
    def _run(self, job: queue.ClaimedJob) -> None:
        lost = threading.Event()
        done = threading.Event()

        def beat() -> None:
            while not done.wait(self.settings.worker_heartbeat_seconds):
                s = new_session()
                try:
                    if not queue.heartbeat(s, job.id, job.lease_token, self.settings.worker_lease_seconds):
                        lost.set()
                        return
                finally:
                    s.close()

        hb = threading.Thread(target=beat, daemon=True)
        hb.start()
        try:
            with span("job", {"job.kind": job.kind, "job.id": str(job.id)}):
                self._execute(job)
            done.set()
            s = new_session()
            try:
                if not queue.complete(s, job.id, job.lease_token):
                    log.warning("job %s finished but lease was lost; result left to the new owner", job.id)
            finally:
                s.close()
        except Exception as exc:  # noqa: BLE001
            done.set()
            retryable = isinstance(exc, (TransientNodeError, ConnectionError, TimeoutError)) or "Operational" in type(exc).__name__
            s = new_session()
            try:
                status = queue.fail(s, job, f"{type(exc).__name__}: {exc}", retryable)
            finally:
                s.close()
            log.warning("job %s %s: %s", job.id, status, exc)
            if status == "failed" and job.investigation_id:
                with session_scope() as s2:
                    inv = s2.get(Investigation, job.investigation_id)
                    if inv and inv.status not in ("completed", "rejected", "cancelled"):
                        inv.status = "failed"
                        inv.error = {"code": "job_failed", "detail": str(exc)[:500]}
        finally:
            done.set()

    def _execute(self, job: queue.ClaimedJob) -> None:
        if job.kind in ("investigate", "resume"):
            assert job.investigation_id is not None
            with session_scope() as s:
                inv = s.get(Investigation, job.investigation_id)
                if inv is None or inv.tenant_id != job.tenant_id:
                    raise PermissionError("job tenant does not match investigation tenant")
                mode = (inv.model_config_ or {}).get("mode", self.settings.model_mode)
                if inv.status == "queued":
                    inv.status = "running"
            rt = Runtime(settings=self.settings, provider=get_provider(self.settings, mode), worker_id=self.worker_id,
                         auto_continue_incomplete=bool(job.payload.get("auto_continue_incomplete")))
            token = set_runtime(rt)
            try:
                advance(str(job.investigation_id), str(job.tenant_id), job.payload.get("resume"))
            finally:
                reset_runtime(token)
        elif job.kind == "eval":
            from app.evals.runner import run_eval_job

            run_eval_job(uuid.UUID(job.payload["eval_run_id"]))
        else:
            raise ValueError(f"unknown job kind {job.kind}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    setup_tracing("metric-investigator-worker")
    w = Worker()

    def _stop(*_: Any) -> None:
        w.stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        w.run_forever()
    finally:
        time.sleep(0.1)


if __name__ == "__main__":
    main()
