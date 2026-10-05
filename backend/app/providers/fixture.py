"""Deterministic fixture provider ("Demo simulation").

It returns scripted, rule-based outputs computed from the structured context so the whole
pipeline runs without credentials. Results produced with it measure the pipeline and the
deterministic controls - not the reasoning quality of a language model - and every run that
uses it is labelled "Demo simulation" in the UI, exports and evaluation reports.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

from app.providers.base import ModelRequest, ModelResult, ProviderTimeout, Usage

D = Decimal


def _dec(x: Any) -> Decimal:
    return D(str(x)) if x is not None else D("0")


def _plan(ctx: dict[str, Any], analysis: str, purpose: str, **extra: Any) -> dict[str, Any]:
    p = {
        "metric_id": ctx["metric"]["key"],
        "metric_version": ctx["metric"]["version"],
        "analysis": analysis,
        "baseline_window": ctx["windows"]["baseline"],
        "current_window": ctx["windows"]["current"],
        "purpose": purpose,
    }
    p.update(extra)
    return p


class FixtureProvider:
    name = "fixture"
    model = "deterministic-fixture-v1"
    simulated = True

    def __init__(self, fail_tasks: set[str] | None = None, delay_s: float = 0.0) -> None:
        self.fail_tasks = fail_tasks or set()
        self.delay_s = delay_s

    def complete(self, request: ModelRequest) -> ModelResult:
        started = time.perf_counter()
        if request.task in self.fail_tasks:
            raise ProviderTimeout(f"simulated timeout for task {request.task}")
        if self.delay_s:
            time.sleep(self.delay_s)
        handler = getattr(self, f"_task_{request.task}")
        data = request.response_model.model_validate(handler(request.context))
        prompt_chars = len(request.system) + len(request.user_message())
        out_chars = len(json.dumps(data.model_dump(mode="json")))
        return ModelResult(
            data=data,
            usage=Usage(prompt_chars // 4, out_chars // 4, estimated=True),
            cost=D("0"),
            provider=self.name,
            model=self.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            simulated=True,
        )

    # ------------------------------------------------------------------ analyst: propose
    def _task_propose(self, ctx: dict[str, Any]) -> dict[str, Any]:
        dims = ctx["allowed_dimensions"]
        comps = ctx.get("components") or {}
        total = _dec((ctx.get("headline") or {}).get("abs_change"))
        merch = _dec((comps.get("components") or {}).get("merchandise"))
        canceled = _dec(comps.get("canceled_merchandise_change"))
        hyps: list[dict[str, Any]] = [
            {"key": "refunds_component", "kind": "component",
             "statement": "Eligible refunds changed and explain a material share of the net sales change.", "plans": []},
            {"key": "discount_component", "kind": "component",
             "statement": "Discount depth changed enough to move net sales.", "plans": []},
        ]
        # Data-driven: only propose what the component split makes plausible, leaving budget for follow-ups.
        if total != 0 and merch / total >= D("0.3"):
            hyps.append({"key": "price_vs_volume", "kind": "price_volume",
                         "statement": "Merchandise moved because of average unit price rather than units sold.", "plans": []})
        if total != 0 and abs(canceled) >= abs(total) * D("0.2"):
            hyps.append({"key": "cancellations", "kind": "component",
                         "statement": "More orders were canceled, removing merchandise from eligible sales.", "plans": []})
        if "region" in dims:
            hyps.append({"key": "region_concentration", "kind": "dimension",
                         "statement": "The change is concentrated in one region.",
                         "plans": [_plan(ctx, "by_dimension", "Net sales contribution by region", dimension="region")]})
        if "category" in dims:
            hyps.append({"key": "category_concentration", "kind": "dimension",
                         "statement": "The change is concentrated in one product category.",
                         "plans": [_plan(ctx, "by_dimension", "Contribution by product category", dimension="category")]})
        if "channel" in dims:
            # Deliberately uses a non-catalog dimension name first; the validator rejects it and
            # the repair loop corrects it. This keeps the repair path visible in fixture mode.
            hyps.append({"key": "channel_concentration", "kind": "dimension",
                         "statement": "The change is concentrated in one sales channel.",
                         "plans": [_plan(ctx, "by_dimension", "Contribution by sales channel", dimension="sales_channel")]})
        hyps.append({"key": "advertising_spend", "kind": "campaign",
                     "statement": "A change in advertising spend coincides with the metric change.",
                     "plans": [_plan(ctx, "campaign_spend", "Campaign spend in both windows")]})
        hyps = hyps[: ctx["budget"]["hypotheses_left"]]
        return {
            "hypotheses": hyps,
            "rationale": "Start with the arithmetic components of net sales, then test concentration one "
                         "dimension at a time and check the coincident advertising change.",
        }

    def _task_repair(self, ctx: dict[str, Any]) -> dict[str, Any]:
        plan = dict(ctx["plan"])
        issues = ctx["issues"]
        note = []
        for issue in issues:
            if issue["field"] == "dimension" and plan.get("dimension") == "sales_channel":
                plan["dimension"] = "channel"
                note.append("renamed dimension sales_channel -> channel (catalog name)")
            elif issue["code"] in ("dimension_not_allowed", "unknown_dimension") and issue["field"] == "dimension":
                allowed = ctx.get("allowed_dimensions", [])
                plan["dimension"] = allowed[0] if allowed else None
                note.append("replaced dimension with an allowed one")
            elif issue["code"] == "out_of_scope":
                plan["baseline_window"] = ctx["windows"]["baseline"]
                plan["current_window"] = ctx["windows"]["current"]
                note.append("reset windows to the investigation scope")
            elif issue["field"].startswith("filters"):
                plan["filters"] = []
                note.append("removed invalid filters")
        for k in list(plan):
            if k not in {"metric_id", "metric_version", "analysis", "baseline_window", "current_window", "dimension",
                         "filters", "order_by", "limit", "lookback_periods", "purpose"}:
                plan.pop(k)
                note.append(f"removed unsupported field {k}")
        return {"plan": plan, "note": "; ".join(note) or "no change possible"}

    # ------------------------------------------------------------------ analyst: revise
    def _task_revise(self, ctx: dict[str, Any]) -> dict[str, Any]:
        f = derive_findings(ctx)
        updates = []
        for h in ctx["hypotheses"]:
            if h["status"] != "open":
                continue
            status, why = f["statuses"].get(h["key"], ("inconclusive", "No evidence collected for this hypothesis."))
            updates.append({"key": h["key"], "status": status, "rationale": why})
        new: list[dict[str, Any]] = []
        keys = {h["key"] for h in ctx["hypotheses"]}
        left = ctx["budget"]["queries_left"]
        top = f.get("top_segment")
        if ctx.get("round", 1) == 1 and left >= 2 and f["material"]:
            if top and top["dimension"] != "category" and "segment_drilldown" not in keys:
                other = "customer_segment" if "customer_segment" in ctx["allowed_dimensions"] else None
                if other:
                    new.append({"key": "segment_drilldown", "kind": "dimension",
                                "statement": f"Within {top['segment']}, the change is broad across customer segments.",
                                "plans": [_plan(ctx, "by_dimension", f"Customer segments within {top['dimension']}={top['segment']}",
                                                dimension=other,
                                                filters=[{"dimension": top["dimension"], "values": [top["segment"]]}])]})
            if "daily_trend" not in keys:
                new.append({"key": "daily_trend", "kind": "trend",
                            "statement": "The change is a step change within the window rather than a single-day anomaly.",
                            "plans": [_plan(ctx, "daily", "Daily series for both windows")]})
        return {
            "updates": updates,
            "new_hypotheses": new[: ctx["budget"]["hypotheses_left"]],
            "rationale": f["rationale"],
            "done": not new,
        }

    # ------------------------------------------------------------------ analyst: draft
    def _task_draft(self, ctx: dict[str, Any]) -> dict[str, Any]:
        f = derive_findings(ctx)
        h = ctx["headline"]
        ev = h["evidence_id"]
        metric = ctx["metric"]
        claims: list[dict[str, Any]] = []
        pct = h["pct_change"]
        if pct is None:
            wording = (f"{metric['name']} was {h['baseline']} in the baseline window and {h['current']} in the current "
                       "window; the percentage change is undefined because the baseline is zero.")
            numeric = [
                {"quantity": "metric_baseline", "evidence_id": ev, "value": h["baseline"] or "0"},
                {"quantity": "metric_current", "evidence_id": ev, "value": h["current"] or "0"},
            ]
        else:
            direction = "fell" if _dec(h["abs_change"]) < 0 else "rose"
            wording = (f"{metric['name']} {direction} from {h['baseline']} to {h['current']} "
                       f"({h['abs_change']}, {pct}%) in the current window versus the baseline window.")
            numeric = [
                {"quantity": "metric_baseline", "evidence_id": ev, "value": h["baseline"]},
                {"quantity": "metric_current", "evidence_id": ev, "value": h["current"]},
                {"quantity": "metric_abs_change", "evidence_id": ev, "value": h["abs_change"]},
                {"quantity": "metric_pct_change", "evidence_id": ev, "value": pct},
            ]
        claims.append({"key": "headline_change", "wording": wording, "claim_type": "observed_change",
                       "evidence_ids": [ev], "numeric": numeric})

        nb = ctx.get("noise_band") or {}
        if nb.get("defined"):
            inside = not f["material"]
            claims.append({
                "key": "variation_context",
                "wording": (f"Typical period-over-period variation in the preceding periods is about ±{nb['band_pct']}%; "
                            + ("the observed change is within that range, so it is not treated as a material change."
                               if inside else "the observed change is outside that range.")),
                "claim_type": "observed_change",
                "evidence_ids": [nb["evidence_id"]],
                "numeric": [{"quantity": "noise_band_pct", "evidence_id": nb["evidence_id"], "value": nb["band_pct"]}],
            })

        comps = ctx.get("components")
        if comps and metric["key"] == "net_sales" and pct is not None:
            c = comps["components"]
            claims.append({
                "key": "component_reconciliation",
                "wording": (f"By component, merchandise contributed {c['merchandise']}, discounts {c['discount']} and "
                            f"refunds {c['refunds']}; together they reconcile to the total change of {comps['total_delta']}."),
                "claim_type": "contribution_estimate",
                "evidence_ids": [comps["evidence_id"]],
                "numeric": [
                    {"quantity": "component_delta", "evidence_id": comps["evidence_id"], "component": k, "value": c[k]}
                    for k in ("merchandise", "discount", "refunds")
                ],
            })

        top = f.get("top_segment")
        if top and f["material"]:
            claims.append({
                "key": f"top_contributor_{top['dimension']}",
                "wording": (f"Looking at {top['dimension'].replace('_', ' ')} alone, {top['segment']} accounts for "
                            f"{top['contribution']} ({top['share']}% of the total change). This is an observed "
                            "numerical contribution, not a causal explanation."),
                "claim_type": "contribution_estimate",
                "evidence_ids": [top["evidence_id"]],
                "numeric": [
                    {"quantity": "segment_contribution", "evidence_id": top["evidence_id"], "dimension": top["dimension"],
                     "segment": top["segment"], "value": top["contribution"]},
                    {"quantity": "segment_share_pct", "evidence_id": top["evidence_id"], "dimension": top["dimension"],
                     "segment": top["segment"], "value": top["share"]},
                ],
            })

        pv = ctx.get("price_volume")
        if pv and pv.get("defined") and f["material"] and f["driver"] in ("component:price", "component:merchandise"):
            claims.append({
                "key": "price_volume_split",
                "wording": (f"Merchandise change splits into a price effect of {pv['price_effect']} and a volume effect "
                            f"of {pv['volume_effect']} (two-factor method; mix not separated)."),
                "claim_type": "contribution_estimate",
                "evidence_ids": [pv["evidence_id"]],
                "numeric": [
                    {"quantity": "price_effect", "evidence_id": pv["evidence_id"], "value": pv["price_effect"]},
                    {"quantity": "volume_effect", "evidence_id": pv["evidence_id"], "value": pv["volume_effect"]},
                ],
            })

        spend = ctx.get("campaign_spend")
        if spend and spend.get("largest_change"):
            lc = spend["largest_change"]
            if lc["pct_change"] is not None and abs(_dec(lc["pct_change"])) >= 20:
                contradicted = f["statuses"].get("advertising_spend", ("", ""))[0] == "refuted"
                claims.append({
                    "key": "advertising_hypothesis",
                    "wording": (f"Spend on campaign '{lc['campaign_name']}' changed by {lc['pct_change']}% in the same window. "
                                "This coincidence may be worth checking, but it is a hypothesis only"
                                + ("; merchandise sales did not move in the same direction, which argues against it."
                                   if contradicted else "; no causal design supports it.")),
                    "claim_type": "hypothesis",
                    "evidence_ids": [spend["evidence_id"]],
                    "numeric": [{"quantity": "spend_change_pct", "evidence_id": spend["evidence_id"],
                                 "campaign_id": lc["campaign_id"], "value": lc["pct_change"]}],
                })

        for i, lim in enumerate(f["limitations"]):
            claims.append({"key": f"limitation_{i + 1}", "wording": lim["text"], "claim_type": "data_limitation",
                           "evidence_ids": lim.get("evidence_ids", []), "numeric": []})

        for i, rec in enumerate(f["next"]):
            claims.append({"key": f"recommendation_{i + 1}", "wording": rec, "claim_type": "recommendation",
                           "evidence_ids": [], "numeric": []})

        return {
            "summary": f["summary"],
            "claims": claims[:16],
            "primary_driver": {"label": f["driver"], "confidence": f["confidence"], "explanation": f["driver_explanation"]},
            "next_investigations": f["next"],
        }

    # ------------------------------------------------------------------ critic
    def _task_critique(self, ctx: dict[str, Any]) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        f = derive_findings(ctx)
        for claim in ctx["claims"]:
            text = claim["wording"].lower()
            if claim.get("origin") == "planted" or ("advertis" in text and "caus" in text):
                findings.append({
                    "claim_key": claim["key"], "issue_type": "unsupported_causality", "severity": "block",
                    "detail": "Claims advertising caused the decline. There is no causal design; spend timing is only a coincidence.",
                })
                if f.get("merch_up_while_spend_down"):
                    findings.append({
                        "claim_key": claim["key"], "issue_type": "contradictory_evidence", "severity": "block",
                        "detail": "Campaign spend fell while merchandise sales rose; the decline sits in refunds, which "
                                  "advertising does not explain.",
                    })
        conc = list(f.get("concentrated_dimensions", []))
        if len(conc) > 1:
            findings.append({
                "claim_key": None, "issue_type": "confounder", "severity": "warn",
                "detail": "More than one dimension shows concentration (" + ", ".join(conc) + "). Dimensions overlap; "
                          "their contributions must not be added together.",
            })
        for q in f.get("quality_issues", []):
            findings.append({"claim_key": None, "issue_type": "missing_data", "severity": "warn", "detail": q})
        if f.get("small_segments"):
            findings.append({"claim_key": None, "issue_type": "small_segment", "severity": "warn",
                             "detail": "Small segments present: " + ", ".join(f["small_segments"][:5])})
        if f.get("segments_examined", 0) >= 10:
            findings.append({"claim_key": None, "issue_type": "multiple_comparisons", "severity": "warn",
                             "detail": f"{f['segments_examined']} segments were compared; isolated large movements in "
                                       "small segments may be noise."})
        return {"findings": findings, "rationale": "Checked causal wording, contradictions, overlap, data quality and segment size."}

    def _task_revise_report(self, ctx: dict[str, Any]) -> dict[str, Any]:
        comment = ctx.get("review_comment", "").strip()
        return {
            "summary": ctx["summary"],
            "revision_note": f"Revised after reviewer request: {comment[:400]}" if comment else "Revised after reviewer request.",
            "added_limitations": [f"Reviewer note addressed: {comment[:200]}"] if comment else [],
            "next_investigations": ctx.get("next_investigations", []),
        }

    # ------------------------------------------------------------------ baseline agent (evaluation)
    def _task_single_pass(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Scripted 'naive single-pass agent': attributes the change to the most visible coincident
        movement without checking data quality, noise or contradictions."""
        h = ctx["headline"]
        headline_claim = [{
            "key": "headline_change", "claim_type": "observed_change", "evidence_ids": [],
            "wording": f"Net sales moved from {h['baseline']} to {h['current']}.",
            "numeric": [{"quantity": "metric_baseline", "evidence_id": "context", "value": h["baseline"] or "0"},
                        {"quantity": "metric_current", "evidence_id": "context", "value": h["current"] or "0"}],
        }]
        spend = ctx.get("campaign_spend") or {}
        lc = spend.get("largest_change")
        if lc and lc["pct_change"] is not None and abs(_dec(lc["pct_change"])) >= 20:
            return {
                "driver": {"label": "undetermined", "confidence": "high",
                           "explanation": f"Advertising change on {lc['campaign_name']}"},
                "explanation": f"Net sales changed because spend on {lc['campaign_name']} changed by {lc['pct_change']}%.",
                "claims": headline_claim,
            }
        comps = (ctx.get("components") or {}).get("components") or {}
        if comps:
            k = max(comps, key=lambda c: abs(_dec(comps[c])))
            label = {"merchandise": "component:merchandise", "discount": "component:discount", "refunds": "component:refunds"}[k]
            if k == "merchandise" and ctx.get("dimension_tops"):
                best = max(ctx["dimension_tops"], key=lambda t: abs(_dec(t["contribution"])))
                label = f"dimension:{best['dimension']}={best['segment']}"
            return {"driver": {"label": label, "confidence": "high", "explanation": f"Largest movement was {k}"},
                    "explanation": f"The change was driven by {k}.", "claims": headline_claim}
        return {"driver": {"label": "undetermined", "confidence": "low", "explanation": "no components"},
                "explanation": f"Metric moved {h.get('pct_change')}%.", "claims": []}


