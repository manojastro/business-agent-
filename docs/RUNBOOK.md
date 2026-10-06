# Runbook

All commands run from the repository root unless noted. Never put real secrets in commands you share; use `.env`.

## 1. Local setup

### Windows PowerShell + Docker Desktop

```powershell
# prerequisites: Docker Desktop running, uv (https://docs.astral.sh/uv/), Node 22 + npm
Copy-Item .env.example .env
# edit .env: replace every change-me value (python -c "import secrets;print(secrets.token_urlsafe(32))")
docker compose up -d db                          # Postgres 17 on 127.0.0.1:55432, roles + databases created on first start
cd backend
uv sync --frozen                                 # locked Python dependencies
uv run python -m app.cli migrate                 # app + analytics migrations + checkpoint tables
uv run python -m app.cli seed-demo               # 2 demo tenants (~66k orders) + users; prints passwords once
uv run python -m app.cli seed-evals              # optional: 40 evaluation tenants (dev + held-out)
uv run pytest                                    # meaningful tests against the real databases
# run the app (3 terminals)
uv run uvicorn app.main:app --port 8000
uv run python -m app.workers.main
cd ..\frontend; npm ci; npm run dev              # http://localhost:5173
```

### Ubuntu 22.04/24.04 + Docker Engine

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 curl
curl -LsSf https://astral.sh/uv/install.sh | sh
cp .env.example .env && nano .env                # replace every change-me value
docker compose up -d db
cd backend && uv sync --frozen && uv run python -m app.cli migrate && uv run python -m app.cli seed-demo
uv run pytest
```

### Full stack in containers (either OS)

```bash
docker compose --profile app up -d --build       # db, migrate, api, worker, web, proxy -> http://localhost:8080
docker compose --profile app run --rm migrate python -m app.cli seed-demo
docker compose --profile app logs -f api worker
docker compose --profile app down                # stop (data volume kept)
docker compose --profile app down -v             # teardown INCLUDING the database volume (destructive)
```

Demo users: `admin@demo.local`, `analyst@acme.demo`, `reviewer@acme.demo`, `analyst@bharat.demo`,
`reviewer@bharat.demo`. Set `DEMO_USER_PASSWORD` (≥ 12 chars) before seeding to choose a shared demo password;
otherwise random ones are printed once. Rotate with `seed-demo --skip-data --reset-passwords`.

## 2. Enable a real model

```dotenv
MODEL_MODE=real
MODEL_PROVIDER=azure_openai            # or openai_compatible
MODEL_NAME=<deployment name or model id>
MODEL_ENDPOINT=https://<resource>.openai.azure.com
MODEL_API_KEY=<secret>
MODEL_INPUT_COST_PER_1K=<from your price sheet>
MODEL_OUTPUT_COST_PER_1K=<from your price sheet>
MAX_RUN_COST=0.50
```

Startup fails with a clear `ConfigError` if any of name/endpoint/key is missing. Individual investigations can
still be created in fixture mode via the API (`"model_mode": "fixture"`). The UI shows provider, model, tokens and cost
per model call; fixture runs carry a **Demo simulation** label everywhere.

## 3. Reproduce the evaluation

```bash
cd backend
uv run python -m app.cli seed-evals
uv run python -m app.cli run-evals --suite synthetic-development --report ../docs/EVALUATION_REPORT_dev.md
uv run python -m app.cli run-evals --suite synthetic-heldout --report ../docs/EVALUATION_REPORT.md
uv run python -m app.cli run-evals --suite synthetic-heldout --limit 10 --model-mode real   # credentialed, costs money
```

Admins can also start runs from Administration → Evaluation (executed by the worker).

## 4. Deploy to a single Ubuntu VM

Provisional budget: **2 vCPU, 4 GB RAM, 30 GB disk** (e.g. Azure B2s). Measured on the demo workload (both demo
tenants + 40 evaluation tenants, idle after a run): db 282 MiB, api 101 MiB, worker 94 MiB, proxy 14 MiB, web 11 MiB
(≈ 0.5 GB total); a fixture-mode investigation takes 3–4 s and ≤ 12 source queries. Real-model runs are bounded by
the provider's latency, not local resources.

```bash
# DNS A record -> VM public IP; open ports 80/443 only (no 5432).
git clone https://github.com/manojastro/business-agent-.git metric-investigator && cd metric-investigator
cp .env.example .env && nano .env
#   APP_ENV=production, SITE_ADDRESS=metrics.example.com, APP_BASE_URL=https://metrics.example.com,
#   CORS_ORIGINS=https://metrics.example.com, strong unique passwords and SESSION_SECRET
docker compose -f compose.yaml -f compose.prod.yaml --profile app up -d --build
docker compose -f compose.yaml -f compose.prod.yaml --profile app run --rm migrate python -m app.cli seed-demo
BASE_URL=https://metrics.example.com SMOKE_PASSWORD=... python3 scripts/smoke.py
```

Caddy obtains and renews the certificate for `SITE_ADDRESS`. Logs rotate (json-file, 5 × 10 MB per service).
Nothing is published externally or purchased by any script; creating the VM/DNS is a manual, authorized step.

**Optional Azure containers**: build/push the `backend` and `frontend` images to Azure Container Registry, run
api and worker as two Azure Container Apps (worker with min replicas 1, no ingress), web behind Container Apps
ingress, and use Azure Database for PostgreSQL Flexible Server. Run `infra/postgres/init/01-roles.sh` logic once as
the server admin (Flexible Server supports RLS and NOBYPASSRLS roles). Keep the database on a private endpoint.

## 5. Create an admin safely

```bash
cd backend
ADMIN_PASSWORD='<at least 12 chars, from a password manager>' uv run python -m app.cli create-admin --email you@company.com --tenant acme-retail
# or omit ADMIN_PASSWORD to be prompted without echo
```

The action is audited. There is no default admin password.

## 6. Inspect a failed run

1. UI: open the investigation → Progress → enable "tool and model calls". Errors appear as red `error`/`model_error`
   events with the failing node.
2. API: `GET /api/v1/investigations/{id}` (`error`), `/steps`, `/evidence` (query plans include validation errors).
3. Database: `SELECT status, attempts, last_error FROM jobs WHERE investigation_id = '<id>';`
4. Worker logs: `docker compose logs worker --since 1h`.
5. Rerun from the UI ("Rerun") or `POST /api/v1/investigations/{id}/rerun` (a new investigation id; the old one stays as evidence).

A job that crashes is reclaimed after `WORKER_LEASE_SECONDS` and resumes from the last checkpoint. Transient
failures are retried up to 5 attempts with exponential backoff; then the investigation is marked `failed`.

## 7. Backup and restore

```bash
scripts/backup.sh                        # backups/<UTC timestamp>/metric_app.dump, metric_analytics.dump, SHA256SUMS; keeps 14
scripts/restore.sh backups/<ts> --yes    # stops api+worker, verifies checksums, restores each DB as its owner, restarts
python3 scripts/smoke.py                 # verify
```

Windows: `powershell -File scripts/backup.ps1`. Schedule backups with cron/Task Scheduler and copy them off the VM.
Retention for evidence and checkpoints: evidence is the audit record and is kept for the life of the report; to
prune old evaluation runs, delete `investigations` of `eval` tenants older than N days together with their
dependent rows and the matching `checkpoints` thread ids (run inside a transaction after a backup).

## 8. Release rollback

1. Every release is a git tag. Before upgrading: `scripts/backup.sh`.
2. Deploy: `git checkout vX.Y.Z && docker compose -f compose.yaml -f compose.prod.yaml --profile app up -d --build`.
3. Roll back the application: `git checkout <previous tag>` and the same `up -d --build`.
4. If the release included a migration, follow §9 **before** starting the old code.

## 9. Migration recovery (reviewed procedure)

1. Migrations run in the `migrate` one-shot container; api/worker start only if it succeeds.
2. Each migration runs in one transaction (Postgres transactional DDL), so a failed migration leaves the schema unchanged — fix and redeploy.
3. To revert an applied migration: stop api/worker, take a backup, then
   `docker compose --profile app run --rm migrate alembic -n app downgrade -1` (or `-n analytics`). Review the
   migration's `downgrade()` first; downgrades that drop data are refused in review.
4. If a downgrade is not safe, restore the pre-release backup (§7) and redeploy the previous tag.

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| `ConfigError: SESSION_SECRET still has the placeholder value` | Set a real value in `.env`. |
| `MODEL_MODE=real requires ...` | Set the model variables or use fixture mode. |
| Port 5432/55432 busy | Change `DB_PORT` in `.env` and the host URLs. |
| `connection refused` to 127.0.0.1:55432 | `docker compose up -d db`; on Windows prefer 127.0.0.1 over `localhost` (IPv6 delay). |
| Source queries time out | `ANALYZE` runs after each load; re-run `seed-demo`, or raise `SOURCE_STATEMENT_TIMEOUT_MS`. |
| Investigation stuck in `queued` | Worker not running: `docker compose logs worker`; `SELECT * FROM worker_heartbeats`. |
| Overview shows "stale" | The as-of date is beyond the data; demo data ends 2026-08-31 (as-of 2026-09-01). |
| Roles missing after changing passwords | Role passwords are set only on first volume init; `ALTER ROLE ... PASSWORD` or recreate the volume. |
| `401` after restart | Sessions are server-side and survive restarts; a `401` means expiry (12 h) — sign in again. |
