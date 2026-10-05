#!/usr/bin/env bash
# Deployment smoke check: login, source freshness, one investigation, evidence access, export.
# Usage: BASE_URL=https://metrics.example.com SMOKE_EMAIL=... SMOKE_REVIEWER=... SMOKE_PASSWORD=... scripts/smoke.sh
set -euo pipefail
exec python3 "$(dirname "$0")/smoke.py"
