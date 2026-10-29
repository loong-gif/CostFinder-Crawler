#!/usr/bin/env python3
"""List inactive itemless offers from the 2026-10-18 monitor batch for approval."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.supabase_access import fetch_all_rows, load_supabase_client

OUT = ROOT / "output/freeze_itemless_offers_20261018.json"


def main() -> None:
    client = load_supabase_client(role="service")
    offers = fetch_all_rows(
        client,
        "promo_offer_master",
        "id,promotion_id,business_id,is_active,created_at,offer_raw_text",
        filters={"created_at": "gte.2026-10-18", "is_active": "eq.false"},
    )
    ids = [row["id"] for row in offers]
    items = []
    for start in range(0, len(ids), 150):
        chunk = ids[start : start + 150]
        if not chunk:
            continue
        items.extend(
            fetch_all_rows(
                client,
                "promo_offer_items",
                "offer_id",
                filters={"offer_id": f"in.({','.join(map(str, chunk))})"},
            )
        )
    has_items = {row["offer_id"] for row in items}
    shells = [row for row in offers if row["id"] not in has_items]
    payload = {
        "generated_at": "2026-10-20",
        "count": len(shells),
        "action": "delete_after_approval",
        "offer_ids": [row["id"] for row in shells],
        "rows": shells,
        "recover_journal_note": "Run recover_journal on monitor host journals from 2026-10-18 before deleting.",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2))
    print(f"wrote {OUT} ({len(shells)} rows)")


if __name__ == "__main__":
    main()
