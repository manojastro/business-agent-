"""Structured outputs the analyst and critic roles must produce."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

HypothesisKind = Literal["component", "dimension", "campaign", "data_quality", "price_volume", "trend"]
HypothesisStatus = Literal["open", "supported", "refuted", "inconclusive"]
ClaimType = Literal["observed_change", "contribution_estimate", "hypothesis", "data_limitation", "recommendation"]

# Quantities a claim may assert. Each one is recomputed deterministically from evidence.
Quantity = Literal[
    "metric_baseline",
    "metric_current",
    "metric_abs_change",
    "metric_pct_change",
    "noise_band_pct",
    "component_delta",
    "segment_contribution",
    "segment_share_pct",
    "segment_pct_change",
    "spend_change_pct",
    "price_effect",
    "volume_effect",
]

DRIVER_PATTERN = re.compile(
    r"^(component:(refunds|discount|price|cancellations|merchandise)"
    r"|dimension:[a-z_]+=[A-Za-z0-9_ -]{1,40}"
    r"|data_quality:(missing_batch|duplicates|missing_dates)"
    r"|zero_baseline|no_material_change|undetermined)$"
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HypothesisProposal(_Strict):
    key: str = Field(pattern=r"^[a-z0-9_]{2,60}$")
    statement: str = Field(max_length=400)
    kind: HypothesisKind
    plans: list[dict[str, Any]] = Field(default_factory=list, max_length=3)


class ProposeOut(_Strict):
    hypotheses: list[HypothesisProposal] = Field(max_length=8)
    rationale: str = Field(max_length=600)


class RepairOut(_Strict):
    plan: dict[str, Any]
    note: str = Field(default="", max_length=300)


class HypothesisUpdate(_Strict):
    key: str
    status: HypothesisStatus
    rationale: str = Field(max_length=500)


class ReviseOut(_Strict):
    updates: list[HypothesisUpdate] = Field(default_factory=list)
    new_hypotheses: list[HypothesisProposal] = Field(default_factory=list, max_length=4)
    rationale: str = Field(max_length=600)
    done: bool = False


class NumericAssertion(_Strict):
    quantity: Quantity
    evidence_id: str
    value: str = Field(max_length=32)
    component: str | None = None
    dimension: str | None = None
    segment: str | None = None
    campaign_id: int | None = None


class DraftClaim(_Strict):
    key: str = Field(pattern=r"^[a-z0-9_]{2,80}$")
    wording: str = Field(max_length=700)
    claim_type: ClaimType
    evidence_ids: list[str] = Field(default_factory=list, max_length=10)
    numeric: list[NumericAssertion] = Field(default_factory=list, max_length=8)
    limitations: list[str] = Field(default_factory=list, max_length=6)


class Driver(_Strict):
    label: str
    confidence: Literal["high", "medium", "low"]
    explanation: str = Field(max_length=500)

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        if not DRIVER_PATTERN.match(v):
            raise ValueError("driver label is not in the allowed grammar")
        return v


class DraftOut(_Strict):
    summary: str = Field(max_length=900)
    claims: list[DraftClaim] = Field(max_length=16)
    primary_driver: Driver
    next_investigations: list[str] = Field(default_factory=list, max_length=6)


class CriticFinding(_Strict):
    claim_key: str | None = None
    issue_type: Literal[
        "unsupported_causality",
        "contradictory_evidence",
        "confounder",
        "missing_data",
        "small_segment",
        "multiple_comparisons",
        "numeric_mismatch",
        "other",
    ]
    detail: str = Field(max_length=600)
    severity: Literal["block", "warn"]


class CriticOut(_Strict):
    findings: list[CriticFinding] = Field(default_factory=list, max_length=20)
    rationale: str = Field(max_length=600)


class ReviseReportOut(_Strict):
    summary: str = Field(max_length=900)
    revision_note: str = Field(max_length=600)
    added_limitations: list[str] = Field(default_factory=list, max_length=6)
    next_investigations: list[str] = Field(default_factory=list, max_length=6)


class SinglePassOut(_Strict):
    """Output of the single-pass baseline agent used in evaluation."""

    driver: Driver
    explanation: str = Field(max_length=900)
    claims: list[DraftClaim] = Field(default_factory=list, max_length=8)
