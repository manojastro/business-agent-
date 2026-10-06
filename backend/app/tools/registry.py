"""Typed tools used by the investigation graph.

Each tool validates its input with a Pydantic schema, returns structured data, has a time and
output-size bound, and writes an auditable ``tool_call`` event. Tools never accept SQL.
"""

from __future__ import annotations

import functools
import json
import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.agents.runtime import emit
from app.db.analytics import SourceAccessError, run_source_query
from app.db.models import EvidenceItem, Hypothesis, Investigation, MetricDefinition, QueryPlanRecord, QueryRun
from app.db.session import session_scope
from app.evidence import ledger
from app.evidence.verification import spend_changes
from app.metrics import calculations as C
from app.metrics.windows import Window, local_midnight_utc
from app.tools.query_plan import (
    CompiledQuery,
    PlanIssue,
    QueryPlan,
    ValidationContext,
    compile_plan,
    params_for_evidence,
    parse_plan,
    validate_plan,
)

T = TypeVar("T")


class ToolError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class QueryBudgetExhausted(ToolError):
    def __init__(self) -> None:
        super().__init__("query_budget_exhausted", "source query budget exhausted")


def tool(name: str, input_model: type[BaseModel], timeout_s: float = 15.0, max_output_bytes: int = 300_000) -> Callable:
    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(state: dict[str, Any], node: str, **kwargs: Any) -> T:
            inp = input_model.model_validate(kwargs)
            started = time.perf_counter()
            ok = False
            summary: dict[str, Any] = {}
            try:
                out = fn(state, inp)
                ok = True
                summary = _summarize(out)
                size = len(json.dumps(summary, default=str))
                if size > max_output_bytes:
                    raise ToolError("output_too_large", f"{name} output {size} bytes exceeds {max_output_bytes}")
                return out
            except ToolError as exc:
                summary = {"error": exc.code, "message": str(exc)[:300]}
                raise
            finally:
                elapsed = (time.perf_counter() - started) * 1000
                emit(
                    state["investigation_id"], state["tenant_id"], node, "tool_call", f"Tool {name}",
                    detail={"tool": name, "input": _redact(inp.model_dump(mode="json")), "ok": ok,
                            "duration_ms": int(elapsed), "timed_out": elapsed > timeout_s * 1000, "output": summary},
                )
        wrapper.tool_name = name  # type: ignore[attr-defined]
        wrapper.input_model = input_model  # type: ignore[attr-defined]
        return wrapper
    return deco


def _summarize(out: Any) -> dict[str, Any]:
    if isinstance(out, dict):
        keep = {}
        for k, v in out.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                keep[k] = v
            elif isinstance(v, list):
                keep[k] = f"<{len(v)} items>"
            elif isinstance(v, dict):
                keep[k] = f"<{len(v)} keys>"
        return keep
    return {"type": type(out).__name__}


def _redact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: ("<plan>" if k == "plan" else v) for k, v in d.items()}


# ---------------------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------------------


def windows_of(state: dict[str, Any]) -> tuple[Window, Window]:
    return Window.from_dict(state["windows"]["baseline"]), Window.from_dict(state["windows"]["current"])


def validation_context(state: dict[str, Any]) -> ValidationContext:
    b, c = windows_of(state)
    return ValidationContext(
        tenant_id=uuid.UUID(state["tenant_id"]),
        metric_definitions=state["catalog"],
        baseline=b,
        current=c,
        as_of=date.fromisoformat(state["as_of"]),
        timezone=state["timezone"],
        refund_watermark=datetime.fromisoformat(state["refund_watermark"]),
    )


def _charge_query(inv: Investigation, plan_hash: str | None = None) -> None:
    """Charge one source query. A retry of an interrupted operation (same operation id) is not charged twice."""
    if plan_hash is not None:
        from sqlalchemy.orm import object_session

        s = object_session(inv)
        if s is not None and s.scalar(select(QueryRun.id).where(QueryRun.operation_id == ledger.operation_id(inv.id, plan_hash))):
            return
    if inv.queries_used >= inv.max_source_queries:
        raise QueryBudgetExhausted()
    inv.queries_used += 1


# ---------------------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------------------


class MetricDefinitionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric_key: str = Field(max_length=64)


