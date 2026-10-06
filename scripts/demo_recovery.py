"""Restart-recovery demonstration: kill the worker process mid-investigation and resume.

Run from backend/:  uv run python ../scripts/demo_recovery.py
Requires the database to be migrated and seeded. Starts its own worker subprocesses with
FIXTURE_DELAY_SECONDS=2 so the run is slow enough to interrupt, and a short lease.
"""

import os
import subprocess
import sys
import time
import uuid
from decimal import Decimal

from sqlalchemy import select, text

sys.path.insert(0, os.getcwd())
from app.config import get_settings  # noqa: E402
from app.db.models import Investigation, InvestigationStep, Job, QueryRun, Tenant, User  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.workers import queue  # noqa: E402

ENV = {**os.environ, "FIXTURE_DELAY_SECONDS": "2", "WORKER_LEASE_SECONDS": "8", "WORKER_HEARTBEAT_SECONDS": "2"}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def snapshot(iid: uuid.UUID) -> tuple[str, int, int, list[str]]:
    with session_scope() as s:
        inv = s.get(Investigation, iid)
        runs = s.scalar(select(text("count(*)")).select_from(QueryRun).where(QueryRun.investigation_id == iid))
        kinds = [k for k in s.scalars(select(InvestigationStep.kind).where(InvestigationStep.investigation_id == iid)
                                      .order_by(InvestigationStep.id)) if k not in ("tool_call", "model_call")]
        return inv.status, inv.queries_used, runs, kinds


def main() -> None:
    with session_scope() as s:
        t = s.scalar(select(Tenant).where(Tenant.slug == "acme-retail"))
        u = s.scalar(select(User).where(User.email == "analyst@acme.demo"))
        inv = Investigation(tenant_id=t.id, owner_id=u.id, question="Why did net sales fall last week? (restart demo)",
                            metric_key="net_sales", as_of_date=get_settings().demo_as_of_date, timezone=t.timezone,
                            max_cost=Decimal("0.5"), model_config_={"mode": "fixture"}, status="queued")
        s.add(inv)
        s.flush()
        iid = inv.id
        queue.enqueue(s, tenant_id=t.id, kind="investigate", investigation_id=iid, operation_id=f"run:{iid}")
    log(f"investigation {iid} queued")
    w1 = subprocess.Popen([sys.executable, "-m", "app.workers.main"], env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log(f"worker #1 started (pid {w1.pid})")
    while True:
        st, q, runs, kinds = snapshot(iid)
        if q >= 6:
            break
        time.sleep(0.5)
    w1.kill()
    w1.wait()
    st, q, runs, kinds = snapshot(iid)
    log(f"worker #1 KILLED mid-run: status={st}, source queries={q}, query runs={runs}, last event={kinds[-1]}")
    with session_scope() as s:
        job = s.scalar(select(Job).where(Job.operation_id == f"run:{iid}"))
        log(f"job still '{job.status}' with lease owned by {job.lease_owner}; waiting for the lease to expire")
    w2 = subprocess.Popen([sys.executable, "-m", "app.workers.main"], env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log(f"worker #2 started (pid {w2.pid})")
    deadline = time.time() + 180
    while time.time() < deadline:
        st, q2, runs2, kinds = snapshot(iid)
        if st in ("awaiting_review", "failed"):
            break
        time.sleep(1)
    w2.terminate()
    w2.wait(timeout=30)
    with session_scope() as s:
        job = s.scalar(select(Job).where(Job.operation_id == f"run:{iid}"))
        attempts, jstatus, owner = job.attempts, job.status, job.lease_owner
    log(f"worker #2 finished: status={st}, source queries={q2}, query runs={runs2}, job={jstatus} after {attempts} attempts (last owner {owner})")
    with session_scope() as s:
        retried = s.scalar(select(text("count(*)")).select_from(QueryRun).where(QueryRun.investigation_id == iid))
    ok = st == "awaiting_review" and runs2 == q2 == retried and attempts == 2
    log("RESULT: " + ("resumed from checkpoint under the same investigation id; completed queries were reused, none duplicated"
                      if ok else "unexpected outcome - inspect the investigation events"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
