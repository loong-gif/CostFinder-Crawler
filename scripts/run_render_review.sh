#!/bin/sh
# Usage: scripts/run_render_review.sh <shard.json> <out.json> [shots_dir]
# Runs one render-review shard in ego-lite. Paths are made absolute and injected into the script.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
abs() { case "$1" in /*) echo "$1" ;; *) echo "$(pwd)/$1" ;; esac; }
SHARD="$(abs "$1")"; OUT="$(abs "$2")"; SHOTS="$(abs "${3:-$(dirname "$OUT")/shots}")"
mkdir -p "$SHOTS"
{
  printf 'globalThis.RR = {"shard": "%s", "out": "%s", "shots": "%s"};\n' "$SHARD" "$OUT" "$SHOTS"
  cat "$HERE/render_review.js"
} | ego-browser nodejs
