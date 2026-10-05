"""Constrained QueryPlan: the only way a model can ask for source data.

A model proposes a ``QueryPlan`` (JSON). It is parsed with ``extra='forbid'`` so there is no
field in which SQL, functions, joins, subqueries or expressions could be expressed. The
validator checks the plan against the semantic catalog and the investigation scope, and the
compiler builds parameterized SQL with SQLAlchemy Core from allowlisted identifiers only.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import Date, String, and_, case, cast, column, func, literal, or_, select, table
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from app.metrics.catalog import DIMENSIONS, dimension_value_ok
from app.metrics.windows import Window, WindowError, validate_pair

Analysis = Literal["totals", "by_dimension", "daily", "history", "campaign_spend", "quality"]
MAX_LIMIT = 50


class PlanWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date
    end: date


class PlanFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimension: str = Field(max_length=40)
    values: list[str] = Field(min_length=1, max_length=10)


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric_id: str = Field(max_length=64)
    metric_version: int = Field(ge=1)
    analysis: Analysis
    baseline_window: PlanWindow
    current_window: PlanWindow
    dimension: str | None = Field(default=None, max_length=40)
    filters: list[PlanFilter] = Field(default_factory=list, max_length=3)
    order_by: Literal["segment", "contribution_asc", "contribution_desc"] = "segment"
    limit: int = Field(default=20, ge=1, le=MAX_LIMIT)
    lookback_periods: int = Field(default=0, ge=0, le=8)
    purpose: str = Field(default="", max_length=300)  # free text: stored, never compiled

    @field_validator("purpose")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    def canonical(self) -> dict[str, Any]:
        """Canonical form used for hashing - excludes the free-text purpose."""
        data = self.model_dump(mode="json", exclude={"purpose"})
        data["filters"] = sorted(
            ({"dimension": f["dimension"], "values": sorted(f["values"])} for f in data["filters"]),
            key=lambda f: f["dimension"],
        )
        return data

    def plan_hash(self) -> str:
        blob = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


@dataclass
class PlanIssue:
    field: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"field": self.field, "code": self.code, "message": self.message}


@dataclass
class ValidationContext:
    tenant_id: uuid.UUID
    metric_definitions: dict[str, dict[str, Any]]  # key -> active definition JSON
    baseline: Window
    current: Window
    as_of: date
    timezone: str
    refund_watermark: datetime


@dataclass
class CompiledQuery:
    statement: Select
    params: dict[str, Any]
    sql: str
    plan_hash: str
    shape: str
    extra: dict[str, Any] = field(default_factory=dict)


def parse_plan(raw: Any) -> tuple[QueryPlan | None, list[PlanIssue]]:
    try:
        if isinstance(raw, str):
            raw = json.loads(raw)
        return QueryPlan.model_validate(raw), []
    except (ValidationError, ValueError, TypeError) as exc:
        issues: list[PlanIssue] = []
        if isinstance(exc, ValidationError):
            for e in exc.errors():
                issues.append(PlanIssue(".".join(str(p) for p in e["loc"]) or "plan", e["type"], e["msg"]))
        else:
            issues.append(PlanIssue("plan", "parse_error", str(exc)[:200]))
        return None, issues


def validate_plan(plan: QueryPlan, ctx: ValidationContext) -> list[PlanIssue]:
    issues: list[PlanIssue] = []
    defn = ctx.metric_definitions.get(plan.metric_id)
    if defn is None:
        return [PlanIssue("metric_id", "unknown_metric", f"Metric '{plan.metric_id}' is not in the catalog")]
    if int(defn["version"]) != plan.metric_version:
        issues.append(
            PlanIssue("metric_version", "version_mismatch", f"Active version is {defn['version']}, plan uses {plan.metric_version}")
        )
    b = Window(plan.baseline_window.start, plan.baseline_window.end)
    c = Window(plan.current_window.start, plan.current_window.end)
    try:
        validate_pair(b, c, ctx.as_of)
    except WindowError as exc:
        issues.append(PlanIssue("windows", "invalid_window", str(exc)))
    if (b, c) != (ctx.baseline, ctx.current):
        issues.append(PlanIssue("windows", "out_of_scope", "Plan windows must match the investigation windows"))

    allowed = set(defn.get("allowed_dimensions", []))
    if plan.analysis == "by_dimension":
        if plan.dimension is None:
            issues.append(PlanIssue("dimension", "required", "by_dimension requires a dimension"))
        elif plan.dimension not in DIMENSIONS:
            issues.append(PlanIssue("dimension", "unknown_dimension", f"'{plan.dimension}' is not a catalog dimension"))
        elif plan.dimension not in allowed:
            issues.append(
                PlanIssue("dimension", "dimension_not_allowed", f"'{plan.dimension}' is not allowed for {plan.metric_id}")
            )
    elif plan.dimension is not None:
        issues.append(PlanIssue("dimension", "unexpected", f"dimension is not used by analysis '{plan.analysis}'"))

    seen: set[str] = set()
    for i, f in enumerate(plan.filters):
        if f.dimension in seen:
            issues.append(PlanIssue(f"filters.{i}", "duplicate_filter", "Each dimension may be filtered once"))
        seen.add(f.dimension)
        if f.dimension not in DIMENSIONS or f.dimension not in allowed:
            issues.append(PlanIssue(f"filters.{i}.dimension", "dimension_not_allowed", f"Cannot filter on '{f.dimension}'"))
            continue
        if DIMENSIONS[f.dimension]["grain"] != "order" and plan.analysis != "by_dimension":
            issues.append(PlanIssue(f"filters.{i}.dimension", "grain_mismatch", "Category filters require a category split"))
        bad = [v for v in f.values if not dimension_value_ok(f.dimension, v)]
        if bad:
            issues.append(PlanIssue(f"filters.{i}.values", "invalid_value", f"Values not in catalog: {bad[:3]}"))
    if plan.analysis == "history" and plan.lookback_periods < 2:
        issues.append(PlanIssue("lookback_periods", "required", "history needs 2-8 lookback periods"))
    if plan.analysis != "history" and plan.lookback_periods:
        issues.append(PlanIssue("lookback_periods", "unexpected", "lookback_periods is only used by 'history'"))
    if plan.analysis in ("campaign_spend", "quality") and plan.filters:
        issues.append(PlanIssue("filters", "unexpected", f"filters are not supported for '{plan.analysis}'"))
    if plan.analysis == "daily" and (c.days + b.days) > MAX_LIMIT * 2:
        issues.append(PlanIssue("windows", "too_many_days", "Daily series limited to 100 days"))
    return issues


# ---------------------------------------------------------------------------------------
# Compiler: allowlisted relations and columns only.
# ---------------------------------------------------------------------------------------

ORDER_FACTS = table(
    "order_facts",
    *(column(n) for n in (
        "tenant_id", "order_id", "source_id", "ordered_at", "status", "channel", "region",
        "customer_segment", "campaign_id", "currency", "merchandise_amount", "units",
        "discount_amount", "has_items", "batch_id", "ingested_at",
    )),
    schema="semantic",
)
ORDER_CATEGORY_FACTS = table(
    "order_category_facts",
    *(column(n) for n in (
        "tenant_id", "order_id", "ordered_at", "status", "channel", "region", "customer_segment",
        "campaign_id", "currency", "category", "category_merchandise", "units",
    )),
    schema="semantic",
)
REFUND_FACTS = table(
    "refund_facts",
    *(column(n) for n in ("tenant_id", "refund_id", "order_id", "amount", "currency", "refunded_at", "ingested_at")),
    schema="semantic",
)
CAMPAIGN_SPEND = table(
    "campaign_spend",
    *(column(n) for n in ("tenant_id", "campaign_id", "campaign_name", "channel", "spend_date", "amount", "currency")),
    schema="semantic",
)


def _dim_expr(rel: Any, dimension: str) -> ColumnElement[Any]:
    col = DIMENSIONS[dimension]["column"]
    if dimension == "campaign":
        return func.coalesce(cast(rel.c.campaign_id, String), literal("none"))
    return rel.c[col]


def _window_bounds(ctx: ValidationContext) -> dict[str, Any]:
    b0, b1 = ctx.baseline.utc_bounds(ctx.timezone)
    c0, c1 = ctx.current.utc_bounds(ctx.timezone)
    return {"b_start": b0, "b_end": b1, "c_start": c0, "c_end": c1}


def _order_refunds_cte() -> Any:
    r = REFUND_FACTS
    return (
        select(r.c.order_id.label("order_id"), func.sum(r.c.amount).label("refund_amount"))
        .where(r.c.tenant_id == literal_param("tenant_id"), r.c.refunded_at < literal_param("refund_watermark"))
        .group_by(r.c.order_id)
        .cte("order_refunds")
    )


def literal_param(name: str) -> Any:
    from sqlalchemy import bindparam

    return bindparam(name)


def _window_label(ts: Any) -> ColumnElement[Any]:
    return case(
        (and_(ts >= literal_param("b_start"), ts < literal_param("b_end")), literal("baseline")),
        else_=literal("current"),
    )


def _in_windows(ts: Any) -> ColumnElement[bool]:
    return or_(
        and_(ts >= literal_param("b_start"), ts < literal_param("b_end")),
        and_(ts >= literal_param("c_start"), ts < literal_param("c_end")),
    )


def _filter_clauses(rel: Any, plan: QueryPlan, params: dict[str, Any]) -> list[ColumnElement[bool]]:
    from sqlalchemy import bindparam

    out = []
    for i, f in enumerate(plan.filters):
        name = f"filter_{i}"
        params[name] = list(f.values)
        out.append(_dim_expr(rel, f.dimension).in_(bindparam(name, expanding=True)))
    return out


def _aggregate_columns(o: Any, refunds: Any, alloc_share: Any | None = None) -> list[Any]:
    completed = o.c.status == literal("completed")
    canceled = o.c.status == literal("canceled")
    if alloc_share is None:
        merch = o.c.merchandise_amount
        disc = o.c.discount_amount
        units = o.c.units
        refund_raw = func.coalesce(refunds.c.refund_amount, 0)
        refund_cap = func.least(refund_raw, func.greatest(o.c.merchandise_amount - o.c.discount_amount, 0))
    else:
        merch, disc, units, refund_cap = alloc_share
        refund_raw = func.coalesce(refunds.c.refund_amount, 0)
    return [
        func.coalesce(func.sum(case((completed, merch), else_=0)), 0).label("merchandise"),
        func.coalesce(func.sum(case((completed, disc), else_=0)), 0).label("discount"),
        func.coalesce(func.sum(case((completed, refund_cap), else_=0)), 0).label("refunds"),
        func.count(func.distinct(case((completed, o.c.order_id)))).label("completed_orders"),
        func.count(func.distinct(case((and_(completed, refund_raw > 0), o.c.order_id)))).label("refunded_orders"),
        func.count(func.distinct(case((canceled, o.c.order_id)))).label("canceled_orders"),
        func.coalesce(func.sum(case((canceled, merch), else_=0)), 0).label("canceled_merchandise"),
        func.coalesce(func.sum(case((completed, units), else_=0)), 0).label("units"),
    ]


def compile_plan(plan: QueryPlan, ctx: ValidationContext) -> CompiledQuery:
    issues = validate_plan(plan, ctx)
    if issues:
        raise ValueError(f"refusing to compile an invalid plan: {[i.code for i in issues]}")
    params: dict[str, Any] = {
        "tenant_id": ctx.tenant_id,
        "refund_watermark": ctx.refund_watermark,
        **_window_bounds(ctx),
    }
    o = ORDER_FACTS
    shape = plan.analysis
    stmt: Select

    if plan.analysis in ("totals", "by_dimension") and not (
        plan.analysis == "by_dimension" and DIMENSIONS[plan.dimension or ""]["grain"] == "order_category"
    ):
        refunds = _order_refunds_cte()
        label = _window_label(o.c.ordered_at).label("window")
        cols: list[Any] = [label]
        group: list[Any] = [label]
        if plan.analysis == "by_dimension":
            seg = _dim_expr(o, plan.dimension or "").label("segment")
            cols.append(seg)
            group.append(seg)
        stmt = (
            select(*cols, *_aggregate_columns(o, refunds))
            .select_from(o.outerjoin(refunds, refunds.c.order_id == o.c.order_id))
            .where(o.c.tenant_id == literal_param("tenant_id"), _in_windows(o.c.ordered_at), *_filter_clauses(o, plan, params))
            .group_by(*group)
            .order_by(*group)
            .limit(plan.limit * 2)
        )
    elif plan.analysis == "by_dimension":
        # Category split: item grain, with order-level discount/refunds allocated by merchandise share.
        oc = ORDER_CATEGORY_FACTS
        refunds = _order_refunds_cte()
        share = oc.c.category_merchandise / func.nullif(o.c.merchandise_amount, 0)
        alloc_disc = o.c.discount_amount * share
        refund_cap = func.least(
            func.coalesce(refunds.c.refund_amount, 0), func.greatest(o.c.merchandise_amount - o.c.discount_amount, 0)
        )
        alloc_refund = refund_cap * share
        label = _window_label(oc.c.ordered_at).label("window")
        seg = oc.c.category.label("segment")
        aggs = _aggregate_columns(
            oc, refunds, alloc_share=(oc.c.category_merchandise, alloc_disc, oc.c.units, alloc_refund)
        )
        stmt = (
            select(label, seg, *aggs)
            .select_from(
                oc.join(o, and_(o.c.order_id == oc.c.order_id, o.c.tenant_id == oc.c.tenant_id)).outerjoin(
                    refunds, refunds.c.order_id == oc.c.order_id
                )
            )
            .where(
                oc.c.tenant_id == literal_param("tenant_id"),
                _in_windows(oc.c.ordered_at),
                *_filter_clauses(oc, plan, params),
            )
            .group_by(label, seg)
            .order_by(label, seg)
            .limit(plan.limit * 2)
        )
        shape = "by_dimension_allocated"
    elif plan.analysis == "daily":
        refunds = _order_refunds_cte()
        day = cast(func.timezone(literal_param("tz"), o.c.ordered_at), Date).label("day")
        params["tz"] = ctx.timezone
        stmt = (
            select(day, *_aggregate_columns(o, refunds))
            .select_from(o.outerjoin(refunds, refunds.c.order_id == o.c.order_id))
            .where(o.c.tenant_id == literal_param("tenant_id"), _in_windows(o.c.ordered_at), *_filter_clauses(o, plan, params))
            .group_by(day)
            .order_by(day)
            .limit(MAX_LIMIT * 2)
        )
    elif plan.analysis == "history":
        # Daily series for the equal-length periods immediately before the baseline window;
        # the noise band is computed from it in application code.
        refunds = _order_refunds_cte()
        span = ctx.baseline.days * plan.lookback_periods
        hist = Window(ctx.baseline.start - timedelta(days=span), ctx.baseline.start)
        h0, h1 = hist.utc_bounds(ctx.timezone)
        params.update(h_start=h0, h_end=h1, tz=ctx.timezone)
        day = cast(func.timezone(literal_param("tz"), o.c.ordered_at), Date).label("day")
        stmt = (
            select(day, *_aggregate_columns(o, refunds))
            .select_from(o.outerjoin(refunds, refunds.c.order_id == o.c.order_id))
            .where(
                o.c.tenant_id == literal_param("tenant_id"),
                o.c.ordered_at >= literal_param("h_start"),
                o.c.ordered_at < literal_param("h_end"),
                *_filter_clauses(o, plan, params),
            )
            .group_by(day)
            .order_by(day)
            .limit(8 * 92)
        )
    elif plan.analysis == "campaign_spend":
        s = CAMPAIGN_SPEND
        params.update(
            b_day_start=ctx.baseline.start, b_day_end=ctx.baseline.end,
            c_day_start=ctx.current.start, c_day_end=ctx.current.end,
        )
        label = case(
            (and_(s.c.spend_date >= literal_param("b_day_start"), s.c.spend_date < literal_param("b_day_end")), literal("baseline")),
            else_=literal("current"),
        ).label("window")
        stmt = (
            select(label, s.c.campaign_id, s.c.campaign_name, s.c.channel, func.sum(s.c.amount).label("spend"))
            .where(
                s.c.tenant_id == literal_param("tenant_id"),
                or_(
                    and_(s.c.spend_date >= literal_param("b_day_start"), s.c.spend_date < literal_param("b_day_end")),
                    and_(s.c.spend_date >= literal_param("c_day_start"), s.c.spend_date < literal_param("c_day_end")),
                ),
            )
            .group_by(label, s.c.campaign_id, s.c.campaign_name, s.c.channel)
            .order_by(label, s.c.campaign_id)
            .limit(plan.limit * 2)
        )
    else:  # quality
        label = _window_label(o.c.ordered_at).label("window")
        params["ingest_start"] = params["b_start"]
        params["ingest_end"] = params["c_end"] + timedelta(days=3)
        null_dates = (
            select(func.count())
            .select_from(o)
            .where(
                o.c.tenant_id == literal_param("tenant_id"),
                o.c.ordered_at.is_(None),
                o.c.ingested_at >= literal_param("ingest_start"),
                o.c.ingested_at < literal_param("ingest_end"),
            )
            .scalar_subquery()
        )
        stmt = (
            select(
                label,
                func.count().label("order_rows"),
                (func.count() - func.count(func.distinct(o.c.source_id))).label("duplicate_rows"),
                func.count(case((o.c.has_items.is_(False), 1))).label("orders_without_items"),
                func.count(case((o.c.currency != literal("INR"), 1))).label("non_reporting_currency_rows"),
                func.count(case((o.c.status == literal("canceled"), 1))).label("canceled_rows"),
                null_dates.label("null_ordered_at_rows"),
            )
            .where(o.c.tenant_id == literal_param("tenant_id"), _in_windows(o.c.ordered_at))
            .group_by(label)
            .order_by(label)
        )

    compiled = stmt.compile(dialect=postgresql.dialect())
    # Record every bound value (catalog literals included) so evidence is fully reproducible.
    all_params = {k: v for k, v in compiled.params.items() if v is not None}
    all_params.update(params)
    return CompiledQuery(
        statement=stmt,
        params=all_params,
        sql=str(compiled),
        plan_hash=plan.plan_hash(),
        shape=shape,
    )


def params_for_evidence(params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in params.items():
        if isinstance(v, (datetime, date)):
            out[k] = v.isoformat()
        elif isinstance(v, uuid.UUID):
            out[k] = str(v)
        else:
            out[k] = v
    return out
