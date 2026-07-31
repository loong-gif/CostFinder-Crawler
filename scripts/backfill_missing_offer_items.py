#!/usr/bin/env python3
"""Build and freeze items for offers that currently have no item rows."""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.supabase_access import fetch_all_rows, load_supabase_client

OUTPUT_DIR = ROOT / "outputs"
MASTER_SELECT = "id,business_id,offer_raw_text,regular_price,discount_price,discount_percent,discount_amount,price_model,is_active,updated_at"
SERVICE_SELECT = "service_id,business_id,service_name,service_category,unit_type,regular_price"


def norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def quantity_from_text(text: str) -> int | None:
    patterns = [
        r"\b(\d+)\s*(?:u\b|units?\b)",
        r"\b(\d+)\s*(?:syringes?\b|vials?\b)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = int(match.group(1))
            if value > 0:
                return value
    return None


def explicit_names(text: str) -> set[str]:
    value = norm(text)
    aliases = {
        "botox": "Botox",
        "dysport": "Dysport",
        "daxxify": "Daxxify",
        "xeomin": "Xeomin",
        "jeuveau": "Jeuveau",
        "letybo": "Letybo",
        "sculptra": "Sculptra",
        "juvederm": "Juvéderm",
        "restylane": "Restylane",
        "radiesse": "Radiesse",
        "kybella": "Kybella",
        "skinvive": "Skinvive",
        "prfm": "PRFM Injections",
    }
    result = {canonical for token, canonical in aliases.items() if re.search(rf"\b{re.escape(token)}\b", value)}
    if re.search(r"\bdermal\s+filler\b", value) or re.search(r"\bfiller\s+treatment\b", value):
        result.add("Dermal Filler")
    return result


def build_plan() -> dict[str, Any]:
    client = load_supabase_client(ROOT)
    offers = fetch_all_rows(client, "promo_offer_master", MASTER_SELECT, order="id.asc")
    items = fetch_all_rows(client, "promo_offer_items", "offer_item_id,offer_id", order="offer_id.asc")
    services = fetch_all_rows(client, "clinic_services", SERVICE_SELECT, order="service_id.asc")
    existing_offer_ids = {int(row["offer_id"]) for row in items}
    targets = [row for row in offers if row.get("regular_price") is None and int(row["id"]) not in existing_offer_ids]
    services_by_business: dict[int, list[dict[str, Any]]] = {}
    for service in services:
        services_by_business.setdefault(int(service["business_id"]), []).append(service)

    planned: list[dict[str, Any]] = []
    for offer in targets:
        text = str(offer.get("offer_raw_text") or "")
        business_id = int(offer["business_id"]) if offer.get("business_id") is not None else None
        business_services = services_by_business.get(business_id, []) if business_id is not None else []
        names = explicit_names(text)
        matches = [s for s in business_services if norm(s.get("service_name")) in {norm(name) for name in names}]
        # Generic tox can only be resolved when the clinic has exactly one
        # neurotoxin service; otherwise leave service_id NULL.
        if not matches and re.search(r"\btox\b", norm(text)):
            neuro = [s for s in business_services if str(s.get("service_category") or "") == "Neurotoxin"]
            if len(neuro) == 1:
                matches = neuro
        if not matches and re.search(r"\bfiller\b", norm(text)):
            filler = [s for s in business_services if str(s.get("service_category") or "") == "Filler"]
            if len(filler) == 1:
                matches = filler
        # A raw text mentioning alternatives does not establish a quantity per
        # service. Keep quantity NULL for multiple matches to avoid inventing a
        # package quantity; a single explicit service can safely use the text quantity.
        quantity = quantity_from_text(text) if len(matches) == 1 else None
        item_specs = [
            {"service_id": int(service["service_id"]), "quantity": quantity}
            for service in sorted(matches, key=lambda row: int(row["service_id"]))
        ] or [{"service_id": None, "quantity": quantity}]
        planned.append(
            {
                "offer_id": int(offer["id"]),
                "business_id": business_id,
                "offer_raw_text": text,
                "old_offer": offer,
                "match_names": sorted(names),
                "matched_service_ids": [int(service["service_id"]) for service in matches],
                "items": item_specs,
                "resolution": "matched" if matches else "unresolved",
            }
        )

    batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return {
        "batch_id": batch_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "rule_version": "promo_offer_items_from_offer_raw_text_v1",
        "target_count": len(targets),
        "matched_offer_count": sum(1 for row in planned if row["resolution"] == "matched"),
        "unresolved_offer_count": sum(1 for row in planned if row["resolution"] == "unresolved"),
        "planned_item_count": sum(len(row["items"]) for row in planned),
        "offers": planned,
    }


def build_sql(plan: dict[str, Any]) -> str:
    rows: list[tuple[Any, ...]] = []
    for offer in plan["offers"]:
        for item in offer["items"]:
            rows.append((offer["offer_id"], item["service_id"], item["quantity"]))
    values = ",\n".join("(" + ", ".join(sql_literal(v) for v in row) + ")" for row in rows)
    target_ids = ", ".join(str(row["offer_id"]) for row in plan["offers"])
    return f"""-- Frozen batch {plan['batch_id']} generated by backfill_missing_offer_items.py
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';
SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;

CREATE TEMP TABLE stage_offer_items (
    stage_id bigint generated always as identity primary key,
    offer_id bigint not null,
    service_id bigint,
    quantity smallint
) ON COMMIT PRESERVE ROWS;
INSERT INTO stage_offer_items(offer_id, service_id, quantity) VALUES
{values};

SELECT id FROM public.promo_offer_master WHERE id IN ({target_ids}) ORDER BY id FOR UPDATE;
SELECT offer_id FROM public.promo_offer_items WHERE offer_id IN ({target_ids}) ORDER BY offer_id, offer_item_id FOR UPDATE;

-- Concurrent workers may have created an item or filled regular_price after
-- the preview. Drop those offers from this frozen batch instead of touching
-- them; the remaining rows still satisfy the original predicates.
DELETE FROM stage_offer_items s
WHERE EXISTS (
    SELECT 1 FROM public.promo_offer_master o
    WHERE o.id = s.offer_id AND o.regular_price IS NOT NULL
)
OR EXISTS (
    SELECT 1 FROM public.promo_offer_items i
    WHERE i.offer_id = s.offer_id
);
CREATE TEMP TABLE stage_offer_ids ON COMMIT PRESERVE ROWS AS
SELECT DISTINCT offer_id FROM stage_offer_items;

INSERT INTO public.promo_offer_items(offer_id, service_id, quantity)
SELECT offer_id, service_id, quantity
FROM stage_offer_items;

DO $$
DECLARE n bigint;
BEGIN
SELECT count(*) INTO n
  FROM public.promo_offer_items i
  WHERE i.offer_id IN (SELECT offer_id FROM stage_offer_ids);
  IF n <> (SELECT count(*) FROM stage_offer_items) THEN
    RAISE EXCEPTION 'item postcondition failed: expected %, got %', (SELECT count(*) FROM stage_offer_items), n;
  END IF;
END $$;

COMMIT;

SELECT jsonb_build_object(
  'batch_id', {sql_literal(plan['batch_id'])},
  'target_offers', {plan['target_count']},
  'matched_offers', {plan['matched_offer_count']},
  'unresolved_offers', {plan['unresolved_offer_count']},
  'staged_offers_after_concurrency_filter', (SELECT count(*) FROM stage_offer_ids),
  'inserted_items', (SELECT count(*) FROM stage_offer_items)
) AS result;
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    plan = build_plan()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    preview_path = args.output_dir / f"missing_offer_items_preview_{plan['batch_id']}.json"
    sql_path = args.output_dir / f"missing_offer_items_{plan['batch_id']}.sql"
    preview_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    sql_path.write_text(build_sql(plan), encoding="utf-8")
    print(json.dumps({"preview": str(preview_path), "sql": str(sql_path), "target_count": plan["target_count"], "matched_offer_count": plan["matched_offer_count"], "unresolved_offer_count": plan["unresolved_offer_count"], "planned_item_count": plan["planned_item_count"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
