#!/usr/bin/env bash
set -euo pipefail

# Start Lightpanda CDP server via Docker on 127.0.0.1:9222.
# Ref: https://github.com/lightpanda-io/browser

CONTAINER_NAME="${LIGHTPANDA_CONTAINER_NAME:-lightpanda}"
CDP_HOST="${LIGHTPANDA_CDP_HOST:-127.0.0.1}"
CDP_PORT="${LIGHTPANDA_CDP_PORT:-9222}"
IMAGE_TAG="${LIGHTPANDA_IMAGE_TAG:-nightly}"
IMAGE="lightpanda/browser:${IMAGE_TAG}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found. Please install Docker first."
  exit 1
fi

if docker ps --format '{{.Names}}' | grep -Fxq "${CONTAINER_NAME}"; then
  echo "Lightpanda container already running: ${CONTAINER_NAME}"
else
  if docker ps -a --format '{{.Names}}' | grep -Fxq "${CONTAINER_NAME}"; then
    docker rm -f "${CONTAINER_NAME}" >/dev/null
  fi

  docker run -d \
    --name "${CONTAINER_NAME}" \
    -p "${CDP_HOST}:${CDP_PORT}:9222" \
    -e LIGHTPANDA_DISABLE_TELEMETRY=true \
    "${IMAGE}" >/dev/null
fi

echo "Lightpanda CDP server is expected at: http://${CDP_HOST}:${CDP_PORT}"
echo "Probe: curl -s http://${CDP_HOST}:${CDP_PORT}/json/version"
