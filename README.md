# Metric Investigator

An evidence-first agent that explains **why a business metric changed**. An analyst asks a question
("Why did net sales fall in the last complete seven days?"). A bounded LangGraph investigation then proposes
hypotheses and **constrained query plans, never raw SQL**. It runs them through a read-only, row-level-security
database role, recomputes every number deterministically, has a critic look for contradictions and unsupported
causal claims, and hands a versioned report to a human reviewer for publication.

Portfolio project built on synthetic commerce data. It is not production-hardened and makes no claims about
measured savings. Category inspiration: [Inconvo](https://github.com/inconvoai/inconvo). This project is not
affiliated with Inconvo and does not use its code or branding.


## What it demonstrates

- **Planning with hard limits**: ≤ 8 hypotheses, ≤ 12 source queries, ≤ 2 repairs per invalid plan, a step budget and a model-cost ceiling. When a budget runs out, it returns a partial report.
- **Constrained data tools**: a strict `QueryPlan` schema → catalog validation → SQLAlchemy compiler over allowlisted views → bound parameters, plus row, byte and time limits.
- **Tenant isolation in the database**: RLS is forced on every source table, the query role is non-owner and `NOBYPASSRLS`, and the tenant context is transaction-local. These are tested against the real database, including the pooled-connection case.
- **Hypothesis revision**: the analyst updates hypothesis statuses from evidence and adds follow-up drill-downs. The critic is a separate role.
- **Deterministic verification**: every numeric assertion is recomputed from stored, hash-addressed evidence. Unbacked numbers and unhedged causal wording are rejected. A planted "advertising caused it" recommendation is rejected on screen.
- **Durability**: a Postgres job queue with leases, heartbeats and fencing tokens, plus LangGraph Postgres checkpoints. If you kill the worker mid-run, the same investigation resumes without duplicating queries.
- **Human publication review**: request changes → a new version → approve → HTML/PDF export with evidence references. Everything is audited.

## Quick start (demo mode, no API keys)

```bash
cp .env.example .env            # then replace every change-me value
docker compose --profile app up -d --build
docker compose --profile app run --rm migrate python -m app.cli seed-demo   # prints demo passwords once
# open http://localhost:8080 and sign in as analyst@acme.demo
```

Developer setup, Windows/Ubuntu commands, enabling a real model, deployment, backup/restore and troubleshooting
are in [docs/RUNBOOK.md](docs/RUNBOOK.md). The five-minute walkthrough is in [docs/DEMO.md](docs/DEMO.md).

## Results (fixture mode, held-out synthetic scenarios)

| System | Driver identification | Numeric accuracy | Unsupported causal claims | Avg queries |
|---|---|---|---|---|
| Fixed query dashboard | 20% | 100% | 0 | 4.0 |
| Single-pass data agent | 40% | 100% | 18 | 5.0 |
| Full investigation graph | 100% | 100% | 0 | 8.8 |

20 held-out scenarios across 10 families (product demand drop, region decline, price cut, discount increase, refund
increase, missing ingestion batch, duplicates, cancellations, zero baseline, unchanged metric with an irrelevant
campaign change). **These results were produced with the deterministic fixture provider.** The analyst rules were
developed on the development split, so this run validates the pipeline and controls, not language-model
reasoning. Real-model results have not been measured yet. See [docs/EVALUATION_REPORT.md](docs/EVALUATION_REPORT.md)
and [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md).

## Stack

Python 3.12, FastAPI, Pydantic 2, SQLAlchemy 2.1, Alembic, psycopg 3, LangGraph 1.2 (Postgres checkpointer),
PostgreSQL 17, pytest, Ruff, mypy, uv · React 19, TypeScript, Vite 8, Tailwind CSS 4, Recharts 3, npm (lockfile)
· Docker Compose, Caddy (TLS), nginx · GitHub Actions.

## Documentation

[Architecture, state transitions, tool contracts, data dictionary](docs/ARCHITECTURE.md) ·
[Metric catalog](docs/METRIC_CATALOG.md) · [Security & threat model](docs/SECURITY.md) ·
[API](docs/API.md) / [OpenAPI](docs/openapi.json) · [Runbook](docs/RUNBOOK.md) · [Demo](docs/DEMO.md) ·
[Evaluation](docs/EVALUATION_REPORT.md) · [Status](docs/IMPLEMENTATION_STATUS.md)

## Resume bullet template

> Built *Metric Investigator*, a multi-tenant agent for business-metric investigation (FastAPI, LangGraph,
> PostgreSQL RLS, React). Model output is limited to validated query plans, and every reported number is
> recomputed from a hash-addressed evidence ledger before human approval. On [N] held-out synthetic scenarios in
> [real-model: MODEL] mode it identified the driver in [X]% of cases with [Y] unsupported causal claims, against
> [A]% for a fixed-query dashboard baseline and [B]% for a single-pass agent, at [C] queries and [cost] per run.
> The security suite recorded 0 cross-tenant reads.

Replace the brackets with your own measured numbers. The fixture-mode figures above are not model results.
