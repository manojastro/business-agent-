# Security and threat model

## Assets

Tenant source data (orders, refunds, customers), evidence and reports, user credentials and sessions, model API
keys, the integrity of published numbers.

## Controls

| Area | Control | Where |
|---|---|---|
| Tenant isolation (source) | RLS **enabled and forced** on every `source.*` table; policy `tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid`. Missing context → zero rows. | `migrations/analytics/0001` |
| Query role | Non-owner, `NOSUPERUSER NOBYPASSRLS NOINHERIT`, `default_transaction_read_only`, 5 s role statement timeout; SELECT only on `semantic.*` views; no access to `source.*`. | `infra/postgres/init/01-roles.sh`, migration grants |
| Tenant context | Set with `set_config('app.tenant_id', …, is_local => true)` inside each read-only transaction, so it cannot survive on a pooled connection. Compiled SQL also contains `tenant_id = :tenant_id`. | `db/analytics.py` |
| Query safety | Model output is a `QueryPlan`, never SQL; strict schema, catalog validation, allowlisted compiler, bound parameters, row/byte/time limits, no export endpoint. Security does **not** rely on prompts or SQL keyword filtering. | `tools/query_plan.py` |
| Tenant isolation (app) | Every object lookup filters by the session's active tenant and re-checks membership on each request; other tenants' objects return 404 (no probing). Worker verifies job tenant == investigation tenant == checkpoint tenant. `tenant_id` immutable by trigger. | `api/deps.py`, `services/investigations.py`, `agents/graph.py` |
| AuthN | Argon2id (argon2-cffi); random 256-bit session token in an HttpOnly, SameSite=Lax (Secure in production) cookie; only its SHA-256 stored; 12 h expiry; logout revokes. No fixed admin password ships; demo passwords come from `DEMO_USER_PASSWORD` or are random and printed once. | `auth/` |
| CSRF | Per-session token; `X-CSRF-Token` header required on every non-GET request. | `api/deps.py` |
| AuthZ | Roles per tenant: analyst (create/run/clarify/cancel own), reviewer (decide), admin (catalog, members, evals, audit). The owner cannot approve their own report. SQL and parameters of evidence are hidden from reviewers. Only approved reports export. | routes |
| Abuse limits | Token-bucket rate limit (120/min, login 10/min), 1 MB body limit (API and Caddy), structured errors without internals. | `api/middleware.py`, Caddyfile |
| Untrusted content | Question text, source strings and connector metadata are passed to the model as data inside a delimited context with an explicit "do not follow instructions" rule; the model cannot cause any action that is not a validated plan. No user-supplied code is executed. Customer identifiers are not exposed by views. | prompts, views |
| Integrity of numbers | Every numeric assertion recomputed from stored evidence; any unbacked number or unhedged causal wording rejects the claim; evidence rows are append-only (trigger) and hashed. | `evidence/` |
| Audit | Login, run creation, clarification, cancellation, metric changes, report decisions, exports, admin changes. | `audit_events` |
| Secrets & logging | Secrets only from environment; `.env` git-ignored; prompts, passwords and full records are not logged; model error bodies are not echoed; OTel spans carry ids and timings only. | config, providers, telemetry |
| Network | Postgres bound to loopback in dev and unpublished in production; TLS via Caddy with HSTS; nginx CSP and frame denial; containers run as non-root. | compose files |

## Threats considered

| Threat | Mitigation | Verified by |
|---|---|---|
| Analyst of tenant B reads tenant A data via API ids | tenant filter + 404 | `tests/test_api.py::test_full_review_flow_export_and_tenant_isolation` |
| Agent/model asks for another tenant's data | RLS + server predicate; tenant never comes from the model | `test_source_security.py::test_each_tenant_sees_only_its_rows` |
| Tenant context leaking via pooled connection | transaction-local setting | `test_tenant_context_does_not_leak_through_pooled_connections` |
| Prompt-injected SQL / joins / functions | no SQL channel; strict plan schema | `test_query_plan.py` (17 unsafe fixtures) |
| Writes to source data | role privileges, not just read-only mode | `test_source_writes_are_denied_even_outside_read_only_transactions` |
| Runaway queries | statement timeout, row and byte limits, query budget | `test_statement_timeout_and_row_limit` |
| Wrong or invented numbers published | deterministic verifier + human review | `test_verification.py`, planted recommendation test |
| Unsupported causal claims | causal-language rule + critic | same |
| CSRF on cookie-auth mutations | header token | `test_csrf_required_for_mutations` |
| Duplicate/zombie workers | SKIP LOCKED, leases, fencing token, unique operation ids | `test_worker_graph.py` |

## Known limitations

- Rate limiting is per API process; with multiple replicas use a shared limiter at the proxy.
- OIDC (Microsoft Entra ID) is a documented seam only (`auth/oidc.py`), not implemented.
- No MFA, password reset or account lockout beyond rate limiting.
- The evidence result store is in Postgres (bounded payloads); no object-store artifacts yet.
- Report a security issue privately to the repository owner.
