"""Synthetic commerce source schema, row-level security and approved semantic views.

Revision ID: 0001
Revises:
Create Date: 2026-10-05

Security model
--------------
* Raw tables live in schema ``source`` and are owned by the analytics owner role.
* Every table has ROW LEVEL SECURITY enabled *and forced* (so even the owner is filtered).
  The policy compares tenant_id with the transaction-scoped setting ``app.tenant_id``.
  A missing or empty setting matches no rows: missing tenant context denies access.
* The read-only query role gets USAGE on schema ``semantic`` and SELECT on its views only.
  It has no privilege on ``source`` and cannot write anything.
* Views are security barriers and pre-aggregate order items to order grain so that
  order -> item -> refund joins cannot fan out.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy.engine import make_url

from app.config import get_settings

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "customers",
    "products",
    "campaigns",
    "campaign_daily_spend",
    "ingestion_batches",
    "orders",
    "order_items",
    "refunds",
)

TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _reader_role() -> str:
    name = make_url(get_settings().analytics_database_url).username
    if not name or not name.replace("_", "").isalnum():
        raise RuntimeError("Unexpected analytics reader role name")
    return name


def upgrade() -> None:
    op.execute("CREATE SCHEMA source")
    op.execute("CREATE SCHEMA semantic")

    op.execute(
        """
        CREATE TABLE source.customers (
            tenant_id    uuid        NOT NULL,
            customer_id  bigint      NOT NULL,
            source_id    text        NOT NULL,
            region       text,
            segment      text        NOT NULL,
            created_at   timestamptz NOT NULL,
            PRIMARY KEY (tenant_id, customer_id)
        );
        CREATE TABLE source.products (
            tenant_id    uuid          NOT NULL,
            product_id   integer       NOT NULL,
            sku          text          NOT NULL,
            name         text          NOT NULL,
            category     text          NOT NULL,
            list_price   numeric(12,2) NOT NULL CHECK (list_price >= 0),
            currency     char(3)       NOT NULL,
            PRIMARY KEY (tenant_id, product_id)
        );
        CREATE TABLE source.campaigns (
            tenant_id    uuid    NOT NULL,
            campaign_id  integer NOT NULL,
            name         text    NOT NULL,
            channel      text    NOT NULL,
            started_on   date    NOT NULL,
            ended_on     date,
            PRIMARY KEY (tenant_id, campaign_id)
        );
        CREATE TABLE source.campaign_daily_spend (
            tenant_id    uuid          NOT NULL,
            campaign_id  integer       NOT NULL,
            spend_date   date          NOT NULL,
            amount       numeric(14,2) NOT NULL CHECK (amount >= 0),
            currency     char(3)       NOT NULL,
            PRIMARY KEY (tenant_id, campaign_id, spend_date),
            FOREIGN KEY (tenant_id, campaign_id) REFERENCES source.campaigns (tenant_id, campaign_id)
        );
        CREATE TABLE source.ingestion_batches (
            tenant_id     uuid        NOT NULL,
            batch_id      bigint      NOT NULL,
            source_table  text        NOT NULL,
            covers_date   date        NOT NULL,
            loaded_at     timestamptz NOT NULL,
            row_count     integer     NOT NULL,
            watermark     timestamptz NOT NULL,
            PRIMARY KEY (tenant_id, batch_id)
        );
        CREATE TABLE source.orders (
            tenant_id        uuid          NOT NULL,
            order_id         bigint        NOT NULL,
            source_id        text          NOT NULL,
            customer_id      bigint        NOT NULL,
            ordered_at       timestamptz,
            status           text          NOT NULL CHECK (status IN ('completed', 'canceled')),
            channel          text          NOT NULL,
            region           text          NOT NULL,
            currency         char(3)       NOT NULL,
            discount_amount  numeric(14,2) NOT NULL DEFAULT 0 CHECK (discount_amount >= 0),
            tax_amount       numeric(14,2) NOT NULL DEFAULT 0,
            shipping_amount  numeric(14,2) NOT NULL DEFAULT 0,
            campaign_id      integer,
            batch_id         bigint        NOT NULL,
            ingested_at      timestamptz   NOT NULL,
            PRIMARY KEY (tenant_id, order_id),
            FOREIGN KEY (tenant_id, customer_id) REFERENCES source.customers (tenant_id, customer_id)
        );
        CREATE INDEX ix_orders_tenant_time ON source.orders (tenant_id, ordered_at);
        CREATE INDEX ix_orders_tenant_source ON source.orders (tenant_id, source_id);
        CREATE TABLE source.order_items (
            tenant_id      uuid          NOT NULL,
            order_item_id  bigint        NOT NULL,
            order_id       bigint        NOT NULL,
            product_id     integer       NOT NULL,
            quantity       integer       NOT NULL CHECK (quantity > 0),
            unit_price     numeric(12,2) NOT NULL CHECK (unit_price >= 0),
            currency       char(3)       NOT NULL,
            PRIMARY KEY (tenant_id, order_item_id),
            FOREIGN KEY (tenant_id, order_id) REFERENCES source.orders (tenant_id, order_id),
            FOREIGN KEY (tenant_id, product_id) REFERENCES source.products (tenant_id, product_id)
        );
        CREATE INDEX ix_items_tenant_order ON source.order_items (tenant_id, order_id);
        CREATE TABLE source.refunds (
            tenant_id    uuid          NOT NULL,
            refund_id    bigint        NOT NULL,
            order_id     bigint        NOT NULL,
            amount       numeric(14,2) NOT NULL CHECK (amount > 0),
            currency     char(3)       NOT NULL,
            refunded_at  timestamptz   NOT NULL,
            reason       text          NOT NULL,
            batch_id     bigint        NOT NULL,
            ingested_at  timestamptz   NOT NULL,
            PRIMARY KEY (tenant_id, refund_id),
            FOREIGN KEY (tenant_id, order_id) REFERENCES source.orders (tenant_id, order_id)
        );
        CREATE INDEX ix_refunds_tenant_order ON source.refunds (tenant_id, order_id);
        """
    )

    for table in TABLES:
        op.execute(f"ALTER TABLE source.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE source.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON source.{table} "
            f"USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})"
        )

    # ---- approved semantic views (the only objects the query role can read) ----
    op.execute(
        """
        CREATE VIEW semantic.order_facts WITH (security_barrier = true) AS
        SELECT o.tenant_id,
               o.order_id,
               o.source_id,
               o.ordered_at,
               o.status,
               o.channel,
               o.region,
               COALESCE(c.segment, 'unknown')            AS customer_segment,
               o.campaign_id,
               o.currency,
               COALESCE(i.merchandise_amount, 0)::numeric(14,2) AS merchandise_amount,
               COALESCE(i.units, 0)::bigint              AS units,
               o.discount_amount,
               (i.order_id IS NOT NULL)                  AS has_items,
               o.batch_id,
               o.ingested_at
          FROM source.orders o
          LEFT JOIN source.customers c
                 ON c.tenant_id = o.tenant_id AND c.customer_id = o.customer_id
          LEFT JOIN (
                SELECT tenant_id, order_id,
                       SUM(quantity * unit_price) AS merchandise_amount,
                       SUM(quantity)              AS units
                  FROM source.order_items
                 GROUP BY tenant_id, order_id
          ) i ON i.tenant_id = o.tenant_id AND i.order_id = o.order_id;

        CREATE VIEW semantic.order_category_facts WITH (security_barrier = true) AS
        SELECT o.tenant_id,
               o.order_id,
               o.ordered_at,
               o.status,
               o.channel,
               o.region,
               COALESCE(c.segment, 'unknown')  AS customer_segment,
               o.campaign_id,
               o.currency,
               p.category,
               SUM(oi.quantity * oi.unit_price)::numeric(14,2) AS category_merchandise,
               SUM(oi.quantity)::bigint        AS units
          FROM source.orders o
          JOIN source.order_items oi ON oi.tenant_id = o.tenant_id AND oi.order_id = o.order_id
          JOIN source.products p     ON p.tenant_id = oi.tenant_id AND p.product_id = oi.product_id
          LEFT JOIN source.customers c ON c.tenant_id = o.tenant_id AND c.customer_id = o.customer_id
         GROUP BY o.tenant_id, o.order_id, o.ordered_at, o.status, o.channel, o.region,
                  c.segment, o.campaign_id, o.currency, p.category;

        CREATE VIEW semantic.refund_facts WITH (security_barrier = true) AS
        SELECT tenant_id, refund_id, order_id, amount, currency, refunded_at, ingested_at
          FROM source.refunds;

        CREATE VIEW semantic.campaign_spend WITH (security_barrier = true) AS
        SELECT s.tenant_id, s.campaign_id, c.name AS campaign_name, c.channel,
               s.spend_date, s.amount, s.currency
          FROM source.campaign_daily_spend s
          JOIN source.campaigns c ON c.tenant_id = s.tenant_id AND c.campaign_id = s.campaign_id;

        CREATE VIEW semantic.ingestion_status WITH (security_barrier = true) AS
        SELECT tenant_id, batch_id, source_table, covers_date, loaded_at, row_count, watermark
          FROM source.ingestion_batches;
        """
    )

    reader = _reader_role()
    op.execute(f'GRANT USAGE ON SCHEMA semantic TO "{reader}"')
    op.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA semantic TO "{reader}"')
    op.execute(f'REVOKE ALL ON SCHEMA source FROM "{reader}"')
    op.execute(f'REVOKE ALL ON ALL TABLES IN SCHEMA source FROM "{reader}"')


def downgrade() -> None:
    op.execute("DROP SCHEMA semantic CASCADE")
    op.execute("DROP SCHEMA source CASCADE")
