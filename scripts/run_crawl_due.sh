#!/bin/sh
# Daily scheduled crawl: plan due businesses, capture them with the monitor in capture-only mode
# (separate state dir, no plans and no pending SQL), then record the results into the schedule.
set -eu
BASE="${COSTFINDER_BASE:-/home/loong/costfinder_monitor}"
CAP="${CRAWL_DUE_CAP:-30}"
mkdir -p "$BASE/logs" "$BASE/state/due_monitor"
exec >>"$BASE/logs/crawl_due.log" 2>&1
date -u
export PYTHONUNBUFFERED=1
cd "$BASE"
PY="$BASE/.venv/bin/python"
SINCE="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"

"$PY" scripts/promo_pipeline.py crawl-schedule plan --management-api --apply --cap "$CAP"
NAMES="$("$PY" -c "import json;print(','.join(json.load(open('state/crawl_schedule/due_monitor.json'))['monitors']))")"
if [ -z "$NAMES" ]; then
  echo "nothing due"
  exit 0
fi

ARGS=""
for n in $(echo "$NAMES" | tr ',' ' '); do ARGS="$ARGS --monitor $n"; done
# Exit code 2 only means some pages failed to capture; the bridge records those per business.
# shellcheck disable=SC2086
"$PY" scripts/promo_pipeline.py monitor --config state/crawl_schedule/due_monitor.json \
  --state-dir state/due_monitor --capture-only $ARGS || true
"$PY" scripts/promo_pipeline.py crawl-record --state-dir state/due_monitor --due state/crawl_schedule/due.json \
  --monitors "$NAMES" --since "$SINCE" --out state/crawl_schedule/results.json
"$PY" scripts/promo_pipeline.py crawl-schedule record --results state/crawl_schedule/results.json --apply
date -u
