#!/bin/sh
set -eu

BASE="${FIRECRAWL_MONITOR_BASE:-/home/loong/costfinder_monitor}"
mkdir -p "$BASE/logs" "$BASE/state"
exec >>"$BASE/logs/monitor.log" 2>&1
date -u
exec python3 "$BASE/scripts/firecrawl_self_hosted_monitor.py" \
  --config "$BASE/config/firecrawl_monitor_migration.json" \
  --state-dir "$BASE/state" \
  --api-url "${FIRECRAWL_SELF_HOSTED_API_URL:-http://127.0.0.1:3002}" \
  --workers "${FIRECRAWL_MONITOR_WORKERS:-3}" \
  --timeout "${FIRECRAWL_HTTP_TIMEOUT_SECS:-120}"
