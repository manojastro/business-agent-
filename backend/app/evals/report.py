"""Render an evaluation summary as Markdown (docs/EVALUATION_REPORT.md)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

LABELS = {
    "fixed_dashboard": "Fixed query dashboard",
    "single_pass": "Single-pass data agent",
    "full_graph": "Full investigation graph",
}


def write_markdown(summary: dict[str, Any], path: Path) -> None:
    systems = summary["systems"]
    lines = [
        f"# Evaluation report - {summary['suite']} ({summary['model_mode']} mode)",
        "",
        f"Generated {summary['generated_at']}.",
        "",
        f"> {summary['fixture_note']}",
        "",
        "| System | Scenarios | Driver identification | Numeric accuracy | Unsupported causal claims | Completion "
        "| Avg queries | Total model cost | Avg latency (ms) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for key, m in systems.items():
        lines.append(
            f"| {LABELS.get(key, key)} | {m['scenarios']} | {m['driver_accuracy']:.0%} | {m['numeric_accuracy']:.0%} | "
            f"{m['unsupported_causal_claims']} | {m['completion_rate']:.0%} | {m['avg_queries']} | {m['total_cost']} | "
            f"{m['avg_latency_ms']} |"
        )
    families = sorted({f for m in systems.values() for f in m["by_family"]})
    lines += ["", "## Driver identification by scenario family", "",
              "| Family | " + " | ".join(LABELS.get(k, k) for k in systems) + " |",
              "|---|" + "---|" * len(systems)]
    for f in families:
        lines.append(f"| {f} | " + " | ".join(f"{systems[k]['by_family'].get(f, 0):.0%}" for k in systems) + " |")
    misses = [r for r in summary["rows"] if r["system"] == "full_graph" and not r["driver_correct"]]
    lines += ["", "## Full-graph misses", ""]
    if not misses:
        lines.append("None in this run.")
    for r in misses:
        lines.append(f"- `{r['slug']}`: expected `{r['expected_driver']}`, predicted `{r['predicted_driver']}`"
                     + (f" ({r.get('error')})" if r.get("error") else ""))
    lines += ["", "## Method", "",
              "- Ground-truth drivers are stored in `eval_suites` (application DB) and never exposed to agent tools.",
              "- Numeric accuracy compares each system's reported baseline/current net sales with totals recomputed by an "
              "independent Python implementation from the regenerated dataset (exact to the paisa).",
              "- Unsupported causal claims: published statements containing causal wording without a hedge.",
              "- The fixed dashboard always names the largest single-dimension contributor; it has no notion of "
              "components, data quality or noise.",
              ""]
    path.write_text("\n".join(lines), encoding="utf-8")
