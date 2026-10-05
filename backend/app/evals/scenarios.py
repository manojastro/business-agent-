"""Demo datasets and ground-truth evaluation scenario families.

Ground truth (``expected_driver``) is stored only in the application database table
``eval_suites``; agent tools read the analytics ``semantic`` views and never see it.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any

from app.seed.synthetic import DatasetSpec, Scenario

DEMO_AS_OF = date(2026, 9, 1)

DEMO_DATASETS = [
    DatasetSpec(
        slug="acme-retail",
        name="Acme Retail (demo)",
        seed=7,
        start=DEMO_AS_OF - timedelta(days=100),
        as_of=DEMO_AS_OF,
        orders_per_day=260,
        scenario=Scenario(
            "refund_increase",
            # Refunds spike in North; the Search campaign's spend is cut in the same week
            # (a coincident, misleading signal used for the planted bad recommendation).
            {"region": "North", "refund_rate": 0.8, "spend_change_campaign": 1, "spend_multiplier": 0.5},
        ),
    ),
    DatasetSpec(
        slug="bharat-bazaar",
        name="Bharat Bazaar (demo)",
        seed=11,
        start=DEMO_AS_OF - timedelta(days=100),
        as_of=DEMO_AS_OF,
        orders_per_day=190,
        scenario=Scenario("product_demand_drop", {"category": "Electronics", "drop": 0.7}),
    ),
]
DEMO_TENANT_SETTINGS = {
    "acme-retail": {"demo_planted_recommendation": True},
    "bharat-bazaar": {},
}

EVAL_AS_OF = date(2026, 9, 1)
EVAL_DAYS = 56  # 6 history periods + baseline + current, 7 days each
EVAL_ORDERS_PER_DAY = 140
SPLITS = {"development": [101, 102], "heldout": [201, 202]}


def _family_params(family: str, rng: random.Random) -> tuple[dict[str, Any], str]:
    current_start = EVAL_AS_OF - timedelta(days=7)
    if family == "product_demand_drop":
        cat = rng.choice(["Electronics", "Home"])
        return {"category": cat, "drop": 0.8}, f"dimension:category={cat}"
    if family == "region_decline":
        region = rng.choice(["North", "South", "East", "West"])
        return {"region": region, "drop": 0.75}, f"dimension:region={region}"
    if family == "price_reduction":
        return {"price_factor": round(rng.uniform(0.74, 0.8), 2)}, "component:price"
    if family == "discount_increase":
        return {"discount_share": 0.85}, "component:discount"
    if family == "refund_increase":
        return {"region": None, "refund_rate": round(rng.uniform(0.28, 0.34), 2)}, "component:refunds"
    if family == "missing_ingestion_batch":
        k = rng.randint(1, 4)
        days = [current_start + timedelta(days=k), current_start + timedelta(days=k + 1)]
        return {"missing_days": days, "null_date_share": 0.02}, "data_quality:missing_batch"
    if family == "duplicate_records":
        return {"dup_share": round(rng.uniform(0.18, 0.24), 2)}, "data_quality:duplicates"
    if family == "canceled_order_change":
        return {"cancel_rate": round(rng.uniform(0.22, 0.28), 2)}, "component:cancellations"
    if family == "zero_baseline":
        return {}, "zero_baseline"
    if family == "unchanged_irrelevant_campaign":
        return {"spend_change_campaign": rng.choice([1, 2, 3]), "spend_multiplier": 2.5}, "no_material_change"
    raise ValueError(family)


FAMILIES = [
    "product_demand_drop",
    "region_decline",
    "price_reduction",
    "discount_increase",
    "refund_increase",
    "missing_ingestion_batch",
    "duplicate_records",
    "canceled_order_change",
    "zero_baseline",
    "unchanged_irrelevant_campaign",
]


def eval_cases(split: str) -> list[dict[str, Any]]:
    """Scenario cases for a split. Each case = dataset spec + ground truth."""
    cases = []
    for seed in SPLITS[split]:
        for family in FAMILIES:
            rng = random.Random(f"{family}:{seed}")
            params, driver = _family_params(family, rng)
            slug = f"eval-{family.replace('_', '-')}-{seed}"
            spec = DatasetSpec(
                slug=slug,
                name=f"Eval {family} seed {seed}",
                seed=seed,
                start=EVAL_AS_OF - timedelta(days=EVAL_DAYS),
                as_of=EVAL_AS_OF,
                orders_per_day=EVAL_ORDERS_PER_DAY,
                scenario=Scenario(family if family != "unchanged_irrelevant_campaign" else "baseline", params),
            )
            cases.append(
                {
                    "slug": slug,
                    "family": family,
                    "seed": seed,
                    "split": split,
                    "spec": spec,
                    "ground_truth": {
                        "expected_driver": driver,
                        "metric": "net_sales",
                        "as_of": EVAL_AS_OF.isoformat(),
                        "params": {k: (v if not isinstance(v, list) else [str(x) for x in v]) for k, v in params.items()},
                    },
                }
            )
    return cases
