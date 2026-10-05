"""Load generated datasets into the analytics database as the owner role.

COPY is staged through temporary tables. Rows are written inside a transaction that sets ``app.tenant_id``; because row-level security
is forced on every source table, the owner itself can only delete and insert rows for that
tenant (the policy's WITH CHECK clause rejects anything else).
"""

from __future__ import annotations

import psycopg

from app.config import get_settings
from app.db.session import psycopg_conninfo
from app.seed.synthetic import Dataset

COLUMNS = {
    "customers": "tenant_id, customer_id, source_id, region, segment, created_at",
    "products": "tenant_id, product_id, sku, name, category, list_price, currency",
    "campaigns": "tenant_id, campaign_id, name, channel, started_on, ended_on",
    "campaign_daily_spend": "tenant_id, campaign_id, spend_date, amount, currency",
    "ingestion_batches": "tenant_id, batch_id, source_table, covers_date, loaded_at, row_count, watermark",
    "orders": (
        "tenant_id, order_id, source_id, customer_id, ordered_at, status, channel, region, currency, "
        "discount_amount, tax_amount, shipping_amount, campaign_id, batch_id, ingested_at"
    ),
    "order_items": "tenant_id, order_item_id, order_id, product_id, quantity, unit_price, currency",
    "refunds": "tenant_id, refund_id, order_id, amount, currency, refunded_at, reason, batch_id, ingested_at",
}
DELETE_ORDER = ["refunds", "order_items", "orders", "customers", "products", "campaign_daily_spend", "campaigns", "ingestion_batches"]
INSERT_ORDER = ["customers", "products", "campaigns", "campaign_daily_spend", "ingestion_batches", "orders", "order_items", "refunds"]


def owner_conninfo() -> str:
    url = get_settings().analytics_owner_database_url
    if not url:
        raise RuntimeError("ANALYTICS_OWNER_DATABASE_URL is required to seed the analytics database")
    return psycopg_conninfo(url)


def load_dataset(ds: Dataset, conninfo: str | None = None) -> dict[str, int]:
    tid = str(ds.spec.tenant_id)
    counts: dict[str, int] = {}
    with psycopg.connect(conninfo or owner_conninfo()) as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tid,))
            for t in DELETE_ORDER:
                conn.execute(f"DELETE FROM source.{t} WHERE tenant_id = %s", (tid,))
            for t in INSERT_ORDER:
                rows = getattr(ds, {"campaign_daily_spend": "spend", "ingestion_batches": "batches",
                                    "order_items": "items"}.get(t, t))
                # COPY cannot target a table with row-level security, so stage into a temp
                # table and INSERT ... SELECT (which the RLS WITH CHECK policy validates).
                stage = f"stage_{t}"
                conn.execute(f"CREATE TEMP TABLE {stage} (LIKE source.{t}) ON COMMIT DROP")
                with conn.cursor() as cur:
                    with cur.copy(f"COPY {stage} ({COLUMNS[t]}) FROM STDIN") as cp:
                        for r in rows:
                            cp.write_row(r)
                conn.execute(f"INSERT INTO source.{t} ({COLUMNS[t]}) SELECT {COLUMNS[t]} FROM {stage}")
                counts[t] = len(rows)
    with psycopg.connect(conninfo or owner_conninfo(), autocommit=True) as conn:
        for t in INSERT_ORDER:  # keep planner statistics current after bulk loads
            conn.execute(f"ANALYZE source.{t}")
    return counts


def delete_tenant(tenant_id: str, conninfo: str | None = None) -> None:
    with psycopg.connect(conninfo or owner_conninfo()) as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
            for t in DELETE_ORDER:
                conn.execute(f"DELETE FROM source.{t} WHERE tenant_id = %s", (tenant_id,))
