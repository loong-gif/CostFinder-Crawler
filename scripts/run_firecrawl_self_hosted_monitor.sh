#!/bin/sh
set -eu

BASE="${FIRECRAWL_MONITOR_BASE:-/home/loong/costfinder_monitor}"
mkdir -p "$BASE/logs" "$BASE/state"
exec >>"$BASE/logs/monitor.log" 2>&1
date -u
export FIRECRAWL_SELF_HOSTED_API_URL="http://127.0.0.1:3002"
export PYTHONUNBUFFERED=1
exec "$BASE/.venv/bin/python" "$BASE/scripts/run_monitor_price_sync.py" \
  --config "$BASE/config/firecrawl_monitor_migration.json" \
  --state-dir "$BASE/state"
