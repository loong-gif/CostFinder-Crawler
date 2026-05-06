#!/usr/bin/env bash
set -euo pipefail

# Run CostFinder with an external CDP browser endpoint (e.g. Lightpanda).
# This script assumes your virtualenv is at .venv and dependencies are installed.

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_ROOT}"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Missing virtualenv interpreter: .venv/bin/python"
  echo "Create it first: uv venv .venv --python 3.11"
  exit 1
fi

export CRAWL4_AI_BASE_DIRECTORY="${PROJECT_ROOT}"
export PLAYWRIGHT_BROWSERS_PATH="${PROJECT_ROOT}/.playwright_browsers"
export CRAWL4AI_CDP_URL="${CRAWL4AI_CDP_URL:-http://127.0.0.1:9222}"
# Disable inherited proxy env to avoid accidental local proxy interception.
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
unset AGENT_BROWSER_PROXY PLAYWRIGHT_MCP_PROXY_SERVER npm_config_proxy npm_config_https_proxy

echo "Using CRAWL4AI_CDP_URL=${CRAWL4AI_CDP_URL}"
echo "Starting crawler..."

".venv/bin/python" main.py "$@"