@tool("get_metric_definition", MetricDefinitionIn)
def get_metric_definition(state: dict[str, Any], inp: MetricDefinitionIn) -> dict[str, Any]:
    with session_scope() as s:
        row = s.scalar(
            select(MetricDefinition)
            .where(
                MetricDefinition.tenant_id == uuid.UUID(state["tenant_id"]),
                MetricDefinition.metric_key == inp.metric_key,
                MetricDefinition.status == "active",
            )
            .order_by(MetricDefinition.version.desc())
        )
        if row is None:
            raise ToolError("unknown_metric", f"metric {inp.metric_key} is not defined for this tenant")
        return dict(row.definition)


class FreshnessIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline_start: date
    current_end: date


FRESHNESS_SQL = (
    "SELECT source_table, covers_date, row_count, watermark FROM semantic.ingestion_status "
    "WHERE tenant_id = :tenant_id AND covers_date >= :start ORDER BY source_table, covers_date"
)


@tool("get_source_freshness", FreshnessIn)
def get_source_freshness(state: dict[str, Any], inp: FreshnessIn) -> dict[str, Any]:
    params = {"tenant_id": state["tenant_id"], "start": inp.baseline_start}
    plan_hash = ledger.canonical_hash({"template": "freshness_v1", "start": inp.baseline_start.isoformat(),
                                       "end": inp.current_end.isoformat()})
    with session_scope() as s:
        inv = s.get(Investigation, uuid.UUID(state["investigation_id"]))
        assert inv is not None
        existing = ledger.find_completed_run(s, inv, plan_hash)
        if existing is None:
            _charge_query(inv, plan_hash)
            compiled = CompiledQuery(statement=text(FRESHNESS_SQL), params=params, sql=FRESHNESS_SQL,  # type: ignore[arg-type]
                                     plan_hash=plan_hash, shape="freshness")
            rec = QueryPlanRecord(investigation_id=inv.id, tenant_id=inv.tenant_id, hypothesis_key=None,
                                  plan={"template": "freshness_v1", **params_for_evidence(params)},
                                  plan_hash=plan_hash, status="valid")
            s.add(rec)
            s.flush()
            run = ledger.start_run(s, inv, rec, compiled)
            s.commit()
            try:
                res = run_source_query(inv.tenant_id, FRESHNESS_SQL, params)
            except (SourceAccessError, OperationalError) as exc:
                ledger.finish_run_failed(s, run, str(exc))
                raise
            ev = ledger.finish_run_ok(s, inv, run, compiled, res, "Source freshness: ingestion batches", kind="freshness")
        else:
            ev = existing
        rows = ev.result["rows"]
        ev_id = str(ev.id)

    tz = state["timezone"]
    current_end_utc = local_midnight_utc(inp.current_end, tz)
    order_days = {r["covers_date"][:10] for r in rows if r["source_table"] == "orders"}
    expected = [(inp.baseline_start + timedelta(days=i)).isoformat()
                for i in range((inp.current_end - inp.baseline_start).days)]
    missing = [d for d in expected if d not in order_days]
    marks = [datetime.fromisoformat(r["watermark"]) for r in rows if r["source_table"] == "orders"]
    watermark = max(marks) if marks else None
    stale = watermark is None or watermark < current_end_utc
    return {
        "evidence_id": ev_id,
        "watermark": watermark.isoformat() if watermark else None,
        "required_watermark": current_end_utc.isoformat(),
        "stale": stale,
        "missing_batch_days": missing,
        "batches_checked": len(rows),
    }


class ProposeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw_plan: dict[str, Any] | str
    hypothesis_key: str | None = None
    attempt: int = Field(ge=0, le=2)


@tool("propose_query_plan", ProposeIn)
def propose_query_plan(state: dict[str, Any], inp: ProposeIn) -> dict[str, Any]:
    plan, issues = parse_plan(inp.raw_plan)
    if plan is not None:
        issues = validate_plan(plan, validation_context(state))
    raw_dict = inp.raw_plan if isinstance(inp.raw_plan, dict) else {"raw": str(inp.raw_plan)[:2000]}
    with session_scope() as s:
        rec = QueryPlanRecord(
            investigation_id=uuid.UUID(state["investigation_id"]),
            tenant_id=uuid.UUID(state["tenant_id"]),
            hypothesis_key=inp.hypothesis_key,
            plan=raw_dict,
            plan_hash=plan.plan_hash() if plan else None,
            status="invalid" if issues else "valid",
            validation_errors=[i.as_dict() for i in issues],
            attempt=inp.attempt,
        )
        s.add(rec)
        s.flush()
        rec_id = str(rec.id)
    return {
        "plan_record_id": rec_id,
        "valid": not issues,
        "plan": plan.model_dump(mode="json") if plan else None,
        "plan_hash": plan.plan_hash() if plan else None,
        "issues": [i.as_dict() for i in issues],
    }


class ExecuteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_record_id: str
    plan: dict[str, Any]
    label: str = Field(max_length=300)


@tool("execute_approved_plan", ExecuteIn, timeout_s=30)
def execute_approved_plan(state: dict[str, Any], inp: ExecuteIn) -> dict[str, Any]:
    plan = QueryPlan.model_validate(inp.plan)
    ctx = validation_context(state)
    issues = validate_plan(plan, ctx)  # re-validate: only approved plans execute
    if issues:
        raise ToolError("plan_not_approved", "; ".join(i.code for i in issues))
    compiled = compile_plan(plan, ctx)
    with session_scope() as s:
        inv = s.get(Investigation, uuid.UUID(state["investigation_id"]))
        assert inv is not None
        rec = s.get(QueryPlanRecord, uuid.UUID(inp.plan_record_id))
        if rec is None or rec.investigation_id != inv.id or rec.status != "valid":
            raise ToolError("plan_not_approved", "plan record is not an approved plan of this investigation")
        existing = ledger.find_completed_run(s, inv, compiled.plan_hash)
        if existing is not None:
            return {"evidence_id": str(existing.id), "reused": True, "row_count": existing.row_count,
                    "shape": existing.result.get("shape"), "rows": existing.result["rows"]}
        _charge_query(inv, compiled.plan_hash)
        run = ledger.start_run(s, inv, rec, compiled)
        s.commit()
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                res = run_source_query(inv.tenant_id, compiled.statement, compiled.params)
                break
            except OperationalError as exc:  # transient connectivity: bounded backoff
                last_exc = exc
                time.sleep(0.2 * 2**attempt)
            except SourceAccessError as exc:
                ledger.finish_run_failed(s, run, f"{exc.code}: {exc}")
                s.commit()
                raise ToolError(exc.code, str(exc)) from exc
        else:
            ledger.finish_run_failed(s, run, f"transient failure: {type(last_exc).__name__}")
            s.commit()
            raise ToolError("source_unavailable", "source query failed after retries")
        ev = ledger.finish_run_ok(s, inv, run, compiled, res, inp.label)
        return {"evidence_id": str(ev.id), "reused": False, "row_count": ev.row_count,
                "shape": compiled.shape, "rows": res.rows}


class DeltaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    totals_evidence_id: str


@tool("calculate_delta", DeltaIn)
def calculate_delta(state: dict[str, Any], inp: DeltaIn) -> dict[str, Any]:
    with session_scope() as s:
        inv = s.get(Investigation, uuid.UUID(state["investigation_id"]))
        ev = s.get(EvidenceItem, uuid.UUID(inp.totals_evidence_id))
        if inv is None or ev is None or ev.investigation_id != inv.id:
            raise ToolError("evidence_not_found", "totals evidence not found")
        w = {r["window"]: C.Aggregates.from_row(r) for r in ev.result["rows"]}
        b, c = w.get("baseline", C.Aggregates()), w.get("current", C.Aggregates())
        out = {
            "delta": C.delta(state["metric"]["key"], b, c),
            "components": C.component_breakdown(b, c),
            "price_volume": C.price_volume(b, c),
            "baseline_aggregates": b.to_json(),
            "current_aggregates": c.to_json(),
        }
        calc = ledger.record_calculation(s, inv, "Headline delta, components and price/volume", "calculate_delta_v1",
                                         [inp.totals_evidence_id], out)
        return {"evidence_id": str(calc.id), **out}


class DecomposeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimension_evidence_id: str
    totals_evidence_id: str
    dimension: str


