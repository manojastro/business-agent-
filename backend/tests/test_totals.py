"""Exact deterministic totals: SQL through the compiler equals an independent Python reference."""

from datetime import timedelta

import pytest

from app.db.analytics import run_source_query
from app.evals.scenarios import DEMO_DATASETS
from app.metrics import calculations as C
from app.metrics.catalog import BUILTIN_METRICS
from app.metrics.windows import Window, default_windows
from app.seed.synthetic import generate, reference_aggregates
from app.tools.query_plan import QueryPlan, ValidationContext, compile_plan

KEYS = ["merchandise", "discount", "refunds", "completed_orders", "refunded_orders", "canceled_orders",
        "canceled_merchandise", "units"]


def _ctx(ds, b: Window, c: Window) -> ValidationContext:  # noqa: ANN001
    return ValidationContext(ds.spec.tenant_id, {k: m.to_definition() for k, m in BUILTIN_METRICS.items()}, b, c,
                             ds.spec.as_of, ds.spec.timezone, ds.watermark)


def _sql_totals(ds, b: Window, c: Window) -> dict[str, C.Aggregates]:  # noqa: ANN001
    p = QueryPlan(metric_id="net_sales", metric_version=1, analysis="totals",
                  baseline_window=b.as_dict(), current_window=c.as_dict())  # type: ignore[arg-type]
    cq = compile_plan(p, _ctx(ds, b, c))
    return {r["window"]: C.Aggregates.from_row(r) for r in run_source_query(ds.spec.tenant_id, cq.statement, cq.params).rows}


def _assert_exact(ds, b: Window, c: Window) -> None:  # noqa: ANN001
    sql = _sql_totals(ds, b, c)
    for name, w in (("baseline", b), ("current", c)):
        ref = reference_aggregates(ds, *w.utc_bounds(ds.spec.timezone), ds.watermark)
        got = sql[name]
        for k in KEYS:
            assert str(getattr(got, k)) == str(ref[k]), f"{ds.spec.slug} {name} {k}"


@pytest.mark.parametrize("spec", DEMO_DATASETS, ids=[d.slug for d in DEMO_DATASETS])
def test_demo_tenant_totals_are_exact(spec, demo_tenants) -> None:  # noqa: ANN001
    ds = generate(spec)
    _assert_exact(ds, *default_windows(spec.as_of))
    # an older pair of windows as well
    b, c = default_windows(spec.as_of - timedelta(days=28))
    _assert_exact(ds, b, c)


def test_small_dataset_totals_with_partial_refunds_and_fanout(small_dataset) -> None:  # noqa: ANN001
    ds = small_dataset
    multi_item_orders = {oid for _, _, oid, *_ in ds.items if sum(1 for x in ds.items if x[2] == oid) > 1}
    refunded = {r[2] for r in ds.refunds}
    assert multi_item_orders & refunded, "fixture must contain refunded multi-item orders to exercise fan-out"
    _assert_exact(ds, *default_windows(ds.spec.as_of))


def test_refund_cohort_attribution_respects_watermark(small_dataset) -> None:  # noqa: ANN001
    ds = small_dataset
    b, c = default_windows(ds.spec.as_of)
    early = c.utc_bounds(ds.spec.timezone)[0] + timedelta(days=2)  # watermark inside the current window
    p = QueryPlan(metric_id="net_sales", metric_version=1, analysis="totals", baseline_window=b.as_dict(),
                  current_window=c.as_dict())  # type: ignore[arg-type]
    ctx = _ctx(ds, b, c)
    ctx.refund_watermark = early
    cq = compile_plan(p, ctx)
    sql = {r["window"]: r for r in run_source_query(ds.spec.tenant_id, cq.statement, cq.params).rows}
    ref = reference_aggregates(ds, *c.utc_bounds(ds.spec.timezone), early)
    assert sql["current"]["refunds"] == str(ref["refunds"])
    full = reference_aggregates(ds, *c.utc_bounds(ds.spec.timezone), ds.watermark)
    assert ref["refunds"] < full["refunds"]  # later refunds excluded by the as-of watermark


def test_category_contributions_reconcile_to_total(small_dataset) -> None:  # noqa: ANN001
    ds = small_dataset
    b, c = default_windows(ds.spec.as_of)
    totals = _sql_totals(ds, b, c)
    p = QueryPlan(metric_id="net_sales", metric_version=1, analysis="by_dimension", dimension="category",
                  baseline_window=b.as_dict(), current_window=c.as_dict())  # type: ignore[arg-type]
    cq = compile_plan(p, _ctx(ds, b, c))
    rows = run_source_query(ds.spec.tenant_id, cq.statement, cq.params).rows
    bs = {r["segment"]: C.Aggregates.from_row(r) for r in rows if r["window"] == "baseline"}
    cs = {r["segment"]: C.Aggregates.from_row(r) for r in rows if r["window"] == "current"}
    out = C.additive_contributions("net_sales", bs, cs, totals["baseline"], totals["current"], allocated=True)
    assert out["reconciles"], out["reconciliation_gap"]


@pytest.mark.parametrize("dim", ["region", "channel", "customer_segment", "campaign"])
def test_order_grain_contributions_reconcile_exactly(small_dataset, dim: str) -> None:  # noqa: ANN001
    ds = small_dataset
    b, c = default_windows(ds.spec.as_of)
    totals = _sql_totals(ds, b, c)
    p = QueryPlan(metric_id="net_sales", metric_version=1, analysis="by_dimension", dimension=dim,
                  baseline_window=b.as_dict(), current_window=c.as_dict())  # type: ignore[arg-type]
    cq = compile_plan(p, _ctx(ds, b, c))
    rows = run_source_query(ds.spec.tenant_id, cq.statement, cq.params).rows
    bs = {r["segment"]: C.Aggregates.from_row(r) for r in rows if r["window"] == "baseline"}
    cs = {r["segment"]: C.Aggregates.from_row(r) for r in rows if r["window"] == "current"}
    out = C.additive_contributions("net_sales", bs, cs, totals["baseline"], totals["current"])
    assert out["reconciles"] and out["reconciliation_gap"] == "0.00"
