"""Health, authentication/session, catalog, connections, freshness, data quality and overview."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import Ctx, client_ip, current, require
from app.api.errors import APIError, not_found
from app.api.schemas import ERRORS, LoginIn, MetricVersionIn, SessionOut, SwitchTenantIn
from app.auth.passwords import verify_password
from app.auth.sessions import COOKIE_NAME, create_session, lookup, revoke
from app.config import get_settings
from app.db.analytics import analytics_engine
from app.db.models import (
    AuditEvent,
    Connection,
    EvalRun,
    Investigation,
    Membership,
    MetricDefinition,
    Tenant,
    User,
)
from app.db.session import get_db
from app.metrics.catalog import DIMENSIONS, validate_definition
from app.services import source

router = APIRouter(prefix="/api/v1", responses=ERRORS)


# ------------------------------------------------------------------------------- health


@router.get("/health/live", tags=["health"])
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", tags=["health"])
def ready(db: Session = Depends(get_db)) -> dict[str, Any]:
    checks = {}
    try:
        db.execute(text("SELECT 1"))
        checks["app_db"] = "ok"
    except Exception:  # noqa: BLE001
        checks["app_db"] = "error"
    try:
        with analytics_engine().connect() as c:
            c.execute(text("SELECT 1"))
        checks["analytics_db"] = "ok"
    except Exception:  # noqa: BLE001
        checks["analytics_db"] = "error"
    if any(v != "ok" for v in checks.values()):
        raise APIError(503, "not_ready", "dependencies unavailable", checks)
    return {"status": "ok", "checks": checks, "model_mode": get_settings().model_mode}


# ------------------------------------------------------------------------------- auth


def _tenants(db: Session, user: User) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Tenant, Membership.role).join(Membership, Membership.tenant_id == Tenant.id)
        .where(Membership.user_id == user.id).order_by(Tenant.kind, Tenant.name)
    ).all()
    return [{"id": str(t.id), "slug": t.slug, "name": t.name, "role": r, "kind": t.kind} for t, r in rows]


def _session_out(db: Session, user: User, row: Any) -> dict[str, Any]:
    tenants = _tenants(db, user)
    active = next((t for t in tenants if t["id"] == str(row.active_tenant_id)), None)
    s = get_settings()
    return {
        "user": {"id": str(user.id), "email": user.email, "display_name": user.display_name},
        "active_tenant": active,
        "tenants": tenants,
        "csrf_token": row.csrf_token,
        "model_mode": s.model_mode,
        "demo_as_of": s.demo_as_of_date.isoformat(),
    }


@router.post("/auth/login", tags=["auth"], response_model=SessionOut)
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict[str, Any]:
    user = db.scalar(select(User).where(func.lower(User.email) == body.email.lower()))
    ok = verify_password(user.password_hash if user else None, body.password)
    if not ok or user is None or not user.is_active:
        db.add(AuditEvent(action="login_failed", object_type="user", detail={"email": body.email.lower()[:320]},
                          ip=client_ip(request)))
        db.commit()
        raise APIError(401, "invalid_credentials", "email or password is incorrect")
    tenants = _tenants(db, user)
    demo_first = sorted(tenants, key=lambda t: t["kind"] != "demo")
    token, row = create_session(db, user.id, uuid.UUID(demo_first[0]["id"]) if demo_first else None,
                                get_settings().session_ttl_hours)
    db.add(AuditEvent(tenant_id=row.active_tenant_id, actor_id=user.id, action="login", object_type="session",
                      object_id=str(row.id), ip=client_ip(request)))
    db.commit()
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", secure=get_settings().is_production,
                        max_age=get_settings().session_ttl_hours * 3600, path="/")
    return _session_out(db, user, row)


@router.post("/auth/logout", tags=["auth"])
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> dict[str, str]:
    row = lookup(db, request.cookies.get(COOKIE_NAME))
    if row is not None:
        if request.headers.get("X-CSRF-Token") != row.csrf_token:
            raise APIError(403, "csrf_failed", "missing or invalid CSRF token")
        revoke(db, row)
        db.commit()
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"status": "signed_out"}


@router.get("/auth/session", tags=["auth"], response_model=SessionOut)
def session_state(ctx: Ctx = Depends(current)) -> dict[str, Any]:
    out = _session_out(ctx.db, ctx.user, ctx.session)
    ctx.db.commit()
    return out


@router.post("/auth/switch-tenant", tags=["auth"], response_model=SessionOut)
def switch_tenant(body: SwitchTenantIn, ctx: Ctx = Depends(current)) -> dict[str, Any]:
    try:
        tid = uuid.UUID(body.tenant_id)
    except ValueError as exc:
        raise not_found("tenant") from exc
    m = ctx.db.scalar(select(Membership).where(Membership.user_id == ctx.user.id, Membership.tenant_id == tid))
    if m is None:
        raise not_found("tenant")
    ctx.session.active_tenant_id = tid
    ctx.db.add(AuditEvent(tenant_id=tid, actor_id=ctx.user.id, action="switch_tenant", object_type="session",
                          object_id=str(ctx.session.id), ip=ctx.ip))
    ctx.db.commit()
    return _session_out(ctx.db, ctx.user, ctx.session)


@router.get("/auth/oidc/config", tags=["auth"])
def oidc_config() -> dict[str, Any]:
    return {"enabled": False, "provider": "microsoft-entra-id", "note": "Integration seam only; see app/auth/oidc.py"}


# ------------------------------------------------------------------------------- catalog


def _metric_out(m: MetricDefinition) -> dict[str, Any]:
    return {"id": str(m.id), "metric_key": m.metric_key, "version": m.version, "status": m.status,
            "definition": m.definition, "change_reason": m.change_reason, "created_at": m.created_at.isoformat()}


@router.get("/metrics", tags=["catalog"])
def list_metrics(ctx: Ctx = Depends(current)) -> dict[str, Any]:
    rows = ctx.db.scalars(select(MetricDefinition).where(MetricDefinition.tenant_id == ctx.tenant_id)
                          .order_by(MetricDefinition.metric_key, MetricDefinition.version.desc())).all()
    return {"items": [_metric_out(m) for m in rows], "dimensions": DIMENSIONS}


@router.post("/metrics/{metric_key}/versions", tags=["catalog"], status_code=201)
def create_metric_version(metric_key: str, body: MetricVersionIn, ctx: Ctx = Depends(require("admin"))) -> dict[str, Any]:
    """Changing a business metric definition is an authorized, audited human action."""
    cur = ctx.db.scalar(select(MetricDefinition).where(
        MetricDefinition.tenant_id == ctx.tenant_id, MetricDefinition.metric_key == metric_key,
        MetricDefinition.status == "active").order_by(MetricDefinition.version.desc()))
    if cur is None:
        raise not_found("metric")
    new_def = dict(cur.definition)
    if body.description is not None:
        new_def["description"] = body.description
    if body.allowed_dimensions is not None:
        new_def["allowed_dimensions"] = body.allowed_dimensions
    new_def["version"] = cur.version + 1
    errors = validate_definition(new_def)
    if errors:
        raise APIError(422, "invalid_definition", "metric definition is invalid", errors)
    for old in ctx.db.scalars(select(MetricDefinition).where(MetricDefinition.tenant_id == ctx.tenant_id,
                                                             MetricDefinition.metric_key == metric_key,
                                                             MetricDefinition.status == "active")):
        old.status = "retired"
    m = MetricDefinition(tenant_id=ctx.tenant_id, metric_key=metric_key, version=cur.version + 1, status="active",
                         definition=new_def, change_reason=body.change_reason, created_by=ctx.user.id)
    ctx.db.add(m)
    ctx.audit("metric_definition_changed", "metric_definition", metric_key,
              {"from_version": cur.version, "to_version": cur.version + 1, "reason": body.change_reason})
    ctx.db.commit()
    return _metric_out(m)


@router.get("/connections", tags=["catalog"])
def list_connections(ctx: Ctx = Depends(current)) -> dict[str, Any]:
    rows = ctx.db.scalars(select(Connection).where(Connection.tenant_id == ctx.tenant_id)).all()
    return {"items": [{"id": str(c.id), "name": c.name, "kind": c.kind, "status": c.status, "config": c.config,
                       "created_at": c.created_at.isoformat()} for c in rows]}


@router.patch("/connections/{connection_id}", tags=["catalog"])
def update_connection(connection_id: uuid.UUID, body: dict[str, str], ctx: Ctx = Depends(require("admin"))) -> dict[str, Any]:
    c = ctx.db.get(Connection, connection_id)
    if c is None or c.tenant_id != ctx.tenant_id:
        raise not_found("connection")
    status = body.get("status")
    if status not in ("active", "disabled"):
        raise APIError(422, "invalid_status", "status must be active or disabled")
    c.status = status
    ctx.audit("connection_updated", "connection", c.id, {"status": status})
    ctx.db.commit()
    return {"id": str(c.id), "status": c.status}


# ------------------------------------------------------------------------------- source views


def _as_of(as_of: date | None) -> date:
    return as_of or get_settings().demo_as_of_date


@router.get("/source/freshness", tags=["source"])
def source_freshness(as_of: date | None = None, ctx: Ctx = Depends(current)) -> dict[str, Any]:
    return source.freshness(ctx.tenant, _as_of(as_of))


@router.get("/source/quality", tags=["source"])
def source_quality(as_of: date | None = None, ctx: Ctx = Depends(current)) -> dict[str, Any]:
    return source.quality(ctx.db, ctx.tenant, _as_of(as_of))


@router.get("/overview", tags=["overview"])
def overview(ctx: Ctx = Depends(current)) -> dict[str, Any]:
    as_of = _as_of(None)
    recent = ctx.db.scalars(select(Investigation).where(Investigation.tenant_id == ctx.tenant_id)
                            .order_by(Investigation.created_at.desc()).limit(8)).all()
    evals = ctx.db.scalars(select(EvalRun).where(EvalRun.status == "done").order_by(EvalRun.started_at.desc()).limit(3)).all()
    return {
        "as_of": as_of.isoformat(),
        "freshness": source.freshness(ctx.tenant, as_of, lookback_days=14),
        "metric_changes": source.metric_changes(ctx.db, ctx.tenant, as_of),
        "recent_runs": [{"id": str(i.id), "question": i.question, "status": i.status, "metric_key": i.metric_key,
                         "created_at": i.created_at.isoformat(), "primary_driver": i.primary_driver,
                         "model_mode": (i.model_config_ or {}).get("mode")} for i in recent],
        "evaluations": [{"id": str(e.id), "model_mode": e.model_mode, "summary": e.summary,
                         "finished_at": e.finished_at.isoformat() if e.finished_at else None} for e in evals],
    }
