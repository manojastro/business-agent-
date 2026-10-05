"""QueryPlan validation and compilation: only catalog-approved, parameterized SQL is produced."""

import uuid
from datetime import UTC, date, datetime

import pytest

from app.metrics.catalog import BUILTIN_METRICS
from app.metrics.windows import default_windows
from app.tools.query_plan import QueryPlan, ValidationContext, compile_plan, parse_plan, validate_plan

AS_OF = date(2026, 9, 1)
B, C_ = default_windows(AS_OF)
CTX = ValidationContext(uuid.uuid4(), {k: m.to_definition() for k, m in BUILTIN_METRICS.items()}, B, C_, AS_OF,
                        "Asia/Kolkata", datetime(2026, 8, 31, 18, 30, tzinfo=UTC))
WIN = {"baseline_window": B.as_dict(), "current_window": C_.as_dict()}


def plan(**kw: object) -> dict:
    return {"metric_id": "net_sales", "metric_version": 1, "analysis": "by_dimension", "dimension": "region", **WIN, **kw}


def issues_for(raw: object) -> list[str]:
    p, issues = parse_plan(raw)
    if p is None:
        return [i.code for i in issues]
    return [i.code for i in validate_plan(p, CTX)]


def test_valid_plan_compiles_to_parameterized_sql() -> None:
    p, issues = parse_plan(plan(filters=[{"dimension": "channel", "values": ["web"]}]))
    assert p is not None and not issues and not validate_plan(p, CTX)
    cq = compile_plan(p, CTX)
    assert "semantic.order_facts" in cq.sql and "%(tenant_id)s" in cq.sql
    assert "'web'" not in cq.sql  # filter values are bound, never inlined
    assert cq.params["filter_0"] == ["web"]
    assert cq.plan_hash == QueryPlan.model_validate(plan(filters=[{"dimension": "channel", "values": ["web"]}],
                                                         purpose="different text")).plan_hash()


UNSAFE_PLANS = [
    ("raw sql field", plan(sql="SELECT * FROM source.orders")),
    ("join field", plan(join={"table": "source.customers"})),
    ("expression field", plan(expression="sum(amount) * 2")),
    ("subquery in dimension", plan(dimension="(select password_hash from users)")),
    ("function in dimension", plan(dimension="pg_sleep(10)")),
    ("injection in filter value", plan(filters=[{"dimension": "region", "values": ["North' OR '1'='1"]}])),
    ("unknown filter dimension", plan(filters=[{"dimension": "customer_id", "values": ["1"]}])),
    ("unknown metric", plan(metric_id="gross_profit")),
    ("stale metric version", plan(metric_version=7)),
    ("window outside scope", plan(baseline_window={"start": "2026-01-01", "end": "2026-01-08"})),
    ("window past as-of", plan(current_window={"start": "2026-08-26", "end": "2026-09-02"},
                               baseline_window={"start": "2026-08-19", "end": "2026-08-26"})),
    ("limit too large", plan(limit=100000)),
    ("unsupported analysis", plan(analysis="export_all")),
    ("dimension not allowed for metric", plan(metric_id="order_count", dimension="category")),
    ("nested filter object", plan(filters=[{"dimension": "region", "values": ["North"], "op": "raw"}])),
    ("history without periods", plan(analysis="history", dimension=None)),
    ("non-json string", "DROP TABLE source.orders;"),
]


@pytest.mark.parametrize(("name", "raw"), UNSAFE_PLANS, ids=[n for n, _ in UNSAFE_PLANS])
def test_unsafe_plans_are_rejected(name: str, raw: object) -> None:
    assert issues_for(raw), f"{name} should be rejected"


def test_compiler_refuses_invalid_plans_even_if_called_directly() -> None:
    p = QueryPlan.model_validate(plan(metric_id="order_count", dimension="category"))
    with pytest.raises(ValueError):
        compile_plan(p, CTX)


@pytest.mark.parametrize("analysis", ["totals", "daily", "campaign_spend", "quality"])
def test_every_analysis_kind_compiles(analysis: str) -> None:
    p = QueryPlan.model_validate({"metric_id": "net_sales", "metric_version": 1, "analysis": analysis, **WIN})
    cq = compile_plan(p, CTX)
    assert "tenant_id" in cq.sql and cq.params["tenant_id"] == CTX.tenant_id


def test_category_split_uses_allocated_item_grain_view() -> None:
    p = QueryPlan.model_validate(plan(dimension="category"))
    cq = compile_plan(p, CTX)
    assert cq.shape == "by_dimension_allocated" and "order_category_facts" in cq.sql
