"""Deterministic numeric-claim verification and causal-language policy."""

from app.evidence.verification import EvidenceView, VerificationContext, verify_claim

TOTALS = EvidenceView("ev-totals", "query", {"shape": "totals", "rows": [
    {"window": "baseline", "merchandise": "1000.00", "discount": "100.00", "refunds": "50.00", "completed_orders": 10,
     "refunded_orders": 1, "canceled_orders": 0, "canceled_merchandise": "0", "units": 20},
    {"window": "current", "merchandise": "1000.00", "discount": "100.00", "refunds": "250.00", "completed_orders": 10,
     "refunded_orders": 4, "canceled_orders": 0, "canceled_merchandise": "0", "units": 20},
]}, {})
REGION = EvidenceView("ev-region", "query", {"shape": "by_dimension", "rows": [
    {"window": "baseline", "segment": "North", "merchandise": "500", "discount": "50", "refunds": "25", "completed_orders": 5,
     "refunded_orders": 0, "canceled_orders": 0, "canceled_merchandise": "0", "units": 10},
    {"window": "baseline", "segment": "South", "merchandise": "500", "discount": "50", "refunds": "25", "completed_orders": 5,
     "refunded_orders": 1, "canceled_orders": 0, "canceled_merchandise": "0", "units": 10},
    {"window": "current", "segment": "North", "merchandise": "500", "discount": "50", "refunds": "215", "completed_orders": 5,
     "refunded_orders": 3, "canceled_orders": 0, "canceled_merchandise": "0", "units": 10},
    {"window": "current", "segment": "South", "merchandise": "500", "discount": "50", "refunds": "35", "completed_orders": 5,
     "refunded_orders": 1, "canceled_orders": 0, "canceled_merchandise": "0", "units": 10},
]}, {})
CTX = VerificationContext("net_sales", "2026-08-18", 7, "ev-totals", {"ev-totals": TOTALS, "ev-region": REGION})


def claim(wording: str, ctype: str = "observed_change", numeric: list | None = None, evidence: list | None = None) -> dict:
    return {"claim_type": ctype, "wording": wording, "evidence_ids": evidence if evidence is not None else ["ev-totals"],
            "numeric": numeric or []}


def test_correct_headline_claim_is_verified() -> None:
    v = verify_claim(claim("Net sales fell from 850.00 to 650.00 (-200.00, -23.53%).", numeric=[
        {"quantity": "metric_baseline", "evidence_id": "ev-totals", "value": "850.00"},
        {"quantity": "metric_current", "evidence_id": "ev-totals", "value": "650.00"},
        {"quantity": "metric_abs_change", "evidence_id": "ev-totals", "value": "-200.00"},
        {"quantity": "metric_pct_change", "evidence_id": "ev-totals", "value": "-23.53"},
    ]), CTX)
    assert v.status == "verified", v.reasons


def test_wrong_number_is_rejected() -> None:
    v = verify_claim(claim("Net sales fell 18.4%.", numeric=[
        {"quantity": "metric_pct_change", "evidence_id": "ev-totals", "value": "-18.40"}]), CTX)
    assert v.status == "rejected" and {r["code"] for r in v.reasons} >= {"numeric_mismatch"}


def test_unbacked_number_in_prose_is_rejected() -> None:
    v = verify_claim(claim("Net sales fell to 650.00, roughly 4321.99 below plan.", numeric=[
        {"quantity": "metric_current", "evidence_id": "ev-totals", "value": "650.00"}]), CTX)
    assert v.status == "rejected" and any(r["code"] == "unbacked_number" for r in v.reasons)


def test_segment_contribution_claims_recomputed_from_evidence() -> None:
    ok = verify_claim(claim("North accounts for -190.00 (95.0% of the total change).", "contribution_estimate", [
        {"quantity": "segment_contribution", "evidence_id": "ev-region", "dimension": "region", "segment": "North", "value": "-190.00"},
        {"quantity": "segment_share_pct", "evidence_id": "ev-region", "dimension": "region", "segment": "North", "value": "95.0"},
    ], ["ev-region"]), CTX)
    assert ok.status == "verified", ok.reasons
    bad = verify_claim(claim("South accounts for -190.00.", "contribution_estimate", [
        {"quantity": "segment_contribution", "evidence_id": "ev-region", "dimension": "region", "segment": "South", "value": "-190.00"},
    ], ["ev-region"]), CTX)
    assert bad.status == "rejected"


def test_causal_wording_rejected_unless_hedged_hypothesis() -> None:
    assert verify_claim(claim("The decline was caused by lower ad spend.", "recommendation", evidence=[]), CTX).status == "rejected"
    assert verify_claim(claim("Restoring the budget will recover the lost sales.", "recommendation", evidence=[]), CTX).status == "rejected"
    hedged = verify_claim(claim("Lower ad spend may have contributed; this is a hypothesis that coincides in time.",
                                "hypothesis", evidence=[]), CTX)
    assert hedged.status == "verified"
    unhedged_hyp = verify_claim(claim("Lower ad spend drove the decline.", "hypothesis", evidence=[]), CTX)
    assert unhedged_hyp.status == "rejected"


def test_observed_claims_need_numeric_backing_and_known_evidence() -> None:
    assert verify_claim(claim("Net sales fell."), CTX).status == "rejected"
    v = verify_claim(claim("Net sales were 650.00.", numeric=[
        {"quantity": "metric_current", "evidence_id": "ev-other-investigation", "value": "650.00"}], evidence=["ev-other-investigation"]), CTX)
    assert v.status == "rejected" and {r["code"] for r in v.reasons} >= {"unknown_evidence", "unverifiable"}


def test_planted_advertising_recommendation_is_rejected() -> None:
    from app.agents.nodes import PLANTED_RECOMMENDATION

    v = verify_claim({**PLANTED_RECOMMENDATION, "evidence_ids": ["ev-totals"], "numeric": [
        {"quantity": "metric_pct_change", "evidence_id": "ev-totals", "value": "-18.40"},
        {"quantity": "metric_abs_change", "evidence_id": "ev-totals", "value": "-1240000.00"}]}, CTX)
    codes = {r["code"] for r in v.reasons}
    assert v.status == "rejected" and {"numeric_mismatch", "unsupported_causality"} <= codes
