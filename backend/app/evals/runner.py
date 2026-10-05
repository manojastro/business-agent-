"""Scenario evaluation: fixed query dashboard vs single-pass data agent vs full investigation graph.

Ground truth comes from ``eval_suites`` (application DB). Reference totals are recomputed by
regenerating each dataset from its seed with the independent Python implementation in
``app.seed.synthetic.reference_aggregates`` - not with the SQL path being evaluated.

Fixture-mode results measure the pipeline and deterministic controls with scripted model
behaviour; they are not evidence of language-model reasoning quality and are labelled as such.
"""

from __future__ import annotations

import secrets
import time
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.agents import prompts as P
from app.agents import schemas as S
from app.agents.graph import advance
from app.agents.runtime import Runtime, reset_runtime, set_runtime
from app.auth.passwords import hash_password
from app.config import get_settings
from app.db.analytics import run_source_query
from app.db.models import Claim, EvalRun, EvalSuite, Investigation, MetricDefinition, Tenant, User
from app.db.session import session_scope
from app.evals.scenarios import eval_cases
from app.evidence.verification import CAUSAL_RE, HEDGE_RE
from app.metrics import calculations as C
from app.metrics.windows import default_windows, local_midnight_utc
from app.providers.base import ModelRequest
from app.providers.factory import get_provider
from app.seed.synthetic import generate, reference_aggregates
from app.tools.query_plan import QueryPlan, ValidationContext, compile_plan

SYSTEMS = ("fixed_dashboard", "single_pass", "full_graph")
EVAL_USER = "eval-runner@system.local"


def _eval_user() -> uuid.UUID:
    with session_scope() as s:
        u = s.scalar(select(User).where(User.email == EVAL_USER))
        if u is None:
            u = User(email=EVAL_USER, display_name="Evaluation runner", password_hash=hash_password(secrets.token_urlsafe(24)),
                     is_active=False)
            s.add(u)
            s.flush()
        return u.id


def _reference(case: dict[str, Any]) -> dict[str, Any]:
    spec = case["spec"]
    ds = generate(spec)
    b, c = default_windows(spec.as_of)
    wm = min(ds.watermark or local_midnight_utc(spec.as_of, spec.timezone), local_midnight_utc(spec.as_of, spec.timezone))
    rb = C.Aggregates.from_row(reference_aggregates(ds, *b.utc_bounds(spec.timezone), wm))
    rc = C.Aggregates.from_row(reference_aggregates(ds, *c.utc_bounds(spec.timezone), wm))
    return C.delta("net_sales", rb, rc)


def _ctx(tenant_id: uuid.UUID, as_of: date) -> ValidationContext:
    with session_scope() as s:
        defs = {d.metric_key: dict(d.definition) for d in s.scalars(
            select(MetricDefinition).where(MetricDefinition.tenant_id == tenant_id, MetricDefinition.status == "active"))}
    b, c = default_windows(as_of)
    return ValidationContext(tenant_id, defs, b, c, as_of, "Asia/Kolkata", local_midnight_utc(as_of, "Asia/Kolkata"))


def _run_plan(ctx: ValidationContext, analysis: str, dimension: str | None = None) -> list[dict[str, Any]]:
    plan = QueryPlan(metric_id="net_sales", metric_version=int(ctx.metric_definitions["net_sales"]["version"]),
                     analysis=analysis, dimension=dimension,  # type: ignore[arg-type]
                     baseline_window=ctx.baseline.as_dict(), current_window=ctx.current.as_dict())  # type: ignore[arg-type]
    cq = compile_plan(plan, ctx)
    return run_source_query(ctx.tenant_id, cq.statement, cq.params).rows


def _windows(rows: list[dict[str, Any]]) -> tuple[C.Aggregates, C.Aggregates]:
    w = {r["window"]: C.Aggregates.from_row(r) for r in rows}
    return w.get("baseline", C.Aggregates()), w.get("current", C.Aggregates())


def _dimension_tops(ctx: ValidationContext, tb: C.Aggregates, tc: C.Aggregates) -> tuple[list[dict[str, Any]], int]:
    tops = []
    q = 0
    for dim in ("region", "category", "channel"):
        rows = _run_plan(ctx, "by_dimension", dim)
        q += 1
        bs = {r["segment"]: C.Aggregates.from_row(r) for r in rows if r["window"] == "baseline"}
        cs = {r["segment"]: C.Aggregates.from_row(r) for r in rows if r["window"] == "current"}
        con = C.additive_contributions("net_sales", bs, cs, tb, tc, allocated=dim == "category")
        best = max(con["rows"], key=lambda r: abs(Decimal(r["contribution"])))
        tops.append({"dimension": dim, "segment": best["segment"], "contribution": best["contribution"],
                     "share": best["share_of_total_delta_pct"]})
    return tops, q


