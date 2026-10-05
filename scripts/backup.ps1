# Windows PowerShell equivalent of scripts/backup.sh. Run from the repository root.
$ErrorActionPreference = "Stop"
$ts = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$out = "backups/$ts"
New-Item -ItemType Directory -Force $out | Out-Null
foreach ($db in "metric_app", "metric_analytics") {
  docker compose exec -T db sh -c "pg_dump -U `$POSTGRES_USER -Fc $db" | Set-Content -AsByteStream "$out/$db.dump"
}
Get-FileHash "$out/*.dump" -Algorithm SHA256 | Format-Table -AutoSize | Out-File "$out/SHA256SUMS.txt"
Write-Host "backup written to $out"
