#!/bin/sh
# Usage: scripts/run_render_review_deep.sh <shard.json> <out.json> [goto_timeout_ms]
# Runs the deep render review (expands collapsed panels, collects visible text and images) in ego-lite.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
abs() { case "$1" in /*) echo "$1" ;; *) echo "$(pwd)/$1" ;; esac; }
SHARD="$(abs "$1")"; OUT="$(abs "$2")"; TIMEOUT="${3:-60000}"
{
  printf 'globalThis.RR = {"shard": "%s", "out": "%s", "timeout": %s};\n' "$SHARD" "$OUT" "$TIMEOUT"
  cat "$HERE/render_review_deep.js"
} | ego-browser nodejs
