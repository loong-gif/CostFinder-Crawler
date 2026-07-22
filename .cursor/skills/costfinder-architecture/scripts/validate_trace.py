#!/usr/bin/env python3
"""Assert architecture trace JSON matches CostFinder pipeline invariants."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def load_trace(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def extraction_rows(trace: dict[str, Any]) -> list[dict[str, Any]]:
    rows = trace.get("extractions") or []
    return [r for r in rows if isinstance(r, dict)]


def relation_rows(trace: dict[str, Any]) -> list[dict[str, Any]]:
    rows = trace.get("relations") or []
    return [r for r in rows if isinstance(r, dict)]


def validate(trace: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    forbidden = trace.get("forbidden") or {}
    legacy_membership = forbidden.get("legacy_membership_table", "promo_membership_plans")
    forbidden_master_service = forbidden.get("master_service_id_column", "service_id")

    for row in extraction_rows(trace):
        target = row.get("target_table") or row.get("target_tables")
        targets = target if isinstance(target, list) else [target]
        for t in targets:
            if t == legacy_membership:
                errors.append(f"{row.get('step')}: target must be clinic_memberships, not {legacy_membership}")
        inp = row.get("input")
        schema = str(row.get("schema") or "")
        if schema.endswith("service_extraction_schema.json") and inp != "firecrawl_search_raw":
            errors.append(f"{row.get('step')}: services must read firecrawl_search_raw, got {inp}")
        if schema.endswith("membership_extraction_schema.json") and inp != "firecrawl_search_raw":
            errors.append(f"{row.get('step')}: membership must read firecrawl_search_raw, got {inp}")
        if schema.endswith("promotion_extraction_schema.json") and inp != "firecrawl_scrape_raw":
            errors.append(f"{row.get('step')}: promotions must read firecrawl_scrape_raw, got {inp}")
        if schema.endswith("offer_extraction_schema.json"):
            if inp != "firecrawl_scrape_raw":
                errors.append(f"{row.get('step')}: offers must read firecrawl_scrape_raw, got {inp}")
            if target == "promo_offer_master" and "promo_offer_items" not in (row.get("target_tables") or []):
                errors.append(f"{row.get('step')}: offer extraction must include promo_offer_items")

    for rel in relation_rows(trace):
        if rel.get("from_table") == "promo_offer_master" and rel.get("fk_column") == forbidden_master_service:
            errors.append("service FK must be on promo_offer_items.service_id, not promo_offer_master.service_id")

    has_items_service_fk = any(
        r.get("from_table") == "promo_offer_items"
        and r.get("fk_column") == "service_id"
        and r.get("to_table") == "clinic_services"
        for r in relation_rows(trace)
    )
    offer_steps = [r for r in extraction_rows(trace) if "offer_extraction_schema" in str(r.get("schema"))]
    if offer_steps and not has_items_service_fk:
        errors.append("missing promo_offer_items.service_id → clinic_services relation")

    has_membership_fk = any(
        r.get("from_table") == "promo_offer_master"
        and r.get("fk_column") == "membership_plan_id"
        and r.get("to_table") == "clinic_memberships"
        for r in relation_rows(trace)
    )
    if offer_steps and not has_membership_fk:
        errors.append("missing promo_offer_master.membership_plan_id → clinic_memberships relation")

    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: validate_trace.py <trace.json>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    trace = load_trace(path)
    errors = validate(trace)
    if errors:
        print(f"FAIL {path.name}")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"OK {path.name} ({trace.get('domain', '?')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
