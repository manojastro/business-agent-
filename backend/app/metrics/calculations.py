"""Vetted deterministic calculations. No model output is ever executed here.

All money is Decimal. Ratios are Decimal with 6 decimal places. ``None`` means undefined
(e.g. zero denominator) and is propagated, never replaced by zero.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any

CENT = Decimal("0.01")
RATIO_Q = Decimal("0.000001")
SMALL_SEGMENT_ORDERS = 30
MATERIAL_CHANGE_PCT = Decimal("3")


def d(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def money(x: Decimal | None) -> str | None:
    return None if x is None else str(x.quantize(CENT, rounding=ROUND_HALF_EVEN))


def ratio(x: Decimal | None) -> str | None:
    return None if x is None else str(x.quantize(RATIO_Q, rounding=ROUND_HALF_EVEN))


@dataclass(frozen=True)
class Aggregates:
    merchandise: Decimal = Decimal("0")
    discount: Decimal = Decimal("0")
    refunds: Decimal = Decimal("0")
    completed_orders: int = 0
    refunded_orders: int = 0
    canceled_orders: int = 0
    canceled_merchandise: Decimal = Decimal("0")
    units: int = 0

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Aggregates:
        return cls(
            merchandise=d(row.get("merchandise")),
            discount=d(row.get("discount")),
            refunds=d(row.get("refunds")),
            completed_orders=int(row.get("completed_orders") or 0),
            refunded_orders=int(row.get("refunded_orders") or 0),
            canceled_orders=int(row.get("canceled_orders") or 0),
            canceled_merchandise=d(row.get("canceled_merchandise")),
            units=int(row.get("units") or 0),
        )

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        for k, v in out.items():
            if isinstance(v, Decimal):
                out[k] = str(v)
        return out

    def __add__(self, other: Aggregates) -> Aggregates:
        return Aggregates(
            self.merchandise + other.merchandise,
            self.discount + other.discount,
            self.refunds + other.refunds,
            self.completed_orders + other.completed_orders,
            self.refunded_orders + other.refunded_orders,
            self.canceled_orders + other.canceled_orders,
            self.canceled_merchandise + other.canceled_merchandise,
            self.units + other.units,
        )


def net_sales(a: Aggregates) -> Decimal:
    return a.merchandise - a.discount - a.refunds


def order_count(a: Aggregates) -> Decimal:
    return Decimal(a.completed_orders)


def average_order_value(a: Aggregates) -> Decimal | None:
    if a.completed_orders == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 34
        return net_sales(a) / Decimal(a.completed_orders)


def refund_rate(a: Aggregates) -> Decimal | None:
    if a.completed_orders == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 34
        return Decimal(a.refunded_orders) / Decimal(a.completed_orders)


CALCULATIONS = {
    "net_sales_v1": net_sales,
    "order_count_v1": order_count,
    "aov_v1": average_order_value,
    "refund_rate_v1": refund_rate,
}
METRIC_TO_CALC = {
    "net_sales": "net_sales_v1",
    "order_count": "order_count_v1",
    "average_order_value": "aov_v1",
    "refund_rate": "refund_rate_v1",
}
UNITS = {"net_sales": "currency", "order_count": "count", "average_order_value": "currency", "refund_rate": "ratio"}


def metric_value(metric_key: str, a: Aggregates) -> Decimal | None:
    return CALCULATIONS[METRIC_TO_CALC[metric_key]](a)


def fmt(metric_key: str, x: Decimal | None) -> str | None:
    if x is None:
        return None
    unit = UNITS[metric_key]
    if unit == "ratio":
        return ratio(x)
    if unit == "count":
        return str(int(x))
    return money(x)


def delta(metric_key: str, baseline: Aggregates, current: Aggregates) -> dict[str, Any]:
    b = metric_value(metric_key, baseline)
    c = metric_value(metric_key, current)
    abs_change = None if (b is None or c is None) else c - b
    pct = None
    if abs_change is not None and b is not None and b != 0:
        with localcontext() as ctx:
            ctx.prec = 34
            pct = (abs_change / abs(b) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return {
        "metric": metric_key,
        "baseline": fmt(metric_key, b),
        "current": fmt(metric_key, c),
        "abs_change": fmt(metric_key, abs_change) if metric_key != "refund_rate" else ratio(abs_change),
        "pct_change": None if pct is None else str(pct),
        "pct_change_defined": pct is not None,
        "material": pct is not None and abs(pct) >= MATERIAL_CHANGE_PCT,
    }


def component_breakdown(baseline: Aggregates, current: Aggregates) -> dict[str, Any]:
    """Net sales delta = merchandise delta - discount delta - refunds delta (exact)."""
    dm = current.merchandise - baseline.merchandise
    dd = current.discount - baseline.discount
    dr = current.refunds - baseline.refunds
    total = net_sales(current) - net_sales(baseline)
    components = {
        "merchandise": dm,
        "discount": -dd,
        "refunds": -dr,
    }
    return {
        "components": {k: money(v) for k, v in components.items()},
        "total_delta": money(total),
        "reconciles": sum(components.values()) == total,
        "canceled_merchandise_change": money(current.canceled_merchandise - baseline.canceled_merchandise),
        "canceled_orders_change": current.canceled_orders - baseline.canceled_orders,
    }


def price_volume(baseline: Aggregates, current: Aggregates) -> dict[str, Any]:
    """Merchandise delta = price effect + volume effect, with price = merchandise per unit.

    price effect  = (P1 - P0) * Q1
    volume effect = (Q1 - Q0) * P0
    The identity is exact; any residual comes only from Decimal division and is reported.
    """
    if baseline.units == 0 or current.units == 0:
        return {"defined": False, "reason": "zero units in a window"}
    with localcontext() as ctx:
        ctx.prec = 34
        p0 = baseline.merchandise / baseline.units
        p1 = current.merchandise / current.units
        price_effect = (p1 - p0) * current.units
        volume_effect = (current.units - baseline.units) * p0
        total = current.merchandise - baseline.merchandise
        residual = total - price_effect - volume_effect
    return {
        "defined": True,
        "avg_unit_price_baseline": money(p0),
        "avg_unit_price_current": money(p1),
        "units_baseline": baseline.units,
        "units_current": current.units,
        "price_effect": money(price_effect),
        "volume_effect": money(volume_effect),
        "merchandise_delta": money(total),
        "residual": money(residual),
        "method": "two-factor: price at current volume, volume at baseline price; mix is not separated (exploratory)",
    }


def additive_contributions(
    metric_key: str,
    baseline_by_segment: dict[str, Aggregates],
    current_by_segment: dict[str, Aggregates],
    total_baseline: Aggregates,
    total_current: Aggregates,
    allocated: bool = False,
) -> dict[str, Any]:
    """Segment contribution = current segment value - baseline segment value.

    Contributions over one dimension must sum to the total delta. Allocated dimensions
    (category) can differ by sub-cent rounding; the tolerance is reported explicitly.
    """
    segments = sorted(set(baseline_by_segment) | set(current_by_segment))
    total_delta = d(metric_value(metric_key, total_current)) - d(metric_value(metric_key, total_baseline))
    rows = []
    s = Decimal("0")
    for seg in segments:
        b = baseline_by_segment.get(seg, Aggregates())
        c = current_by_segment.get(seg, Aggregates())
        bv = d(metric_value(metric_key, b))
        cv = d(metric_value(metric_key, c))
        contrib = cv - bv
        s += contrib
        share = None
        if total_delta != 0:
            with localcontext() as ctx:
                ctx.prec = 34
                share = (contrib / total_delta * 100).quantize(Decimal("0.1"))
        pct = None
        if bv != 0:
            with localcontext() as ctx:
                ctx.prec = 34
                pct = (contrib / abs(bv) * 100).quantize(Decimal("0.01"))
        rows.append(
            {
                "segment": seg,
                "baseline": fmt(metric_key, bv),
                "current": fmt(metric_key, cv),
                "contribution": fmt(metric_key, contrib),
                "share_of_total_delta_pct": None if share is None else str(share),
                "segment_pct_change": None if pct is None else str(pct),
                "baseline_orders": b.completed_orders,
                "current_orders": c.completed_orders,
                "small_segment": min(b.completed_orders, c.completed_orders) < SMALL_SEGMENT_ORDERS,
            }
        )
    tolerance = (CENT * len(segments)) if allocated else Decimal("0")
    gap = s - total_delta
    return {
        "metric": metric_key,
        "rows": rows,
        "total_delta": fmt(metric_key, total_delta),
        "sum_of_contributions": fmt(metric_key, s),
        "reconciliation_gap": money(gap) if metric_key != "order_count" else str(int(gap)),
        "tolerance": str(tolerance),
        "reconciles": abs(gap) <= tolerance,
        "segments_examined": len(segments),
    }


def rate_mix_decomposition(
    metric_key: str,
    baseline_by_segment: dict[str, Aggregates],
    current_by_segment: dict[str, Aggregates],
) -> dict[str, Any]:
    """Exploratory decomposition of a ratio metric into within-segment rate and mix effects.

    total change = sum_s w1_s (r1_s - r0_s)  +  sum_s (w1_s - w0_s) r0_s
    where w is the segment's share of eligible orders and r the segment's metric value.
    """
    segments = sorted(set(baseline_by_segment) | set(current_by_segment))
    n0 = sum(a.completed_orders for a in baseline_by_segment.values())
    n1 = sum(a.completed_orders for a in current_by_segment.values())
    if n0 == 0 or n1 == 0:
        return {"defined": False, "reason": "zero eligible orders in a window"}
    rows = []
    with localcontext() as ctx:
        ctx.prec = 34
        rate_total = Decimal("0")
        mix_total = Decimal("0")
        for seg in segments:
            b = baseline_by_segment.get(seg, Aggregates())
            c = current_by_segment.get(seg, Aggregates())
            w0 = Decimal(b.completed_orders) / n0
            w1 = Decimal(c.completed_orders) / n1
            r0 = d(metric_value(metric_key, b)) if b.completed_orders else Decimal("0")
            r1 = d(metric_value(metric_key, c)) if c.completed_orders else Decimal("0")
            rate_eff = w1 * (r1 - r0)
            mix_eff = (w1 - w0) * r0
            rate_total += rate_eff
            mix_total += mix_eff
            rows.append(
                {
                    "segment": seg,
                    "baseline_value": fmt(metric_key, r0) if b.completed_orders else None,
                    "current_value": fmt(metric_key, r1) if c.completed_orders else None,
                    "baseline_share": ratio(w0),
                    "current_share": ratio(w1),
                    "rate_effect": ratio(rate_eff),
                    "mix_effect": ratio(mix_eff),
                    "small_segment": min(b.completed_orders, c.completed_orders) < SMALL_SEGMENT_ORDERS,
                }
            )
    return {
        "defined": True,
        "rows": rows,
        "rate_effect_total": ratio(rate_total),
        "mix_effect_total": ratio(mix_total),
        "exploratory": True,
    }


def dominant_contributor(contrib: dict[str, Any], min_share: Decimal = Decimal("60")) -> dict[str, Any] | None:
    """The segment carrying at least ``min_share`` percent of the total delta, if any."""
    best = None
    for row in contrib.get("rows", []):
        share = row.get("share_of_total_delta_pct")
        if share is None:
            continue
        if Decimal(share) >= min_share and (best is None or Decimal(share) > Decimal(best["share_of_total_delta_pct"])):
            best = row
    return best


def noise_band(
    metric_key: str, daily_rows: list[dict[str, Any]], baseline_start: str, period_days: int
) -> dict[str, Any]:
    """Typical variation of a period-over-period change, estimated from the daily history.

    Additive metrics: remove the weekday pattern (subtract each weekday's mean), estimate the
    residual daily variance (n - 7 degrees of freedom), scale to a period sum assuming
    independent days, and convert to the standard deviation of the % difference between two
    periods: sd_change = sqrt(2) * sqrt(period_days * var_resid) / mean_period * 100.
    The band is 2 * sd_change. This uses ~35 degrees of freedom for six weeks of history,
    far more stable than the sample deviation of five week-over-week changes.
    Ratio metrics fall back to the deviation of period-over-period changes.
    """
    from datetime import date as _date

    start = _date.fromisoformat(baseline_start)
    periods: dict[int, Aggregates] = {}
    daily: list[tuple[_date, Decimal]] = []
    for row in daily_rows:
        day = _date.fromisoformat(str(row["day"])[:10])
        if day >= start:
            continue
        a = Aggregates.from_row(row)
        idx = ((start - day).days - 1) // period_days
        periods[idx] = periods.get(idx, Aggregates()) + a
        v = metric_value(metric_key, a)
        if v is not None:
            daily.append((day, v))
    ordered = [periods[i] for i in sorted(periods, reverse=True)]  # oldest first
    values = [metric_value(metric_key, a) for a in ordered]
    changes: list[Decimal] = []
    with localcontext() as ctx:
        ctx.prec = 34
        for prev, cur in zip(values, values[1:], strict=False):
            if prev is None or cur is None or prev == 0:
                continue
            changes.append((cur - prev) / abs(prev) * 100)
        info = {
            "periods": len(values),
            "period_values": [fmt(metric_key, v) for v in values],
            "changes_pct": [str(c.quantize(Decimal("0.01"))) for c in changes],
        }
        additive = metric_key in ("net_sales", "order_count")
        if additive and len(daily) >= 21:
            by_wd: dict[int, list[Decimal]] = {}
            for day, v in daily:
                by_wd.setdefault(day.weekday(), []).append(v)
            means = {wd: sum(vs, Decimal(0)) / len(vs) for wd, vs in by_wd.items()}
            resid = [v - means[day.weekday()] for day, v in daily]
            dof = len(resid) - len(by_wd)
            if dof > 0:
                var = sum((r * r for r in resid), Decimal(0)) / dof
                mean_period = sum((v for _, v in daily), Decimal(0)) / len(daily) * period_days
                if mean_period > 0:
                    sd_change = (Decimal(2) * period_days * var).sqrt() / mean_period * 100
                    band = (2 * sd_change).quantize(Decimal("0.01"))
                    return {"defined": True, **info, "stdev_pct": str(sd_change.quantize(Decimal("0.01"))),
                            "band_pct": str(band), "degrees_of_freedom": dof,
                            "method": "weekday-adjusted daily residual variance scaled to a period; band = 2 sd of the "
                                      "% change between two independent periods"}
        if len(changes) < 3:
            return {"defined": False, **info}
        mean = sum(changes, Decimal(0)) / len(changes)
        var_c = sum(((c - mean) ** 2 for c in changes), Decimal(0)) / (len(changes) - 1)
        sd = var_c.sqrt()
    return {"defined": True, **info, "stdev_pct": str(sd.quantize(Decimal("0.01"))),
            "band_pct": str((2 * sd).quantize(Decimal("0.01"))), "degrees_of_freedom": len(changes) - 1,
            "method": "two sample standard deviations of period-over-period % change"}


def is_material(pct_change: str | None, band: dict[str, Any] | None) -> bool:
    if pct_change is None:
        return True  # undefined change (zero baseline) is always surfaced
    threshold = MATERIAL_CHANGE_PCT
    if band and band.get("defined"):
        threshold = max(threshold, Decimal(band["band_pct"]))
    return abs(Decimal(pct_change)) > threshold
