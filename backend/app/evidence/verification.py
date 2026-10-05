"""Deterministic verification of claims against stored evidence.

Every numeric assertion is recomputed from the stored evidence rows with the same vetted
calculation functions, every number appearing in the claim's prose must be backed, and
causal wording is rejected unless the claim is a hedged hypothesis. No model is involved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.metrics import calculations as C

CAUSAL_RE = re.compile(
    r"\b(caus(?:e|ed|es|ing)|because of|due to|drove|driven by|drives|led to|leads? to|resulted in|"
    r"result of|as a result|impacted by|thanks to|will (?:recover|restore|increase|fix|reverse|improve|bring back))\b",
    re.IGNORECASE,
)
HEDGE_RE = re.compile(r"\b(may|might|could|possibl[ey]|hypothes[ie]s|coincid\w*|correlat\w*)\b", re.IGNORECASE)
NUMBER_RE = re.compile(r"(?<![\w.])[-+−]?₹?\d[\d,]*(?:\.\d+)?%?")
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

PCT_QUANTITIES = {"metric_pct_change", "noise_band_pct", "segment_share_pct", "segment_pct_change", "spend_change_pct"}
SMALL_INT_LIMIT = 31  # bare integers up to this size (days, thresholds) need no evidence


def parse_number(text: str) -> Decimal | None:
    t = text.strip().replace("₹", "").replace(",", "").replace("%", "").replace("−", "-").replace("+", "")
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


@dataclass
class EvidenceView:
    id: str
    kind: str
    result: dict[str, Any]
    params: dict[str, Any]


@dataclass
class VerificationContext:
    metric_key: str
    baseline_start: str
    period_days: int
    totals_evidence_id: str | None
    evidence: dict[str, EvidenceView]


@dataclass
class ClaimVerdict:
    status: str  # verified | rejected
    reasons: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reasons": self.reasons, "checks": self.checks}


def _query_source(ev: EvidenceView, ctx: VerificationContext) -> EvidenceView | None:
    """Follow calculation evidence back to the query evidence it was computed from."""
    seen = 0
    while ev.kind != "query" and seen < 4:
        inputs = ev.result.get("inputs") or []
        if not inputs or inputs[0] not in ctx.evidence:
            return None
        ev = ctx.evidence[inputs[0]]
        seen += 1
    return ev if ev.kind == "query" else None


def _windows(ev: EvidenceView) -> dict[str, C.Aggregates]:
    return {r["window"]: C.Aggregates.from_row(r) for r in ev.result.get("rows", []) if "window" in r}


def _segments(ev: EvidenceView) -> tuple[dict[str, C.Aggregates], dict[str, C.Aggregates]]:
    b: dict[str, C.Aggregates] = {}
    c: dict[str, C.Aggregates] = {}
    for r in ev.result.get("rows", []):
        (b if r["window"] == "baseline" else c)[str(r["segment"])] = C.Aggregates.from_row(r)
    return b, c


def expected_value(a: dict[str, Any], ctx: VerificationContext) -> str | None:
    ev = ctx.evidence.get(a["evidence_id"])
    if ev is None:
        raise LookupError("evidence not found in this investigation")
    src = _query_source(ev, ctx)
    if src is None:
        raise LookupError("evidence has no query source")
    shape = src.result.get("shape")
    q = a["quantity"]
    if q.startswith("metric_") or q in ("component_delta", "price_effect", "volume_effect"):
        if shape != "totals":
            raise LookupError(f"{q} requires totals evidence, got {shape}")
        w = _windows(src)
        b, c = w.get("baseline", C.Aggregates()), w.get("current", C.Aggregates())
        if q.startswith("metric_"):
            dl = C.delta(ctx.metric_key, b, c)
            return {"metric_baseline": dl["baseline"], "metric_current": dl["current"],
                    "metric_abs_change": dl["abs_change"], "metric_pct_change": dl["pct_change"]}[q]
        if q == "component_delta":
            return C.component_breakdown(b, c)["components"].get(a.get("component") or "")
        pv = C.price_volume(b, c)
        return pv.get(q) if pv.get("defined") else None
    if q == "noise_band_pct":
        if shape != "history":
            raise LookupError("noise_band_pct requires history evidence")
        nb = C.noise_band(ctx.metric_key, src.result["rows"], ctx.baseline_start, ctx.period_days)
        return nb.get("band_pct")
    if q.startswith("segment_"):
        if shape not in ("by_dimension", "by_dimension_allocated"):
            raise LookupError("segment quantities require by_dimension evidence")
        totals = ctx.evidence.get(ctx.totals_evidence_id or "")
        if totals is None:
            raise LookupError("totals evidence missing")
        tw = _windows(totals)
        b, c = _segments(src)
        contrib = C.additive_contributions(
            ctx.metric_key, b, c, tw.get("baseline", C.Aggregates()), tw.get("current", C.Aggregates()),
            allocated=shape == "by_dimension_allocated",
        )
        for row in contrib["rows"]:
            if row["segment"] == a.get("segment"):
                return {"segment_contribution": row["contribution"], "segment_share_pct": row["share_of_total_delta_pct"],
                        "segment_pct_change": row["segment_pct_change"]}[q]
        raise LookupError(f"segment {a.get('segment')!r} not in evidence")
    if q == "spend_change_pct":
        if shape != "campaign_spend":
            raise LookupError("spend_change_pct requires campaign_spend evidence")
        return spend_changes(src.result["rows"]).get(int(a.get("campaign_id") or 0), {}).get("pct_change")
    raise LookupError(f"unsupported quantity {q}")


def spend_changes(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        cid = int(r["campaign_id"])
        e = out.setdefault(cid, {"campaign_id": cid, "campaign_name": r["campaign_name"], "channel": r["channel"],
                                 "baseline": Decimal("0"), "current": Decimal("0")})
        e[r["window"]] += Decimal(str(r["spend"]))
    for e in out.values():
        b, c = e["baseline"], e["current"]
        e["pct_change"] = None if b == 0 else str(((c - b) / b * 100).quantize(Decimal("0.01")))
        e["baseline"], e["current"] = C.money(b), C.money(c)
    return out


def _tolerance(quantity: str, metric_key: str) -> Decimal:
    if quantity in PCT_QUANTITIES:
        return Decimal("0.05")
    if metric_key == "refund_rate" and quantity.startswith("metric_"):
        return Decimal("0.000001")
    return Decimal("0.01")


def _flatten_numbers(obj: Any, out: set[Decimal]) -> None:
    if isinstance(obj, dict):
        for v in obj.values():
            _flatten_numbers(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _flatten_numbers(v, out)
    elif isinstance(obj, (int, float, Decimal)) and not isinstance(obj, bool):
        out.add(abs(Decimal(str(obj))))
    elif isinstance(obj, str) and len(obj) < 40:
        n = parse_number(obj)
        if n is not None:
            out.add(abs(n))


def verify_claim(claim: dict[str, Any], ctx: VerificationContext) -> ClaimVerdict:
    reasons: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    ctype = claim["claim_type"]
    wording = claim["wording"]

    for eid in claim.get("evidence_ids", []):
        if eid not in ctx.evidence:
            reasons.append({"code": "unknown_evidence", "detail": f"evidence {eid} does not belong to this investigation"})

    if ctype in ("observed_change", "contribution_estimate") and not claim.get("numeric"):
        reasons.append({"code": "no_numeric_backing", "detail": f"{ctype} claims must carry numeric assertions"})

    backed: set[Decimal] = set()
    for a in claim.get("numeric", []):
        stated = parse_number(str(a["value"]))
        try:
            exp = expected_value(a, ctx)
        except (LookupError, KeyError, ValueError, InvalidOperation) as exc:
            reasons.append({"code": "unverifiable", "quantity": a["quantity"], "detail": str(exc)[:200]})
            continue
        expd = parse_number(exp) if exp is not None else None
        tol = _tolerance(a["quantity"], ctx.metric_key)
        ok = stated is not None and expd is not None and abs(stated - expd) <= tol
        checks.append({"quantity": a["quantity"], "stated": a["value"], "recomputed": exp, "tolerance": str(tol), "ok": ok,
                       "evidence_id": a["evidence_id"]})
        if ok and stated is not None:
            backed.add(abs(stated))
        else:
            reasons.append({"code": "numeric_mismatch", "quantity": a["quantity"],
                            "detail": f"stated {a['value']} but evidence gives {exp}"})

    # Every number in the prose must be an asserted (and verified) value, a value present in the
    # referenced evidence, or a small bare integer.
    evidence_numbers: set[Decimal] = set()
    for eid in claim.get("evidence_ids", []):
        if eid in ctx.evidence:
            _flatten_numbers(ctx.evidence[eid].result, evidence_numbers)
    text = DATE_RE.sub(" ", wording)
    for m in NUMBER_RE.finditer(text):
        raw = m.group(0)
        n = parse_number(raw)
        if n is None:
            continue
        if abs(n) in backed or abs(n) in evidence_numbers:
            continue
        if n == n.to_integral_value() and abs(n) <= SMALL_INT_LIMIT and "." not in raw and "%" not in raw:
            continue
        reasons.append({"code": "unbacked_number", "detail": f"'{raw}' in the wording is not backed by evidence"})

    if CAUSAL_RE.search(wording):
        hedged = HEDGE_RE.search(wording) is not None
        if not (ctype == "hypothesis" and hedged):
            reasons.append({"code": "unsupported_causality",
                            "detail": "causal wording without a documented causal design"})

    return ClaimVerdict("rejected" if reasons else "verified", reasons, checks)
