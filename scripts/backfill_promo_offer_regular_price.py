#!/usr/bin/env python3
"""Freeze and apply the promo_offer_master regular-price repair batch.

The preview phase reads through the server-side Supabase REST client and emits
both a complete pre-image and a frozen SQL batch.  The SQL batch is executed
through Supabase MCP so all writes happen in one serializable transaction.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.supabase_access import fetch_all_rows, load_supabase_client


OUTPUT_DIR = ROOT / "outputs"

MASTER_SELECT = (
    "id,business_id,membership_plan_id,regular_price,discount_price,"
    "discount_percent,discount_amount,is_membership_required,offer_fingerprint,"
    "created_at,offer_raw_text,promotion_id,is_active,is_new_customer_required,"
    "price_model,updated_at,is_recaculated"
)
ITEM_SELECT = "offer_item_id,offer_id,service_id,quantity,unit_price,created_at,updated_at"
SERVICE_SELECT = (
    "service_id,business_id,service_name,regular_price,unit_type,created_at,"
    "updated_at,source_url,service_category"
)


def number(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    if not result.is_finite():
        return None
    return result


def positive(value: Any) -> Decimal | None:
    result = number(value)
    return result if result is not None and result > 0 else None


def json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    return value


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(value)
        return format(Decimal(str(value)), "f")
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def values_sql(rows: list[tuple[Any, ...]]) -> str:
    return ",\n".join("(" + ", ".join(sql_literal(v) for v in row) + ")" for row in rows)


def classify() -> dict[str, Any]:
    client = load_supabase_client(ROOT)
    masters = fetch_all_rows(client, "promo_offer_master", MASTER_SELECT, order="id.asc")
    items = fetch_all_rows(client, "promo_offer_items", ITEM_SELECT, order="offer_item_id.asc")
    services = fetch_all_rows(client, "clinic_services", SERVICE_SELECT, order="service_id.asc")

    target = {int(row["id"]): row for row in masters if row.get("regular_price") is None}
    if not target:
        raise RuntimeError("No promo_offer_master rows currently need regular_price backfill")

    items_by_offer: dict[int, list[dict[str, Any]]] = {}
    for item in items:
        items_by_offer.setdefault(int(item["offer_id"]), []).append(item)
    services_by_id = {int(row["service_id"]): row for row in services}

    missing_by_service: dict[int, list[tuple[int, dict[str, Any], dict[str, Any]]]] = {}
    for offer_id, offer in target.items():
        offer_items = items_by_offer.get(offer_id, [])
        if len(offer_items) != 1:
            continue
        item = offer_items[0]
        service_id = item.get("service_id")
        service = services_by_id.get(int(service_id)) if service_id is not None else None
        if service is not None and service.get("regular_price") is None:
            missing_by_service.setdefault(int(service_id), []).append((offer_id, offer, item))

    migration_services: list[dict[str, Any]] = []
    migration_offer_ids: set[int] = set()
    effective_service_price: dict[int, Decimal] = {}
    conflict_service_ids: set[int] = set()
    no_price_service_ids: set[int] = set()

    for service_id, candidates in sorted(missing_by_service.items()):
        prices = sorted({positive(offer.get("discount_price")) for _, offer, _ in candidates if positive(offer.get("discount_price"))})
        if len(prices) > 1:
            conflict_service_ids.add(service_id)
            continue
        if not prices:
            no_price_service_ids.add(service_id)
            continue
        candidate_price = prices[0]
        effective_service_price[service_id] = candidate_price
        service = services_by_id[service_id]
        source_candidates = [
            (offer_id, offer, item)
            for offer_id, offer, item in candidates
            if positive(offer.get("discount_price")) == candidate_price
        ]
        for offer_id, offer, item in source_candidates:
            migration_offer_ids.add(offer_id)
            migration_services.append(
                {
                    "service": service,
                    "candidate_price": candidate_price,
                    "source_offer": offer,
                    "source_item": item,
                }
            )

    def computed_total(offer_id: int, *, effective: bool) -> tuple[Decimal | None, str | None, list[dict[str, Any]]]:
        offer_items = items_by_offer.get(offer_id, [])
        if not offer_items:
            return None, "no_items", []
        total = Decimal("0")
        evidence: list[dict[str, Any]] = []
        for item in offer_items:
            service_id = item.get("service_id")
            quantity = number(item.get("quantity"))
            service = services_by_id.get(int(service_id)) if service_id is not None else None
            service_price = None
            if service is not None:
                service_price = number(service.get("regular_price"))
                if effective and service_price is None and int(service_id) in effective_service_price:
                    service_price = effective_service_price[int(service_id)]
            if service is None or service_price is None:
                return None, "missing_service_price", evidence
            if quantity is None or quantity <= 0:
                return None, "invalid_quantity", evidence
            total += service_price * quantity
            evidence.append(
                {
                    "offer_item_id": int(item["offer_item_id"]),
                    "service_id": int(service_id),
                    "quantity": json_value(quantity),
                    "service_regular_price": json_value(service_price),
                }
            )
        return total, None, evidence

    backfills: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for offer_id, offer in sorted(target.items()):
        if offer_id in migration_offer_ids:
            continue
        total, reason, evidence = computed_total(offer_id, effective=True)
        if reason == "no_items":
            skipped.append({"offer_id": offer_id, "reason": "no_items"})
            continue
        if reason == "missing_service_price":
            offer_items = items_by_offer.get(offer_id, [])
            service_ids = {int(item["service_id"]) for item in offer_items if item.get("service_id") is not None}
            if service_ids & conflict_service_ids:
                skipped.append({"offer_id": offer_id, "reason": "conflicting_service_price", "service_ids": sorted(service_ids & conflict_service_ids)})
            elif service_ids & no_price_service_ids:
                skipped.append({"offer_id": offer_id, "reason": "missing_positive_discount_price", "service_ids": sorted(service_ids & no_price_service_ids)})
            else:
                skipped.append({"offer_id": offer_id, "reason": reason})
            continue
        if reason is not None or total is None:
            skipped.append({"offer_id": offer_id, "reason": reason or "not_computable"})
            continue
        discount = number(offer.get("discount_price"))
        if discount is not None and total < discount:
            skipped.append(
                {
                    "offer_id": offer_id,
                    "reason": "computed_regular_below_discount",
                    "computed_regular_price": json_value(total),
                    "discount_price": json_value(discount),
                    "items": evidence,
                }
            )
            continue
        backfills.append(
            {
                "offer": offer,
                "computed_regular_price": total,
                "items": evidence,
            }
        )

    # Add explicit category details for migration-source rows and conflicts.
    for row in migration_services:
        row["candidate_price"] = json_value(row["candidate_price"])
    for service_id in sorted(conflict_service_ids):
        for offer_id, offer, item in missing_by_service[service_id]:
            skipped.append(
                {
                    "offer_id": offer_id,
                    "reason": "conflicting_service_price",
                    "service_id": service_id,
                    "discount_price": json_value(number(offer.get("discount_price"))),
                }
            )
    for service_id in sorted(no_price_service_ids):
        for offer_id, offer, item in missing_by_service[service_id]:
            if offer_id not in migration_offer_ids:
                skipped.append(
                    {
                        "offer_id": offer_id,
                        "reason": "missing_positive_discount_price",
                        "service_id": service_id,
                        "discount_price": json_value(number(offer.get("discount_price"))),
                    }
                )

    # Migration source rows are deliberately not included in the backfill set.
    skipped_by_id = {int(row["offer_id"]): row for row in skipped}
    skipped = [skipped_by_id[key] for key in sorted(skipped_by_id)]
    if len(migration_offer_ids) + len(backfills) + len(skipped) != len(target):
        raise RuntimeError("Offer partitions are not mutually exhaustive")

    batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    service_preimages = [
        {
            "service_id": int(row["service"]["service_id"]),
            "old_row": row["service"],
            "candidate_price": row["candidate_price"],
            "source_offer_id": int(row["source_offer"]["id"]),
            "source_item_id": int(row["source_item"]["offer_item_id"]),
        }
        for row in sorted(migration_services, key=lambda item: int(item["service"]["service_id"]))
    ]
    source_preimages = [
        {
            "offer_id": int(row["source_offer"]["id"]),
            "old_row": row["source_offer"],
            "service_id": int(row["service"]["service_id"]),
            "candidate_price": row["candidate_price"],
        }
        for row in sorted(migration_services, key=lambda item: int(item["source_offer"]["id"]))
    ]
    backfill_preimages = [
        {
            "offer_id": int(row["offer"]["id"]),
            "old_row": row["offer"],
            "computed_regular_price": json_value(row["computed_regular_price"]),
            "items": row["items"],
        }
        for row in sorted(backfills, key=lambda item: int(item["offer"]["id"]))
    ]
    preview = {
        "batch_id": batch_id,
        "project": "costfinder",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "rule_version": "promo_offer_regular_price_backfill_v1",
        "counts": {
            "target": len(target),
            "service_migrations": len(service_preimages),
            "source_offers": len(source_preimages),
            "backfills": len(backfill_preimages),
            "skipped": len(skipped),
            "skipped_by_reason": {
                reason: sum(1 for row in skipped if row["reason"] == reason)
                for reason in sorted({row["reason"] for row in skipped})
            },
        },
        "service_preimages": service_preimages,
        "source_offer_preimages": source_preimages,
        "backfill_offer_preimages": backfill_preimages,
        "skipped": skipped,
    }
    return preview


def build_sql(preview: dict[str, Any], all_target_ids: list[int]) -> str:
    services = preview["service_preimages"]
    sources = preview["source_offer_preimages"]
    backfills = preview["backfill_offer_preimages"]
    service_rows = [
        (row["service_id"], Decimal(str(row["candidate_price"])), row["old_row"].get("updated_at"))
        for row in services
    ]
    source_rows = [
        (
            row["offer_id"],
            Decimal(str(row["candidate_price"])),
            row["old_row"].get("is_active"),
            row["old_row"].get("updated_at"),
        )
        for row in sources
    ]
    backfill_rows = [
        (row["offer_id"], Decimal(str(row["computed_regular_price"])), row["old_row"].get("updated_at"))
        for row in backfills
    ]
    service_values = values_sql(service_rows)
    source_values = values_sql(source_rows)
    backfill_values = values_sql(backfill_rows)
    target_ids = ", ".join(str(int(value)) for value in sorted(all_target_ids))
    expected = preview["counts"]
    return f"""-- Frozen batch {preview['batch_id']} generated by backfill_promo_offer_regular_price.py
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';
SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;

