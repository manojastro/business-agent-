"""Application database schema (metric_app). Source analytics data lives in a separate database."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _tenant_fk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="demo")  # demo | eval
    reporting_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Kolkata")
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created()


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "tenant_id", name="uq_membership_user_tenant"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # admin | reviewer | analyst
    created_at: Mapped[datetime] = _created()


class UserSession(Base):
    __tablename__ = "sessions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    active_tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Connection(Base):
    __tablename__ = "connections"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="synthetic_postgres")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)  # never secrets
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = _created()


class MetricDefinition(Base):
    __tablename__ = "metric_definitions"
    __table_args__ = (UniqueConstraint("tenant_id", "metric_key", "version", name="uq_metric_version"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    metric_key: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active | retired
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    change_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = _created()


class Investigation(Base):
    __tablename__ = "investigations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "owner_id", "idempotency_key", name="uq_investigation_idem"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    metric_key: Mapped[str | None] = mapped_column(String(64))
    metric_version: Mapped[int | None] = mapped_column(Integer)
    baseline_start: Mapped[date | None] = mapped_column(Date)
    baseline_end: Mapped[date | None] = mapped_column(Date)  # exclusive
    current_start: Mapped[date | None] = mapped_column(Date)
    current_end: Mapped[date | None] = mapped_column(Date)  # exclusive
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    source_watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    model_config_: Mapped[dict[str, Any]] = mapped_column("model_config", JSONB, nullable=False, default=dict)
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=200_000)
    step_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=40)
    max_source_queries: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    max_cost: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    steps_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queries_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_used: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal("0"))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    incomplete_data_ack: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pending_question: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    primary_driver: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    rerun_of: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InvestigationStep(Base):
    """Append-only event log; its id is the cursor for SSE replay."""

    __tablename__ = "investigation_steps"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("investigations.id"), nullable=False, index=True
    )
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    node: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()


class Hypothesis(Base):
    __tablename__ = "hypotheses"
    __table_args__ = (UniqueConstraint("investigation_id", "key", name="uq_hypothesis_key"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    investigation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("investigations.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class QueryPlanRecord(Base):
    __tablename__ = "query_plans"
    id: Mapped[uuid.UUID] = _uuid_pk()
    investigation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("investigations.id"), nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    hypothesis_key: Mapped[str | None] = mapped_column(String(80))
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    plan_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # valid | invalid
    validation_errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _created()


class QueryRun(Base):
    __tablename__ = "query_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    investigation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("investigations.id"), nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    query_plan_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("query_plans.id"))
    operation_id: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    compiled_sql: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # running | succeeded | failed
    row_count: Mapped[int | None] = mapped_column(Integer)
    execution_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvidenceItem(Base):
    __tablename__ = "evidence_items"
    id: Mapped[uuid.UUID] = _uuid_pk()
    investigation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("investigations.id"), nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    query_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("query_runs.id"))
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # query | calculation | freshness | quality
    plan_hash: Mapped[str | None] = mapped_column(String(64))
    compiled_sql: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)  # bounded
    artifact_uri: Mapped[str | None] = mapped_column(String(500))
    result_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metric_key: Mapped[str | None] = mapped_column(String(64))
    metric_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created()


class Claim(Base):
    __tablename__ = "claims"
    id: Mapped[uuid.UUID] = _uuid_pk()
    investigation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("investigations.id"), nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    claim_key: Mapped[str] = mapped_column(String(120), nullable=False)
    wording: Mapped[str] = mapped_column(Text, nullable=False)
    claim_type: Mapped[str] = mapped_column(String(32), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="analyst")
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    numeric_fields: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    limitations: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    verification_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    verification_detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[uuid.UUID] = _uuid_pk()
    investigation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("investigations.id"), unique=True, nullable=False)
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="in_review")
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ReportVersion(Base):
    __tablename__ = "report_versions"
    __table_args__ = (UniqueConstraint("report_id", "version", name="uq_report_version"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    report_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reports.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = _created()


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[uuid.UUID] = _uuid_pk()
    report_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reports.id"), nullable=False)
    report_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("report_versions.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    reviewer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)  # approve | request_changes | reject
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = _created()


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "run_after"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    investigation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("investigations.id"))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # investigate | resume | eval
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    operation_id: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str] = mapped_column(String(48), nullable=False)
    object_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()


class EvalSuite(Base):
    """Ground truth lives here (application DB) - never in agent-readable analytics tables."""

    __tablename__ = "eval_suites"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    split: Mapped[str] = mapped_column(String(16), nullable=False)  # development | heldout
    scenarios: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created()


class EvalRun(Base):
    __tablename__ = "eval_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    suite_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("eval_suites.id"), nullable=False)
    systems: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    model_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    started_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
