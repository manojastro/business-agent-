"""Read-only source summaries for the overview and data-quality pages.

These use the same QueryPlan compiler, read-only role and tenant context as investigations;
they create no evidence because they are dashboards, not investigation findings.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.analytics import run_source_query
from app.db.models import MetricDefinition, Tenant
from app.metrics import calculations as C
from app.metrics.windows import Window, default_windows, local_midnight_utc
from app.tools.query_plan import QueryPlan, ValidationContext, compile_plan
from app.tools.registry import FRESHNESS_SQL


def _catalog(db: Session, tenant: Tenant) -> dict[str, dict[str, Any]]:
    rows = db.scalars(select(MetricDefinition).where(MetricDefinition.tenant_id == tenant.id,
                                                     MetricDefinition.status == "active")
                      .order_by(MetricDefinition.metric_key, MetricDefinition.version)).all()
    return {r.metric_key: dict(r.definition) for r in rows}


def freshness(tenant: Tenant, as_of: date, lookback_days: int = 21) -> dict[str, Any]:
    start = as_of - timedelta(days=lookback_days)
    res = run_source_query(tenant.id, FRESHNESS_SQL, {"tenant_id": str(tenant.id), "start": start})
    by_day: dict[str, dict[str, Any]] = {}
    marks = []
    for r in res.rows:
        d = by_day.setdefault(r["covers_date"][:10], {"date": r["covers_date"][:10], "orders_rows": None, "refunds_rows": None})
        d[f"{r['source_table']}_rows"] = r["row_count"]
        if r["source_table"] == "orders":
            marks.append(datetime.fromisoformat(r["watermark"]))
    days = [(start + timedelta(days=i)).isoformat() for i in range(lookback_days)]
    missing = [d for d in days if by_day.get(d, {}).get("orders_rows") is None]
    wm = max(marks) if marks else None
    required = local_midnight_utc(as_of, tenant.timezone)
    return {
        "as_of": as_of.isoformat(),
        "watermark": wm.isoformat() if wm else None,
        "required_watermark": required.isoformat(),
        "fresh": bool(wm and wm >= required),
        "missing_order_batch_days": missing,
        "days": [by_day.get(d, {"date": d, "orders_rows": None, "refunds_rows": None}) for d in days],
    }


def metric_changes(db: Session, tenant: Tenant, as_of: date) -> list[dict[str, Any]]:
    catalog = _catalog(db, tenant)
    b, c = default_windows(as_of)
    fr = freshness(tenant, as_of, lookback_days=15)
    wm = datetime.fromisoformat(fr["watermark"]) if fr["watermark"] else local_midnight_utc(as_of, tenant.timezone)
    ctx = ValidationContext(tenant.id, catalog, b, c, as_of, tenant.timezone,
                            min(wm, local_midnight_utc(as_of, tenant.timezone)))
    plan = QueryPlan(metric_id="net_sales", metric_version=int(catalog["net_sales"]["version"]), analysis="totals",
                     baseline_window=b.as_dict(), current_window=c.as_dict())  # type: ignore[arg-type]
    cq = compile_plan(plan, ctx)
    rows = {r["window"]: C.Aggregates.from_row(r) for r in run_source_query(tenant.id, cq.statement, cq.params).rows}
    base, cur = rows.get("baseline", C.Aggregates()), rows.get("current", C.Aggregates())
    out = []
    for key, d in catalog.items():
        dl = C.delta(key, base, cur)
        out.append({"metric": key, "name": d["name"], "version": d["version"], "unit": d["unit"], **dl,
                    "baseline_window": b.as_dict(), "current_window": c.as_dict()})
    return out


def quality(db: Session, tenant: Tenant, as_of: date) -> dict[str, Any]:
    catalog = _catalog(db, tenant)
    b, c = default_windows(as_of)
    ctx = ValidationContext(tenant.id, catalog, b, c, as_of, tenant.timezone, local_midnight_utc(as_of, tenant.timezone))
    version = int(catalog["net_sales"]["version"])
    q = QueryPlan(metric_id="net_sales", metric_version=version, analysis="quality",
                  baseline_window=b.as_dict(), current_window=c.as_dict())  # type: ignore[arg-type]
    cq = compile_plan(q, ctx)
    qrows = run_source_query(tenant.id, cq.statement, cq.params).rows
    daily = QueryPlan(metric_id="net_sales", metric_version=version, analysis="daily",
                      baseline_window=b.as_dict(), current_window=c.as_dict())  # type: ignore[arg-type]
    dq = compile_plan(daily, ctx)
    drows = run_source_query(tenant.id, dq.statement, dq.params).rows
    return {
        "windows": {"baseline": b.as_dict(), "current": c.as_dict()},
        "checks": qrows,
        "daily_orders": [{"day": r["day"], "completed_orders": r["completed_orders"], "canceled_orders": r["canceled_orders"]}
                         for r in drows],
        "freshness": freshness(tenant, as_of),
    }


def window_of(d: dict[str, str]) -> Window:
    return Window.from_dict(d)
