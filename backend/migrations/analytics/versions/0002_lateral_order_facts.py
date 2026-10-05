"""Compute per-order item totals with a LATERAL subquery so only orders inside the requested
windows are aggregated (the view stays fan-out free and keeps the same columns).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE VIEW semantic.order_facts WITH (security_barrier = true) AS
        SELECT o.tenant_id,
               o.order_id,
               o.source_id,
               o.ordered_at,
               o.status,
               o.channel,
               o.region,
               COALESCE(c.segment, 'unknown')                    AS customer_segment,
               o.campaign_id,
               o.currency,
               COALESCE(i.merchandise_amount, 0)::numeric(14,2) AS merchandise_amount,
               COALESCE(i.units, 0)::bigint                      AS units,
               o.discount_amount,
               (i.item_rows > 0)                                 AS has_items,
               o.batch_id,
               o.ingested_at
          FROM source.orders o
          LEFT JOIN source.customers c
                 ON c.tenant_id = o.tenant_id AND c.customer_id = o.customer_id
          LEFT JOIN LATERAL (
                SELECT SUM(oi.quantity * oi.unit_price) AS merchandise_amount,
                       SUM(oi.quantity)                 AS units,
                       COUNT(*)                         AS item_rows
                  FROM source.order_items oi
                 WHERE oi.tenant_id = o.tenant_id AND oi.order_id = o.order_id
          ) i ON true;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE VIEW semantic.order_facts WITH (security_barrier = true) AS
        SELECT o.tenant_id, o.order_id, o.source_id, o.ordered_at, o.status, o.channel, o.region,
               COALESCE(c.segment, 'unknown') AS customer_segment, o.campaign_id, o.currency,
               COALESCE(i.merchandise_amount, 0)::numeric(14,2) AS merchandise_amount,
               COALESCE(i.units, 0)::bigint AS units, o.discount_amount, (i.order_id IS NOT NULL) AS has_items,
               o.batch_id, o.ingested_at
          FROM source.orders o
          LEFT JOIN source.customers c ON c.tenant_id = o.tenant_id AND c.customer_id = o.customer_id
          LEFT JOIN (SELECT tenant_id, order_id, SUM(quantity * unit_price) AS merchandise_amount, SUM(quantity) AS units
                       FROM source.order_items GROUP BY tenant_id, order_id) i
                 ON i.tenant_id = o.tenant_id AND i.order_id = o.order_id;
        """
    )
