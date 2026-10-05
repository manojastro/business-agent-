#!/usr/bin/env bash
# Logical backup of both databases (custom format) into ./backups/<timestamp>/.
# Usage: scripts/backup.sh            (run from the repository root on the Docker host)
set -euo pipefail
cd "$(dirname "$0")/.."
ts=$(date -u +%Y%m%dT%H%M%SZ)
out="backups/$ts"
mkdir -p "$out"
for db in metric_app metric_analytics; do
  docker compose exec -T db sh -c "pg_dump -U \"\$POSTGRES_USER\" -Fc $db" > "$out/$db.dump"
done
sha256sum "$out"/*.dump > "$out/SHA256SUMS"
echo "backup written to $out"
# Retention: keep the 14 most recent backups.
ls -1dt backups/*/ 2>/dev/null | tail -n +15 | xargs -r rm -rf
