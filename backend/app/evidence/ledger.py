"""Evidence ledger: append-only, hash-addressed records of every query and calculation."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.analytics import SourceResult
from app.db.models import EvidenceItem, Investigation, QueryPlanRecord, QueryRun
from app.tools.query_plan import CompiledQuery, params_for_evidence


def canonical_hash(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def operation_id(investigation_id: uuid.UUID, plan_hash: str) -> str:
    return f"{investigation_id}:{plan_hash}"


def find_completed_run(session: Session, inv: Investigation, plan_hash: str) -> EvidenceItem | None:
    """Reuse evidence for an identical plan in this investigation (idempotent replays/resume)."""
    run = session.scalar(select(QueryRun).where(QueryRun.operation_id == operation_id(inv.id, plan_hash)))
    if run is None or run.status != "succeeded":
        return None
    return session.scalar(select(EvidenceItem).where(EvidenceItem.query_run_id == run.id))


def start_run(session: Session, inv: Investigation, plan_rec: QueryPlanRecord, compiled: CompiledQuery) -> QueryRun:
    op = operation_id(inv.id, compiled.plan_hash)
    run = session.scalar(select(QueryRun).where(QueryRun.operation_id == op))
    if run is None:
        run = QueryRun(
            investigation_id=inv.id,
            tenant_id=inv.tenant_id,
            query_plan_id=plan_rec.id,
            operation_id=op,
            plan_hash=compiled.plan_hash,
            compiled_sql=compiled.sql,
            params=params_for_evidence(compiled.params),
            status="running",
        )
        session.add(run)
    else:  # a previous attempt failed or was interrupted: same operation, new attempt
        run.status = "running"
        run.error = None
    session.flush()
    return run


def finish_run_ok(
    session: Session,
    inv: Investigation,
    run: QueryRun,
    compiled: CompiledQuery,
    result: SourceResult,
    label: str,
    kind: str = "query",
) -> EvidenceItem:
    run.status = "succeeded"
    run.row_count = result.row_count
    run.execution_ms = result.execution_ms
    run.finished_at = datetime.now(UTC)
    payload = {"shape": compiled.shape, "rows": result.rows, "truncated": result.truncated}
    ev = EvidenceItem(
        investigation_id=inv.id,
        tenant_id=inv.tenant_id,
        query_run_id=run.id,
        label=label[:300],
        kind=kind,
        plan_hash=compiled.plan_hash,
        compiled_sql=compiled.sql,
        params=params_for_evidence(compiled.params),
        row_count=result.row_count,
        result=payload,
        result_hash=canonical_hash(payload),
        execution_ms=result.execution_ms,
        source_watermark=inv.source_watermark,
        metric_key=inv.metric_key,
        metric_version=inv.metric_version,
    )
    session.add(ev)
    session.flush()
    return ev


def finish_run_failed(session: Session, run: QueryRun, error: str) -> None:
    run.status = "failed"
    run.error = error[:1000]
    run.finished_at = datetime.now(UTC)
    session.flush()


def record_calculation(
    session: Session,
    inv: Investigation,
    label: str,
    function: str,
    inputs: list[str],
    output: dict[str, Any],
    kind: str = "calculation",
) -> EvidenceItem:
    payload = {"function": function, "inputs": inputs, "output": output}
    h = canonical_hash(payload)
    existing = session.scalar(
        select(EvidenceItem).where(EvidenceItem.investigation_id == inv.id, EvidenceItem.result_hash == h)
    )
    if existing is not None:
        return existing
    ev = EvidenceItem(
        investigation_id=inv.id,
        tenant_id=inv.tenant_id,
        label=label[:300],
        kind=kind,
        params={"inputs": inputs, "function": function},
        row_count=len(output.get("rows", [])) if isinstance(output.get("rows"), list) else 1,
        result=payload,
        result_hash=h,
        source_watermark=inv.source_watermark,
        metric_key=inv.metric_key,
        metric_version=inv.metric_version,
    )
    session.add(ev)
    session.flush()
    return ev
