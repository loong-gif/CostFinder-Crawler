#!/usr/bin/env python3
"""Apply only frozen, high-confidence membership benefit repairs."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.supabase_access import fetch_all_rows, load_supabase_client

MANIFEST = ROOT / ".firecrawl" / "live_repair_manifest_20260913.json"
OUT = ROOT / ".firecrawl" / "membership_live_repair_apply_20260913.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write the frozen ready entries")
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ready = [item for item in manifest["entries"] if item.get("record_type") == "membership" and item.get("decision") == "ready"]
    client = load_supabase_client(ROOT)
    current_rows = {row["plan_id"]: row for row in fetch_all_rows(client, "clinic_memberships", "*")}
    results = []
    for item in ready:
        plan_id = int(item["record_id"])
        row = current_rows.get(plan_id)
        if row is None:
            results.append({"record_id": plan_id, "status": "conflict", "reason": "record_missing"})
            continue
        if row.get("updated_at") != item.get("old_updated_at"):
            results.append({"record_id": plan_id, "status": "conflict", "reason": "updated_at_changed"})
            continue
        if row.get("benefits") != item.get("old_content"):
            results.append({"record_id": plan_id, "status": "conflict", "reason": "benefits_changed"})
            continue
        payload = {"benefits": item["proposed_content"], "is_offer_extracted": False, "updated_at": datetime.now(timezone.utc).isoformat()}
        if not args.apply:
            results.append({"record_id": plan_id, "status": "would_update", "payload": payload})
            continue
        updated = client.update_row("clinic_memberships", {"plan_id": f"eq.{plan_id}"}, payload)
        if len(updated) != 1:
            raise RuntimeError(f"plan {plan_id} update returned {len(updated)} rows")
        results.append({"record_id": plan_id, "status": "updated", "benefit_count": len(item["proposed_content"]), "is_offer_extracted": updated[0].get("is_offer_extracted")})
    payload = {"applied_at": datetime.now(timezone.utc).isoformat(), "dry_run": not args.apply, "ready_count": len(ready), "results": results}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"result": str(OUT), "dry_run": not args.apply, "ready_count": len(ready), "result_counts": {status: sum(1 for item in results if item["status"] == status) for status in sorted({item["status"] for item in results})}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
