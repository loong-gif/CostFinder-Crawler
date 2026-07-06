#!/usr/bin/env bash
# Self-hosted Firecrawl v2.11.9: enable /v2/monitor without full Supabase.
# Run in WSL after `docker compose up -d --build` (api image must exist).
set -euo pipefail

FIRECRAWL_DIR="${FIRECRAWL_DIR:-$HOME/firecrawl}"
cd "$FIRECRAWL_DIR"
PATCH_DIR="$FIRECRAWL_DIR/patches"
mkdir -p "$PATCH_DIR"
BYPASS_TEAM_ID="00000000-0000-0000-0000-000000000001"

log() { echo "==> $*"; }

log "Patch connection.js: DB pools when DATABASE_URL is set"
docker run --rm firecrawl-api cat /app/dist/src/db/connection.js > "$PATCH_DIR/connection.js"
python3 - "$PATCH_DIR/connection.js" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
text = path.read_text()
old = """const useDbAuthentication = config_1.config.USE_DB_AUTHENTICATION;
const mainDb = useDbAuthentication
    ? makeDb(config_1.config.DATABASE_URL, "firecrawl-api")
    : null;
const replicaDb = useDbAuthentication
    ? makeDb(config_1.config.DATABASE_REPLICA_URL ?? config_1.config.DATABASE_URL, "firecrawl-api-rr")
    : null;"""
new = """const useDbAuthentication = config_1.config.USE_DB_AUTHENTICATION;
const enableDbPools = useDbAuthentication || !!config_1.config.DATABASE_URL;
const mainDb = enableDbPools
    ? makeDb(config_1.config.DATABASE_URL, "firecrawl-api")
    : null;
const replicaDb = enableDbPools
    ? makeDb(config_1.config.DATABASE_REPLICA_URL ?? config_1.config.DATABASE_URL, "firecrawl-api-rr")
    : null;"""
if old not in text:
    raise SystemExit("connection.js patch anchor not found")
text = text.replace(old, new, 1).replace(
    "if (useDbAuthentication && !mainDb) {",
    "if (enableDbPools && !mainDb) {",
    1,
)
path.write_text(text)
PY

log "Patch auth.js: bypass team_id must be valid UUID for monitor queries"
docker run --rm firecrawl-api cat /app/dist/src/controllers/auth.js > "$PATCH_DIR/auth.js"
sed -i "s/team_id: \"bypass\"/team_id: \"${BYPASS_TEAM_ID}\"/g" "$PATCH_DIR/auth.js"

log "Patch queue-worker.js: start monitor scheduler when DATABASE_URL is set"
docker run --rm firecrawl-api cat /app/dist/src/services/queue-worker.js > "$PATCH_DIR/queue-worker.js"
python3 - "$PATCH_DIR/queue-worker.js" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
text = path.read_text()
old = "if (config_1.config.USE_DB_AUTHENTICATION && !config_1.config.DISABLE_MONITORING) {"
new = "if ((config_1.config.USE_DB_AUTHENTICATION || config_1.config.DATABASE_URL) && !config_1.config.DISABLE_MONITORING) {"
if old not in text:
    raise SystemExit("queue-worker.js patch anchor not found")
path.write_text(text.replace(old, new, 1))
PY

log "Write docker-compose.override.yaml"
cat > docker-compose.override.yaml <<'YAML'
services:
  api:
    environment:
      USE_DB_AUTHENTICATION: "false"
      DATABASE_URL: postgresql://postgres:postgres@nuq-postgres:5432/postgres
      DATABASE_REPLICA_URL: postgresql://postgres:postgres@nuq-postgres:5432/postgres
    volumes:
      - ./patches/connection.js:/app/dist/src/db/connection.js:ro
      - ./patches/auth.js:/app/dist/src/controllers/auth.js:ro
      - ./patches/queue-worker.js:/app/dist/src/services/queue-worker.js:ro
YAML

python3 - <<'PY'
from pathlib import Path
vals = {
    "USE_DB_AUTHENTICATION": "false",
    "DATABASE_URL": "postgresql://postgres:postgres@nuq-postgres:5432/postgres",
    "DATABASE_REPLICA_URL": "postgresql://postgres:postgres@nuq-postgres:5432/postgres",
}
path = Path(".env")
lines = path.read_text().splitlines() if path.exists() else []
out, seen = [], set()
for line in lines:
    if "=" in line and not line.strip().startswith("#"):
        k = line.split("=", 1)[0]
        if k in vals:
            out.append(f"{k}={vals[k]}")
            seen.add(k)
            continue
    out.append(line)
for k, v in vals.items():
    if k not in seen:
        out.append(f"{k}={v}")
