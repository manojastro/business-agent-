"""Request context: authenticated user, active tenant, role checks, CSRF and auditing."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import APIError, forbidden
from app.auth.sessions import COOKIE_NAME, csrf_ok, lookup
from app.db.models import AuditEvent, Membership, Tenant, User, UserSession
from app.db.session import get_db

ROLE_RANK = {"analyst": 1, "reviewer": 1, "admin": 2}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class Ctx:
    db: Session
    user: User
    session: UserSession
    tenant: Tenant
    role: str
    ip: str | None

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.tenant.id

    def audit(self, action: str, object_type: str, object_id: Any = None, detail: dict[str, Any] | None = None) -> None:
        self.db.add(AuditEvent(tenant_id=self.tenant.id, actor_id=self.user.id, action=action, object_type=object_type,
                               object_id=str(object_id) if object_id is not None else None, detail=detail or {},
                               ip=self.ip))

    def can_view_sql(self) -> bool:
        return self.role in ("admin", "analyst")


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def current(request: Request, db: Session = Depends(get_db)) -> Ctx:
    row = lookup(db, request.cookies.get(COOKIE_NAME))
    if row is None:
        raise APIError(401, "unauthenticated", "sign in required")
    if request.method not in SAFE_METHODS and not csrf_ok(row, request.headers.get("X-CSRF-Token")):
        raise APIError(403, "csrf_failed", "missing or invalid CSRF token")
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise APIError(401, "unauthenticated", "account disabled")
    if row.active_tenant_id is None:
        raise APIError(403, "no_tenant", "no active tenant membership")
    membership = db.scalar(
        select(Membership).where(Membership.user_id == user.id, Membership.tenant_id == row.active_tenant_id)
    )
    if membership is None:  # membership revoked after sign-in: deny immediately
        raise APIError(403, "not_a_member", "you are not a member of the active tenant")
    tenant = db.get(Tenant, row.active_tenant_id)
    assert tenant is not None
    row.last_seen_at = datetime.now(UTC)
    return Ctx(db=db, user=user, session=row, tenant=tenant, role=membership.role, ip=client_ip(request))


def require(*roles: str):  # noqa: ANN201 - FastAPI dependency factory
    def dep(ctx: Ctx = Depends(current)) -> Ctx:
        if ctx.role not in roles:
            raise forbidden(f"requires role: {', '.join(roles)}")
        return ctx

    return dep


def paginate(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, 100)), max(0, offset)
