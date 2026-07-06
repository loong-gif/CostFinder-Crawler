#!/usr/bin/env bash
# Bootstrap self-hosted Firecrawl on WSL2 (Ubuntu) + Windows portproxy/firewall.
# Idempotent-ish: safe to re-run after fixing failures.
#
# Usage (in WSL on the Windows VPS):
#   OPENAI_API_KEY=sk-... ALLOWED_CLIENT_IP=58.44.21.62 bash scripts/bootstrap_firecrawl_wsl.sh
#
# Optional env:
#   FIRECRAWL_VERSION   default: v2.11.9
#   FIRECRAWL_PORT      default: 3002
#   FIRECRAWL_DIR         default: ~/firecrawl
#   ALLOWED_CLIENT_IP   dev machine egress IPv4 for Windows firewall (required)
#   THINKBOOK_SSH_PUBKEY  optional: append thinkbook agent key for remote SSH

set -euo pipefail

FIRECRAWL_VERSION="${FIRECRAWL_VERSION:-v2.11.9}"
FIRECRAWL_PORT="${FIRECRAWL_PORT:-3002}"
FIRECRAWL_DIR="${FIRECRAWL_DIR:-$HOME/firecrawl}"
OPENAI_API_KEY="${OPENAI_API_KEY:-}"
ALLOWED_CLIENT_IP="${ALLOWED_CLIENT_IP:-}"
THINKBOOK_SSH_PUBKEY="${THINKBOOK_SSH_PUBKEY:-ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGyYA52oQLR0mhTkTc4Ug9x6s7a12Z1KG9VWQtG5n/MQ loong@thinkbook}"

log() { echo "==> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

[[ -n "$OPENAI_API_KEY" ]] || die "OPENAI_API_KEY required (judging LLM)"
[[ -n "$ALLOWED_CLIENT_IP" ]] || die "ALLOWED_CLIENT_IP required (dev machine egress IPv4 for firewall)"

log "Preflight"
lsb_release -a 2>/dev/null || true
free -h
nproc
df -h /
command -v docker >/dev/null 2>&1 && docker --version || echo "docker: not installed"

install_docker() {
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    log "Docker already installed and running"
    return
  fi

  log "Installing Docker CE"
  sudo apt-get remove -y docker docker-engine docker.io containerd runc 2>/dev/null || true
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl gnupg lsb-release git

  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg

  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

  if ! getent group docker >/dev/null; then
    sudo groupadd docker
  fi
  sudo usermod -aG docker "$USER" || true

  if command -v systemctl >/dev/null 2>&1 && systemctl is-system-running >/dev/null 2>&1; then
    sudo systemctl enable --now docker
  else
    sudo service docker start || true
  fi

  if ! docker info >/dev/null 2>&1; then
    die "Docker daemon not reachable; try: sudo service docker start"
  fi

  docker run --rm hello-world >/dev/null
  log "Docker OK"
}

clone_and_configure() {
  if [[ ! -d "$FIRECRAWL_DIR/.git" ]]; then
    log "Cloning Firecrawl"
    git clone https://github.com/firecrawl/firecrawl.git "$FIRECRAWL_DIR"
  fi

  cd "$FIRECRAWL_DIR"
  git fetch --tags --quiet
  git checkout "$FIRECRAWL_VERSION"

  if [[ ! -f .env ]]; then
    cp .env.example .env
  fi

  local bull_key
  bull_key="$(openssl rand -hex 32)"

  upsert_env() {
    local key="$1" val="$2"
    python3 - "$key" "$val" <<'PY'
import sys
from pathlib import Path
key, val = sys.argv[1], sys.argv[2]
path = Path(".env")
lines = path.read_text().splitlines() if path.exists() else []
out, found = [], False
for line in lines:
    if line.startswith(f"{key}="):
        out.append(f"{key}={val}")
        found = True
    else:
        out.append(line)
if not found:
    out.append(f"{key}={val}")
path.write_text("\n".join(out) + "\n")
PY
  }

  upsert_env PORT "$FIRECRAWL_PORT"
  upsert_env HOST "0.0.0.0"
  # ponytail: monitor needs Postgres tables; scrape auth stays bypassed (no Supabase).
  upsert_env USE_DB_AUTHENTICATION "false"
  upsert_env DATABASE_URL "postgresql://postgres:postgres@nuq-postgres:5432/postgres"
  upsert_env DATABASE_REPLICA_URL "postgresql://postgres:postgres@nuq-postgres:5432/postgres"
  upsert_env BULL_AUTH_KEY "$bull_key"
  upsert_env OPENAI_API_KEY "$OPENAI_API_KEY"

  log "Configured .env (PORT=$FIRECRAWL_PORT, OPENAI_API_KEY set, BULL_AUTH_KEY rotated)"
}

start_firecrawl() {
  cd "$FIRECRAWL_DIR"
  log "Starting Firecrawl (first run may take several minutes to build images)"
  docker compose up -d --build
  docker compose ps

  local fix_script
  fix_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fix_firecrawl_selfhosted_monitor.sh"
  if [[ -f "$fix_script" ]]; then
    log "Applying self-hosted monitor patches"
    bash "$fix_script"
  else
    log "WARN: $fix_script not found — monitor API may not work until you run it"
  fi

  log "Waiting for API on localhost:$FIRECRAWL_PORT"
  for i in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:${FIRECRAWL_PORT}/" >/dev/null 2>&1 \
      || curl -sf -X POST "http://127.0.0.1:${FIRECRAWL_PORT}/v2/scrape" \
        -H "Content-Type: application/json" -d '{"url":"https://example.com"}' >/dev/null 2>&1; then
      break
    fi
    sleep 5
  done

  log "Smoke test: scrape"
  curl -sf -X POST "http://127.0.0.1:${FIRECRAWL_PORT}/v2/scrape" \
    -H "Content-Type: application/json" \
    -d '{"url":"https://example.com"}' | head -c 400
  echo

  log "Smoke test: monitor create"
  local monitor_resp monitor_id
  monitor_resp="$(curl -sf -X POST "http://127.0.0.1:${FIRECRAWL_PORT}/v2/monitor" \
    -H "Content-Type: application/json" \
    -d '{"name":"self-hosted smoke","schedule":{"text":"daily","timezone":"UTC"},"targets":[{"type":"scrape","urls":["https://example.com"]}]}')"
  echo "$monitor_resp" | head -c 400
  echo
  monitor_id="$(echo "$monitor_resp" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id') or d.get('data',{}).get('id') or '')" 2>/dev/null || true)"
  if [[ -n "$monitor_id" ]]; then
    log "Monitor id=$monitor_id — waiting 90s for first check"
    sleep 90
    curl -sf "http://127.0.0.1:${FIRECRAWL_PORT}/v2/monitor/${monitor_id}/checks" | head -c 600
    echo
  fi
}

