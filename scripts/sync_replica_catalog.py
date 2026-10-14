#!/usr/bin/env python3
"""Sync catalog tables from production Supabase to replica via REST API.

Usage:
  python3 scripts/sync_replica_catalog.py export
  python3 scripts/sync_replica_catalog.py import
  python3 scripts/sync_replica_catalog.py sequences
  python3 scripts/sync_replica_catalog.py all
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = PROJECT_ROOT / "output" / "sync_replica_20260930"
DATA_DIR = OUT_DIR / "data"

PROD_REF = "kdlpkjzcnbkjcvwsvlwn"
REPLICA_REF = "ebamlycrvgtivuluipvn"

CATALOG_TABLES = [
    "master_business_info",
    "clinic_services",
    "clinic_memberships",
    "clinic_promotions",
    "promo_offer_master",
    "promo_offer_items",
    "promo_offer_terms",
    "promo_offer_price_history",
]

# FK-safe import order
IMPORT_ORDER = CATALOG_TABLES

BATCH_SIZE = 50

REPLICA_COLUMNS_PATH = OUT_DIR / "replica_columns.json"

IMPORT_DEFAULTS: dict[str, dict[str, Any]] = {
    "clinic_memberships": {
        "is_active": True,
        "is_checked": False,
        "is_offer_extracted": False,
        "benefits": [],
    }
}


def replica_columns() -> dict[str, set[str]]:
    data = json.loads(REPLICA_COLUMNS_PATH.read_text(encoding="utf-8"))
    return {table: set(cols) for table, cols in data.items()}


def filter_rows(table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = replica_columns()[table]
    defaults = IMPORT_DEFAULTS.get(table, {})
    out: list[dict[str, Any]] = []
    for row in rows:
        item = {k: v for k, v in row.items() if k in allowed}
        for key, val in defaults.items():
            if item.get(key) is None:
                item[key] = val
        out.append(item)
    return out


def _jwt_ref(token: str) -> str:
    import base64

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        body = json.loads(base64.urlsafe_b64decode(payload))
        return str(body.get("ref") or "")
    except Exception:
        return ""


def service_role_key(project_ref: str) -> str:
    out = subprocess.check_output(
        ["supabase", "projects", "api-keys", "--project-ref", project_ref, "-o", "json"],
        cwd=PROJECT_ROOT,
        text=True,
    )
    keys = json.loads(out)
    for row in keys:
        if row.get("name") != "service_role":
            continue
        key = (row.get("api_key") or "").strip()
        if key.startswith("eyJ") and _jwt_ref(key) == project_ref:
            return key
    raise SystemExit(f"JWT service_role not found for {project_ref}")


def rest_get_all(base_url: str, key: str, table: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        url = f"{base_url.rstrip('/')}/rest/v1/{table}?select=*"
        req = urllib.request.Request(
            url,
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Range": f"{offset}-{offset + 999}",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            chunk = json.loads(resp.read().decode("utf-8"))
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return rows


def rest_post_batch(base_url: str, key: str, table: str, batch: list[dict[str, Any]]) -> None:
    url = f"{base_url.rstrip('/')}/rest/v1/{table}"
    body = json.dumps(batch).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    last_err: Exception | None = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=180):
                return
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:2000]
            raise SystemExit(f"POST {table} failed ({e.code}): {detail}") from e
        except Exception as e:
            last_err = e
            time.sleep(min(2 ** attempt, 10))
    raise SystemExit(f"POST {table} failed after retries: {last_err}") from last_err


def export_tables() -> None:
    key = service_role_key(PROD_REF)
    base = f"https://{PROD_REF}.supabase.co"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    summary: dict[str, int] = {}
    for table in CATALOG_TABLES:
        rows = rest_get_all(base, key, table)
        (DATA_DIR / f"{table}.json").write_text(
            json.dumps(rows, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        summary[table] = len(rows)
        print(f"exported {table}: {len(rows)}")
    (OUT_DIR / "export_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )


def import_tables(start_from: str | None = None) -> None:
    key = service_role_key(REPLICA_REF)
    base = f"https://{REPLICA_REF}.supabase.co"
    summary: dict[str, int] = {}
    tables = IMPORT_ORDER
    if start_from:
        if start_from not in IMPORT_ORDER:
            raise SystemExit(f"unknown table: {start_from}")
        tables = IMPORT_ORDER[IMPORT_ORDER.index(start_from) :]
    for table in tables:
        path = DATA_DIR / f"{table}.json"
        if not path.exists():
            raise SystemExit(f"missing export file: {path}")
        rows: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
        rows = filter_rows(table, rows)
        for i in range(0, len(rows), BATCH_SIZE):
            rest_post_batch(base, key, table, rows[i : i + BATCH_SIZE])
        summary[table] = len(rows)
        print(f"imported {table}: {len(rows)}")
    (OUT_DIR / "import_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )


def sequences_sql() -> str:
    specs = [
        ("master_business_info", "business_id"),
        ("clinic_services", "service_id"),
        ("clinic_memberships", "plan_id"),
        ("clinic_promotions", "promotion_id"),
        ("promo_offer_master", "id"),
        ("promo_offer_items", "offer_item_id"),
        ("promo_offer_price_history", "id"),
    ]
    lines = ["BEGIN;"]
    for table, col in specs:
        lines.append(
            f"SELECT setval(pg_get_serial_sequence('public.{table}', '{col}'), "
            f"COALESCE((SELECT MAX({col}) FROM public.{table}), 1), true);"
        )
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def write_sequences_sql() -> None:
    path = OUT_DIR / "reset_sequences.sql"
    path.write_text(sequences_sql(), encoding="utf-8")
    print(f"wrote {path}")


def main() -> None:
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    start_from = None
    if len(sys.argv) > 2 and sys.argv[1].lower() == "import":
        start_from = sys.argv[2]
    if cmd in ("export", "all"):
        export_tables()
    if cmd in ("import", "all"):
        import_tables(start_from=start_from)
    if cmd in ("sequences", "all"):
        write_sequences_sql()
    if cmd not in ("export", "import", "sequences", "all"):
        raise SystemExit(f"unknown command: {cmd}")


if __name__ == "__main__":
    main()
