"""Administration: members, audit log, evaluation suites and runs."""

from __future__ import annotations

import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import Ctx, current, paginate, require
from app.api.errors import APIError, not_found
from app.api.schemas import ERRORS, EvalRunIn, MemberIn, MemberRoleIn
from app.auth.passwords import hash_password
from app.config import get_settings
from app.db.models import AuditEvent, EvalRun, EvalSuite, Membership, User
from app.workers import queue

router = APIRouter(prefix="/api/v1", responses=ERRORS)


@router.get("/admin/members", tags=["admin"])
def members(ctx: Ctx = Depends(require("admin"))) -> dict[str, Any]:
    rows = ctx.db.execute(select(Membership, User).join(User, User.id == Membership.user_id)
                          .where(Membership.tenant_id == ctx.tenant_id).order_by(User.email)).all()
    return {"items": [{"membership_id": str(m.id), "user_id": str(u.id), "email": u.email, "display_name": u.display_name,
                       "role": m.role, "active": u.is_active} for m, u in rows]}


@router.post("/admin/members", tags=["admin"], status_code=201)
def add_member(body: MemberIn, ctx: Ctx = Depends(require("admin"))) -> dict[str, Any]:
    user = ctx.db.scalar(select(User).where(User.email == body.email.lower()))
    temp_password = None
    if user is None:
        temp_password = secrets.token_urlsafe(12)
        user = User(email=body.email.lower(), display_name=body.display_name, password_hash=hash_password(temp_password))
        ctx.db.add(user)
        ctx.db.flush()
    if ctx.db.scalar(select(Membership).where(Membership.user_id == user.id, Membership.tenant_id == ctx.tenant_id)):
        raise APIError(409, "already_member", "user is already a member of this tenant")
    m = Membership(user_id=user.id, tenant_id=ctx.tenant_id, role=body.role)
    ctx.db.add(m)
    ctx.audit("member_added", "membership", user.id, {"email": user.email, "role": body.role})
    ctx.db.commit()
    return {"membership_id": str(m.id), "user_id": str(user.id), "temporary_password": temp_password,
            "note": "The temporary password is shown once; share it through a secure channel." if temp_password else None}


@router.patch("/admin/members/{membership_id}", tags=["admin"])
def change_role(membership_id: uuid.UUID, body: MemberRoleIn, ctx: Ctx = Depends(require("admin"))) -> dict[str, Any]:
    m = ctx.db.get(Membership, membership_id)
    if m is None or m.tenant_id != ctx.tenant_id:
        raise not_found("membership")
    if m.user_id == ctx.user.id and body.role != "admin":
        raise APIError(409, "self_demotion", "admins cannot remove their own admin role")
    old = m.role
    m.role = body.role
    ctx.audit("member_role_changed", "membership", m.id, {"from": old, "to": body.role})
    ctx.db.commit()
    return {"membership_id": str(m.id), "role": m.role}


@router.delete("/admin/members/{membership_id}", tags=["admin"])
def remove_member(membership_id: uuid.UUID, ctx: Ctx = Depends(require("admin"))) -> dict[str, Any]:
    m = ctx.db.get(Membership, membership_id)
    if m is None or m.tenant_id != ctx.tenant_id:
        raise not_found("membership")
    if m.user_id == ctx.user.id:
        raise APIError(409, "self_removal", "admins cannot remove themselves")
    ctx.audit("member_removed", "membership", m.id, {"user_id": str(m.user_id)})
    ctx.db.delete(m)
    ctx.db.commit()
    return {"removed": True}


@router.get("/admin/audit", tags=["admin"])
def audit_log(limit: int = 50, offset: int = 0, action: str | None = None, ctx: Ctx = Depends(require("admin"))) -> dict[str, Any]:
    limit, offset = paginate(limit, offset)
    q = select(AuditEvent, User).outerjoin(User, User.id == AuditEvent.actor_id).where(AuditEvent.tenant_id == ctx.tenant_id)
    if action:
        q = q.where(AuditEvent.action == action)
    rows = ctx.db.execute(q.order_by(AuditEvent.id.desc()).offset(offset).limit(limit + 1)).all()
    return {"items": [{"id": a.id, "action": a.action, "object_type": a.object_type, "object_id": a.object_id,
                       "detail": a.detail, "actor": u.email if u else None, "ip": a.ip,
                       "created_at": a.created_at.isoformat()} for a, u in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None}


# ------------------------------------------------------------------------------- evaluation


@router.get("/eval/suites", tags=["evaluation"])
def suites(ctx: Ctx = Depends(current)) -> dict[str, Any]:
    rows = ctx.db.scalars(select(EvalSuite).order_by(EvalSuite.name)).all()
    # Ground truth labels are not returned by the API; only counts and families.
    return {"items": [{"id": str(s.id), "name": s.name, "split": s.split, "description": s.description,
                       "scenario_count": len(s.scenarios),
                       "families": sorted({x["family"] for x in s.scenarios})} for s in rows]}


@router.get("/eval/runs", tags=["evaluation"])
def runs(ctx: Ctx = Depends(current)) -> dict[str, Any]:
    rows = ctx.db.execute(select(EvalRun, EvalSuite).join(EvalSuite, EvalSuite.id == EvalRun.suite_id)
                          .order_by(EvalRun.started_at.desc()).limit(20)).all()
    return {"items": [{"id": str(r.id), "suite": s.name, "systems": r.systems, "model_mode": r.model_mode, "status": r.status,
                       "summary": r.summary, "started_at": r.started_at.isoformat(),
                       "finished_at": r.finished_at.isoformat() if r.finished_at else None} for r, s in rows]}


@router.get("/eval/runs/{run_id}", tags=["evaluation"])
def run_detail(run_id: uuid.UUID, ctx: Ctx = Depends(require("admin"))) -> dict[str, Any]:
    r = ctx.db.get(EvalRun, run_id)
    if r is None:
        raise not_found("evaluation run")
    return {"id": str(r.id), "status": r.status, "systems": r.systems, "model_mode": r.model_mode, "summary": r.summary,
            "results": r.results, "started_at": r.started_at.isoformat(),
            "finished_at": r.finished_at.isoformat() if r.finished_at else None}


@router.post("/eval/runs", tags=["evaluation"], status_code=202)
def start_run(body: EvalRunIn, ctx: Ctx = Depends(require("admin"))) -> dict[str, Any]:
    suite = ctx.db.scalar(select(EvalSuite).where(EvalSuite.name == body.suite))
    if suite is None:
        raise not_found("evaluation suite")
    mode = body.model_mode or get_settings().model_mode
    run = EvalRun(suite_id=suite.id, systems=list(body.systems), model_mode=mode, status="queued", created_by=ctx.user.id,
                  summary={"limit": body.limit})
    ctx.db.add(run)
    ctx.db.flush()
    queue.enqueue(ctx.db, tenant_id=ctx.tenant_id, kind="eval", operation_id=f"eval:{run.id}",
                  payload={"eval_run_id": str(run.id)}, max_attempts=1)
    ctx.audit("eval_run_started", "eval_run", run.id, {"suite": body.suite, "systems": body.systems, "mode": mode})
    ctx.db.commit()
    return {"id": str(run.id), "status": "queued"}
