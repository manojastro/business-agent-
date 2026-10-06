# Five-minute demo

Setup: stack running (RUNBOOK §1), demo seeded, two browser windows (normal + private).

| Time | Action | What to point out |
|---|---|---|
| 0:00 | Sign in as `analyst@acme.demo`. Overview. | Net sales −10.7% last complete week (as-of 2026-09-01, Asia/Kolkata, INR); live freshness chart; "Demo simulation mode" badge. |
| 0:40 | New investigation → keep the default question and *Net sales* → Start. | Asynchronous run, idempotency key, live progress over SSE. |
| 1:00 | Watch Progress. | Scope (metric v1, half-open windows), freshness and quality checks with evidence links, baseline with a ±7.2% noise band, 6 hypotheses, a QueryPlan **rejected by the validator** (`sales_channel`) and **repaired**, a second round that drills into North by customer segment — the **hypothesis revision** (refunds & North → supported, advertising → refuted). |
| 2:00 | Click an evidence link. | Drawer: date scope, bound parameters, compiled SQL from allowlisted views, result rows, result hash, watermark; calculation evidence shows its inputs. |
| 2:40 | Point at the red "Claim rejected" event. | The seeded misleading recommendation ("ad spend cut caused an 18.4% drop; restore budget") fails numeric verification (real change is −10.72%) and the critic flags unsupported causality and contradictory evidence (merchandise rose while spend fell). |
| 3:10 | Report tab. | Findings vs hypotheses vs limitations vs recommendations; component chart (refunds −₹14.5L, merchandise +₹2.7L); region chart from evidence; refund-maturity limitation. Analyst cannot approve their own report. |
| 3:40 | Private window: sign in as `reviewer@acme.demo` → Report review → open it → Request changes ("call out refund maturity in the summary"). | New version 2 with revision note; summary re-verified for numbers. |
| 4:20 | Approve version 2. Export HTML and PDF. | Exports carry version, content hash, approver, evidence IDs, Demo-simulation label. Audit log (as admin) shows every decision and export. |

## Separate: tenant isolation (1 minute)

1. Sign in as `analyst@bharat.demo`; paste the Acme investigation URL → "investigation not found" (404, not 403).
2. Show `backend/tests/test_source_security.py`: the read-only role cannot read `source.*`, cannot write even with
   read-only mode switched off, sees zero rows without tenant context, and a pooled connection does not keep the
   previous tenant. Run: `uv run pytest tests/test_source_security.py -q`.

## Separate: restart recovery (1 minute)

```bash
cd backend && uv run python ../scripts/demo_recovery.py
```

Starts a worker with a slowed fixture model, kills it after six source queries, starts another worker; the lease
expires, the job is reclaimed and the graph resumes from its checkpoint under the same investigation id with no
duplicated queries. Recorded output: [evidence_restart_recovery.txt](evidence_restart_recovery.txt).

## Source-freshness failure (30 seconds)

New investigation with as-of **2026-09-04** (data ends 2026-08-31): the run stops with a clarification — "source
data is incomplete … continue with an INCOMPLETE DATA label, or stop?". Continue → the report leads with the
INCOMPLETE DATA label and names `data_quality:missing_batch`; Stop → status `insufficient_evidence`.

## Ambiguity (30 seconds)

Question "Why did revenue drop last week?" with metric "Infer" → the agent asks which metric instead of choosing.