CREATE TEMP TABLE stage_services (
    service_id bigint primary key,
    candidate_price numeric not null,
    old_updated_at timestamptz
) ON COMMIT PRESERVE ROWS;
INSERT INTO stage_services(service_id, candidate_price, old_updated_at) VALUES
{service_values};

CREATE TEMP TABLE stage_sources (
    offer_id bigint primary key,
    candidate_price numeric not null,
    old_is_active boolean,
    old_updated_at timestamptz
) ON COMMIT PRESERVE ROWS;
INSERT INTO stage_sources(offer_id, candidate_price, old_is_active, old_updated_at) VALUES
{source_values};

CREATE TEMP TABLE stage_backfills (
    offer_id bigint primary key,
    computed_regular_price numeric not null,
    old_updated_at timestamptz
) ON COMMIT PRESERVE ROWS;
INSERT INTO stage_backfills(offer_id, computed_regular_price, old_updated_at) VALUES
{backfill_values};

-- Lock all rows in deterministic order before validating the frozen snapshot.
SELECT service_id FROM public.clinic_services WHERE service_id IN (SELECT service_id FROM stage_services) ORDER BY service_id FOR UPDATE;
SELECT offer_id FROM public.promo_offer_items WHERE offer_id IN ({target_ids}) ORDER BY offer_id, offer_item_id FOR UPDATE;
SELECT id FROM public.promo_offer_master WHERE id IN ({target_ids}) ORDER BY id FOR UPDATE;

