"""PostgreSQL-backed durable job queue with leases, heartbeats and fencing tokens.

* ``enqueue`` is idempotent on ``operation_id`` (duplicate submissions create one job).
* ``claim`` uses FOR UPDATE SKIP LOCKED and also reclaims jobs whose lease expired (crashed
  worker). It never hands out a job for an investigation that has another live lease.
* Every state change after claiming is fenced by ``lease_token``: a worker that lost its lease
  (e.g. paused past expiry) cannot complete or fail a job that someone else now owns.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Job


@dataclass
class ClaimedJob:
    id: uuid.UUID
    tenant_id: uuid.UUID
    investigation_id: uuid.UUID | None
    kind: str
    payload: dict[str, Any]
    lease_token: uuid.UUID
    attempts: int
    max_attempts: int


def enqueue(
    s: Session,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    operation_id: str,
    investigation_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    max_attempts: int = 5,
) -> bool:
    """Returns True if a new job was created, False if the operation already existed."""
    stmt = (
        insert(Job)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            kind=kind,
            payload=payload or {},
            operation_id=operation_id,
            status="queued",
            max_attempts=max_attempts,
        )
        .on_conflict_do_nothing(index_elements=["operation_id"])
    )
    return bool(s.execute(stmt).rowcount)


CLAIM_SQL = text(
    """
    UPDATE jobs SET status = 'running',
                    attempts = attempts + 1,
                    lease_owner = :owner,
                    lease_token = :token,
                    lease_expires_at = now() + make_interval(secs => :lease),
                    heartbeat_at = now()
     WHERE id = (
        SELECT j.id FROM jobs j
         WHERE ((j.status = 'queued' AND j.run_after <= now())
                OR (j.status = 'running' AND j.lease_expires_at < now()))
           AND j.attempts < j.max_attempts
           AND NOT EXISTS (
                SELECT 1 FROM jobs o
                 WHERE o.investigation_id = j.investigation_id
                   AND o.id <> j.id AND o.status = 'running' AND o.lease_expires_at >= now())
         ORDER BY j.created_at
         FOR UPDATE SKIP LOCKED
         LIMIT 1)
    RETURNING id, tenant_id, investigation_id, kind, payload, lease_token, attempts, max_attempts
    """
)


def claim(s: Session, owner: str, lease_seconds: int) -> ClaimedJob | None:
    row = s.execute(CLAIM_SQL, {"owner": owner, "token": uuid.uuid4(), "lease": lease_seconds}).mappings().first()
    s.commit()
    return ClaimedJob(**row) if row else None


def heartbeat(s: Session, job_id: uuid.UUID, token: uuid.UUID, lease_seconds: int) -> bool:
    res = s.execute(
        text("UPDATE jobs SET heartbeat_at = now(), lease_expires_at = now() + make_interval(secs => :lease) "
             "WHERE id = :id AND lease_token = :token AND status = 'running'"),
        {"id": job_id, "token": token, "lease": lease_seconds},
    )
    s.commit()
    return res.rowcount == 1


def complete(s: Session, job_id: uuid.UUID, token: uuid.UUID) -> bool:
    res = s.execute(
        text("UPDATE jobs SET status = 'done', finished_at = now(), lease_expires_at = NULL "
             "WHERE id = :id AND lease_token = :token AND status = 'running'"),
        {"id": job_id, "token": token},
    )
    s.commit()
    return res.rowcount == 1


def fail(s: Session, job: ClaimedJob, error: str, retryable: bool) -> str:
    """Requeue with bounded exponential backoff, or mark failed. Returns the new status."""
    final = (not retryable) or job.attempts >= job.max_attempts
    status = "failed" if final else "queued"
    run_after = datetime.now(UTC) + timedelta(seconds=min(60, 2 ** job.attempts))
    s.execute(
        text("UPDATE jobs SET status = :status, last_error = :err, run_after = :run_after, lease_expires_at = NULL, "
             "finished_at = CASE WHEN :status = 'failed' THEN now() ELSE NULL END "
             "WHERE id = :id AND lease_token = :token"),
        {"status": status, "err": error[:2000], "run_after": run_after, "id": job.id, "token": job.lease_token},
    )
    s.commit()
    return status
