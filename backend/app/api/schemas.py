"""Request/response models (with OpenAPI examples)."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ErrorBody(BaseModel):
    code: str = Field(examples=["not_found"])
    message: str
    details: Any = None


class ErrorResponse(BaseModel):
    error: ErrorBody


ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Not signed in"},
    403: {"model": ErrorResponse, "description": "Forbidden / CSRF failure"},
    404: {"model": ErrorResponse, "description": "Not found (also returned for other tenants' objects)"},
    409: {"model": ErrorResponse, "description": "Conflict with current state"},
    422: {"model": ErrorResponse, "description": "Validation error"},
    429: {"model": ErrorResponse, "description": "Rate limited"},
}


class LoginIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"email": "analyst@acme.demo", "password": "<from seed output>"}]})
    email: str = Field(pattern=r"^[^@\s]{1,64}@[^@\s]{1,255}$", max_length=320)
    password: str = Field(min_length=1, max_length=200)


class TenantOut(BaseModel):
    id: str
    slug: str
    name: str
    role: str
    kind: str


class SessionOut(BaseModel):
    user: dict[str, Any]
    active_tenant: TenantOut
    tenants: list[TenantOut]
    csrf_token: str
    model_mode: str
    demo_as_of: str


class SwitchTenantIn(BaseModel):
    tenant_id: str


class WindowIn(BaseModel):
    start: date
    end: date


class InvestigationCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [
            {"question": "Why did net sales fall in the last complete seven days compared with the preceding seven days?",
             "metric_key": "net_sales", "as_of": "2026-09-01"},
            {"question": "Why did revenue drop last week?"},
        ]},
    )
    question: str = Field(min_length=5, max_length=2000)
    metric_key: Literal["net_sales", "order_count", "average_order_value", "refund_rate"] | None = None
    as_of: date | None = None
    baseline_window: WindowIn | None = None
    current_window: WindowIn | None = None
    model_mode: Literal["fixture", "real"] | None = None


class InvestigationCreated(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "id": "7d9f0c1e-2c3a-4d55-9a3e-0e6a3b6c9f10", "status": "queued",
        "status_url": "/api/v1/investigations/7d9f0c1e-2c3a-4d55-9a3e-0e6a3b6c9f10",
        "events_url": "/api/v1/investigations/7d9f0c1e-2c3a-4d55-9a3e-0e6a3b6c9f10/events", "created": True}]})
    id: str
    status: str
    status_url: str
    events_url: str
    created: bool


class ClarificationIn(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"metric_key": "net_sales"}, {"choice": "continue"}]})
    metric_key: str | None = None
    choice: Literal["continue", "stop"] | None = None


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [
        {"decision": "request_changes", "comment": "State the refund maturity caveat in the summary.", "version": 1}]})
    decision: Literal["approve", "request_changes", "reject"]
    comment: str = Field(default="", max_length=2000)
    version: int = Field(ge=1)


class MetricVersionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = Field(default=None, max_length=500)
    allowed_dimensions: list[str] | None = None
    change_reason: str = Field(min_length=5, max_length=1000)


class MemberIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(pattern=r"^[^@\s]{1,64}@[^@\s]{1,255}$", max_length=320)
    display_name: str = Field(min_length=1, max_length=200)
    role: Literal["admin", "reviewer", "analyst"]


class MemberRoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["admin", "reviewer", "analyst"]


class EvalRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [
        {"suite": "synthetic-heldout", "systems": ["fixed_dashboard", "single_pass", "full_graph"], "limit": 10}]})
    suite: str
    systems: list[Literal["fixed_dashboard", "single_pass", "full_graph"]] = Field(min_length=1)
    limit: int | None = Field(default=None, ge=1, le=200)
    model_mode: Literal["fixture", "real"] | None = None


class Page(BaseModel):
    items: list[dict[str, Any]]
    next_offset: int | None
