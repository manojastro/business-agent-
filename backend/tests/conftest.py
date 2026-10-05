"""Test fixtures. Tests run against the real PostgreSQL databases (app + analytics).

Prerequisites (see RUNBOOK.md):  python -m app.cli migrate && python -m app.cli seed-demo
"""

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

os.environ.setdefault("APP_ENV", "test")

from app.config import get_settings  # noqa: E402
from app.db.models import Investigation, Tenant, User  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.seed.app_seed import ensure_membership, ensure_tenant, ensure_user  # noqa: E402
from app.seed.loader import load_dataset  # noqa: E402
from app.seed.synthetic import DatasetSpec, Scenario, generate  # noqa: E402

TEST_PASSWORD = "test-" + secrets.token_urlsafe(12)
AS_OF = date(2026, 9, 1)


@pytest.fixture(scope="session")
def settings():  # noqa: ANN201
    return get_settings()


@pytest.fixture(scope="session")
def demo_tenants() -> dict[str, Tenant]:
    with session_scope() as s:
        rows = {t.slug: t for t in s.scalars(select(Tenant).where(Tenant.kind == "demo"))}
    if {"acme-retail", "bharat-bazaar"} - set(rows):
        pytest.skip("demo data not seeded: run `python -m app.cli seed-demo`")
    return rows


@pytest.fixture(scope="session")
def small_dataset():  # noqa: ANN201
    """A small tenant used for exact-arithmetic tests (loaded once per session)."""
    spec = DatasetSpec(slug="test-small", name="Test small", seed=5, start=AS_OF - timedelta(days=28), as_of=AS_OF,
                       orders_per_day=40, scenario=Scenario("refund_increase", {"region": "South", "refund_rate": 0.3}))
    ds = generate(spec)
    load_dataset(ds)
    with session_scope() as s:
        ensure_tenant(s, spec, "demo_test", {})
    return ds


@pytest.fixture(scope="session")
def users(demo_tenants: dict[str, Tenant], small_dataset) -> dict[str, str]:  # noqa: ANN001
    """Dedicated test users (separate from demo users) with a per-session random password."""
    spec = {
        "t-analyst-a@test.local": [("acme-retail", "analyst")],
        "t-analyst2-a@test.local": [("acme-retail", "analyst")],
        "t-reviewer-a@test.local": [("acme-retail", "reviewer")],
        "t-admin-a@test.local": [("acme-retail", "admin"), ("test-small", "admin")],
        "t-analyst-b@test.local": [("bharat-bazaar", "analyst")],
    }
    with session_scope() as s:
        tenants = {t.slug: t for t in s.scalars(select(Tenant))}
        for email, roles in spec.items():
            u, _ = ensure_user(s, email, email.split("@")[0], TEST_PASSWORD, rotate=True)
            for slug, role in roles:
                ensure_membership(s, u, tenants[slug], role)
    return {e.split("@")[0]: e for e in spec}


@pytest.fixture()
def client():  # noqa: ANN201
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()

    def make(email: str) -> TestClient:
        c = TestClient(app)
        r = c.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
        assert r.status_code == 200, r.text
        c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        return c

    return make


@pytest.fixture()
def new_investigation(demo_tenants: dict[str, Tenant], users: dict[str, str]) -> Iterator:  # noqa: ANN201
    def make(slug: str = "acme-retail", as_of: date = AS_OF, metric_key: str | None = "net_sales",
             question: str = "Why did net sales fall last week?") -> tuple[str, str]:
        with session_scope() as s:
            t = s.scalar(select(Tenant).where(Tenant.slug == slug))
            u = s.scalar(select(User).where(User.email == users["t-analyst-a"]))
            assert t and u
            inv = Investigation(tenant_id=t.id, owner_id=u.id, question=question, metric_key=metric_key,
                                as_of_date=as_of, timezone=t.timezone, max_cost=Decimal("0.5"),
                                model_config_={"mode": "fixture"}, status="queued")
            s.add(inv)
            s.flush()
            return str(inv.id), str(t.id)

    yield make


def uid() -> str:
    return uuid.uuid4().hex[:8]
