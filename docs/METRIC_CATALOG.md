# Metric catalog (version 1)

Source of truth: `backend/app/metrics/catalog.py`, copied per tenant into `metric_definitions`. New versions are
created only by an admin (`POST /api/v1/metrics/{key}/versions`), validated, and audited. A version can change
description and allowed dimensions; the calculation is bound to vetted code and cannot be replaced.

## Common rules (all metrics)

| Rule | Definition |
|---|---|
| Eligible orders | `status = 'completed'` and `ordered_at` inside the window |
| Windows | Half-open `[start, end)` of complete local days in the tenant timezone (`Asia/Kolkata` for the demo), converted to UTC. The as-of day is incomplete and excluded. Baseline and current have equal length and the same weekday coverage. |
| Cancellation | Canceled orders are excluded from every metric and reported separately as canceled merchandise. |
| Missing dates | Orders with NULL `ordered_at` cannot be placed in a window; they are excluded and counted by the quality check. |
| Currency | INR only; currency stored on every row. Multiple currencies would need a dated exchange-rate policy (not implemented). |
| Rounding | Exact Decimal arithmetic; half-even to 2 dp for display and claims. |
| Percent change | `(current - baseline) / |baseline| × 100`; **undefined** when the baseline is 0. |
| Materiality | Change is material if `|%| > max(3%, noise band)`; the noise band is 2 sd of the % difference between two periods, estimated from weekday-adjusted daily residuals over the 6 preceding periods. |

## Metrics

| Key | Definition | Additive | Allowed dimensions |
|---|---|---|---|
| `net_sales` | Σ(qty × unit_price) − Σ discount_amount − Σ min(refunds as of watermark, merchandise − discount) | yes | region, channel, customer_segment, campaign, category |
| `order_count` | count of eligible completed orders | yes | region, channel, customer_segment, campaign |
| `average_order_value` | net_sales / order_count; undefined if no orders | no (rate/mix split, exploratory) | region, channel, customer_segment, campaign |
| `refund_rate` | completed orders with ≥ 1 refund before the watermark / completed orders | no | region, channel, customer_segment, campaign |

Net sales details:

- **Gross merchandise** = completed item quantity × unit price, excluding tax and shipping.
- **Discounts** are an order-level component reported separately.
- **Refunds** are attributed to the *original order cohort* and counted only if `refunded_at` is before the as-of
  watermark. Cash-flow refund timing (by refund date) is a different metric and is not used. Recent cohorts have
  had less time to be refunded; reports carry a maturity limitation.
- **Partial refunds** count at their amount; per-order refunds are capped at merchandise − discount.
- **Negative net sales** for a segment are reported, not clipped.
- **Fan-out**: items are aggregated to order grain and refunds to order grain before joining.
- **Category split** (item grain): order-level discounts and refunds are allocated by each category's share of the
  order's merchandise; contributions reconcile within one paisa per segment.

## Decompositions

- **Components** (exact): Δnet = Δmerchandise − Δdiscount − Δrefunds.
- **Dimension contributions** (exact for order-grain dimensions): segment current − segment baseline, one
  dimension at a time; never add contributions across dimensions.
- **Price/volume** (two-factor, mix not separated → exploratory): price effect = (P₁−P₀)·Q₁, volume effect = (Q₁−Q₀)·P₀,
  P = merchandise per unit.
- **Ratio metrics**: rate effect Σw₁(r₁−r₀) + mix effect Σ(w₁−w₀)r₀ (exploratory).

## Ambiguous terms

`revenue`, `sales`, `performance` always trigger a clarification question. Recognised synonyms: net revenue →
net_sales; orders / number of orders → order_count; AOV / basket size → average_order_value; return rate → refund_rate.
