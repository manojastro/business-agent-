"""Investigations, live events, evidence, clarification, cancellation, reports and exports."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select

from app.api.deps import Ctx, current, paginate, require
from app.api.errors import APIError, forbidden, not_found
from app.api.schemas import ERRORS, ClarificationIn, DecisionIn, InvestigationCreate, InvestigationCreated
from app.db.models import (
    Approval,
    Claim,
    EvidenceItem,
    Hypothesis,
    Investigation,
    InvestigationStep,
    QueryPlanRecord,
    Report,
    ReportVersion,
    User,
)
from app.db.session import session_scope
from app.reports.export import render_html, render_pdf
from app.services import investigations as svc

router = APIRouter(prefix="/api/v1", responses=ERRORS)


def _inv_out(i: Investigation, owner: User | None = None) -> dict[str, Any]:
    return {
        "id": str(i.id),
        "question": i.question,
        "status": i.status,
        "metric_key": i.metric_key,
        "metric_version": i.metric_version,
        "baseline_window": {"start": i.baseline_start.isoformat(), "end": i.baseline_end.isoformat()}
        if i.baseline_start and i.baseline_end else None,
        "current_window": {"start": i.current_start.isoformat(), "end": i.current_end.isoformat()}
        if i.current_start and i.current_end else None,
        "as_of": i.as_of_date.isoformat(),
        "timezone": i.timezone,
        "source_watermark": i.source_watermark.isoformat() if i.source_watermark else None,
        "model": i.model_config_,
        "simulation_label": "Demo simulation" if (i.model_config_ or {}).get("mode") == "fixture" else None,
        "budgets": svc.budget_view(i),
        "pending_question": i.pending_question,
        "primary_driver": i.primary_driver,
        "error": i.error,
        "cancel_requested": i.cancel_requested,
        "incomplete_data": i.incomplete_data_ack,
        "owner": {"id": str(owner.id), "display_name": owner.display_name} if owner else {"id": str(i.owner_id)},
        "rerun_of": str(i.rerun_of) if i.rerun_of else None,
        "created_at": i.created_at.isoformat(),
        "updated_at": i.updated_at.isoformat(),
        "completed_at": i.completed_at.isoformat() if i.completed_at else None,
    }


@router.post("/investigations", tags=["investigations"], status_code=202, response_model=InvestigationCreated)
def create_investigation(
    body: InvestigationCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
    ctx: Ctx = Depends(require("analyst", "admin")),
) -> dict[str, Any]:
    inv, created = svc.create(
        ctx.db, tenant=ctx.tenant, user=ctx.user, question=body.question, metric_key=body.metric_key, as_of=body.as_of,
        baseline=body.baseline_window.model_dump() if body.baseline_window else None,
        current=body.current_window.model_dump() if body.current_window else None,
        idempotency_key=idempotency_key, model_mode=body.model_mode,
    )
    if created:
        ctx.audit("investigation_created", "investigation", inv.id, {"metric": body.metric_key, "mode": inv.model_config_.get("mode")})
    ctx.db.commit()
    response.headers["Location"] = f"/api/v1/investigations/{inv.id}"
    return {"id": str(inv.id), "status": inv.status, "status_url": f"/api/v1/investigations/{inv.id}",
            "events_url": f"/api/v1/investigations/{inv.id}/events", "created": created}


@router.get("/investigations", tags=["investigations"])
def list_investigations(status: str | None = None, limit: int = 20, offset: int = 0, ctx: Ctx = Depends(current)) -> dict[str, Any]:
    limit, offset = paginate(limit, offset)
    q = select(Investigation).where(Investigation.tenant_id == ctx.tenant_id)
    if status:
        q = q.where(Investigation.status == status)
    rows = ctx.db.scalars(q.order_by(Investigation.created_at.desc()).offset(offset).limit(limit + 1)).all()
    return {"items": [_inv_out(i) for i in rows[:limit]], "next_offset": offset + limit if len(rows) > limit else None}


@router.get("/investigations/{investigation_id}", tags=["investigations"])
def get_investigation(investigation_id: uuid.UUID, ctx: Ctx = Depends(current)) -> dict[str, Any]:
    inv = svc.get_owned(ctx.db, ctx.tenant_id, investigation_id)
    out = _inv_out(inv, ctx.db.get(User, inv.owner_id))
    hyps = ctx.db.scalars(select(Hypothesis).where(Hypothesis.investigation_id == inv.id).order_by(Hypothesis.created_at)).all()
    out["hypotheses"] = [{"key": h.key, "statement": h.statement, "kind": h.kind, "status": h.status,
                          "rationale": h.rationale, "evidence_ids": h.evidence_ids} for h in hyps]
    rep = ctx.db.scalar(select(Report).where(Report.investigation_id == inv.id))
    out["report"] = {"id": str(rep.id), "status": rep.status, "current_version": rep.current_version} if rep else None
    out["permissions"] = {
        "can_review": ctx.role in ("reviewer", "admin") and ctx.user.id != inv.owner_id,
        "can_cancel": ctx.role == "admin" or ctx.user.id == inv.owner_id,
        "can_rerun": ctx.role in ("analyst", "admin"),
        "can_view_sql": ctx.can_view_sql(),
    }
    return out


@router.get("/investigations/{investigation_id}/steps", tags=["investigations"])
def list_steps(investigation_id: uuid.UUID, after: int = 0, limit: int = 200, ctx: Ctx = Depends(current)) -> dict[str, Any]:
    """Polling fallback for the event stream (cursor = step id)."""
    svc.get_owned(ctx.db, ctx.tenant_id, investigation_id)
    limit = max(1, min(limit, 500))
    rows = ctx.db.scalars(select(InvestigationStep).where(InvestigationStep.investigation_id == investigation_id,
                                                          InvestigationStep.id > after)
                          .order_by(InvestigationStep.id).limit(limit)).all()
    items = [_step_out(r) for r in rows]
    return {"items": items, "next_after": items[-1]["id"] if items else after}


def _step_out(r: InvestigationStep) -> dict[str, Any]:
    return {"id": r.id, "node": r.node, "kind": r.kind, "title": r.title, "rationale": r.rationale,
            "detail": r.detail, "created_at": r.created_at.isoformat()}


def _fetch_events(investigation_id: uuid.UUID, tenant_id: uuid.UUID, cursor: int) -> tuple[bool, str, list[dict[str, Any]]]:
    with session_scope() as s:
        inv = s.get(Investigation, investigation_id)
        if inv is None or inv.tenant_id != tenant_id:
            return False, "", []
        rows = s.scalars(select(InvestigationStep).where(InvestigationStep.investigation_id == investigation_id,
                                                        InvestigationStep.id > cursor)
                         .order_by(InvestigationStep.id).limit(200)).all()
        return True, inv.status, [_step_out(r) for r in rows]


@router.get("/investigations/{investigation_id}/events", tags=["investigations"])
async def stream_events(
    investigation_id: uuid.UUID,
    request: Request,
    after: int = 0,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ctx: Ctx = Depends(current),
) -> StreamingResponse:
    """Server-sent events. Reconnecting with Last-Event-ID (or ?after=) replays persisted history."""
    svc.get_owned(ctx.db, ctx.tenant_id, investigation_id)
    tenant_id = ctx.tenant_id
    ctx.db.close()
    cursor = int(last_event_id) if last_event_id and last_event_id.isdigit() else after

    async def gen() -> Any:
        nonlocal cursor
        idle = 0
        last_status = None
        yield "retry: 3000\n\n"
        for _ in range(1800):  # ~30 minutes, then the client reconnects
            if await request.is_disconnected():
                return
            found, status, payload = await asyncio.to_thread(_fetch_events, investigation_id, tenant_id, cursor)
            if not found:
                return
            for p in payload:
                cursor = p["id"]
                yield f"id: {p['id']}\nevent: step\ndata: {json.dumps(p, default=str)}\n\n"
            if status != last_status:
                last_status = status
                yield f"event: status\ndata: {json.dumps({'status': status})}\n\n"
            if not payload:
                idle += 1
                if idle % 15 == 0:
                    yield ": keep-alive\n\n"
                if status in svc.TERMINAL and idle > 2:
                    return
            else:
                idle = 0
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})


@router.post("/investigations/{investigation_id}/clarification", tags=["investigations"])
def clarify(investigation_id: uuid.UUID, body: ClarificationIn, ctx: Ctx = Depends(require("analyst", "admin"))) -> dict[str, Any]:
    inv = svc.get_owned(ctx.db, ctx.tenant_id, investigation_id)
    if ctx.role != "admin" and inv.owner_id != ctx.user.id:
        raise forbidden("only the investigation owner can answer clarifications")
    svc.submit_clarification(ctx.db, inv, body.model_dump(exclude_none=True))
    ctx.audit("clarification_submitted", "investigation", inv.id, body.model_dump(exclude_none=True))
    ctx.db.commit()
    return {"status": inv.status}


@router.post("/investigations/{investigation_id}/cancel", tags=["investigations"])
def cancel(investigation_id: uuid.UUID, ctx: Ctx = Depends(current)) -> dict[str, Any]:
    inv = svc.get_owned(ctx.db, ctx.tenant_id, investigation_id)
    if ctx.role != "admin" and inv.owner_id != ctx.user.id:
        raise forbidden("only the owner or an admin can cancel")
    svc.cancel(ctx.db, inv)
    ctx.audit("investigation_cancel_requested", "investigation", inv.id)
    ctx.db.commit()
    return {"status": inv.status, "cancel_requested": True}


@router.post("/investigations/{investigation_id}/rerun", tags=["investigations"], status_code=202, response_model=InvestigationCreated)
def rerun(investigation_id: uuid.UUID, ctx: Ctx = Depends(require("analyst", "admin"))) -> dict[str, Any]:
    old = svc.get_owned(ctx.db, ctx.tenant_id, investigation_id)
    inv, _ = svc.create(
        ctx.db, tenant=ctx.tenant, user=ctx.user, question=old.question, metric_key=old.metric_key, as_of=old.as_of_date,
        baseline={"start": old.baseline_start, "end": old.baseline_end} if old.baseline_start and old.baseline_end else None,
        current={"start": old.current_start, "end": old.current_end} if old.current_start and old.current_end else None,
        idempotency_key=None, model_mode=(old.model_config_ or {}).get("mode"), rerun_of=old.id,
    )
    ctx.audit("investigation_rerun", "investigation", inv.id, {"rerun_of": str(old.id)})
    ctx.db.commit()
    return {"id": str(inv.id), "status": inv.status, "status_url": f"/api/v1/investigations/{inv.id}",
            "events_url": f"/api/v1/investigations/{inv.id}/events", "created": True}


# ------------------------------------------------------------------------------- evidence


def _evidence_out(e: EvidenceItem, ctx: Ctx, full: bool) -> dict[str, Any]:
    out = {"id": str(e.id), "investigation_id": str(e.investigation_id), "label": e.label, "kind": e.kind,
           "row_count": e.row_count, "result_hash": e.result_hash, "plan_hash": e.plan_hash, "execution_ms": e.execution_ms,
           "source_watermark": e.source_watermark.isoformat() if e.source_watermark else None,
           "metric_key": e.metric_key, "metric_version": e.metric_version, "created_at": e.created_at.isoformat()}
    if full:
        out["result"] = e.result
        if ctx.can_view_sql():
            out["compiled_sql"] = e.compiled_sql
            out["params"] = e.params
        else:
            out["compiled_sql"] = None
            out["params"] = {"redacted": "SQL and parameters are visible to analysts and admins"}
    return out


@router.get("/investigations/{investigation_id}/evidence", tags=["evidence"])
def list_evidence(investigation_id: uuid.UUID, ctx: Ctx = Depends(current)) -> dict[str, Any]:
    svc.get_owned(ctx.db, ctx.tenant_id, investigation_id)
    rows = ctx.db.scalars(select(EvidenceItem).where(EvidenceItem.investigation_id == investigation_id)
                          .order_by(EvidenceItem.created_at)).all()
    plans = ctx.db.scalars(select(QueryPlanRecord).where(QueryPlanRecord.investigation_id == investigation_id)
                           .order_by(QueryPlanRecord.created_at)).all()
    return {"items": [_evidence_out(e, ctx, False) for e in rows],
            "query_plans": [{"id": str(p.id), "hypothesis_key": p.hypothesis_key, "status": p.status, "attempt": p.attempt,
                             "plan": p.plan, "plan_hash": p.plan_hash, "validation_errors": p.validation_errors}
                            for p in plans]}


@router.get("/evidence/{evidence_id}", tags=["evidence"])
def get_evidence(evidence_id: uuid.UUID, ctx: Ctx = Depends(current)) -> dict[str, Any]:
    e = ctx.db.get(EvidenceItem, evidence_id)
    if e is None or e.tenant_id != ctx.tenant_id:
        raise not_found("evidence")
    return _evidence_out(e, ctx, True)


@router.get("/evidence", tags=["evidence"])
def search_evidence(limit: int = 50, offset: int = 0, kind: str | None = None, ctx: Ctx = Depends(current)) -> dict[str, Any]:
    limit, offset = paginate(limit, offset)
    q = select(EvidenceItem).where(EvidenceItem.tenant_id == ctx.tenant_id)
    if kind:
        q = q.where(EvidenceItem.kind == kind)
    rows = ctx.db.scalars(q.order_by(EvidenceItem.created_at.desc()).offset(offset).limit(limit + 1)).all()
    return {"items": [_evidence_out(e, ctx, False) for e in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None}


@router.get("/investigations/{investigation_id}/claims", tags=["evidence"])
def list_claims(investigation_id: uuid.UUID, ctx: Ctx = Depends(current)) -> dict[str, Any]:
    svc.get_owned(ctx.db, ctx.tenant_id, investigation_id)
    rows = ctx.db.scalars(select(Claim).where(Claim.investigation_id == investigation_id).order_by(Claim.created_at)).all()
    return {"items": [{"id": str(c.id), "key": c.claim_key, "wording": c.wording, "claim_type": c.claim_type,
                       "origin": c.origin, "evidence_ids": c.evidence_ids, "numeric_fields": c.numeric_fields,
                       "scope": c.scope, "limitations": c.limitations, "verification_status": c.verification_status,
                       "verification_detail": c.verification_detail} for c in rows]}


# ------------------------------------------------------------------------------- reports


def _report_for(ctx: Ctx, investigation_id: uuid.UUID) -> tuple[Investigation, Report]:
    inv = svc.get_owned(ctx.db, ctx.tenant_id, investigation_id)
    rep = ctx.db.scalar(select(Report).where(Report.investigation_id == inv.id))
    if rep is None:
        raise not_found("report")
    return inv, rep


@router.get("/investigations/{investigation_id}/report", tags=["reports"])
def get_report(investigation_id: uuid.UUID, version: int | None = None, ctx: Ctx = Depends(current)) -> dict[str, Any]:
    inv, rep = _report_for(ctx, investigation_id)
    versions = ctx.db.scalars(select(ReportVersion).where(ReportVersion.report_id == rep.id).order_by(ReportVersion.version)).all()
    chosen = next((v for v in versions if v.version == (version or rep.current_version)), None)
    if chosen is None:
        raise not_found("report version")
    approvals = ctx.db.execute(select(Approval, User).join(User, User.id == Approval.reviewer_id)
                               .where(Approval.report_id == rep.id).order_by(Approval.created_at)).all()
    return {
        "id": str(rep.id), "investigation_id": str(inv.id), "status": rep.status, "current_version": rep.current_version,
        "version": chosen.version, "content": chosen.content, "content_hash": chosen.content_hash,
        "versions": [{"version": v.version, "created_at": v.created_at.isoformat(), "revision_note": v.revision_note,
                      "content_hash": v.content_hash} for v in versions],
        "decisions": [{"decision": a.decision, "comment": a.comment, "reviewer": u.display_name,
                       "report_version_id": str(a.report_version_id), "created_at": a.created_at.isoformat()}
                      for a, u in approvals],
    }


@router.get("/reports", tags=["reports"])
def list_reports(status: str | None = None, limit: int = 20, offset: int = 0, ctx: Ctx = Depends(current)) -> dict[str, Any]:
    limit, offset = paginate(limit, offset)
    q = select(Report, Investigation).join(Investigation, Investigation.id == Report.investigation_id).where(
        Report.tenant_id == ctx.tenant_id)
    if status:
        q = q.where(Report.status == status)
    rows = ctx.db.execute(q.order_by(Report.updated_at.desc()).offset(offset).limit(limit + 1)).all()
    return {"items": [{"id": str(r.id), "investigation_id": str(i.id), "question": i.question, "status": r.status,
                       "investigation_status": i.status, "current_version": r.current_version,
                       "primary_driver": i.primary_driver, "updated_at": r.updated_at.isoformat(),
                       "owner_id": str(i.owner_id)} for r, i in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None}


@router.post("/investigations/{investigation_id}/report/decision", tags=["reports"])
def decide(investigation_id: uuid.UUID, body: DecisionIn, ctx: Ctx = Depends(require("reviewer", "admin"))) -> dict[str, Any]:
    inv = svc.get_owned(ctx.db, ctx.tenant_id, investigation_id)
    appr = svc.decide(ctx.db, inv=inv, reviewer=ctx.user, decision=body.decision, comment=body.comment, version=body.version)
    ctx.audit("report_decision", "report", appr.report_id, {"decision": body.decision, "version": body.version})
    ctx.db.commit()
    return {"decision": body.decision, "status": "accepted"}


@router.get("/investigations/{investigation_id}/report/export", tags=["reports"])
def export_report(investigation_id: uuid.UUID, format: str = "html", ctx: Ctx = Depends(current)) -> Response:
    inv, rep = _report_for(ctx, investigation_id)
    if rep.status != "approved":
        raise APIError(409, "not_approved", "only approved reports can be exported")
    if format not in ("html", "pdf"):
        raise APIError(422, "invalid_format", "format must be html or pdf")
    rv = ctx.db.scalar(select(ReportVersion).where(ReportVersion.report_id == rep.id, ReportVersion.version == rep.current_version))
    assert rv is not None
    appr = ctx.db.execute(select(Approval, User).join(User, User.id == Approval.reviewer_id)
                          .where(Approval.report_version_id == rv.id, Approval.decision == "approve")).first()
    meta = {"version": rv.version, "status": rep.status, "content_hash": rv.content_hash,
            "approver": appr[1].display_name if appr else None,
            "approved_at": appr[0].created_at.isoformat() if appr else None}
    ctx.audit("report_exported", "report", rep.id, {"format": format, "version": rv.version})
    ctx.db.commit()
    stamp = datetime.now().strftime("%Y%m%d")
    name = f"metric-investigation-{str(inv.id)[:8]}-v{rv.version}-{stamp}"
    if format == "html":
        return HTMLResponse(render_html(rv.content, meta), headers={"Content-Disposition": f'attachment; filename="{name}.html"'})
    return Response(render_pdf(rv.content, meta), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'})