path.write_text("\n".join(out) + "\n")
PY

log "Create monitor tables + scheduler RPC in nuq-postgres"
docker exec -i firecrawl-nuq-postgres-1 psql -U postgres -d postgres <<'SQL'
CREATE TABLE IF NOT EXISTS monitors (
  id uuid PRIMARY KEY,
  team_id uuid NOT NULL,
  name text NOT NULL,
  status text NOT NULL DEFAULT 'active',
  schedule_cron text NOT NULL,
  schedule_timezone text NOT NULL DEFAULT 'UTC',
  next_run_at timestamptz,
  last_run_at timestamptz,
  last_check_id uuid,
  current_check_id uuid,
  locked_at timestamptz,
  locked_until timestamptz,
  retention_days integer NOT NULL DEFAULT 30,
  estimated_credits_per_month integer,
  targets jsonb NOT NULL DEFAULT '[]'::jsonb,
  webhook jsonb,
  notification jsonb,
  last_check_summary jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz,
  goal text,
  judge_enabled boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS monitor_checks (
  id uuid PRIMARY KEY,
  monitor_id uuid NOT NULL REFERENCES monitors(id),
  team_id uuid NOT NULL,
  trigger text NOT NULL,
  status text NOT NULL DEFAULT 'queued',
  scheduled_for timestamptz,
  started_at timestamptz,
  finished_at timestamptz,
  estimated_credits integer,
  reserved_credits integer,
  actual_credits integer,
  autumn_lock_id text,
  billing_status text NOT NULL DEFAULT 'not_applicable',
  total_pages integer NOT NULL DEFAULT 0,
  same_count integer NOT NULL DEFAULT 0,
  changed_count integer NOT NULL DEFAULT 0,
  new_count integer NOT NULL DEFAULT 0,
  removed_count integer NOT NULL DEFAULT 0,
  error_count integer NOT NULL DEFAULT 0,
  target_results jsonb,
  webhook_payload jsonb,
  email_payload jsonb,
  notification_status jsonb,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS monitor_pages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  monitor_id uuid NOT NULL REFERENCES monitors(id),
  team_id uuid NOT NULL,
  target_id text NOT NULL,
  url text NOT NULL,
  url_hash bytea NOT NULL,
  source text NOT NULL,
  first_seen_check_id uuid,
  last_seen_check_id uuid,
  last_changed_check_id uuid,
  last_scrape_id uuid,
  last_status text NOT NULL,
  is_removed boolean NOT NULL DEFAULT false,
  removed_at timestamptz,
  metadata jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS monitor_check_pages (
  id uuid PRIMARY KEY,
  check_id uuid NOT NULL REFERENCES monitor_checks(id),
  monitor_id uuid NOT NULL REFERENCES monitors(id),
  team_id uuid NOT NULL,
  target_id text NOT NULL,
  url text NOT NULL,
  url_hash bytea NOT NULL,
  status text NOT NULL,
  previous_scrape_id uuid,
  current_scrape_id uuid,
  diff_gcs_key text,
  diff_text_bytes integer,
  diff_json_bytes integer,
  status_code integer,
  error text,
  metadata jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  judgment jsonb
);

CREATE TABLE IF NOT EXISTS monitor_email_recipients (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  monitor_id uuid NOT NULL REFERENCES monitors(id),
  team_id uuid NOT NULL,
  email text NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  token text NOT NULL,
  source text NOT NULL DEFAULT 'opt_in',
  confirmation_sent_at timestamptz,
  confirmed_at timestamptz,
  unsubscribed_at timestamptz,
  last_notified_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION monitoring_claim_due_monitors(
  p_worker_id text,
  p_limit integer,
  p_lease_seconds integer
)
RETURNS SETOF monitors
LANGUAGE plpgsql
AS $$
BEGIN
  RETURN QUERY
  UPDATE monitors m
  SET
    locked_at = now(),
    locked_until = now() + make_interval(secs => p_lease_seconds),
    current_check_id = gen_random_uuid(),
    updated_at = now()
  WHERE m.id IN (
    SELECT id
    FROM monitors
    WHERE status = 'active'
      AND deleted_at IS NULL
      AND (next_run_at IS NULL OR next_run_at <= now())
      AND (locked_until IS NULL OR locked_until < now())
    ORDER BY next_run_at NULLS FIRST, created_at
    LIMIT p_limit
    FOR UPDATE SKIP LOCKED
  )
  RETURNING m.*;
END;
$$;
SQL

log "Restart api"
docker compose up -d api
sleep 15
docker compose ps api

log "Smoke: monitor list"
curl -sf "http://127.0.0.1:3002/v2/monitor" | head -c 300
echo