def run_fixed_dashboard(case: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    ctx = _ctx(case["spec"].tenant_id, case["spec"].as_of)
    tb, tc = _windows(_run_plan(ctx, "totals"))
    tops, q = _dimension_tops(ctx, tb, tc)
    best = max(tops, key=lambda t: abs(Decimal(t["contribution"])))
    dl = C.delta("net_sales", tb, tc)
    return {"driver": f"dimension:{best['dimension']}={best['segment']}", "headline": dl, "queries": q + 1,
            "unsupported_causal": 0, "completed": True, "cost": "0", "tokens": 0,
            "latency_ms": int((time.perf_counter() - t0) * 1000)}


def run_single_pass(case: dict[str, Any], mode: str) -> dict[str, Any]:
    t0 = time.perf_counter()
    ctx = _ctx(case["spec"].tenant_id, case["spec"].as_of)
    tb, tc = _windows(_run_plan(ctx, "totals"))
    tops, q = _dimension_tops(ctx, tb, tc)
    spend_rows = _run_plan(ctx, "campaign_spend")
    from app.evidence.verification import spend_changes

    changes = spend_changes(spend_rows)
    largest = max(changes.values(), key=lambda e: abs(Decimal(e["pct_change"] or 0)), default=None)
    dl = C.delta("net_sales", tb, tc)
    context = {"metric": {"key": "net_sales", "name": "Net sales"}, "headline": dl,
               "components": C.component_breakdown(tb, tc), "dimension_tops": tops,
               "campaign_spend": {"largest_change": largest}}
    provider = get_provider(mode=mode)
    res = provider.complete(ModelRequest(task="single_pass", role="analyst", system=P.ANALYST_SYSTEM,
                                         instructions=P.SINGLE_PASS_INSTRUCTIONS, context=context,
                                         response_model=S.SinglePassOut))
    out: S.SinglePassOut = res.data  # type: ignore[assignment]
    texts = [out.explanation, out.driver.explanation] + [c.wording for c in out.claims]
    causal = sum(1 for t in texts if CAUSAL_RE.search(t) and not HEDGE_RE.search(t))
    stated = {a.quantity: a.value for c in out.claims for a in c.numeric}
    reported = {"baseline": stated.get("metric_baseline"), "current": stated.get("metric_current")}
    return {"driver": out.driver.label, "headline": reported, "queries": q + 2, "unsupported_causal": causal,
            "completed": True, "cost": str(res.cost), "tokens": res.usage.total,
            "latency_ms": int((time.perf_counter() - t0) * 1000)}


def run_full_graph(case: dict[str, Any], mode: str, owner_id: uuid.UUID) -> dict[str, Any]:
    t0 = time.perf_counter()
    s_ = get_settings()
    tid = case["spec"].tenant_id
    with session_scope() as s:
        inv = Investigation(tenant_id=tid, owner_id=owner_id, metric_key="net_sales", as_of_date=case["spec"].as_of,
                            question="Why did net sales change in the last complete seven days versus the preceding seven?",
                            timezone="Asia/Kolkata", max_cost=s_.max_run_cost, max_source_queries=s_.max_source_queries,
                            model_config_={"mode": mode, "purpose": "evaluation"}, status="running")
        s.add(inv)
        s.flush()
        iid = str(inv.id)
    rt = Runtime(settings=s_, provider=get_provider(mode=mode), auto_continue_incomplete=True, worker_id="eval")
    token = set_runtime(rt)
    try:
        result = advance(iid, str(tid))
    finally:
        reset_runtime(token)
    with session_scope() as s:
        inv2 = s.get(Investigation, uuid.UUID(iid))
        assert inv2 is not None
        inv = inv2
        assert inv is not None
        claims = s.scalars(select(Claim).where(Claim.investigation_id == inv.id)).all()
        headline = next((c for c in claims if c.claim_key == "headline_change" and c.verification_status == "verified"), None)
        stated = {}
        if headline:
            for a in headline.numeric_fields.get("assertions", []):
                stated[a["quantity"]] = a["value"]
        published = [c for c in claims if c.verification_status == "verified"]
        causal = sum(1 for c in published if CAUSAL_RE.search(c.wording) and not HEDGE_RE.search(c.wording))
        completed = inv.status in ("awaiting_review", "completed")
        driver = (inv.primary_driver or {}).get("label", "undetermined")
        out = {"driver": driver, "headline": {"baseline": stated.get("metric_baseline"), "current": stated.get("metric_current")},
               "queries": inv.queries_used, "unsupported_causal": causal, "completed": completed, "cost": str(inv.cost_used),
               "tokens": inv.tokens_used, "latency_ms": int((time.perf_counter() - t0) * 1000),
               "rejected_claims": sum(1 for c in claims if c.verification_status == "rejected"),
               "investigation_id": iid, "status": inv.status}
        if inv.status == "awaiting_review":  # evaluation runs are never published
            inv.status = "cancelled"
            inv.cancel_requested = True
    _ = result
    return out


def _score(system: str, case: dict[str, Any], out: dict[str, Any], ref: dict[str, Any]) -> dict[str, Any]:
    expected = case["ground_truth"]["expected_driver"]
    h = out.get("headline") or {}
    numeric_ok = h.get("baseline") == ref["baseline"] and h.get("current") == ref["current"]
    return {"system": system, "slug": case["slug"], "family": case["family"], "seed": case["seed"],
            "expected_driver": expected, "predicted_driver": out["driver"], "driver_correct": out["driver"] == expected,
            "numeric_correct": numeric_ok, **{k: v for k, v in out.items() if k not in ("headline", "driver")}}


def _summarize(rows: list[dict[str, Any]], systems: list[str], mode: str, suite: str) -> dict[str, Any]:
    by: dict[str, Any] = {}
    for sys_ in systems:
        rs = [r for r in rows if r["system"] == sys_]
        n = len(rs) or 1
        by[sys_] = {
            "scenarios": len(rs),
            "driver_accuracy": round(sum(r["driver_correct"] for r in rs) / n, 3),
            "numeric_accuracy": round(sum(r["numeric_correct"] for r in rs) / n, 3),
            "unsupported_causal_claims": sum(r["unsupported_causal"] for r in rs),
            "completion_rate": round(sum(r["completed"] for r in rs) / n, 3),
            "avg_queries": round(sum(r["queries"] for r in rs) / n, 2),
            "total_cost": str(sum(Decimal(r["cost"]) for r in rs)),
            "avg_latency_ms": int(sum(r["latency_ms"] for r in rs) / n),
            "by_family": {f: round(sum(r["driver_correct"] for r in rs if r["family"] == f) /
                                   max(1, sum(1 for r in rs if r["family"] == f)), 2)
                          for f in sorted({r["family"] for r in rs})},
        }
    return {"suite": suite, "model_mode": mode, "systems": by, "rows": rows,
            "fixture_note": ("Fixture mode: analyst/critic/single-pass behaviour is scripted and deterministic. These "
                             "numbers validate the pipeline and controls, not language-model reasoning.") if mode == "fixture" else
                            "Real-model mode: results depend on the configured model and may vary between runs.",
            "generated_at": datetime.now(UTC).isoformat()}


def run_suite(suite_name: str, systems: list[str], limit: int | None = None, model_mode: str | None = None,
              run_id: uuid.UUID | None = None) -> dict[str, Any]:
    mode = model_mode or get_settings().model_mode
    with session_scope() as s:
        suite = s.scalar(select(EvalSuite).where(EvalSuite.name == suite_name))
        if suite is None:
            raise SystemExit(f"unknown suite {suite_name}; run `python -m app.cli seed-evals` first")
        split = suite.split
        wanted = {x["slug"] for x in suite.scenarios}
    cases = [c for c in eval_cases(split) if c["slug"] in wanted][: limit or None]
    owner = _eval_user()
    rows: list[dict[str, Any]] = []
    for case in cases:
        ref = _reference(case)
        for system in systems:
            try:
                if system == "fixed_dashboard":
                    out = run_fixed_dashboard(case)
                elif system == "single_pass":
                    out = run_single_pass(case, mode)
                else:
                    out = run_full_graph(case, mode, owner)
            except Exception as exc:  # noqa: BLE001 - a failed scenario is a measured outcome
                out = {"driver": "error", "headline": {}, "queries": 0, "unsupported_causal": 0, "completed": False,
                       "cost": "0", "tokens": 0, "latency_ms": 0, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            rows.append(_score(system, case, out, ref))
    summary = _summarize(rows, systems, mode, suite_name)
    if run_id is not None:
        with session_scope() as s:
            r = s.get(EvalRun, run_id)
            if r is not None:
                r.results = rows
                r.summary = {k: v for k, v in summary.items() if k != "rows"}
                r.status = "done"
                r.finished_at = datetime.now(UTC)
    return summary


def run_eval_job(run_id: uuid.UUID) -> None:
    with session_scope() as s:
        r = s.get(EvalRun, run_id)
        if r is None:
            return
        r.status = "running"
        suite = s.get(EvalSuite, r.suite_id)
        assert suite is not None
        name, systems, mode, limit = suite.name, list(r.systems), r.model_mode, (r.summary or {}).get("limit")
    try:
        run_suite(name, systems, limit=limit, model_mode=mode, run_id=run_id)
    except Exception as exc:
        with session_scope() as s:
            r = s.get(EvalRun, run_id)
            if r is not None:
                r.status = "failed"
                r.summary = {"error": str(exc)[:500]}
        raise


def tenants_for(split: str) -> list[uuid.UUID]:
    with session_scope() as s:
        return list(s.scalars(select(Tenant.id).where(Tenant.kind == "eval", Tenant.slug.like("eval-%"))))
