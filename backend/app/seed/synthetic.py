"""Deterministic synthetic commerce data.

Every dataset is generated from (slug, seed, scenario) with ``random.Random(seed)`` so a reset
always reproduces identical rows. Baselines are clean; data-quality problems (duplicates,
missing batches, missing dates, cancellations, late refunds) only appear in the scenario that
is about them.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any
from zoneinfo import ZoneInfo

NAMESPACE = uuid.UUID("6f1c1d8e-7a51-4c1e-9a55-1f6f2b0f7a10")
CENT = Decimal("0.01")
TZ = "Asia/Kolkata"

REGIONS = [("North", 25), ("South", 25), ("East", 20), ("West", 20), ("Central", 10)]
CHANNELS = [("web", 45), ("app", 35), ("marketplace", 20)]
SEGMENTS = [("new", 30), ("returning", 55), ("vip", 15)]
CATEGORIES: dict[str, tuple[int, int, int]] = {
    # category: (min price, max price, popularity weight)
    "Electronics": (3000, 9000, 14),
    "Apparel": (600, 2500, 26),
    "Home": (900, 4500, 18),
    "Beauty": (250, 1200, 20),
    "Grocery": (150, 700, 22),
}
CAMPAIGN_TEMPLATES = [
    ("Search Always-On", "web"),
    ("Social Prospecting", "app"),
    ("Marketplace Boost", "marketplace"),
    ("Festive Teaser", "web"),
]


def tenant_uuid(slug: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, slug)


def _pick(rng: random.Random, options: list[tuple[str, int]]) -> str:
    return rng.choices([o for o, _ in options], weights=[w for _, w in options], k=1)[0]


def _q(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_EVEN)


@dataclass
class Scenario:
    family: str = "baseline"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class DatasetSpec:
    slug: str
    name: str
    seed: int
    start: date  # first local day with data
    as_of: date  # data covers [start, as_of); as_of itself is not loaded
    orders_per_day: int
    scenario: Scenario = field(default_factory=Scenario)
    timezone: str = TZ
    window_days: int = 7

    @property
    def tenant_id(self) -> uuid.UUID:
        return tenant_uuid(self.slug)

    @property
    def current_start(self) -> date:
        return self.as_of - timedelta(days=self.window_days)


@dataclass
class Dataset:
    spec: DatasetSpec
    customers: list[tuple] = field(default_factory=list)
    products: list[tuple] = field(default_factory=list)
    campaigns: list[tuple] = field(default_factory=list)
    spend: list[tuple] = field(default_factory=list)
    batches: list[tuple] = field(default_factory=list)
    orders: list[tuple] = field(default_factory=list)
    items: list[tuple] = field(default_factory=list)
    refunds: list[tuple] = field(default_factory=list)
    watermark: datetime | None = None


def _local_dt(d: date, seconds: int, tz: str) -> datetime:
    return (datetime.combine(d, time.min, tzinfo=ZoneInfo(tz)) + timedelta(seconds=seconds)).astimezone(UTC)


def generate(spec: DatasetSpec) -> Dataset:
    rng = random.Random(f"{spec.slug}:{spec.seed}")
    sc = spec.scenario
    p = sc.params
    tid = spec.tenant_id
    ds = Dataset(spec)
    tz = spec.timezone
    data_end_utc = _local_dt(spec.as_of, 0, tz)

    # ---- products ----
    product_ids_by_cat: dict[str, list[int]] = {}
    price: dict[int, Decimal] = {}
    pid = 0
    for cat, (lo, hi, _) in CATEGORIES.items():
        for j in range(8):
            pid += 1
            list_price = _q(Decimal(rng.randint(lo, hi)) - Decimal("0.01"))
            ds.products.append((tid, pid, f"SKU-{cat[:3].upper()}-{j + 1:02d}", f"{cat} item {j + 1}", cat, list_price, "INR"))
            product_ids_by_cat.setdefault(cat, []).append(pid)
            price[pid] = list_price
    cat_weights = [(c, w) for c, (_, _, w) in CATEGORIES.items()]

    # ---- customers (identifiers are synthetic; views never expose them) ----
    n_customers = max(400, spec.orders_per_day * 25)
    cust_region: dict[int, str] = {}
    for cid in range(1, n_customers + 1):
        region = _pick(rng, REGIONS)
        cust_region[cid] = region
        created = _local_dt(spec.start - timedelta(days=rng.randint(1, 700)), rng.randint(0, 86399), tz)
        ds.customers.append((tid, cid, f"CUS-{cid:06d}", region, _pick(rng, SEGMENTS), created))

    # ---- campaigns and spend ----
    for k, (cname, ch) in enumerate(CAMPAIGN_TEMPLATES, start=1):
        ds.campaigns.append((tid, k, cname, ch, spec.start, None))
    days = (spec.as_of - spec.start).days
    for i in range(days):
        day = spec.start + timedelta(days=i)
        in_current = day >= spec.current_start
        for k, (_, ch) in enumerate(CAMPAIGN_TEMPLATES, start=1):
            base = Decimal({"web": 9000, "app": 7000, "marketplace": 4000}[ch]) * (1 + Decimal(k % 2) / 4)
            amt = base * Decimal(str(round(rng.uniform(0.9, 1.1), 3)))
            if in_current and p.get("spend_change_campaign") == k:
                amt *= Decimal(str(p.get("spend_multiplier", 1)))
            ds.spend.append((tid, k, day, _q(amt), "INR"))

    # ---- orders ----
    order_id = 0
    item_id = 0
    refund_id = 0
    batch_seq = 0
    orders_batch_for_day: dict[date, int] = {}
    refunds_batch_for_day: dict[date, int] = {}
    missing_days = set(p.get("missing_days", [])) if sc.family == "missing_ingestion_batch" else set()
    rows_per_batch: dict[int, int] = {}

    def batch(kind: str, d: date) -> int:
        nonlocal batch_seq
        table = orders_batch_for_day if kind == "orders" else refunds_batch_for_day
        if d not in table:
            batch_seq += 1
            table[d] = batch_seq
        return table[d]

    zero_baseline = sc.family == "zero_baseline"
    # The loader runs every day even when there is nothing to load (row_count 0), except on
    # days whose batch is deliberately missing in the missing_ingestion_batch scenario.
    for i in range(days):
        batch("orders", spec.start + timedelta(days=i))
    for i in range(days):
        day = spec.start + timedelta(days=i)
        in_current = day >= spec.current_start
        if zero_baseline and not in_current:
            continue
        weekday_factor = 1.15 if day.weekday() >= 5 else 1.0
        n = int(spec.orders_per_day * weekday_factor * rng.uniform(0.92, 1.08))
        for _ in range(n):
            cid = rng.randint(1, n_customers)
            region = cust_region[cid]
            channel = _pick(rng, CHANNELS)
            # draw everything for this order up-front so scenario skips don't shift other rows' randomness much
            n_items = rng.choices([1, 2, 3], weights=[60, 30, 10])[0]
            lines = []
            for _ in range(n_items):
                cat = _pick(rng, cat_weights)
                prod = rng.choice(product_ids_by_cat[cat])
                qty = rng.choices([1, 2, 3], weights=[75, 20, 5])[0]
                unit = _q(price[prod] * Decimal(str(round(rng.uniform(0.95, 1.05), 3))))
                lines.append((cat, prod, qty, unit))
            seconds = rng.randint(0, 86399)
            disc_draw = rng.random()
            disc_pct = Decimal(str(round(rng.uniform(0.05, 0.15), 3)))
            cancel_draw = rng.random()
            refund_draw = rng.random()
            partial_draw = rng.random()
            partial_pct = Decimal(str(round(rng.uniform(0.2, 0.6), 3)))
            refund_delay = rng.choices([0, 1, 2, 3, 4, 5, 6], weights=[10, 30, 25, 15, 10, 6, 4])[0]
            refund_secs = rng.randint(0, 86399)
            campaign_draw = rng.random()
            dup_draw = rng.random()
            null_date_draw = rng.random()

            if in_current:
                if sc.family == "region_decline" and region == p["region"] and rng.random() < p.get("drop", 0.45):
                    continue
                if sc.family == "product_demand_drop":
                    lines = [ln for ln in lines if not (ln[0] == p["category"] and rng.random() < p.get("drop", 0.6))]
                    if not lines:
                        continue
                if sc.family == "price_reduction":
                    lines = [(c, pr, q, _q(u * Decimal(str(p.get("price_factor", 0.82))))) for c, pr, q, u in lines]
                if sc.family == "discount_increase" and rng.random() < p.get("discount_share", 0.75):
                    disc_draw = 0.0
                    disc_pct = Decimal(str(round(rng.uniform(0.2, 0.3), 3)))

            merch = sum((Decimal(q) * u for _, _, q, u in lines), Decimal("0"))
            discount = _q(merch * disc_pct) if disc_draw < 0.30 else Decimal("0.00")

            cancel_rate = 0.03
            if in_current and sc.family == "canceled_order_change":
                cancel_rate = p.get("cancel_rate", 0.18)
            status = "canceled" if cancel_draw < cancel_rate else "completed"

            campaign_id = None
            if campaign_draw < 0.25:
                matching = [k for k, (_, ch) in enumerate(CAMPAIGN_TEMPLATES, start=1) if ch == channel]
                campaign_id = matching[0] if matching else None

            ordered_at = _local_dt(day, seconds, tz)
            order_id += 1
            bid = batch("orders", day)
            ingested = _local_dt(day + timedelta(days=1), 2 * 3600 + rng.randint(0, 1800), tz)
            src = f"ORD-{spec.slug[:6].upper()}-{order_id:07d}"
            stored_at: datetime | None = ordered_at
            if sc.family == "missing_ingestion_batch" and in_current and null_date_draw < p.get("null_date_share", 0.0):
                stored_at = None
            if day in missing_days:
                continue  # the batch for this day never arrived
            ds.orders.append(
                (tid, order_id, src, cid, stored_at, status, channel, region, "INR", discount,
                 _q(merch * Decimal("0.05")), Decimal("49.00") if merch < 999 else Decimal("0.00"),
                 campaign_id, bid, ingested)
            )
            rows_per_batch[bid] = rows_per_batch.get(bid, 0) + 1
            for _cat, prod, qty, unit in lines:
                item_id += 1
                ds.items.append((tid, item_id, order_id, prod, qty, unit, "INR"))

            if sc.family == "duplicate_records" and in_current and dup_draw < p.get("dup_share", 0.12):
                dup_oid = order_id + 5_000_000
                ds.orders.append(
                    (tid, dup_oid, src, cid, stored_at, status, channel, region, "INR", discount,
                     _q(merch * Decimal("0.05")), Decimal("0.00"), campaign_id, bid, ingested + timedelta(minutes=7))
                )
                rows_per_batch[bid] += 1
                for _cat, prod, qty, unit in lines:
                    item_id += 1
                    ds.items.append((tid, item_id, dup_oid, prod, qty, unit, "INR"))

            if status != "completed":
                continue
            refund_rate = 0.04
            delay = refund_delay
            if in_current and sc.family == "refund_increase":
                if p.get("region") in (None, region):
                    refund_rate = p.get("refund_rate", 0.24)
                    delay = min(refund_delay, 1)
            if refund_draw < refund_rate:
                refundable = merch - discount
                amount = _q(refundable * partial_pct) if partial_draw < 0.3 else refundable
                refunded_at = _local_dt(day + timedelta(days=delay), refund_secs, tz)
                if amount > 0 and refunded_at < data_end_utc:
                    refund_id += 1
                    rday = refunded_at.astimezone(ZoneInfo(tz)).date()
                    rb = batch("refunds", rday)
                    ds.refunds.append(
                        (tid, refund_id, order_id, amount, "INR", refunded_at,
                         rng.choice(["damaged", "wrong_size", "not_as_described", "late_delivery"]),
                         rb, _local_dt(rday + timedelta(days=1), 3 * 3600, tz))
                    )
                    rows_per_batch[rb] = rows_per_batch.get(rb, 0) + 1

    # ---- ingestion batches ----
    for kind, table in (("orders", orders_batch_for_day), ("refunds", refunds_batch_for_day)):
        for day, bid in sorted(table.items()):
            if kind == "orders" and day in missing_days:
                continue
            ds.batches.append(
                (tid, bid, kind, day, _local_dt(day + timedelta(days=1), 2 * 3600, tz),
                 rows_per_batch.get(bid, 0), _local_dt(day + timedelta(days=1), 0, tz))
            )
    ds.watermark = max(b[6] for b in ds.batches) if ds.batches else None
    return ds


# ---------------------------------------------------------------------------------------
# Independent Python reference implementation of the metric rules (used by tests to prove
# that SQL totals are exact).
# ---------------------------------------------------------------------------------------


def reference_aggregates(ds: Dataset, start_utc: datetime, end_utc: datetime, watermark: datetime) -> dict[str, Any]:
    merch_by_order: dict[int, Decimal] = {}
    units_by_order: dict[int, int] = {}
    for _, _, oid, _, qty, unit, _ in ds.items:
        merch_by_order[oid] = merch_by_order.get(oid, Decimal("0")) + Decimal(qty) * unit
        units_by_order[oid] = units_by_order.get(oid, 0) + qty
    refunds_by_order: dict[int, Decimal] = {}
    for _, _, oid, amount, _, refunded_at, *_ in ds.refunds:
        if refunded_at < watermark:
            refunds_by_order[oid] = refunds_by_order.get(oid, Decimal("0")) + amount
    merch_t = discount_t = refunds_t = canceled_m = Decimal("0")
    completed = refunded = canceled = units = 0
    for row in ds.orders:
        oid, ordered_at, status, discount = row[1], row[4], row[5], row[9]
        if ordered_at is None or not (start_utc <= ordered_at < end_utc):
            continue
        merch = merch_by_order.get(oid, Decimal("0"))
        if status == "canceled":
            canceled += 1
            canceled_m += merch
            continue
        r = refunds_by_order.get(oid, Decimal("0"))
        merch_t += merch
        discount_t += discount
        refunds_t += min(r, max(merch - discount, Decimal("0")))
        completed += 1
        refunded += 1 if r > 0 else 0
        units += units_by_order.get(oid, 0)
    return {
        "merchandise": merch_t, "discount": discount_t, "refunds": refunds_t, "completed_orders": completed,
        "refunded_orders": refunded, "canceled_orders": canceled, "canceled_merchandise": canceled_m, "units": units,
    }