@tool("decompose_by_dimension", DecomposeIn)
def decompose_by_dimension(state: dict[str, Any], inp: DecomposeIn) -> dict[str, Any]:
    metric = state["metric"]
    with session_scope() as s:
        inv = s.get(Investigation, uuid.UUID(state["investigation_id"]))
        dim_ev = s.get(EvidenceItem, uuid.UUID(inp.dimension_evidence_id))
        tot_ev = s.get(EvidenceItem, uuid.UUID(inp.totals_evidence_id))
        if inv is None or dim_ev is None or tot_ev is None or dim_ev.investigation_id != inv.id:
            raise ToolError("evidence_not_found", "evidence not found")
        bseg: dict[str, C.Aggregates] = {}
        cseg: dict[str, C.Aggregates] = {}
        for r in dim_ev.result["rows"]:
            (bseg if r["window"] == "baseline" else cseg)[str(r["segment"])] = C.Aggregates.from_row(r)
        filtered = any(k.startswith("filter_") for k in dim_ev.params)
        if filtered:
            tb = sum(bseg.values(), C.Aggregates())
            tc = sum(cseg.values(), C.Aggregates())
        else:
            tw = {r["window"]: C.Aggregates.from_row(r) for r in tot_ev.result["rows"]}
            tb, tc = tw.get("baseline", C.Aggregates()), tw.get("current", C.Aggregates())
        allocated = dim_ev.result.get("shape") == "by_dimension_allocated"
        out: dict[str, Any] = {"dimension": inp.dimension, "filtered": filtered,
                               "contributions": C.additive_contributions(metric["key"], bseg, cseg, tb, tc, allocated)}
        if not metric["additive"]:
            out["rate_mix"] = C.rate_mix_decomposition(metric["key"], bseg, cseg)
        calc = ledger.record_calculation(s, inv, f"Contribution by {inp.dimension}", "decompose_by_dimension_v1",
                                         [inp.dimension_evidence_id, inp.totals_evidence_id], out)
        return {"evidence_id": str(calc.id), **out}


class CompareIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypothesis_key: str
    evidence_id: str
    analysis: str
    dimension: str | None = None


@tool("compare_hypothesis", CompareIn)
def compare_hypothesis(state: dict[str, Any], inp: CompareIn) -> dict[str, Any]:
    """Summarize evidence for one hypothesis in a bounded, model-readable form."""
    with session_scope() as s:
        ev = s.get(EvidenceItem, uuid.UUID(inp.evidence_id))
        if ev is None or str(ev.investigation_id) != state["investigation_id"]:
            raise ToolError("evidence_not_found", "evidence not found")
        result = ev.result
    if inp.analysis == "campaign_spend":
        rows = result["rows"]
        changes = spend_changes(rows)
        largest = None
        for e in changes.values():
            if e["pct_change"] is not None and (largest is None or abs(float(e["pct_change"])) > abs(float(largest["pct_change"]))):
                largest = e
        return {"analysis": "campaign_spend", "evidence_id": inp.evidence_id, "campaigns": list(changes.values()),
                "largest_change": largest}
    if inp.analysis == "daily":
        series = []
        for r in result["rows"][:100]:
            a = C.Aggregates.from_row(r)
            series.append({"day": r["day"], "value": C.fmt(state["metric"]["key"], C.metric_value(state["metric"]["key"], a))})
        return {"analysis": "daily", "evidence_id": inp.evidence_id, "series": series}
    out = result.get("output", {})
    contrib = out.get("contributions", {})
    rows = sorted(contrib.get("rows", []), key=lambda r: abs(float(r["contribution"] or 0)), reverse=True)[:10]
    return {
        "analysis": "by_dimension",
        "evidence_id": inp.evidence_id,
        "dimension": inp.dimension,
        "filtered": out.get("filtered", False),
        "contributions": {**{k: v for k, v in contrib.items() if k != "rows"}, "rows": rows},
        "rate_mix": out.get("rate_mix"),
    }


class AttachIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypothesis_key: str
    evidence_ids: list[str] = Field(max_length=20)


@tool("attach_evidence", AttachIn)
def attach_evidence(state: dict[str, Any], inp: AttachIn) -> dict[str, Any]:
    with session_scope() as s:
        h = s.scalar(select(Hypothesis).where(Hypothesis.investigation_id == uuid.UUID(state["investigation_id"]),
                                              Hypothesis.key == inp.hypothesis_key))
        if h is None:
            raise ToolError("unknown_hypothesis", inp.hypothesis_key)
        valid = {str(i) for i in s.scalars(select(EvidenceItem.id).where(
            EvidenceItem.investigation_id == h.investigation_id,
            EvidenceItem.id.in_([uuid.UUID(e) for e in inp.evidence_ids])))}
        merged = list(dict.fromkeys([*h.evidence_ids, *[e for e in inp.evidence_ids if e in valid]]))
        h.evidence_ids = merged
        return {"hypothesis_key": h.key, "evidence_count": len(merged)}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "PlanIssue",
    "QueryBudgetExhausted",
    "ToolError",
    "attach_evidence",
    "calculate_delta",
    "compare_hypothesis",
    "decompose_by_dimension",
    "execute_approved_plan",
    "get_metric_definition",
    "get_source_freshness",
    "propose_query_plan",
    "validation_context",
    "windows_of",
]
