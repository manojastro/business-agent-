"""Bootstrap application data: tenants, metric catalog v1, connections, demo users, eval suites.

No fixed password ships with the project: demo passwords come from DEMO_USER_PASSWORD or are
generated randomly and printed once by the seed command.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.passwords import hash_password
from app.db.models import AuditEvent, Connection, EvalSuite, Membership, MetricDefinition, Tenant, User
from app.evals.scenarios import DEMO_DATASETS, DEMO_TENANT_SETTINGS, eval_cases
from app.metrics.catalog import BUILTIN_METRICS
from app.seed.loader import load_dataset
from app.seed.synthetic import DatasetSpec, generate

DEMO_USERS = [
    # email, display name, [(tenant slug, role)]
    ("admin@demo.local", "Demo Admin", [("acme-retail", "admin"), ("bharat-bazaar", "admin")]),
    ("analyst@acme.demo", "Asha Analyst (Acme)", [("acme-retail", "analyst")]),
    ("reviewer@acme.demo", "Ravi Reviewer (Acme)", [("acme-retail", "reviewer")]),
    ("analyst@bharat.demo", "Bina Analyst (Bharat)", [("bharat-bazaar", "analyst")]),
    ("reviewer@bharat.demo", "Vikram Reviewer (Bharat)", [("bharat-bazaar", "reviewer")]),
]


def ensure_tenant(s: Session, spec: DatasetSpec, kind: str, settings: dict[str, Any]) -> Tenant:
    t = s.get(Tenant, spec.tenant_id)
    if t is None:
        t = Tenant(id=spec.tenant_id, slug=spec.slug, name=spec.name, kind=kind, timezone=spec.timezone,
                   reporting_currency="INR", settings=settings)
        s.add(t)
    else:
        t.name, t.kind, t.settings = spec.name, kind, settings
    s.flush()
    if not s.scalar(select(MetricDefinition.id).where(MetricDefinition.tenant_id == t.id)):
        for m in BUILTIN_METRICS.values():
            s.add(MetricDefinition(tenant_id=t.id, metric_key=m.key, version=m.version, status="active",
                                   definition=m.to_definition(), change_reason="Initial catalog (seed)"))
    if not s.scalar(select(Connection.id).where(Connection.tenant_id == t.id)):
        s.add(Connection(tenant_id=t.id, name="Synthetic commerce (PostgreSQL)", kind="synthetic_postgres",
                         config={"database": "metric_analytics", "schema": "semantic", "access": "read-only role, RLS",
                                 "seed": spec.seed, "scenario": "hidden" if kind == "eval" else spec.scenario.family}))
    return t


def ensure_user(
    s: Session, email: str, name: str, password: str | None, rotate: bool = False
) -> tuple[User, str | None]:
    """Create the user (or rotate its password). Returns the password issued, if any."""
    u = s.scalar(select(User).where(User.email == email))
    issued = None
    if u is None:
        issued = password or secrets.token_urlsafe(12)
        u = User(email=email, display_name=name, password_hash=hash_password(issued))
        s.add(u)
        s.flush()
    elif rotate:
        issued = password or secrets.token_urlsafe(12)
        u.password_hash = hash_password(issued)
    return u, issued


def ensure_membership(s: Session, user: User, tenant: Tenant, role: str) -> None:
    m = s.scalar(select(Membership).where(Membership.user_id == user.id, Membership.tenant_id == tenant.id))
    if m is None:
        s.add(Membership(user_id=user.id, tenant_id=tenant.id, role=role))
    else:
        m.role = role


def seed_demo(s: Session, password: str | None, reset_passwords: bool = False, load_data: bool = True) -> list[tuple[str, str]]:
    tenants: dict[str, Tenant] = {}
    for spec in DEMO_DATASETS:
        if load_data:
            counts = load_dataset(generate(spec))
            print(f"  loaded {spec.slug}: {counts['orders']} orders, {counts['order_items']} items, {counts['refunds']} refunds")
        tenants[spec.slug] = ensure_tenant(s, spec, "demo", DEMO_TENANT_SETTINGS.get(spec.slug, {}))
    issued: list[tuple[str, str]] = []
    for email, name, roles in DEMO_USERS:
        user, new_pw = ensure_user(s, email, name, password, rotate=reset_passwords)
        if new_pw:
            issued.append((email, new_pw))
        for slug, role in roles:
            ensure_membership(s, user, tenants[slug], role)
    s.add(AuditEvent(action="seed_demo", object_type="system", detail={"tenants": list(tenants)}))
    return issued


def seed_evals(s: Session, splits: list[str], load_data: bool = True) -> dict[str, int]:
    out = {}
    for split in splits:
        cases = eval_cases(split)
        scenarios = []
        for case in cases:
            spec: DatasetSpec = case["spec"]
            if load_data:
                load_dataset(generate(spec))
            ensure_tenant(s, spec, "eval", {})
            scenarios.append({"slug": case["slug"], "tenant_id": str(spec.tenant_id), "family": case["family"],
                              "seed": case["seed"], "ground_truth": case["ground_truth"]})
        name = f"synthetic-{split}"
        suite = s.scalar(select(EvalSuite).where(EvalSuite.name == name))
        if suite is None:
            s.add(EvalSuite(name=name, split=split, scenarios=scenarios,
                            description=f"Ten scenario families x {len(scenarios) // 10} seeds ({split} split)"))
        else:
            suite.scenarios = scenarios
        out[split] = len(scenarios)
        print(f"  {name}: {len(scenarios)} scenarios")
    return out


def eval_tenant_ids(s: Session) -> set[uuid.UUID]:
    return set(s.scalars(select(Tenant.id).where(Tenant.kind == "eval")))