wsl_ip() {
  ip -4 addr show eth0 2>/dev/null | grep -oP 'inet \K[0-9.]+' | head -1
}

setup_windows_network() {
  local wsl_ip="$1"
  [[ -n "$wsl_ip" ]] || die "Could not detect WSL eth0 IPv4"

  log "Configuring Windows portproxy 0.0.0.0:${FIRECRAWL_PORT} -> ${wsl_ip}:${FIRECRAWL_PORT}"
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "
    \$port = ${FIRECRAWL_PORT}
    \$wslIp = '${wsl_ip}'
    \$clientIp = '${ALLOWED_CLIENT_IP}'
    \$pubKey = '${THINKBOOK_SSH_PUBKEY}'

    # Append thinkbook SSH key if missing (for remote automation)
    \$keyFile = 'C:\\ProgramData\\ssh\\administrators_authorized_keys'
    if (Test-Path \$keyFile) {
      \$content = Get-Content \$keyFile -Raw
      if (\$content -notmatch [regex]::Escape(\$pubKey.Split()[0])) {
        Add-Content -Path \$keyFile -Value \$pubKey -Encoding Ascii
        icacls.exe \$keyFile /inheritance:r /grant 'SYSTEM:(F)' /grant 'Administrators:(F)' | Out-Null
      }
    }

    netsh interface portproxy delete v4tov4 listenport=\$port listenaddress=0.0.0.0 2>\$null | Out-Null
    netsh interface portproxy add v4tov4 listenport=\$port listenaddress=0.0.0.0 connectport=\$port connectaddress=\$wslIp
    netsh interface portproxy show v4tov4

    \$ruleName = 'Firecrawl ${FIRECRAWL_PORT}'
    Get-NetFirewallRule -DisplayName \$ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName \$ruleName -Direction Inbound -Protocol TCP -LocalPort \$port -RemoteAddress \"\${clientIp}/32\" -Action Allow -Enabled True | Out-Null
    Write-Host \"Firewall rule '\$ruleName' allows \$clientIp -> TCP \$port\"
  "
}

install_docker
clone_and_configure
start_firecrawl

WSL_IP="$(wsl_ip)"
setup_windows_network "$WSL_IP"

cat <<EOF

================================================
Firecrawl self-hosted bootstrap complete.

WSL API:     http://127.0.0.1:${FIRECRAWL_PORT}
Windows/VPS: http://72.52.161.65:${FIRECRAWL_PORT}  (from ${ALLOWED_CLIENT_IP} only)

CostFinder-Crawler .env:
  FIRECRAWL_API_URL=http://72.52.161.65:${FIRECRAWL_PORT}
  FIRECRAWL_API_KEY=self-hosted

Note: WSL IP changes on reboot — re-run setup_windows_network or this script.
================================================
EOF
