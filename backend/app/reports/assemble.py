"""Deterministic report assembly from the structured evidence ledger.

Prose comes only from verified claims; numbers in charts and tables come straight from
evidence and carry their evidence IDs, so a report can be regenerated without losing provenance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

SECTION_ORDER = ("findings", "hypotheses", "limitations", "recommendations")


def build_content(
    *,
    state: dict[str, Any],
    tenant: dict[str, Any],
    investigation: dict[str, Any],
    claims: list[dict[str, Any]],
    hypotheses: list[dict[str, Any]],
    evidence_index: list[dict[str, Any]],
    critic_findings: list[dict[str, Any]],
    model: dict[str, Any],
    version: int,
    summary: str,
    revision_note: str = "",
    extra_limitations: list[str] | None = None,
) -> dict[str, Any]:
    verified = [c for c in claims if c["verification_status"] == "verified"]
    rejected = [c for c in claims if c["verification_status"] == "rejected"]

    def section(types: tuple[str, ...]) -> list[dict[str, Any]]:
        return [
            {"key": c["claim_key"], "wording": c["wording"], "claim_type": c["claim_type"],
             "evidence_ids": c["evidence_ids"], "numeric": c["numeric_fields"].get("assertions", []),
             "verification": c["verification_detail"].get("checks", [])}
            for c in verified if c["claim_type"] in types
        ]

    limitations = section(("data_limitation",))
    for f in critic_findings:
        if f["severity"] == "warn":
            limitations.append({"key": f"critic_{f['issue_type']}", "wording": f["detail"], "claim_type": "critic_warning",
                                "evidence_ids": [], "numeric": [], "verification": []})
    for text in extra_limitations or []:
        limitations.append({"key": "added", "wording": text, "claim_type": "data_limitation",
                            "evidence_ids": [], "numeric": [], "verification": []})
    if state.get("incomplete_data"):
        limitations.insert(0, {"key": "incomplete_data_label", "wording": "INCOMPLETE DATA: the analyst chose to continue "
                               "although source data for the windows is incomplete.", "claim_type": "data_limitation",
                               "evidence_ids": [], "numeric": [], "verification": []})
    if state.get("budget_exhausted"):
        limitations.insert(0, {"key": "partial_report", "wording": "PARTIAL REPORT: an investigation budget was exhausted "
                               f"({state['budget_exhausted']}); some hypotheses were not tested.",
                               "claim_type": "data_limitation", "evidence_ids": [], "numeric": [], "verification": []})

    headline_verified = any(c["claim_key"] == "headline_change" for c in verified)
    driver = state.get("driver") or {"label": "undetermined", "confidence": "low", "explanation": ""}
    if not headline_verified:
        driver = {"label": "undetermined", "confidence": "low",
                  "explanation": "The headline figure could not be verified, so no driver is reported."}

    charts = _charts(state)
    return {
        "schema": "metric-investigator.report.v1",
        "version": version,
        "generated_at": datetime.now(UTC).isoformat(),
        "revision_note": revision_note,
        "title": f"{state['metric']['name']}: {state['windows']['current']['start']} to "
                 f"{state['windows']['current']['end']} (exclusive) vs prior period",
        "question": investigation["question"],
        "tenant": tenant,
        "scope": {
            "metric": {"key": state["metric"]["key"], "name": state["metric"]["name"],
                       "version": state["metric"]["version"], "formula": state["metric"]["formula"]},
            "baseline_window": state["windows"]["baseline"],
            "current_window": state["windows"]["current"],
            "timezone": state["timezone"],
            "as_of": state["as_of"],
            "source_watermark": state.get("freshness", {}).get("watermark"),
            "refund_watermark": state.get("refund_watermark"),
            "currency": "INR",
        },
        "model": model,
        "simulation_label": "Demo simulation" if model.get("simulated") else None,
        "summary": summary,
        "primary_driver": driver,
        "headline": state.get("headline"),
        "materiality": state.get("noise_band"),
        "findings": section(("observed_change", "contribution_estimate")),
        "hypotheses": section(("hypothesis",)),
        "hypothesis_table": [
            {"key": h["key"], "statement": h["statement"], "kind": h["kind"], "status": h["status"],
             "rationale": h["rationale"], "evidence_ids": h["evidence_ids"]}
            for h in hypotheses
        ],
        "limitations": limitations,
        "recommendations": section(("recommendation",)),
        "rejected_claims": [
            {"key": c["claim_key"], "wording": c["wording"], "origin": c["origin"],
             "reasons": c["verification_detail"].get("reasons", [])}
            for c in rejected
        ],
        "charts": charts,
        "evidence_index": evidence_index,
        "budgets": state.get("budget_snapshot", {}),
    }


def _charts(state: dict[str, Any]) -> dict[str, Any]:
    charts: dict[str, Any] = {}
    comps = state.get("components")
    if comps and comps.get("components"):
        charts["components"] = {
            "evidence_id": comps["evidence_id"],
            "points": [{"label": k, "value": v} for k, v in comps["components"].items()],
            "total": comps.get("total_delta"),
        }
    top = None
    for _key, r in (state.get("results") or {}).items():
        if r.get("analysis") == "by_dimension" and not r.get("filtered"):
            if top is None or r.get("dimension") == (state.get("driver") or {}).get("label", "").split(":")[-1].split("=")[0]:
                top = r
    if top:
        charts["dimension"] = {
            "evidence_id": top["evidence_id"],
            "dimension": top["dimension"],
            "points": [{"label": r["segment"], "value": r["contribution"], "small": r.get("small_segment", False)}
                       for r in sorted(top["contributions"]["rows"], key=lambda x: x["segment"])],
        }
    for _key, r in (state.get("results") or {}).items():
        if r.get("analysis") == "daily":
            charts["daily"] = {"evidence_id": r["evidence_id"], "points": r["series"]}
    return charts
