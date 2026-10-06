# Implementation status

Last updated: 2026-10-06. A feature is "done" only when UI, API, authorization, persistence and an acceptance
check work together. Everything below was run on Windows 11 + Docker Desktop (Postgres 17, Python 3.12, Node 22).

## Milestones

| Milestone | Status | Gate evidence |
|---|---|---|
| 1 Repo, locks, config validation, Compose, migrations, auth, seed, health | Done | Fresh `migrate` + `seed-demo`; both tenants sign in (`test_api.py`); `/health/ready` checks both DBs. |
| 2 Catalog, calculations, QueryPlan validator/compiler, read-only access, evidence ledger, DB security | Done | SQL totals equal the independent Python reference exactly for both demo tenants and a fan-out fixture (`test_totals.py`); cross-tenant/pooled-connection/write tests pass (`test_source_security.py`). |
| 3 Graph, providers, fixture mode, durable worker, clarification, critic, verification, cancellation, resume | Done | Seeded Acme investigation produces verified evidence through the real backend; kill/resume demo (`docs/evidence_restart_recovery.txt`). |
| 4 Dashboard, live progress, evidence explorer, review, exports, audit views | Done | Analyst run → reviewer request-changes → approve → HTML/PDF export, via API tests and manually in the browser. |
| 5 Evaluation, negative cases, CI, tracing, cost accounting, smoke, hardening | Done except items listed below | Fixture evaluation on dev and held-out splits; CI workflow; Docker smoke through Caddy (`docs/evidence_smoke_docker.txt`). |

## Commands executed and results

| Command | Result |
|---|---|
| `uv run ruff check app tests` | All checks passed |
| `uv run mypy` | Success: no issues in 60 source files |
| `uv run pytest` | 87 passed (≈ 6–9 min; integration tests against Postgres) |
| `npm run typecheck` / `npm run build` | pass; bundle 710 kB (208 kB gzip) |
| `docker compose --profile app build` / `up -d` | all images built; api, worker, web healthy |
| `scripts/smoke.py` against `http://localhost:8080` | OK (login, freshness, investigation, evidence, approval, HTML+PDF export) |
| `scripts/demo_recovery.py` | OK: worker killed after 6 queries; resumed by a second worker; 10 queries / 10 runs, 2 job attempts |
| `run-evals --suite synthetic-development` (fixture) | full graph 100% driver identification, 100% numeric, 0 unsupported causal claims |
| `run-evals --suite synthetic-heldout` (fixture) | full graph 100% (20/20); single-pass 40% with 18 unsupported causal claims; fixed dashboard 20% — see `EVALUATION_REPORT.md` |

### Failures found and fixed during the build (for the record)

- COPY into RLS tables is not allowed → loader stages through temp tables and `INSERT … SELECT` (RLS WITH CHECK applies).
- Noise band from five week-over-week changes was unstable (bands of 19–23%) → replaced with a weekday-adjusted daily-residual estimator (35 d.o.f.); the Acme demo volume was raised so its decline is material.
- Product-mix shifts made the price/volume split claim "price" drivers → concentration is checked before the mix-blind price split.
- Order-facts view aggregated every item of a tenant (5 s timeouts at 360k orders) → LATERAL per-order aggregation (446 ms → 75 ms) and `ANALYZE` after loads.
- Metric validator allowed a category split for order count → restricted to additive currency metrics.
- `INSERT … ON CONFLICT` rowcount was unreliable through the ORM → `RETURNING`.
- A query interrupted by a worker kill was charged twice to the budget → retries of the same operation id are free.
- Interrupt nodes re-emitted "waiting" events on resume → emitted only on first entry.

## Not done / limitations (honest list)

- **Real-model mode has not been run**: no model credentials were available. The Azure OpenAI / OpenAI-compatible
  adapter is implemented and unit-reachable but untested against a live endpoint; the 80% held-out target in real
  mode is therefore **unmeasured**. CI has a manual, credentialed `real-model-eval` job with a spending cap.
- **Fixture-mode evaluation results are not evidence of model reasoning**: the fixture analyst encodes
  deterministic rules that were developed against the development split; held-out scenarios share the same
  generator, so 100% mainly shows the pipeline, verifier and controls work end to end.
- No Playwright/browser automation in CI; the browser flow was verified manually. CI runs the API-level end-to-end smoke.
- OIDC / Microsoft Entra ID: integration seam and checklist only.
- Rate limiter is in-process; no MFA/password reset.
- Only one synthetic connector; one currency (INR).
- Real cost accounting depends on the per-1k prices you configure; fixture runs report estimated tokens and zero cost.
- OpenTelemetry export is wired (opt-in via `OTEL_EXPORTER_OTLP_ENDPOINT`) but was not tested against a collector.
- Azure deployment is a written note, not scripted or executed; no external resources were created.
- `test_api.py` and `test_worker_graph.py` create investigations in the demo tenants (they show up in "Recent investigations").