DO $$
DECLARE
    n bigint;
BEGIN
    SELECT count(*) INTO n FROM stage_services s JOIN public.clinic_services c USING(service_id)
      WHERE c.regular_price IS NULL AND c.updated_at IS NOT DISTINCT FROM s.old_updated_at;
    IF n <> {expected['service_migrations']} THEN RAISE EXCEPTION 'service snapshot changed: expected %, got %', {expected['service_migrations']}, n; END IF;

    SELECT count(*) INTO n FROM stage_sources s JOIN public.promo_offer_master o ON o.id = s.offer_id
      WHERE o.regular_price IS NULL AND o.discount_price = s.candidate_price
        AND o.is_active IS NOT DISTINCT FROM s.old_is_active
        AND o.updated_at IS NOT DISTINCT FROM s.old_updated_at;
    IF n <> {expected['source_offers']} THEN RAISE EXCEPTION 'source snapshot changed: expected %, got %', {expected['source_offers']}, n; END IF;

    SELECT count(*) INTO n FROM stage_backfills s JOIN public.promo_offer_master o ON o.id = s.offer_id
      WHERE o.regular_price IS NULL AND o.updated_at IS NOT DISTINCT FROM s.old_updated_at;
    IF n <> {expected['backfills']} THEN RAISE EXCEPTION 'backfill snapshot changed: expected %, got %', {expected['backfills']}, n; END IF;
