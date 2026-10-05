"""Versioned semantic catalog: metric contracts, dimensions, join rules and null handling.

The built-in version-1 definitions below are copied into ``metric_definitions`` for each
tenant by the seed command. Later versions are created by an authorized admin through the
API (audited); each version must reference a calculation implemented in
``app.metrics.calculations`` so that a definition change can never inject SQL or formulas.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

MetricKey = Literal["net_sales", "order_count", "average_order_value", "refund_rate"]

DIMENSIONS: dict[str, dict[str, Any]] = {
    "region": {
        "label": "Region",
        "grain": "order",
        "column": "region",
        "values": ["North", "South", "East", "West", "Central"],
        "null_handling": "Orders always carry a shipping region; NULL is rejected at ingestion.",
    },
    "channel": {
        "label": "Sales channel",
        "grain": "order",
        "column": "channel",
        "values": ["web", "app", "marketplace"],
        "null_handling": "Required at ingestion.",
    },
    "customer_segment": {
        "label": "Customer segment",
        "grain": "order",
        "column": "customer_segment",
        "values": ["new", "returning", "vip", "unknown"],
        "null_handling": "Orders whose customer row is missing are reported as 'unknown'.",
    },
    "campaign": {
        "label": "Attributed campaign",
        "grain": "order",
        "column": "campaign_id",
        "values_pattern": r"^(none|[0-9]{1,4})$",
        "null_handling": "Orders without campaign attribution are reported as 'none'.",
    },
    "category": {
        "label": "Product category",
        "grain": "order_category",
        "column": "category",
        "values": ["Electronics", "Apparel", "Home", "Beauty", "Grocery"],
        "null_handling": "Orders without items have no category rows and are excluded from category splits.",
        "allocation": (
            "Order-level discounts and refunds are allocated to categories in proportion to "
            "each category's share of the order's merchandise value."
        ),
    },
}


@dataclass(frozen=True)
class MetricSpec:
    key: str
    version: int
    name: str
    unit: Literal["currency", "count", "ratio"]
    additive: bool
    calculation: str
    description: str
    formula: str
    allowed_dimensions: tuple[str, ...]
    numerator: str | None = None
    denominator: str | None = None
    rules: dict[str, str] = field(default_factory=dict)

    def to_definition(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "version": self.version,
            "name": self.name,
            "unit": self.unit,
            "additive": self.additive,
            "calculation": self.calculation,
            "description": self.description,
            "formula": self.formula,
            "allowed_dimensions": list(self.allowed_dimensions),
            "numerator": self.numerator,
            "denominator": self.denominator,
            "rules": dict(self.rules),
            "currency": "INR",
        }


COMMON_RULES = {
    "eligible_orders": "Orders with status 'completed' and a non-null ordered_at inside the window.",
    "cancellation": "Canceled orders are excluded from every metric; they are reported separately as canceled merchandise.",
    "missing_dates": "Orders with NULL ordered_at cannot be placed in a window; they are excluded and surfaced as a data-quality count.",
    "windows": "Half-open [start, end) windows of complete local reporting days in the tenant timezone, converted to UTC.",
    "currency": "Single reporting currency INR. Currency is stored on every row; rows in other currencies are rejected by the quality check.",
    "rounding": "Exact Decimal arithmetic; values are rounded half-even to 2 decimals for display only.",
}

BUILTIN_METRICS: dict[str, MetricSpec] = {
    "net_sales": MetricSpec(
        key="net_sales",
        version=1,
        name="Net sales",
        unit="currency",
        additive=True,
        calculation="net_sales_v1",
        description="Merchandise sales less discounts and eligible refunds, attributed to the original order cohort.",
        formula="SUM(qty * unit_price) - SUM(discount_amount) - SUM(LEAST(refunds_as_of_watermark, merchandise - discount))",
        allowed_dimensions=("region", "channel", "customer_segment", "campaign", "category"),
        rules={
            **COMMON_RULES,
            "gross_merchandise": "Completed item quantity multiplied by unit price, excluding tax and shipping.",
            "discounts": "Order-level discount_amount, reported as a separate component.",
            "refund_attribution": (
                "Refunds are attributed to the original order's window (order cohort) and counted only if "
                "refunded_at is before the as-of watermark. Cash-flow refund timing is a different metric and is not used."
            ),
            "partial_refunds": "Partial refunds count at their amount; total refunds per order are capped at the order's merchandise less discount.",
            "negative_values": "Net sales may be negative for a segment when refunds exceed sales; the value is reported, not clipped.",
            "fanout": "Items are pre-aggregated to order grain and refunds to order grain before joining, so no join fan-out.",
        },
    ),
    "order_count": MetricSpec(
        key="order_count",
        version=1,
        name="Order count",
        unit="count",
        additive=True,
        calculation="order_count_v1",
        description="Number of eligible completed orders.",
        formula="COUNT(orders WHERE status = 'completed')",
        allowed_dimensions=("region", "channel", "customer_segment", "campaign"),
        rules=dict(COMMON_RULES),
    ),
    "average_order_value": MetricSpec(
        key="average_order_value",
        version=1,
        name="Average order value",
        unit="currency",
        additive=False,
        calculation="aov_v1",
        description="Net sales divided by eligible completed orders.",
        formula="net_sales / order_count",
        allowed_dimensions=("region", "channel", "customer_segment", "campaign"),
        numerator="net_sales",
        denominator="order_count",
        rules={
            **COMMON_RULES,
            "zero_denominator": "If there are no eligible orders the value is undefined (null) and no percentage change is computed.",
            "decomposition": "Non-additive: segment changes are split into rate effect and mix effect (exploratory).",
        },
    ),
    "refund_rate": MetricSpec(
        key="refund_rate",
        version=1,
        name="Refund rate",
        unit="ratio",
        additive=False,
        calculation="refund_rate_v1",
        description="Eligible completed orders with at least one qualifying refund divided by eligible completed orders.",
        formula="COUNT(completed orders with refunds_as_of_watermark > 0) / COUNT(completed orders)",
        allowed_dimensions=("region", "channel", "customer_segment", "campaign"),
        numerator="refunded_orders",
        denominator="order_count",
        rules={
            **COMMON_RULES,
            "qualifying_refund": "Any refund with amount > 0 recorded before the as-of watermark; partial refunds qualify.",
            "zero_denominator": "Undefined (null) when there are no eligible orders.",
            "maturity": "Recent cohorts have had less time to accrue refunds; the current window can understate the final rate.",
        },
    ),
}

SUPPORTED_CALCULATIONS = {m.calculation for m in BUILTIN_METRICS.values()}

# Terms that map unambiguously to a metric.
SYNONYMS: dict[str, str] = {
    "net sales": "net_sales",
    "net revenue": "net_sales",
    "order count": "order_count",
    "number of orders": "order_count",
    "orders": "order_count",
    "average order value": "average_order_value",
    "aov": "average_order_value",
    "basket size": "average_order_value",
    "refund rate": "refund_rate",
    "return rate": "refund_rate",
}
# Terms that must trigger a clarification instead of a silent choice.
AMBIGUOUS_TERMS: dict[str, list[str]] = {
    "revenue": ["net_sales", "average_order_value"],
    "sales": ["net_sales", "order_count"],
    "performance": ["net_sales", "order_count", "average_order_value", "refund_rate"],
}


def resolve_metric_term(question: str) -> tuple[str | None, list[str]]:
    """Return (metric_key, ambiguous_candidates). Never silently picks for an ambiguous term."""
    q = question.lower()
    # Longest synonyms first so "net sales" wins over "sales".
    for term in sorted(SYNONYMS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(term)}\b", q):
            return SYNONYMS[term], []
    for term, candidates in AMBIGUOUS_TERMS.items():
        if re.search(rf"\b{re.escape(term)}\b", q):
            return None, candidates
    return None, list(BUILTIN_METRICS)


def validate_definition(defn: dict[str, Any]) -> list[str]:
    """Validate an admin-submitted metric definition version."""
    errors: list[str] = []
    if defn.get("calculation") not in SUPPORTED_CALCULATIONS:
        errors.append(f"calculation must be one of {sorted(SUPPORTED_CALCULATIONS)}")
    base = BUILTIN_METRICS.get(str(defn.get("key")))
    if base is None:
        errors.append("unknown metric key")
        return errors
    if defn.get("calculation") != base.calculation:
        errors.append("calculation cannot change the metric family")
    dims = defn.get("allowed_dimensions", [])
    if not isinstance(dims, list) or not all(isinstance(d, str) for d in dims):
        errors.append("allowed_dimensions must be a list of strings")
    else:
        unknown = [d for d in dims if d not in DIMENSIONS]
        if unknown:
            errors.append(f"unknown dimensions: {unknown}")
        if not base.additive and "category" in dims:
            errors.append("category split is only defined for additive currency metrics")
    return errors


def dimension_value_ok(dimension: str, value: str) -> bool:
    spec = DIMENSIONS.get(dimension)
    if spec is None or not isinstance(value, str) or len(value) > 40:
        return False
    if "values" in spec:
        return value in spec["values"]
    return re.fullmatch(spec["values_pattern"], value) is not None
