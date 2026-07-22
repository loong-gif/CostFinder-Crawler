#!/usr/bin/env python3
"""Backfill clinic_services Botox rows from promo_offer_master Botox offers.

Usage:
    python scripts/backfill_clinic_services_from_offers.py --dry-run
    python scripts/backfill_clinic_services_from_offers.py --apply --link-offers
    python scripts/backfill_clinic_services_from_offers.py --business-id 241 --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import OUTPUT_DIR
from crawler.staging_recrawl import fetch_all_rows, load_supabase_client
from utils.clinic_services_db import apply_fields, fetch_service_row
from utils.clinic_services_from_offers import (
    BOTOX_SERVICE_NAME,
    fields_have_updates,
    offer_to_clinic_fields,
    pick_winner_botox_offer,
)
from utils.schema_contract import OFFER_MASTER_WITH_ITEMS_SELECT
from utils.promo_offer_items_db import fetch_items_for_offer, link_item_to_service

REPORT_PREFIX = "clinic_services_from_offers"
OFFER_SELECT = OFFER_MASTER_WITH_ITEMS_SELECT


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill clinic_services Botox catalog from promo_offer_master."
    )
    parser.add_argument("--business-id", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Report only; no DB writes")
    parser.add_argument("--apply", action="store_true", help="Update clinic_services rows")
    parser.add_argument(
        "--link-offers",
        action="store_true",
        help="Set promo_offer_master.service_id for all Botox offers per linked business",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing clinic_services.regular_price",
    )
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args()


def fetch_botox_offers(client, *, business_id: Optional[int]) -> List[Dict[str, Any]]:
    filters: Dict[str, str] = {}
    if business_id is not None:
        filters["business_id"] = f"eq.{business_id}"
    rows = fetch_all_rows(
        client,
        "promo_offer_master",
        OFFER_SELECT,
        filters=filters,
        order="business_id.asc",
    )
    from utils.clinic_services_from_offers import flatten_offer_row, BOTOX_SERVICE_NAME, offer_item_name

    return [
        flatten_offer_row(row)
        for row in rows
        if str(offer_item_name(flatten_offer_row(row)) or "").strip() == BOTOX_SERVICE_NAME
    ]


def link_offers_to_service(
    client,
    *,
    business_id: int,
    service_id: int,
    offer_ids: List[int],
) -> int:
    linked = 0
    for offer_id in offer_ids:
        items = fetch_items_for_offer(client, offer_id)
        if items:
            link_item_to_service(client, int(items[0]["offer_item_id"]), service_id)
        else:
            from utils.promo_offer_items_db import upsert_offer_items

            upsert_offer_items(
                client,
                offer_id,
                [{"item_name": BOTOX_SERVICE_NAME, "service_id": service_id}],
            )
        linked += 1
    return linked


def process_business(
    client,
    business_id: int,
    offers: List[Dict[str, Any]],
    *,
    apply: bool,
    link_offers: bool,
    force: bool,
) -> Dict[str, Any]:
    winner = pick_winner_botox_offer(offers)
    report: Dict[str, Any] = {
        "business_id": business_id,
        "offer_count": len(offers),
        "winner_offer_id": winner.get("id") if winner else None,
        "status": "pending",
        "service_id": None,
        "regular_price": None,
        "unit_type": None,
        "service_area": None,
        "linked_offers": 0,
        "error": "",
    }
    if winner is None:
        report["status"] = "no_winner"
        return report

    clinic_row = fetch_service_row(client, business_id, BOTOX_SERVICE_NAME)
    if not clinic_row:
        report["status"] = "missing_skeleton"
        return report

    fields = offer_to_clinic_fields(winner)
    report["regular_price"] = float(fields.regular_price) if fields.regular_price else None
    report["unit_type"] = fields.unit_type
    report["service_area"] = fields.service_area

    if not fields_have_updates(fields):
        report["status"] = "no_updates"
        return report

    service_id = int(clinic_row["service_id"])
    report["service_id"] = service_id

    if apply:
        try:
            apply_fields(
                client,
                service_id,
                fields,
                force_price=force,
                existing_price=clinic_row.get("regular_price"),
                existing_row=clinic_row,
            )
            if link_offers:
                offer_ids = [int(o["id"]) for o in offers if o.get("id") is not None]
                report["linked_offers"] = link_offers_to_service(
                    client,
                    business_id=business_id,
                    service_id=service_id,
                    offer_ids=offer_ids,
                )
            report["status"] = "applied"
        except Exception as exc:
            report["status"] = "error"
            report["error"] = str(exc)
            log.warning(
                "backfill clinic_services failed business_id={bid}: {err}".format(
                    bid=business_id, err=exc
                )
            )
    else:
        report["status"] = "dry_run"

    return report


def main() -> None:
    args = parse_args()
    if args.apply and args.dry_run:
        raise SystemExit("Use only one of --apply or --dry-run")
    apply = bool(args.apply)
    if not apply and not args.dry_run:
        args.dry_run = True

    load_dotenv(PROJECT_ROOT / ".env")
    client = load_supabase_client()

    offers = fetch_botox_offers(client, business_id=args.business_id)
    if not offers:
        raise RuntimeError("No Botox offers found")

    grouped: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for offer in offers:
        bid = offer.get("business_id")
        if bid is None:
            continue
        grouped[int(bid)].append(offer)

    reports = [
        process_business(
            client,
            business_id,
            group,
            apply=apply,
            link_offers=args.link_offers,
            force=args.force,
        )
        for business_id, group in sorted(grouped.items())
    ]

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = args.output or (OUTPUT_DIR / f"{REPORT_PREFIX}_{stamp}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "report_path": str(out_path),
        "apply": apply,
        "link_offers": bool(args.link_offers),
        "force": bool(args.force),
        "business_count": len(reports),
        "applied": sum(1 for r in reports if r.get("status") == "applied"),
        "dry_run_rows": sum(1 for r in reports if r.get("status") == "dry_run"),
        "missing_skeleton": sum(1 for r in reports if r.get("status") == "missing_skeleton"),
        "no_updates": sum(1 for r in reports if r.get("status") == "no_updates"),
        "with_price": sum(1 for r in reports if r.get("regular_price") is not None),
        "linked_offers_total": sum(int(r.get("linked_offers") or 0) for r in reports),
        "errors": sum(1 for r in reports if r.get("status") == "error"),
        "rows": reports,
    }
    out_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k != "rows"},
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        )
    )
    print(f"report_path={out_path}")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(repr(value) + " is not JSON serializable")


if __name__ == "__main__":
    main()
