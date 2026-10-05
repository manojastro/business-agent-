"""Investigation business rules (kept out of route handlers)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.errors import APIError, forbidden, not_found
from app.config import get_settings
from app.db.models import Approval, Investigation, Report, ReportVersion, Tenant, User
from app.metrics.catalog import BUILTIN_METRICS
from app.metrics.windows import Window, WindowError, validate_pair
from app.workers import queue

TERMINAL = {"completed", "rejected", "cancelled", "failed", "insufficient_evidence"}


def get_owned(db: Session, tenant_id: uuid.UUID, investigation_id: uuid.UUID) -> Investigation:
    inv = db.get(Investigation, investigation_id)
    if inv is None or inv.tenant_id != tenant_id:
        raise not_found("investigation")
    return inv


def create(
    db: Session,
    *,
    tenant: Tenant,
    user: User,
    question: str,
    metric_key: str | None,
    as_of: date | None,
    baseline: dict[str, date] | None,
    current: dict[str, date] | None,
    idempotency_key: str | None,
    model_mode: str | None = None,
    rerun_of: uuid.UUID | None = None,
) -> tuple[Investigation, bool]:
    s = get_settings()
    if idempotency_key:
        existing = db.scalar(select(Investigation).where(
            Investigation.tenant_id == tenant.id, Investigation.owner_id == user.id,
            Investigation.idempotency_key == idempotency_key))
        if existing is not None:
            return existing, False
    if metric_key is not None and metric_key not in BUILTIN_METRICS:
        raise APIError(422, "unknown_metric", f"unknown metric {metric_key}")
    as_of = as_of or s.demo_as_of_date
    if (baseline is None) != (current is None):
        raise APIError(422, "invalid_windows", "provide both baseline and current windows, or neither")
    if baseline and current:
        try:
            validate_pair(Window(baseline["start"], baseline["end"]), Window(current["start"], current["end"]), as_of)
        except WindowError as exc:
            raise APIError(422, "invalid_windows", str(exc)) from exc
    mode = model_mode or s.model_mode
    if mode == "real" and not (s.model_name and s.model_endpoint and s.model_api_key):
        raise APIError(409, "real_model_not_configured", "real model mode is not configured on this server")
    inv = Investigation(
        tenant_id=tenant.id,
        owner_id=user.id,
        question=question.strip()[:2000],
        metric_key=metric_key,
        as_of_date=as_of,
        timezone=tenant.timezone,
        baseline_start=baseline["start"] if baseline else None,
        baseline_end=baseline["end"] if baseline else None,
        current_start=current["start"] if current else None,
        current_end=current["end"] if current else None,
        status="queued",
        model_config_={"mode": mode, "provider": "fixture" if mode == "fixture" else s.model_provider,
                       "model": "deterministic-fixture-v1" if mode == "fixture" else s.model_name},
        max_cost=s.max_run_cost,
        max_source_queries=s.max_source_queries,
        idempotency_key=idempotency_key,
        rerun_of=rerun_of,
    )
    db.add(inv)
    try:
        db.flush()
    except IntegrityError:  # concurrent request with the same idempotency key
        db.rollback()
        existing = db.scalar(select(Investigation).where(
            Investigation.tenant_id == tenant.id, Investigation.owner_id == user.id,
            Investigation.idempotency_key == idempotency_key))
        if existing is None:
            raise
        return existing, False
    queue.enqueue(db, tenant_id=tenant.id, kind="investigate", investigation_id=inv.id, operation_id=f"run:{inv.id}")
    return inv, True


def cancel(db: Session, inv: Investigation) -> None:
    if inv.status in TERMINAL:
        raise APIError(409, "already_finished", f"investigation is {inv.status}")
    inv.cancel_requested = True
    if inv.status in ("awaiting_clarification", "awaiting_review", "queued"):
        # Nothing is running: wake the graph so it routes to the cancelled node.
        queue.enqueue(db, tenant_id=inv.tenant_id, kind="resume", investigation_id=inv.id,
                      operation_id=f"cancel:{inv.id}", payload={"resume": {"decision": "cancel", "choice": "stop"}})


def submit_clarification(db: Session, inv: Investigation, answer: dict[str, Any]) -> None:
    if inv.status != "awaiting_clarification" or not inv.pending_question:
        raise APIError(409, "no_pending_question", "this investigation is not waiting for clarification")
    ptype = inv.pending_question.get("type")
    if ptype == "metric" and answer.get("metric_key") not in BUILTIN_METRICS:
        raise APIError(422, "invalid_answer", "metric_key must be one of the offered metrics")
    if ptype == "incomplete_data" and answer.get("choice") not in ("continue", "stop"):
        raise APIError(422, "invalid_answer", "choice must be 'continue' or 'stop'")
    inv.status = "running"
    queue.enqueue(db, tenant_id=inv.tenant_id, kind="resume", investigation_id=inv.id,
                  operation_id=f"clarify:{inv.id}:{uuid.uuid4()}", payload={"resume": answer})


def decide(db: Session, *, inv: Investigation, reviewer: User, decision: str, comment: str, version: int) -> Approval:
    report = db.scalar(select(Report).where(Report.investigation_id == inv.id))
    if report is None or inv.status != "awaiting_review":
        raise APIError(409, "not_in_review", "no report is awaiting review")
    if version != report.current_version:
        raise APIError(409, "stale_version", f"current version is {report.current_version}")
    if reviewer.id == inv.owner_id:
        raise forbidden("the investigation owner cannot review their own report")
    if decision == "request_changes" and not comment.strip():
        raise APIError(422, "comment_required", "explain the requested changes")
    rv = db.scalar(select(ReportVersion).where(ReportVersion.report_id == report.id, ReportVersion.version == version))
    assert rv is not None
    appr = Approval(report_id=report.id, report_version_id=rv.id, tenant_id=inv.tenant_id, reviewer_id=reviewer.id,
                    decision=decision, comment=comment.strip()[:2000])
    db.add(appr)
    report.status = {"approve": "approving", "request_changes": "changes_requested", "reject": "rejecting"}[decision]
    inv.status = "running"
    queue.enqueue(db, tenant_id=inv.tenant_id, kind="resume", investigation_id=inv.id,
                  operation_id=f"review:{inv.id}:v{version}",
                  payload={"resume": {"decision": decision, "comment": comment.strip()[:1000]}})
    return appr


def budget_view(inv: Investigation) -> dict[str, Any]:
    s = get_settings()
    return {
        "queries_used": inv.queries_used,
        "queries_max": inv.max_source_queries,
        "steps_used": inv.steps_used,
        "steps_max": inv.step_budget,
        "tokens_used": inv.tokens_used,
        "cost_used": str(inv.cost_used),
        "cost_max": str(inv.max_cost),
        "hypotheses_max": s.max_hypotheses,
        "cost_note": "fixture mode: no model cost" if (inv.model_config_ or {}).get("mode") == "fixture" else "",
    }


def as_decimal(x: Any) -> Decimal:
    return Decimal(str(x))
