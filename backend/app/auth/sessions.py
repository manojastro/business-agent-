"""Secure server-side sessions. The cookie carries a random token; only its SHA-256 is stored."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import UserSession

COOKIE_NAME = "mi_session"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(s: Session, user_id: uuid.UUID, tenant_id: uuid.UUID | None, ttl_hours: int) -> tuple[str, UserSession]:
    token = secrets.token_urlsafe(32)
    row = UserSession(
        token_hash=_hash(token),
        user_id=user_id,
        active_tenant_id=tenant_id,
        csrf_token=secrets.token_urlsafe(24),
        expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours),
    )
    s.add(row)
    s.flush()
    return token, row


def lookup(s: Session, token: str | None) -> UserSession | None:
    if not token or len(token) > 200:
        return None
    row = s.scalar(select(UserSession).where(UserSession.token_hash == _hash(token)))
    if row is None or row.revoked_at is not None or row.expires_at <= datetime.now(UTC):
        return None
    return row


def revoke(s: Session, row: UserSession) -> None:
    row.revoked_at = datetime.now(UTC)


def csrf_ok(row: UserSession, header_value: str | None) -> bool:
    return bool(header_value) and secrets.compare_digest(row.csrf_token, header_value or "")
