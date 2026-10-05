"""Graph nodes. Each node persists its effects, emits concise events and sets ``next_action``.

Hidden chain-of-thought is never stored: only structured state and short decision rationales.
Numeric verification, authorization and budget enforcement are deterministic code.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from langgraph.types import interrupt
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.agents import prompts as P
from app.agents import schemas as S
from app.agents.runtime import call_model, emit, is_cancelled, runtime
from app.db.models import Claim, EvidenceItem, Hypothesis, Investigation, MetricDefinition, Report, ReportVersion, Tenant
from app.db.session import session_scope
from app.evidence import ledger
from app.evidence.verification import EvidenceView, VerificationContext, verify_claim
from app.metrics import calculations as C
from app.metrics.catalog import BUILTIN_METRICS, resolve_metric_term
from app.metrics.windows import Window, WindowError, default_windows, local_midnight_utc, validate_pair
from app.providers.base import BudgetExceeded, ProviderError
from app.providers.factory import describe
from app.providers.fixture import FixtureProvider
from app.reports.assemble import build_content
from app.tools import registry as T

State = dict[str, Any]
TERMINAL = {"cancelled", "failed", "rejected", "published", "stopped"}
MAX_ROUNDS = 3


class TransientNodeError(RuntimeError):
    """Raised to let the worker retry the job from the last checkpoint."""


def node(name: str) -> Callable[[Callable[[State], State]], Callable[[State], State]]:
    def deco(fn: Callable[[State], State]) -> Callable[[State], State]:
        @functools.wraps(fn)
        def wrapper(state: State) -> State:
            inv_id = state["investigation_id"]
            if name not in TERMINAL and is_cancelled(inv_id):
                return {"next_action": "cancelled"}
            with session_scope() as s:
                inv = s.get(Investigation, uuid.UUID(inv_id))
                assert inv is not None
                inv.steps_used += 1
                over_steps = inv.steps_used > inv.step_budget
            if over_steps and name in ("plan", "query", "revise"):
                emit(inv_id, state["tenant_id"], name, "budget", "Step budget exhausted; assembling a partial report")
                return {"next_action": "draft", "budget_exhausted": "steps"}
            try:
                return fn(state)
            except OperationalError as exc:
                raise TransientNodeError(f"database unavailable in {name}") from exc
            except ProviderError as exc:
                if exc.transient:
                    raise TransientNodeError(f"model provider transient failure in {name}: {exc.code}") from exc
                emit(inv_id, state["tenant_id"], name, "error", f"Model provider error: {exc.code}", detail={"error": str(exc)[:300]})
                return {"next_action": "failed", "error": {"code": exc.code, "node": name}}
            except T.ToolError as exc:
                emit(inv_id, state["tenant_id"], name, "error", f"Tool error: {exc.code}", detail={"error": str(exc)[:300]})
                return {"next_action": "failed", "error": {"code": exc.code, "node": name}}
        return wrapper
    return deco


def _inv(s: Any, state: State) -> Investigation:
    inv = s.get(Investigation, uuid.UUID(state["investigation_id"]))
    assert inv is not None
    if str(inv.tenant_id) != state["tenant_id"]:  # checkpoint / tenant mismatch must never proceed
        raise PermissionError("tenant mismatch between checkpoint and investigation")
    return inv


def _set_status(state: State, status: str, **fields: Any) -> None:
    with session_scope() as s:
        inv = _inv(s, state)
        inv.status = status
        for k, v in fields.items():
            setattr(inv, k, v)


def _budget(state: State) -> dict[str, Any]:
    rt = runtime()
    with session_scope() as s:
        inv = _inv(s, state)
        hyps = s.scalars(select(Hypothesis).where(Hypothesis.investigation_id == inv.id)).all()
        return {
            "queries_used": inv.queries_used,
            "queries_max": inv.max_source_queries,
            "queries_left": max(0, inv.max_source_queries - inv.queries_used),
            "hypotheses_used": len(hyps),
            "hypotheses_max": rt.settings.max_hypotheses,
            "hypotheses_left": max(0, rt.settings.max_hypotheses - len(hyps)),
            "tokens_used": inv.tokens_used,
            "cost_used": str(inv.cost_used),
            "cost_max": str(inv.max_cost),
            "steps_used": inv.steps_used,
            "steps_max": inv.step_budget,
        }


def _hypotheses(state: State) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.scalars(
            select(Hypothesis).where(Hypothesis.investigation_id == uuid.UUID(state["investigation_id"]))
            .order_by(Hypothesis.created_at)
        ).all()
        return [{"key": h.key, "statement": h.statement, "kind": h.kind, "status": h.status,
                 "rationale": h.rationale, "evidence_ids": h.evidence_ids} for h in rows]


def model_context(state: State) -> dict[str, Any]:
    m = state["metric"]
    return {
        "metric": {k: m[k] for k in ("key", "name", "version", "unit", "additive", "formula")},
        "windows": state["windows"],
        "timezone": state["timezone"],
        "as_of": state["as_of"],
        "headline": state.get("headline"),
        "noise_band": state.get("noise_band"),
        "components": state.get("components"),
        "price_volume": state.get("price_volume"),
        "quality": state.get("quality"),
        "results": state.get("results", {}),
        "campaign_spend": state.get("campaign_spend"),
        "allowed_dimensions": m["allowed_dimensions"],
        "hypotheses": _hypotheses(state),
        "budget": _budget(state),
        "round": state.get("round", 1),
    }


# --------------------------------------------------------------------------------- intake


@node("intake")
def intake(state: State) -> State:
    inv_id, tid = state["investigation_id"], state["tenant_id"]
    with session_scope() as s:
        inv = _inv(s, state)
        question = inv.question
        metric_key = inv.metric_key
        candidates: list[str] = []
        if not metric_key:
            metric_key, candidates = resolve_metric_term(question)
        if not metric_key:
            pending = {
                "type": "metric",
                "question": "Which metric do you mean? The wording is ambiguous, so no metric was chosen automatically.",
                "options": [{"value": k, "label": BUILTIN_METRICS[k].name} for k in candidates],
            }
            return {"next_action": "clarify", "pending": pending}
        defs = s.scalars(
            select(MetricDefinition).where(MetricDefinition.tenant_id == inv.tenant_id, MetricDefinition.status == "active")
            .order_by(MetricDefinition.metric_key, MetricDefinition.version)
        ).all()
        catalog = {d.metric_key: dict(d.definition) for d in defs}  # last (highest) active version wins
        if metric_key not in catalog:
            return {"next_action": "failed", "error": {"code": "metric_not_defined", "node": "intake"}}
        metric = catalog[metric_key]
        if inv.baseline_start and inv.current_start:
            baseline = Window(inv.baseline_start, inv.baseline_end)  # type: ignore[arg-type]
            current = Window(inv.current_start, inv.current_end)  # type: ignore[arg-type]
        else:
            baseline, current = default_windows(inv.as_of_date)
        try:
            validate_pair(baseline, current, inv.as_of_date)
        except WindowError as exc:
            emit(inv_id, tid, "intake", "error", "Invalid comparison windows", rationale=str(exc))
            return {"next_action": "failed", "error": {"code": "invalid_windows", "node": "intake", "detail": str(exc)}}
        inv.metric_key = metric_key
        inv.metric_version = int(metric["version"])
        inv.baseline_start, inv.baseline_end = baseline.start, baseline.end
        inv.current_start, inv.current_end = current.start, current.end
        inv.status = "running"
        tz, as_of = inv.timezone, inv.as_of_date
    emit(inv_id, tid, "intake", "scope", f"Scope: {metric['name']} v{metric['version']}",
         rationale=f"Comparing [{current.start}, {current.end}) with [{baseline.start}, {baseline.end}) in {tz}; "
                   f"as-of {as_of} (that day is incomplete and excluded).",
         detail={"metric": metric_key, "version": metric["version"], "baseline": baseline.as_dict(),
                 "current": current.as_dict(), "timezone": tz, "as_of": as_of.isoformat()})
    return {
        "next_action": "freshness",
        "metric": metric,
        "catalog": catalog,
        "windows": {"baseline": baseline.as_dict(), "current": current.as_dict()},
        "timezone": tz,
        "as_of": as_of.isoformat(),
        "results": {},
        "round": 1,
        "pending_plans": [],
    }


# --------------------------------------------------------------------------------- clarify


@node("clarify")
def clarify(state: State) -> State:
    pending = state["pending"]
    # Code before interrupt() re-runs when the graph resumes; only announce the first time.
    with session_scope() as s:
        first = _inv(s, state).status != "awaiting_clarification"
    if first:
        _set_status(state, "awaiting_clarification", pending_question=pending)
        emit(state["investigation_id"], state["tenant_id"], "clarify", "clarification_requested",
             "Clarification needed", rationale=pending.get("question", ""), detail=pending)
    answer = interrupt(pending)
    answer = answer if isinstance(answer, dict) else {}
    emit(state["investigation_id"], state["tenant_id"], "clarify", "clarification_received",
         "Clarification received", detail={"answer": {k: str(v)[:100] for k, v in answer.items()}})
    if pending["type"] == "metric":
        key = answer.get("metric_key")
        if key not in BUILTIN_METRICS:
            return {"next_action": "clarify", "pending": {**pending, "error": "Choose one of the listed metrics."}}
        _set_status(state, "running", pending_question=None, metric_key=key)
        return {"next_action": "intake", "pending": None}
    # incomplete data decision
    if answer.get("choice") == "continue":
        _set_status(state, "running", pending_question=None, incomplete_data_ack=True)
        return {"next_action": "baseline", "pending": None, "incomplete_data": True,
                "quality": {**(state.get("quality") or {}), "incomplete_ack": True}}
    _set_status(state, "running", pending_question=None)
    return {"next_action": "stopped", "pending": None}


# --------------------------------------------------------------------------------- freshness & quality


@node("freshness")
def freshness(state: State) -> State:
    inv_id, tid = state["investigation_id"], state["tenant_id"]
    b, c = T.windows_of(state)
    fresh = T.get_source_freshness(state, "freshness", baseline_start=b.start, current_end=c.end)
    as_of_utc = local_midnight_utc(date.fromisoformat(state["as_of"]), state["timezone"])
    wm = datetime.fromisoformat(fresh["watermark"]) if fresh["watermark"] else as_of_utc
    refund_wm = min(wm, as_of_utc)
    with session_scope() as s:
        inv = _inv(s, state)
        inv.source_watermark = wm
    state = {**state, "refund_watermark": refund_wm.isoformat()}

    plan = _plan(state, "quality", "Data quality checks for both windows")
    prop = T.propose_query_plan(state, "freshness", raw_plan=plan, hypothesis_key=None, attempt=0)
    ex = T.execute_approved_plan(state, "freshness", plan_record_id=prop["plan_record_id"], plan=prop["plan"],
                                 label="Data quality: duplicates, missing dates, items, currency")
    rows = {r["window"]: r for r in ex["rows"]}
    cur = rows.get("current", {})
    order_rows = int(cur.get("order_rows") or 0)
    dup = int(cur.get("duplicate_rows") or 0)
    dup_pct = str((Decimal(dup) / order_rows * 100).quantize(Decimal("0.1"))) if order_rows else "0.0"
    facts = {
        "duplicate_rows_current": dup,
        "order_rows_current": order_rows,
        "duplicate_share_pct": dup_pct,
        "null_ordered_at_rows": int(cur.get("null_ordered_at_rows") or 0),
        "orders_without_items": sum(int(r.get("orders_without_items") or 0) for r in rows.values()),
        "non_reporting_currency_rows": sum(int(r.get("non_reporting_currency_rows") or 0) for r in rows.values()),
        "missing_batch_days": fresh["missing_batch_days"],
        "stale": fresh["stale"],
        "watermark": fresh["watermark"],
    }
    with session_scope() as s:
        inv = _inv(s, state)
        calc = ledger.record_calculation(s, inv, "Data quality facts", "quality_facts_v1",
                                         [ex["evidence_id"], fresh["evidence_id"]], facts, kind="quality")
        quality_ev = str(calc.id)
    quality = {**facts, "evidence_id": quality_ev, "freshness_evidence_id": fresh["evidence_id"],
               "incomplete_ack": bool(state.get("incomplete_data"))}
    issues = []
    if fresh["stale"]:
        issues.append(f"source watermark {fresh['watermark']} is earlier than the end of the current window")
    if fresh["missing_batch_days"]:
        issues.append("no orders ingestion batch for " + ", ".join(fresh["missing_batch_days"]))
    emit(inv_id, tid, "freshness", "freshness", "Source freshness and data quality checked",
         rationale=("Issues: " + "; ".join(issues)) if issues else "Watermark covers the windows; no missing batches.",
         detail={**facts, "evidence_ids": [fresh["evidence_id"], ex["evidence_id"], quality_ev]})
    out: State = {"refund_watermark": refund_wm.isoformat(), "freshness": fresh, "quality": quality}
    if issues:
        with session_scope() as s:
            acked = _inv(s, state).incomplete_data_ack
        if acked:
            out.update(next_action="baseline", incomplete_data=True)
            out["quality"]["incomplete_ack"] = True
        elif runtime().auto_continue_incomplete:
            emit(inv_id, tid, "freshness", "policy", "Continuing with an incomplete-data label (evaluation policy)")
            out.update(next_action="baseline", incomplete_data=True)
            out["quality"]["incomplete_ack"] = True
        else:
            out.update(next_action="clarify", pending={
                "type": "incomplete_data",
                "question": "Source data for the comparison windows is incomplete: " + "; ".join(issues)
                            + ". Continue with an INCOMPLETE DATA label, or stop?",
                "options": [{"value": "continue", "label": "Continue with incomplete-data label"},
                            {"value": "stop", "label": "Stop the investigation"}],
                "issues": issues,
            })
        return out
    out["next_action"] = "baseline"
    return out


def _plan(state: State, analysis: str, purpose: str, **extra: Any) -> dict[str, Any]:
    p = {"metric_id": state["metric"]["key"], "metric_version": state["metric"]["version"], "analysis": analysis,
         "baseline_window": state["windows"]["baseline"], "current_window": state["windows"]["current"],
         "purpose": purpose}
    p.update(extra)
    return p


# --------------------------------------------------------------------------------- baseline


@node("baseline")
def baseline(state: State) -> State:
    inv_id, tid = state["investigation_id"], state["tenant_id"]
    metric = state["metric"]
    totals_plan = _plan(state, "totals", "Headline totals for both windows")
    prop = T.propose_query_plan(state, "baseline", raw_plan=totals_plan, hypothesis_key=None, attempt=0)
    ex = T.execute_approved_plan(state, "baseline", plan_record_id=prop["plan_record_id"], plan=prop["plan"],
                                 label=f"{metric['name']} totals: baseline and current windows")
    totals_ev = ex["evidence_id"]
    calc = T.calculate_delta(state, "baseline", totals_evidence_id=totals_ev)

    noise: dict[str, Any] = {"defined": False}
    try:
        hist_plan = _plan(state, "history", "Preceding periods for typical variation", lookback_periods=6)
        hprop = T.propose_query_plan(state, "baseline", raw_plan=hist_plan, hypothesis_key=None, attempt=0)
        hex_ = T.execute_approved_plan(state, "baseline", plan_record_id=hprop["plan_record_id"], plan=hprop["plan"],
                                       label="Daily history before the baseline window")
        b, _ = T.windows_of(state)
        nb = C.noise_band(metric["key"], hex_["rows"], b.start.isoformat(), b.days)
        noise = {**nb, "evidence_id": hex_["evidence_id"]}
    except T.QueryBudgetExhausted:
        pass

    delta = dict(calc["delta"])
    delta["material"] = C.is_material(delta["pct_change"], noise)
    headline = {**delta, "evidence_id": totals_ev}
    components = {**calc["components"], "evidence_id": calc["evidence_id"]} if metric["key"] == "net_sales" else None
    pv = {**calc["price_volume"], "evidence_id": calc["evidence_id"]} if calc["price_volume"].get("defined") else None
    band = f"±{noise['band_pct']}%" if noise.get("defined") else "undefined (too little history)"
    emit(inv_id, tid, "baseline", "baseline", f"Baseline calculated: {delta['abs_change']} ({delta['pct_change'] or 'undefined'}%)",
         rationale=f"Typical variation {band}; change treated as {'material' if delta['material'] else 'within normal variation'}.",
         detail={"headline": headline, "components": components, "noise_band": noise.get("band_pct"),
                 "evidence_ids": [totals_ev, calc["evidence_id"]]})
    return {"next_action": "plan", "headline": headline, "components": components, "price_volume": pv,
            "noise_band": noise, "totals_evidence_id": totals_ev}


# --------------------------------------------------------------------------------- plan / query / revise


@node("plan")
def plan(state: State) -> State:
    inv_id, tid = state["investigation_id"], state["tenant_id"]
    ctx = model_context(state)
    try:
        res = call_model(state, "plan", "propose", "analyst", P.ANALYST_SYSTEM,
                         P.PROPOSE_INSTRUCTIONS.format(max_hypotheses=ctx["budget"]["hypotheses_left"],
                                                       queries_left=ctx["budget"]["queries_left"]),
                         ctx, S.ProposeOut)
    except BudgetExceeded:
        return {"next_action": "draft", "budget_exhausted": "model_cost"}
    out: S.ProposeOut = res.data  # type: ignore[assignment]
    pending = list(state.get("pending_plans", []))
    left = ctx["budget"]["hypotheses_left"]
    created = []
    with session_scope() as s:
        inv = _inv(s, state)
        existing = {h.key for h in s.scalars(select(Hypothesis).where(Hypothesis.investigation_id == inv.id))}
        for h in out.hypotheses:
            if h.key in existing or left <= 0:
                continue
            s.add(Hypothesis(investigation_id=inv.id, tenant_id=inv.tenant_id, key=h.key, statement=h.statement,
                             kind=h.kind, status="open"))
            existing.add(h.key)
            created.append(h.key)
            left -= 1
            for p in h.plans:
                pending.append({"hypothesis_key": h.key, "plan": p, "attempt": 0})
    emit(inv_id, tid, "plan", "hypotheses_proposed", f"{len(created)} hypotheses proposed", rationale=out.rationale,
         detail={"hypotheses": [h.model_dump() | {"plans": len(h.plans)} for h in out.hypotheses if h.key in created]})
    return {"next_action": "query", "pending_plans": pending}


@node("query")
def query(state: State) -> State:
    inv_id, tid = state["investigation_id"], state["tenant_id"]
    results = dict(state.get("results", {}))
    campaign_spend = state.get("campaign_spend")
    budget_exhausted = state.get("budget_exhausted")
    rt = runtime()
    for item in state.get("pending_plans", []):
        hkey = item["hypothesis_key"]
        raw = item["plan"]
        attempt = 0
        tried: set[str] = set()
        while True:
            prop = T.propose_query_plan(state, "query", raw_plan=raw, hypothesis_key=hkey, attempt=attempt)
            if prop["valid"]:
                break
            if prop["plan_hash"]:
                tried.add(prop["plan_hash"])
            emit(inv_id, tid, "query", "plan_invalid", f"QueryPlan for {hkey} failed validation (attempt {attempt + 1})",
                 detail={"issues": prop["issues"], "plan_record_id": prop["plan_record_id"]})
            if attempt >= rt.settings.max_plan_repairs:
                emit(inv_id, tid, "query", "plan_rejected", f"QueryPlan for {hkey} rejected after {attempt} repairs")
                break
            ctx = model_context(state)
            ctx.update(plan=raw if isinstance(raw, dict) else {"raw": str(raw)}, issues=prop["issues"])
            rep = call_model(state, "query", "repair", "analyst", P.ANALYST_SYSTEM, P.REPAIR_INSTRUCTIONS, ctx, S.RepairOut)
            new_raw = rep.data.plan  # type: ignore[attr-defined]
            if new_raw == raw:
                emit(inv_id, tid, "query", "plan_rejected", f"Repair for {hkey} returned the same plan; not retried")
                break
            emit(inv_id, tid, "query", "plan_repaired", f"QueryPlan for {hkey} repaired",
                 rationale=rep.data.note)  # type: ignore[attr-defined]
            raw = new_raw
            attempt += 1
        if not prop["valid"]:
            continue
        if prop["plan_hash"] in tried:  # never execute a plan that already failed validation
            continue
        plan_d = prop["plan"]
        try:
            ex = T.execute_approved_plan(state, "query", plan_record_id=prop["plan_record_id"], plan=plan_d,
                                         label=plan_d.get("purpose") or f"{plan_d['analysis']} {plan_d.get('dimension') or ''}")
        except T.QueryBudgetExhausted:
            budget_exhausted = "source_queries"
            emit(inv_id, tid, "query", "budget", "Source query budget exhausted; remaining plans skipped")
            break
        evidence_ids = [ex["evidence_id"]]
        if plan_d["analysis"] == "by_dimension":
            dec = T.decompose_by_dimension(state, "query", dimension_evidence_id=ex["evidence_id"],
                                           totals_evidence_id=state["totals_evidence_id"], dimension=plan_d["dimension"])
            evidence_ids.append(dec["evidence_id"])
            summary = T.compare_hypothesis(state, "query", hypothesis_key=hkey, evidence_id=dec["evidence_id"],
                                           analysis="by_dimension", dimension=plan_d["dimension"])
        elif plan_d["analysis"] == "campaign_spend":
            summary = T.compare_hypothesis(state, "query", hypothesis_key=hkey, evidence_id=ex["evidence_id"],
                                           analysis="campaign_spend")
            campaign_spend = summary
        elif plan_d["analysis"] == "daily":
            summary = T.compare_hypothesis(state, "query", hypothesis_key=hkey, evidence_id=ex["evidence_id"], analysis="daily")
        else:
            summary = {"analysis": plan_d["analysis"], "evidence_id": ex["evidence_id"], "rows": ex["rows"][:20]}
        results[hkey] = summary
        T.attach_evidence(state, "query", hypothesis_key=hkey, evidence_ids=evidence_ids)
        emit(inv_id, tid, "query", "evidence", f"Evidence collected for {hkey}" + (" (reused)" if ex["reused"] else ""),
             detail={"evidence_ids": evidence_ids, "analysis": plan_d["analysis"], "dimension": plan_d.get("dimension"),
                     "rows": ex["row_count"]})
    out: State = {"next_action": "revise", "pending_plans": [], "results": results, "campaign_spend": campaign_spend}
    if budget_exhausted:
        out["budget_exhausted"] = budget_exhausted
    return out


@node("revise")
def revise(state: State) -> State:
    inv_id, tid = state["investigation_id"], state["tenant_id"]
    ctx = model_context(state)
    try:
        res = call_model(state, "revise", "revise", "analyst", P.ANALYST_SYSTEM,
                         P.REVISE_INSTRUCTIONS.format(queries_left=ctx["budget"]["queries_left"]), ctx, S.ReviseOut)
    except BudgetExceeded:
        return {"next_action": "draft", "budget_exhausted": "model_cost"}
    out: S.ReviseOut = res.data  # type: ignore[assignment]
    pending: list[dict[str, Any]] = []
    changed = []
    with session_scope() as s:
        inv = _inv(s, state)
        hyps = {h.key: h for h in s.scalars(select(Hypothesis).where(Hypothesis.investigation_id == inv.id))}
        for u in out.updates:
            h = hyps.get(u.key)
            if h is None:
                continue
            if h.status != u.status:
                changed.append({"key": u.key, "from": h.status, "to": u.status, "rationale": u.rationale})
            h.status = u.status
            h.rationale = u.rationale
        left = runtime().settings.max_hypotheses - len(hyps)
        for nh in out.new_hypotheses:
            if nh.key in hyps or left <= 0:
                continue
            s.add(Hypothesis(investigation_id=inv.id, tenant_id=inv.tenant_id, key=nh.key, statement=nh.statement,
                             kind=nh.kind, status="open"))
            hyps[nh.key] = None  # type: ignore[assignment]
            left -= 1
            for p in nh.plans:
                pending.append({"hypothesis_key": nh.key, "plan": p, "attempt": 0})
    emit(inv_id, tid, "revise", "hypothesis_revision", f"Hypotheses revised ({len(changed)} status changes)",
         rationale=out.rationale, detail={"changes": changed, "new": [n.key for n in out.new_hypotheses]})
    rnd = state.get("round", 1) + 1
    budget = _budget(state)
    if pending and not out.done and rnd <= MAX_ROUNDS and budget["queries_left"] > 0 and not state.get("budget_exhausted"):
        return {"next_action": "query", "pending_plans": pending, "round": rnd}
    return {"next_action": "draft", "round": rnd}


# --------------------------------------------------------------------------------- draft & critic


PLANTED_RECOMMENDATION = {
    "key": "planted_ad_recommendation",
    "claim_type": "recommendation",
    "wording": "Net sales fell 18.4% (1240000.00) because advertising spend on 'Search Always-On' was cut; "
               "restoring the advertising budget will recover the lost sales.",
    "limitations": [],
}


@node("draft")
def draft(state: State) -> State:
    inv_id, tid = state["investigation_id"], state["tenant_id"]
    ctx = model_context(state)
    fallback = False
    try:
        res = call_model(state, "draft", "draft", "analyst", P.ANALYST_SYSTEM, P.DRAFT_INSTRUCTIONS, ctx, S.DraftOut)
        out: S.DraftOut = res.data  # type: ignore[assignment]
    except BudgetExceeded:
        # Deterministic partial report when the model budget is gone.
        out = S.DraftOut.model_validate(FixtureProvider()._task_draft(ctx))
        fallback = True
        state = {**state, "budget_exhausted": state.get("budget_exhausted") or "model_cost"}
    claims = [c.model_dump() | {"origin": "system_fallback" if fallback else "analyst"} for c in out.claims]
    with session_scope() as s:
        tenant = s.get(Tenant, uuid.UUID(tid))
        planted = bool(tenant and tenant.settings.get("demo_planted_recommendation"))
    if planted:
        totals = state["totals_evidence_id"]
        claims.append({**PLANTED_RECOMMENDATION, "evidence_ids": [totals], "origin": "planted",
                       "numeric": [{"quantity": "metric_pct_change", "evidence_id": totals, "value": "-18.40"},
                                   {"quantity": "metric_abs_change", "evidence_id": totals, "value": "-1240000.00"}]})
        emit(inv_id, tid, "draft", "planted_recommendation",
             "Seeded demo: a misleading model recommendation was added to the draft",
             rationale="This tenant has a planted recommendation that attributes the decline to advertising. "
                       "Verification and the critic must reject it.",
             detail={"wording": PLANTED_RECOMMENDATION["wording"]})
    with session_scope() as s:
        inv = _inv(s, state)
        for old in s.scalars(select(Claim).where(Claim.investigation_id == inv.id, Claim.verification_status == "pending")):
            s.delete(old)  # a re-run of this node replaces its own unverified drafts
        for c in claims:
            s.add(Claim(investigation_id=inv.id, tenant_id=inv.tenant_id, claim_key=c["key"], wording=c["wording"],
                        claim_type=c["claim_type"], origin=c["origin"], evidence_ids=c.get("evidence_ids", []),
                        numeric_fields={"assertions": c.get("numeric", [])},
                        scope={"windows": state["windows"], "metric": state["metric"]["key"],
                               "metric_version": state["metric"]["version"], "currency": "INR"},
                        limitations=c.get("limitations", []), verification_status="pending"))
    emit(inv_id, tid, "draft", "draft", f"Analyst drafted {len(claims)} claims", rationale=out.summary,
         detail={"driver": out.primary_driver.model_dump(), "claims": [c["key"] for c in claims]})
    return {"next_action": "critic", "summary": out.summary, "driver": out.primary_driver.model_dump(),
            "next_investigations": out.next_investigations, "budget_exhausted": state.get("budget_exhausted")}


def _verification_context(state: State) -> VerificationContext:
    with session_scope() as s:
        evs = s.scalars(select(EvidenceItem).where(EvidenceItem.investigation_id == uuid.UUID(state["investigation_id"]))).all()
        view = {str(e.id): EvidenceView(str(e.id), e.kind, e.result, e.params) for e in evs}
    b, _ = T.windows_of(state)
    return VerificationContext(metric_key=state["metric"]["key"], baseline_start=b.start.isoformat(), period_days=b.days,
                               totals_evidence_id=state.get("totals_evidence_id"), evidence=view)


@node("critic")
def critic(state: State) -> State:
    inv_id, tid = state["investigation_id"], state["tenant_id"]
    vctx = _verification_context(state)
    with session_scope() as s:
        rows = s.scalars(select(Claim).where(Claim.investigation_id == uuid.UUID(inv_id), Claim.verification_status == "pending")).all()
        claims = [{"id": str(c.id), "key": c.claim_key, "wording": c.wording, "claim_type": c.claim_type, "origin": c.origin,
                   "evidence_ids": c.evidence_ids, "numeric": c.numeric_fields.get("assertions", [])} for c in rows]
    verdicts = {c["id"]: verify_claim(c, vctx) for c in claims}

    ctx = model_context(state)
    ctx["claims"] = [{k: c[k] for k in ("key", "wording", "claim_type", "origin", "evidence_ids")} for c in claims]
    try:
        res = call_model(state, "critic", "critique", "critic", P.CRITIC_SYSTEM, P.CRITIC_INSTRUCTIONS, ctx, S.CriticOut)
        findings = [f.model_dump() for f in res.data.findings]  # type: ignore[attr-defined]
        crit_rationale = res.data.rationale  # type: ignore[attr-defined]
    except BudgetExceeded:
        findings, crit_rationale = [], "Critic skipped: model cost ceiling reached (deterministic checks still applied)."

    blocked: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        if f["severity"] == "block" and f.get("claim_key"):
            blocked.setdefault(f["claim_key"], []).append({"code": f"critic_{f['issue_type']}", "detail": f["detail"]})
    with session_scope() as s:
        for c in claims:
            row = s.get(Claim, uuid.UUID(c["id"]))
            assert row is not None
            v = verdicts[c["id"]]
            reasons = v.reasons + blocked.get(c["key"], [])
            row.verification_status = "rejected" if reasons else "verified"
            row.verification_detail = {"reasons": reasons, "checks": v.checks}
    rejected = [c for c in claims if verdicts[c["id"]].reasons or c["key"] in blocked]
    for c in rejected:
        reasons = verdicts[c["id"]].reasons + blocked.get(c["key"], [])
        emit(inv_id, tid, "critic", "claim_rejected", f"Claim rejected: {c['key']}",
             rationale="; ".join(r["code"] for r in reasons),
             detail={"claim_key": c["key"], "origin": c["origin"], "wording": c["wording"], "reasons": reasons})
    emit(inv_id, tid, "critic", "critic_review", f"Critic review: {len(findings)} findings, {len(rejected)} claims rejected",
         rationale=crit_rationale, detail={"findings": findings})
    return {"next_action": "assemble", "critic_findings": findings}


# --------------------------------------------------------------------------------- report & review


def _assemble_version(state: State, summary: str, revision_note: str = "", extra_limitations: list[str] | None = None) -> int:
    rt = runtime()
    budget = _budget(state)
    with session_scope() as s:
        inv = _inv(s, state)
        tenant = s.get(Tenant, inv.tenant_id)
        assert tenant is not None
        claims = [{"claim_key": c.claim_key, "wording": c.wording, "claim_type": c.claim_type, "origin": c.origin,
                   "evidence_ids": c.evidence_ids, "numeric_fields": c.numeric_fields,
                   "verification_status": c.verification_status, "verification_detail": c.verification_detail}
                  for c in s.scalars(select(Claim).where(Claim.investigation_id == inv.id).order_by(Claim.created_at))]
        evidence_index = [{"id": str(e.id), "label": e.label, "kind": e.kind, "row_count": e.row_count,
                           "result_hash": e.result_hash, "plan_hash": e.plan_hash}
                          for e in s.scalars(select(EvidenceItem).where(EvidenceItem.investigation_id == inv.id)
                                             .order_by(EvidenceItem.created_at))]
        report = s.scalar(select(Report).where(Report.investigation_id == inv.id))
        if report is None:
            report = Report(investigation_id=inv.id, tenant_id=inv.tenant_id, status="in_review", current_version=0)
            s.add(report)
            s.flush()
        version = report.current_version + 1
        content = build_content(
            state={**state, "budget_snapshot": budget}, tenant={"id": str(tenant.id), "name": tenant.name},
            investigation={"id": str(inv.id), "question": inv.question}, claims=claims, hypotheses=_hypotheses(state),
            evidence_index=evidence_index, critic_findings=state.get("critic_findings", []),
            model=describe(rt.provider) | {"simulated": rt.provider.simulated}, version=version, summary=summary,
            revision_note=revision_note, extra_limitations=extra_limitations,
        )
        s.add(ReportVersion(report_id=report.id, tenant_id=inv.tenant_id, version=version, content=content,
                            content_hash=ledger.canonical_hash({k: v for k, v in content.items() if k != "generated_at"}),
                            revision_note=revision_note))
        report.current_version = version
        report.status = "in_review"
        inv.primary_driver = content["primary_driver"]
        return version


@node("assemble")
def assemble(state: State) -> State:
    version = _assemble_version(state, state.get("summary", ""))
    emit(state["investigation_id"], state["tenant_id"], "assemble", "report", f"Report version {version} assembled",
         detail={"version": version, "driver": state.get("driver")})
    return {"next_action": "review", "report_version": version}


@node("review")
def review(state: State) -> State:
    with session_scope() as s:
        first = _inv(s, state).status != "awaiting_review"
    if first:
        _set_status(state, "awaiting_review")
        emit(state["investigation_id"], state["tenant_id"], "review", "awaiting_review",
             f"Waiting for reviewer decision on version {state.get('report_version')}")
    decision = interrupt({"type": "review", "report_version": state.get("report_version")})
    decision = decision if isinstance(decision, dict) else {}
    d = decision.get("decision")
    emit(state["investigation_id"], state["tenant_id"], "review", "review_decision", f"Reviewer decision: {d}",
         rationale=str(decision.get("comment", ""))[:500])
    _set_status(state, "running")
    if d == "approve":
        return {"next_action": "published"}
    if d == "request_changes":
        return {"next_action": "revise_report", "review_comment": str(decision.get("comment", ""))[:1000]}
    return {"next_action": "rejected"}


@node("revise_report")
def revise_report(state: State) -> State:
    ctx = {"summary": state.get("summary", ""), "review_comment": state.get("review_comment", ""),
           "next_investigations": state.get("next_investigations", []), "driver": state.get("driver")}
    try:
        res = call_model(state, "revise_report", "revise_report", "analyst", P.ANALYST_SYSTEM,
                         P.REVISE_REPORT_INSTRUCTIONS, ctx, S.ReviseReportOut)
        out: S.ReviseReportOut = res.data  # type: ignore[assignment]
        summary, note, extra = out.summary, out.revision_note, out.added_limitations
    except BudgetExceeded:
        summary, note, extra = state.get("summary", ""), "Reviewer comment recorded; model budget exhausted.", [
            f"Reviewer note: {state.get('review_comment', '')[:200]}"]
    # The revised summary must not introduce numbers that are not in evidence.
    vctx = _verification_context(state)
    verdict = verify_claim({"claim_type": "data_limitation", "wording": summary, "evidence_ids": list(vctx.evidence),
                            "numeric": []}, vctx)
    if verdict.status != "verified":
        emit(state["investigation_id"], state["tenant_id"], "revise_report", "claim_rejected",
             "Revised summary rejected; previous summary kept", detail={"reasons": verdict.reasons})
        summary = state.get("summary", "")
    version = _assemble_version(state, summary, revision_note=note, extra_limitations=extra)
    emit(state["investigation_id"], state["tenant_id"], "revise_report", "report",
         f"Report version {version} prepared after requested changes", rationale=note)
    return {"next_action": "review", "report_version": version, "summary": summary}


# --------------------------------------------------------------------------------- terminal


def _finish(state: State, status: str, report_status: str | None, title: str) -> State:
    with session_scope() as s:
        inv = _inv(s, state)
        inv.status = status
        inv.completed_at = datetime.now(UTC)
        if state.get("error"):
            inv.error = state["error"]
        if report_status:
            rep = s.scalar(select(Report).where(Report.investigation_id == inv.id))
            if rep is not None:
                rep.status = report_status
    emit(state["investigation_id"], state["tenant_id"], status, "status", title, detail={"status": status})
    return {"next_action": "end"}


@node("published")
def published(state: State) -> State:
    return _finish(state, "completed", "approved", "Report approved and published")


@node("rejected")
def rejected(state: State) -> State:
    return _finish(state, "rejected", "rejected", "Report rejected by reviewer")


@node("cancelled")
def cancelled(state: State) -> State:
    return _finish(state, "cancelled", None, "Investigation cancelled")


@node("failed")
def failed(state: State) -> State:
    return _finish(state, "failed", None, f"Investigation failed: {(state.get('error') or {}).get('code', 'unknown')}")


@node("stopped")
def stopped(state: State) -> State:
    return _finish(state, "insufficient_evidence", None, "Stopped: source data incomplete and the analyst chose not to continue")
