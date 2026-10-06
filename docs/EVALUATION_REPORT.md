# Evaluation report - synthetic-heldout (fixture mode)

Generated 2026-10-06T05:14:26.527230+00:00.

> Fixture mode: analyst/critic/single-pass behaviour is scripted and deterministic. These numbers validate the pipeline and controls, not language-model reasoning.

| System | Scenarios | Driver identification | Numeric accuracy | Unsupported causal claims | Completion | Avg queries | Total model cost | Avg latency (ms) |
|---|---|---|---|---|---|---|---|---|
| Fixed query dashboard | 20 | 20% | 100% | 0 | 100% | 4.0 | 0 | 422 |
| Single-pass data agent | 20 | 40% | 100% | 18 | 100% | 5.0 | 0 | 324 |
| Full investigation graph | 20 | 100% | 100% | 0 | 100% | 8.8 | 0.000000 | 3946 |

## Driver identification by scenario family

| Family | Fixed query dashboard | Single-pass data agent | Full investigation graph |
|---|---|---|---|
| canceled_order_change | 0% | 0% | 100% |
| discount_increase | 0% | 100% | 100% |
| duplicate_records | 0% | 0% | 100% |
| missing_ingestion_batch | 0% | 0% | 100% |
| price_reduction | 0% | 0% | 100% |
| product_demand_drop | 100% | 100% | 100% |
| refund_increase | 0% | 100% | 100% |
| region_decline | 100% | 100% | 100% |
| unchanged_irrelevant_campaign | 0% | 0% | 100% |
| zero_baseline | 0% | 0% | 100% |

## Full-graph misses

None in this run.

## Method

- Ground-truth drivers are stored in `eval_suites` (application DB) and never exposed to agent tools.
- Numeric accuracy compares each system's reported baseline/current net sales with totals recomputed by an independent Python implementation from the regenerated dataset (exact to the paisa).
- Unsupported causal claims: published statements containing causal wording without a hedge.
- The fixed dashboard always names the largest single-dimension contributor; it has no notion of components, data quality or noise.
