#!/usr/bin/env python3
"""Deduplicate active promo_offer_master rows by offer_fingerprint.

Physical DELETE losers after remapping claims/saved_deals FKs.
Default is --dry-run.

Usage:
    python scripts/dedupe_promo_offer_master.py --dry-run
    python scripts/dedupe_promo_offer_master.py
    python scripts/dedupe_promo_offer_master.py --create-index
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.offer_fingerprint import compute_offer_fingerprint
from utils.supabase_rest import SupabaseRestClient

OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
PAGE_SIZE = 500
SELECT = (
    "id,source_url,source_name,service_name,unit_type,service_category,"
    "offer_raw_text,regular_price,discount_price,discount_amount,discount_percent,"
    "status,created_at"
)
MERGE_TEXT_FIELDS = (
    "service_category",
    "offer_raw_text",
    "template_type",
    "unit_type",
    "start_date",
    "end_date",
)
MERGE_NUMERIC_FIELDS = (
    "regular_price",
    "discount_price",
    "discount_amount",
    "discount_percent",
)


def load_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, key)


def fetch_active_rows(client: SupabaseRestClient) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        batch = client.fetch_rows(
            "promo_offer_master",
            SELECT,
            filters={"status": "eq.active"},
            limit=PAGE_SIZE,
            offset=offset,
        )
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def _has_value(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def score_row(row: Dict[str, Any]) -> Tuple[int, int, str]:
    score = 0
    regular = row.get("regular_price")
    discount = row.get("discount_price")
    if _has_value(regular) and _has_value(discount):
        score += 100
    elif _has_value(regular) or _has_value(discount):
        score += 20
    score += min(len(str(row.get("offer_raw_text") or "")), 50)
    category = str(row.get("service_category") or "").strip()
    if category and category not in {"Others", "Package"}:
        score += 10
    ts = str(row.get("created_at") or "")
    return score, len(str(row.get("offer_raw_text") or "")), ts


def merge_winner_fields(winner: Dict[str, Any], losers: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    updates: Dict[str, Any] = {}
    for field in MERGE_TEXT_FIELDS:
        if _has_value(winner.get(field)):
            continue
        for loser in losers:
            value = loser.get(field)
            if _has_value(value):
                updates[field] = value
                break
    for field in MERGE_NUMERIC_FIELDS:
        if _has_value(winner.get(field)):
            continue
        for loser in losers:
            value = loser.get(field)
            if _has_value(value):
                updates[field] = value
                break
    return updates


def attach_fingerprints(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []
    for row in rows:
        copy = dict(row)
        copy["offer_fingerprint"] = compute_offer_fingerprint(
            source_url=str(row.get("source_url") or ""),
            service_name=str(row.get("service_name") or ""),
            unit_type=row.get("unit_type"),
        )
        enriched.append(copy)
    return enriched


def group_duplicates(rows: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get("offer_fingerprint") or "")].append(row)
    return {fp: members for fp, members in groups.items() if len(members) > 1}


def pick_winner(members: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    ordered = sorted(members, key=score_row, reverse=True)
    return ordered[0], ordered[1:]


def remap_claims(client: SupabaseRestClient, loser_id: Any, winner_id: Any, *, dry_run: bool) -> int:
    rows = client.fetch_rows(
        "claims",
        "id",
        filters={"deal_id": f"eq.{loser_id}"},
        limit=PAGE_SIZE,
    )
    if dry_run or not rows:
        return len(rows)
    # ponytail: one PATCH per loser_id, not per claim row
    client.update_row(
        "claims",
        {"deal_id": f"eq.{loser_id}"},
        {"deal_id": winner_id},
    )
    return len(rows)


def remap_saved_deals(
    client: SupabaseRestClient,
    loser_id: Any,
    winner_id: Any,
    *,
    dry_run: bool,
    winner_consumer_ids: set[Any],
) -> Tuple[int, int, set[Any]]:
    rows = client.fetch_rows(
        "saved_deals",
        "id,consumer_id,deal_id",
        filters={"deal_id": f"eq.{loser_id}"},
        limit=PAGE_SIZE,
    )
    if dry_run:
        return len(rows), 0, winner_consumer_ids

    updated = 0
    deleted = 0
    consumers = set(winner_consumer_ids)
    for row in rows:
        consumer_id = row.get("consumer_id")
        if consumer_id in consumers:
            client.delete_rows("saved_deals", {"id": f"eq.{row['id']}"})
            deleted += 1
        else:
            client.update_row(
                "saved_deals",
                {"id": f"eq.{row['id']}"},
                {"deal_id": winner_id},
            )
            consumers.add(consumer_id)
            updated += 1
    return updated, deleted, consumers


def load_winner_consumer_ids(
    client: SupabaseRestClient,
    winner_id: Any,
) -> set[Any]:
    rows = client.fetch_rows(
        "saved_deals",
        "consumer_id",
        filters={"deal_id": f"eq.{winner_id}"},
        limit=PAGE_SIZE,
    )
    return {row.get("consumer_id") for row in rows if row.get("consumer_id") is not None}


def assert_no_duplicate_fingerprints(rows: Sequence[Dict[str, Any]]) -> None:
    counts: Dict[str, int] = defaultdict(int)
    for row in rows:
        counts[str(row.get("offer_fingerprint") or "")] += 1
    dupes = {fp: count for fp, count in counts.items() if fp and count > 1}
    if dupes:
        sample = next(iter(dupes.items()))
        raise RuntimeError(
            f"Duplicate fingerprint groups remain: {len(dupes)} "
            f"(example {sample[0]} x{sample[1]})"
        )


def create_unique_index() -> int:
    script = PROJECT_ROOT / "scripts" / "apply_sql_migration.py"
    sql_file = PROJECT_ROOT / "config" / "sql" / "m003b_promo_offer_active_fp_index.sql"
    result = subprocess.run(
        [sys.executable, str(script), str(sql_file)],
        cwd=PROJECT_ROOT,
        check=False,
    )
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute deletes/updates (default is dry-run)",
    )
    parser.add_argument(
        "--create-index",
        action="store_true",
        help="After successful dedupe, apply m003b unique index migration",
    )
    args = parser.parse_args()
    dry_run = not args.apply

    client = load_client()
    rows = fetch_active_rows(client)
    enriched = attach_fingerprints(rows)
    duplicate_groups = group_duplicates(enriched)

    report: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "active_rows": len(rows),
        "duplicate_groups": len(duplicate_groups),
        "rows_to_delete": sum(len(m) - 1 for m in duplicate_groups.values()),
        "groups": [],
        "claims_remapped": 0,
        "saved_deals_updated": 0,
        "saved_deals_deleted": 0,
        "fingerprints_backfilled": 0,
        "winners_merged": 0,
    }

    for group_idx, (fingerprint, members) in enumerate(
        sorted(
            duplicate_groups.items(),
            key=lambda item: (-len(item[1]), item[0]),
        ),
        start=1,
    ):
        winner, losers = pick_winner(members)
        merge_updates = merge_winner_fields(winner, losers)
        group_report = {
            "offer_fingerprint": fingerprint,
            "winner_id": winner.get("id"),
            "loser_ids": [row.get("id") for row in losers],
            "merge_updates": merge_updates,
        }
        report["groups"].append(group_report)

        if dry_run:
            continue

        if group_idx % 25 == 0 or group_idx == len(duplicate_groups):
            print(
                f"Deduping group {group_idx}/{len(duplicate_groups)}...",
                flush=True,
            )

        if merge_updates:
            client.update_row(
                "promo_offer_master",
                {"id": f"eq.{winner['id']}"},
                {**merge_updates, "offer_fingerprint": fingerprint},
            )
            report["winners_merged"] += 1
        else:
            client.update_row(
                "promo_offer_master",
                {"id": f"eq.{winner['id']}"},
                {"offer_fingerprint": fingerprint},
            )

        winner_consumers = load_winner_consumer_ids(client, winner["id"])
        for loser in losers:
            loser_id = loser["id"]
            winner_id = winner["id"]
            report["claims_remapped"] += remap_claims(
                client, loser_id, winner_id, dry_run=False
            )
            updated, deleted, winner_consumers = remap_saved_deals(
                client,
                loser_id,
                winner_id,
                dry_run=False,
                winner_consumer_ids=winner_consumers,
            )
            report["saved_deals_updated"] += updated
            report["saved_deals_deleted"] += deleted
            client.delete_rows("promo_offer_master", {"id": f"eq.{loser_id}"})

    if not dry_run:
        remaining = attach_fingerprints(fetch_active_rows(client))
        try:
            missing_fp = client.fetch_rows(
                "promo_offer_master",
                "id",
                filters={"status": "eq.active", "offer_fingerprint": "is.null"},
                limit=PAGE_SIZE,
            )
        except Exception:
            missing_fp = remaining
        missing_ids = {row["id"] for row in missing_fp}
        for row in remaining:
            if row["id"] not in missing_ids:
                continue
            client.update_row(
                "promo_offer_master",
                {"id": f"eq.{row['id']}"},
                {"offer_fingerprint": row["offer_fingerprint"]},
            )
            report["fingerprints_backfilled"] += 1
        assert_no_duplicate_fingerprints(remaining)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = OUTPUT_DIR / f"dedupe_promo_offer_master_{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Report: {out_path}")

    if args.create_index and not dry_run:
        if duplicate_groups:
            print("Skipping index creation: duplicate groups remain", file=sys.stderr)
            return 1
        return create_unique_index()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
