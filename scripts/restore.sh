#!/usr/bin/env bash
# Restore both databases from a backup directory. DESTRUCTIVE: replaces current data.
# Usage: scripts/restore.sh backups/<timestamp> --yes
# Each database is restored by its owning role so ownership, RLS policies and grants stay intact.
set -euo pipefail
cd "$(dirname "$0")/.."
dir=${1:?backup directory required}
[ "${2:-}" = "--yes" ] || { echo "refusing to restore without --yes"; exit 2; }
(cd "$dir" && sha256sum -c SHA256SUMS)
docker compose stop api worker
docker compose exec -T db sh -c 'pg_restore -U "$APP_DB_USER" -d metric_app --clean --if-exists --no-owner' < "$dir/metric_app.dump"
docker compose exec -T db sh -c 'pg_restore -U "$ANALYTICS_OWNER_USER" -d metric_analytics --clean --if-exists --no-owner' < "$dir/metric_analytics.dump"
docker compose start api worker
echo "restored from $dir; run scripts/smoke.sh to verify"