END $$;

UPDATE public.clinic_services c
SET regular_price = s.candidate_price
FROM stage_services s
WHERE c.service_id = s.service_id AND c.regular_price IS NULL;

UPDATE public.promo_offer_master o
SET is_active = false
FROM stage_sources s
WHERE o.id = s.offer_id AND o.regular_price IS NULL AND o.discount_price = s.candidate_price
  AND o.is_active IS DISTINCT FROM false;

UPDATE public.promo_offer_master o
SET regular_price = s.computed_regular_price
FROM stage_backfills s
WHERE o.id = s.offer_id AND o.regular_price IS NULL;

DO $$
DECLARE
    n bigint;
BEGIN
    SELECT count(*) INTO n FROM stage_services s JOIN public.clinic_services c USING(service_id)
      WHERE c.regular_price = s.candidate_price;
    IF n <> {expected['service_migrations']} THEN RAISE EXCEPTION 'service postcondition failed: %', n; END IF;
    SELECT count(*) INTO n FROM stage_sources s JOIN public.promo_offer_master o ON o.id = s.offer_id
      WHERE o.is_active = false;
    IF n <> {expected['source_offers']} THEN RAISE EXCEPTION 'source postcondition failed: %', n; END IF;
    SELECT count(*) INTO n FROM stage_backfills s JOIN public.promo_offer_master o ON o.id = s.offer_id
      WHERE o.regular_price = s.computed_regular_price;
    IF n <> {expected['backfills']} THEN RAISE EXCEPTION 'backfill postcondition failed: %', n; END IF;
    IF EXISTS (
      SELECT 1 FROM public.promo_offer_master o JOIN stage_backfills s ON o.id = s.offer_id
      WHERE o.discount_price IS NOT NULL AND o.discount_price > o.regular_price
    ) THEN RAISE EXCEPTION 'discount_price > regular_price postcondition failed'; END IF;
END $$;

COMMIT;

SELECT jsonb_build_object(
  'batch_id', {sql_literal(preview['batch_id'])},
  'service_migrations', (SELECT count(*) FROM stage_services s JOIN public.clinic_services c USING(service_id) WHERE c.regular_price = s.candidate_price),
  'source_offers_inactive', (SELECT count(*) FROM stage_sources s JOIN public.promo_offer_master o ON o.id = s.offer_id WHERE o.is_active = false),
  'backfills', (SELECT count(*) FROM stage_backfills s JOIN public.promo_offer_master o ON o.id = s.offer_id WHERE o.regular_price = s.computed_regular_price)
) AS result;
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    preview = classify()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = preview["batch_id"]
    preview_path = args.output_dir / f"promo_offer_regular_price_backfill_preview_{stamp}.json"
    sql_path = args.output_dir / f"promo_offer_regular_price_backfill_{stamp}.sql"
    preview_path.write_text(json.dumps(preview, ensure_ascii=False, indent=2, default=json_value), encoding="utf-8")
    target_ids = sorted({row["offer_id"] for row in preview["source_offer_preimages"]} | {row["offer_id"] for row in preview["backfill_offer_preimages"]} | {row["offer_id"] for row in preview["skipped"]})
    sql_path.write_text(build_sql(preview, target_ids), encoding="utf-8")
    print(json.dumps({"preview": str(preview_path), "sql": str(sql_path), "counts": preview["counts"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