# ---------------------------------------------------------------------------------------
# Shared deterministic interpretation used by the fixture analyst/critic. (Real models
# receive the same context and produce their own outputs.)
# ---------------------------------------------------------------------------------------


def derive_findings(ctx: dict[str, Any]) -> dict[str, Any]:  # noqa: C901 - one readable rule table
    h = ctx["headline"]
    q = ctx.get("quality") or {}
    comps = ctx.get("components") or {}
    pv = ctx.get("price_volume") or {}
    results = ctx.get("results") or {}
    metric = ctx["metric"]["key"]
    statuses: dict[str, tuple[str, str]] = {}
    limitations: list[dict[str, Any]] = []
    quality_issues: list[str] = []
    nxt: list[str] = []

    material = bool(h.get("material"))
    pct = h.get("pct_change")
    total = _dec(h.get("abs_change")) if h.get("abs_change") is not None else D("0")

    # ---- data quality first: it changes the answer ----
    driver = None
    if q.get("missing_batch_days"):
        days = ", ".join(q["missing_batch_days"])
        quality_issues.append(f"No orders ingestion batch for {days}; the current window is incomplete.")
        limitations.append({"text": f"Orders ingestion batches are missing for {days}. Totals for the current window are "
                                    "incomplete and the decline is at least partly a data gap.",
                            "evidence_ids": [q.get("freshness_evidence_id")] if q.get("freshness_evidence_id") else []})
        driver = "data_quality:missing_batch"
    if q.get("duplicate_share_pct") and _dec(q["duplicate_share_pct"]) > 1:
        quality_issues.append(f"{q['duplicate_share_pct']}% of current-window order rows share a source_id with another row.")
        limitations.append({"text": f"About {q['duplicate_share_pct']}% of order rows in the current window are duplicates "
                                    "of the same source order; the increase is inflated by duplicated records.",
                            "evidence_ids": [q.get("evidence_id")] if q.get("evidence_id") else []})
        driver = driver or "data_quality:duplicates"
    if q.get("null_ordered_at_rows"):
        quality_issues.append(f"{q['null_ordered_at_rows']} recently ingested orders have no order date and were excluded.")
        limitations.append({"text": f"{q['null_ordered_at_rows']} orders without an order date could not be placed in a window.",
                            "evidence_ids": [q.get("evidence_id")] if q.get("evidence_id") else []})
    if q.get("incomplete_ack"):
        limitations.append({"text": "The analyst chose to continue with incomplete data; findings carry an incomplete-data label.",
                            "evidence_ids": []})

    if driver is None and h.get("baseline") is not None and _dec(h["baseline"]) == 0:
        driver = "zero_baseline"
    if driver is None and not material:
        driver = "no_material_change"

    # ---- components ----
    c = comps.get("components") or {}
    shares = {}
    if total != 0 and c:
        for k in ("merchandise", "discount", "refunds"):
            shares[k] = _dec(c[k]) / total * 100
    refunds_share = shares.get("refunds", D("0"))
    discount_share = shares.get("discount", D("0"))
    merch_share = shares.get("merchandise", D("0"))
    statuses["refunds_component"] = (
        ("supported", f"Refunds account for {refunds_share:.1f}% of the change.") if refunds_share >= 50
        else ("refuted", f"Refunds account for only {refunds_share:.1f}% of the change.")
    )
    statuses["discount_component"] = (
        ("supported", f"Discounts account for {discount_share:.1f}% of the change.") if discount_share >= 50
        else ("refuted", f"Discounts account for only {discount_share:.1f}% of the change.")
    )
    canceled_change = _dec(comps.get("canceled_merchandise_change"))
    merch_delta = _dec(c.get("merchandise")) if c else D("0")
    cancel_driven = merch_delta < 0 and canceled_change > 0 and canceled_change >= abs(merch_delta) * D("0.5")
    statuses["cancellations"] = (
        ("supported", f"Canceled merchandise rose by {canceled_change}, covering most of the merchandise decline.")
        if cancel_driven else ("refuted", "Canceled merchandise did not rise enough to explain the change.")
    )
    price_driven = False
    if pv.get("defined") and merch_delta != 0:
        price_share = _dec(pv["price_effect"]) / merch_delta * 100
        price_driven = price_share >= 60
        statuses["price_vs_volume"] = (
            ("supported", f"Price effect is {price_share:.1f}% of the merchandise change.") if price_driven
            else ("refuted", f"Price effect is only {price_share:.1f}% of the merchandise change.")
        )

    # ---- dimension concentration (one dimension at a time) ----
    top_segment = None
    concentrated: list[str] = []
    small: list[str] = []
    segments_examined = 0
    for key, r in results.items():
        if r.get("analysis") != "by_dimension" or r.get("filtered"):
            continue
        contrib = r["contributions"]
        segments_examined += contrib.get("segments_examined", 0)
        for row in contrib["rows"]:
            if row.get("small_segment"):
                small.append(f"{r['dimension']}={row['segment']}")
        best = None
        for row in contrib["rows"]:
            share = row.get("share_of_total_delta_pct")
            if share is None or row.get("segment_pct_change") is None:
                continue
            if _dec(share) >= 60 and (best is None or _dec(share) > _dec(best["share_of_total_delta_pct"])):
                best = row
        if best and pct is not None and abs(_dec(best["segment_pct_change"])) >= abs(_dec(pct)) * D("1.8"):
            concentrated.append(r["dimension"])
            cand = {"dimension": r["dimension"], "segment": best["segment"], "contribution": best["contribution"],
                    "share": best["share_of_total_delta_pct"], "segment_pct": best["segment_pct_change"],
                    "evidence_id": r["evidence_id"]}
            if top_segment is None or _dec(cand["share"]) > _dec(top_segment["share"]):
                top_segment = cand
            statuses[key] = ("supported", f"{best['segment']} carries {best['share_of_total_delta_pct']}% of the change "
                                          f"with a {best['segment_pct_change']}% segment change.")
        else:
            statuses[key] = ("refuted", "No single segment dominates the change on this dimension.")

    # ---- advertising coincidence ----
    spend = ctx.get("campaign_spend") or {}
    lc = spend.get("largest_change")
    merch_up_while_spend_down = False
    if lc and lc["pct_change"] is not None:
        sp = _dec(lc["pct_change"])
        if abs(sp) >= 20:
            merch_up_while_spend_down = sp < 0 and merch_delta > 0
            if merch_up_while_spend_down or (sp > 0 and merch_delta < 0):
                statuses["advertising_spend"] = ("refuted", "Spend and merchandise moved in opposite directions; the timing "
                                                            "coincides but the evidence contradicts an advertising explanation.")
            else:
                statuses["advertising_spend"] = ("inconclusive", "Spend changed in the same window; correlation only, no causal design.")
        else:
            statuses["advertising_spend"] = ("refuted", "Campaign spend was stable across the windows.")
    for key, r in results.items():
        if r.get("analysis") == "daily":
            statuses[key] = ("inconclusive", "Daily series attached for inspection; not used as a driver test.")
        if r.get("filtered"):
            statuses[key] = ("inconclusive", "Drill-down attached; segments within the filter are reported for context.")

    # ---- driver ----
    if driver is None and metric == "net_sales":
        if refunds_share >= 50:
            driver = "component:refunds"
        elif discount_share >= 50:
            driver = "component:discount"
        elif cancel_driven:
            driver = "component:cancellations"
        elif top_segment:
            # A concentrated category also lowers the *average* unit price through mix, so
            # concentration is checked before the (mix-blind) price/volume split.
            driver = f"dimension:{top_segment['dimension']}={top_segment['segment']}"
            if price_driven and top_segment["dimension"] == "category":
                statuses["price_vs_volume"] = ("inconclusive", "Average unit price moved mainly through category mix, "
                                                               "not like-for-like price changes.")
        elif price_driven:
            driver = "component:price"
        elif merch_share >= 50:
            driver = "component:merchandise"
    if driver is None and top_segment:
        driver = f"dimension:{top_segment['dimension']}={top_segment['segment']}"
    driver = driver or "undetermined"

    confidence = "high" if driver.startswith(("data_quality", "zero_baseline")) else ("medium" if material else "low")
    explanations = {
        "component:refunds": "Higher eligible refunds account for most of the change.",
        "component:discount": "Deeper discounts account for most of the change.",
        "component:cancellations": "More canceled orders removed merchandise from eligible sales.",
        "component:price": "Lower average unit prices account for most of the merchandise change.",
        "component:merchandise": "Merchandise sales moved without a single dominant segment.",
        "data_quality:missing_batch": "Missing ingestion batches make the current window incomplete.",
        "data_quality:duplicates": "Duplicated order records inflate the current window.",
        "zero_baseline": "The baseline is zero, so a percentage change is undefined.",
        "no_material_change": "The change is within typical variation.",
        "undetermined": "Evidence does not isolate a single driver.",
    }
    driver_expl = explanations.get(driver) or (
        f"{top_segment['segment']} ({top_segment['dimension']}) carries {top_segment['share']}% of the change." if top_segment else ""
    )

    if driver == "component:refunds":
        where = f" in {top_segment['segment']}" if top_segment else ""
        nxt.append(f"Break down refunds{where} by reason code, product and fulfilment partner.")
        nxt.append("Re-run after refunds for the current cohort mature (refund timing lags orders).")
    elif driver.startswith("dimension:") and top_segment:
        nxt.append(f"Investigate {top_segment['segment']} ({top_segment['dimension']}) by product, channel and day.")
    elif driver.startswith("data_quality"):
        nxt.append("Reload or deduplicate the affected ingestion batches, then re-run the investigation.")
    elif driver == "component:discount":
        nxt.append("Review promotion calendar and discount rules active in the current window.")
    elif driver == "component:price":
        nxt.append("Review price list changes and markdowns effective in the current window.")
    elif driver == "component:cancellations":
        nxt.append("Break down cancellations by reason, payment method and fulfilment partner.")
    if metric == "net_sales" and (refunds_share != 0 or driver == "component:refunds"):
        limitations.append({"text": "Refunds are counted by original order cohort as of the data watermark; recent orders "
                                    "have had less time to be refunded, so current-window refunds may still rise.",
                            "evidence_ids": [comps.get("evidence_id")] if comps.get("evidence_id") else []})
    if small:
        limitations.append({"text": "Some segments are small (fewer than 30 orders in a window); their percentage changes are noisy.",
                            "evidence_ids": []})

    pct_txt = f"{pct}%" if pct is not None else "an undefined percentage (zero baseline)"
    summary = (f"{ctx['metric']['name']} changed by {h.get('abs_change')} ({pct_txt}). "
               f"Primary driver: {driver}. {driver_expl}")
    rationale = "Statuses follow component shares, single-dimension concentration and the direction of spend versus merchandise."
    return {
        "statuses": statuses, "driver": driver, "confidence": confidence, "driver_explanation": driver_expl,
        "material": material, "top_segment": top_segment, "limitations": limitations, "next": nxt,
        "summary": summary, "rationale": rationale, "quality_issues": quality_issues,
        "concentrated_dimensions": concentrated, "small_segments": sorted(set(small)),
        "segments_examined": segments_examined, "merch_up_while_spend_down": merch_up_while_spend_down,
    }
