"""Metric arithmetic, windows, timezone boundaries, zero denominators (pure unit tests)."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.metrics import calculations as C
from app.metrics.catalog import BUILTIN_METRICS, resolve_metric_term, validate_definition
from app.metrics.windows import Window, WindowError, default_windows, validate_pair

A = C.Aggregates


def agg(m: str, d: str = "0", r: str = "0", n: int = 0, rn: int = 0, units: int = 0) -> C.Aggregates:
    return A(Decimal(m), Decimal(d), Decimal(r), n, rn, 0, Decimal("0"), units)


def test_net_sales_and_components_reconcile_exactly() -> None:
    b = agg("1000.10", "100.00", "50.05", 10, 1, 20)
    c = agg("900.00", "120.00", "180.00", 9, 3, 18)
    assert C.net_sales(b) == Decimal("850.05")
    comp = C.component_breakdown(b, c)
    assert comp["components"] == {"merchandise": "-100.10", "discount": "-20.00", "refunds": "-129.95"}
    assert comp["total_delta"] == "-250.05" and comp["reconciles"]


def test_aov_and_refund_rate_definitions() -> None:
    a = agg("1000", "100", "100", 8, 2)
    assert C.average_order_value(a) == Decimal("100")  # (1000-100-100)/8
    assert C.refund_rate(a) == Decimal("0.25")


def test_zero_denominators_are_undefined_not_zero() -> None:
    empty = agg("0")
    assert C.average_order_value(empty) is None
    assert C.refund_rate(empty) is None
    d = C.delta("net_sales", empty, agg("500", n=5))
    assert d["pct_change"] is None and d["pct_change_defined"] is False
    assert d["abs_change"] == "500.00"
    d2 = C.delta("average_order_value", empty, agg("500", n=5))
    assert d2["baseline"] is None and d2["abs_change"] is None


def test_negative_net_sales_is_reported_not_clipped() -> None:
    a = agg("100", "0", "150", 1, 1)
    assert C.net_sales(a) == Decimal("-50")


def test_price_volume_identity_is_exact() -> None:
    b = agg("1000.00", units=100)
    c = agg("880.00", units=110)
    pv = C.price_volume(b, c)
    assert Decimal(pv["price_effect"]) + Decimal(pv["volume_effect"]) == Decimal(pv["merchandise_delta"])
    assert pv["residual"] == "0.00"


def test_additive_contributions_reconcile_and_flag_small_segments() -> None:
    bs = {"North": agg("500", n=50), "South": agg("500", n=10)}
    cs = {"North": agg("300", n=40), "South": agg("520", n=12)}
    out = C.additive_contributions("net_sales", bs, cs, bs["North"] + bs["South"], cs["North"] + cs["South"])
    assert out["reconciles"] and out["sum_of_contributions"] == out["total_delta"] == "-180.00"
    north = next(r for r in out["rows"] if r["segment"] == "North")
    assert north["share_of_total_delta_pct"] == "111.1"
    assert next(r for r in out["rows"] if r["segment"] == "South")["small_segment"] is True


def test_rate_mix_decomposition_sums_to_total_change() -> None:
    bs = {"a": agg("1000", n=10, rn=1), "b": agg("1000", n=10, rn=5)}
    cs = {"a": agg("1000", n=5, rn=1), "b": agg("1000", n=15, rn=6)}
    out = C.rate_mix_decomposition("refund_rate", bs, cs)
    total = C.refund_rate(cs["a"] + cs["b"]) - C.refund_rate(bs["a"] + bs["b"])  # type: ignore[operator]
    assert abs(Decimal(out["rate_effect_total"]) + Decimal(out["mix_effect_total"]) - total) < Decimal("0.000002")


def test_default_windows_are_half_open_complete_days() -> None:
    b, c = default_windows(date(2026, 9, 1))
    assert c == Window(date(2026, 8, 25), date(2026, 9, 1))
    assert b == Window(date(2026, 8, 18), date(2026, 8, 25))
    validate_pair(b, c, date(2026, 9, 1))


def test_window_validation_rules() -> None:
    with pytest.raises(WindowError):  # unequal duration
        validate_pair(Window(date(2026, 8, 1), date(2026, 8, 8)), Window(date(2026, 8, 8), date(2026, 8, 14)), date(2026, 9, 1))
    with pytest.raises(WindowError):  # includes the incomplete as-of day
        validate_pair(Window(date(2026, 8, 19), date(2026, 8, 26)), Window(date(2026, 8, 26), date(2026, 9, 2)), date(2026, 9, 1))
    with pytest.raises(WindowError):  # different weekday coverage
        validate_pair(Window(date(2026, 8, 1), date(2026, 8, 3)), Window(date(2026, 8, 4), date(2026, 8, 6)), date(2026, 9, 1))


def test_timezone_boundaries_asia_kolkata() -> None:
    w = Window(date(2026, 8, 25), date(2026, 8, 26))
    start, end = w.utc_bounds("Asia/Kolkata")
    assert start == datetime(2026, 8, 24, 18, 30, tzinfo=UTC)
    assert end == datetime(2026, 8, 25, 18, 30, tzinfo=UTC)
    late_local = datetime(2026, 8, 25, 18, 29, 59, tzinfo=UTC)  # 23:59:59 IST on the 25th -> inside
    next_local = datetime(2026, 8, 25, 18, 30, 0, tzinfo=UTC)  # 00:00 IST on the 26th -> outside (half-open)
    assert start <= late_local < end
    assert not (start <= next_local < end)


def test_ambiguous_terms_require_clarification() -> None:
    assert resolve_metric_term("Why did net sales fall?") == ("net_sales", [])
    metric, options = resolve_metric_term("Why did revenue drop last week?")
    assert metric is None and "net_sales" in options
    assert resolve_metric_term("What happened to AOV?")[0] == "average_order_value"


def test_metric_definition_changes_are_validated() -> None:
    d = BUILTIN_METRICS["order_count"].to_definition()
    d["allowed_dimensions"] = ["region", "not_a_dimension"]
    assert validate_definition(d)
    d2 = BUILTIN_METRICS["refund_rate"].to_definition()
    d2["allowed_dimensions"] = ["category"]
    assert any("category" in e for e in validate_definition(d2))
    d3 = BUILTIN_METRICS["net_sales"].to_definition()
    d3["calculation"] = "drop table"
    assert validate_definition(d3)


def test_noise_band_uses_complete_history() -> None:
    rows = []
    for i in range(42):
        day = date(2026, 7, 7).fromordinal(date(2026, 7, 7).toordinal() + i)
        rows.append({"day": day.isoformat(), "merchandise": str(1000 + (i % 7) * 50 + (i % 3) * 10), "discount": "0",
                     "refunds": "0", "completed_orders": 10, "refunded_orders": 0, "canceled_orders": 0,
                     "canceled_merchandise": "0", "units": 10})
    nb = C.noise_band("net_sales", rows, "2026-08-18", 7)
    assert nb["defined"] and nb["periods"] == 6 and nb["degrees_of_freedom"] == 35
    assert C.is_material("-40.00", nb) and not C.is_material("1.00", nb)
