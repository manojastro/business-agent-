# API

Machine-readable specification: [openapi.json](openapi.json) (also served at `/api/openapi.json`, docs UI at
`/api/docs`). Regenerate: `uv run python -m app.cli export-openapi ../docs/openapi.json`.

## Conventions

- **Versioning**: all routes under `/api/v1`. Breaking changes ship as `/api/v2` alongside v1 for at least one release.
- **Auth**: `POST /auth/login` sets an HttpOnly `mi_session` cookie and returns `csrf_token`. Every non-GET request
  must send `X-CSRF-Token`.
- **Errors**: `{"error": {"code": "stable_code", "message": "...", "details": ...}}`. Codes include
  `unauthenticated`, `invalid_credentials`, `csrf_failed`, `forbidden`, `not_found`, `validation_error`,
  `invalid_windows`, `unknown_metric`, `not_in_review`, `stale_version`, `comment_required`, `not_approved`,
  `rate_limited`, `payload_too_large`, `real_model_not_configured`.
- **Pagination**: `limit` (≤ 100) and `offset`; responses include `next_offset` (null at the end). Steps use a cursor `after`.
- **Rate limits**: 120 requests/min per client (login 10/min) → `429 rate_limited`.

## Examples

```http
POST /api/v1/auth/login
{"email": "analyst@acme.demo", "password": "<from seed output>"}
200 {"user": {...}, "active_tenant": {"slug": "acme-retail", "role": "analyst", ...}, "csrf_token": "…", "model_mode": "fixture"}
```

```http
POST /api/v1/investigations
X-CSRF-Token: …
Idempotency-Key: 6c1d…        (same key → same investigation, created=false)
{"question": "Why did net sales fall in the last complete seven days compared with the preceding seven days?",
 "metric_key": "net_sales", "as_of": "2026-09-01"}
202 Location: /api/v1/investigations/7d9f…
{"id": "7d9f…", "status": "queued", "status_url": "/api/v1/investigations/7d9f…",
 "events_url": "/api/v1/investigations/7d9f…/events", "created": true}
```

```http
GET /api/v1/investigations/7d9f…/events          (text/event-stream; reconnect with Last-Event-ID to replay)
id: 1042
event: step
data: {"id":1042,"node":"baseline","kind":"baseline","title":"Baseline calculated: -1186440.98 (-10.72%)", ...}

event: status
data: {"status":"awaiting_review"}
```

Polling fallback: `GET /api/v1/investigations/{id}/steps?after=1042`.

```http
POST /api/v1/investigations/{id}/clarification
{"metric_key": "net_sales"}          or        {"choice": "continue"}  |  {"choice": "stop"}
```

```http
POST /api/v1/investigations/{id}/report/decision      (reviewer/admin, not the owner)
{"decision": "request_changes", "comment": "State the refund maturity caveat.", "version": 1}
```

```http
GET /api/v1/investigations/{id}/report/export?format=pdf    (approved reports only; audited)
```

## Endpoint list

| Method | Path | Role |
|---|---|---|
| GET | `/health/live`, `/health/ready` | public |
| POST | `/auth/login`, `/auth/logout`; GET `/auth/session`; POST `/auth/switch-tenant`; GET `/auth/oidc/config` | — |
| GET | `/overview`, `/source/freshness`, `/source/quality` | member |
| GET | `/metrics`; POST `/metrics/{key}/versions` | member / admin |
| GET | `/connections`; PATCH `/connections/{id}` | member / admin |
| POST/GET | `/investigations` | analyst, admin / member |
| GET | `/investigations/{id}`, `/steps`, `/events`, `/evidence`, `/claims`, `/report` | member |
| POST | `/investigations/{id}/clarification`, `/cancel`, `/rerun` | owner or admin |
| POST | `/investigations/{id}/report/decision` | reviewer, admin (not owner) |
| GET | `/investigations/{id}/report/export` | member (approved only) |
| GET | `/evidence`, `/evidence/{id}` | member (SQL for analyst/admin only) |
| GET | `/reports` | member |
| GET/POST/PATCH/DELETE | `/admin/members…`, GET `/admin/audit` | admin |
| GET | `/eval/suites`, `/eval/runs`; GET `/eval/runs/{id}`, POST `/eval/runs` | member / admin |
