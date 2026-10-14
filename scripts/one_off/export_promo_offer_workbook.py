#!/usr/bin/env python3
"""Export promo offer tables to xlsx matching QA workbook layout."""

from __future__ import annotations

import argparse
import csv
import io
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

QA_HEADERS = [
    "qa_status",
    "qa_notes",
    None,
    None,
    "offer_id",
    "business_id",
    "promotion_id",
    "membership_plan_id",
    "source_type",
    "source_business_id",
    "source_title",
    "source_url",
    "item_names",
    "offer_raw_text",
    "regular_price",
    "discount_price",
    "discount_percent",
    "discount_amount",
    "service_ids",
    "matched_service_names",
    "service_business_ids",
    "quantities",
    "unit_prices",
    "is_membership_required",
    "item_count",
    "offer_item_ids",
    "service_categories",
    "conditions",
    "source_content",
    "service_regular_prices",
    "service_unit_types",
    "service_areas",
    "service_source_urls",
    "source_start_date",
    "source_end_date",
    "source_membership_price",
    "source_billing_period",
    "source_minimum_commitment_months",
]

TABLE_QUERIES = {
    "promo_offer_master_rows": """
        SELECT id, offer_raw_text, discount_percent, discount_amount, regular_price,
               discount_price, is_membership_required, created_at, business_id,
               membership_plan_id, offer_fingerprint, promotion_id, is_active,
               is_new_customer_required, price_model, updated_at, moderation_status,
               moderation_notes,
               CASE
                 WHEN conditions IS NULL OR cardinality(conditions) = 0 THEN NULL
                 ELSE (SELECT json_agg(elem)::text FROM unnest(conditions) AS elem)
               END AS conditions
        FROM public.promo_offer_master
        ORDER BY id
    """,
    "promo_offer_items_rows": """
        SELECT offer_item_id, offer_id, service_id, quantity, created_at, updated_at,
               unit_price, "check", reason, item_name
        FROM public.promo_offer_items
        ORDER BY offer_item_id
    """,
    "clinic_promotions_rows": """
        SELECT promotion_id, business_id, promotion_title, source_url, campaign_start_date,
               campaign_end_date, created_at, updated_at, is_active, promotion_content,
               is_offer_extracted, is_ocr_required
        FROM public.clinic_promotions
        ORDER BY promotion_id
    """,
    "clinic_memberships_rows": """
        SELECT plan_id, business_id, membership_name, membership_price, billing_period,
               minimum_commitment_months, created_at, updated_at, benefits, source_url,
               is_ocr_required, is_offer_extracted
        FROM public.clinic_memberships
        ORDER BY plan_id
    """,
    "clinic_services_rows": """
        SELECT service_id, business_id, service_name, regular_price, unit_type, service_area,
               created_at, updated_at, source_url, service_category
        FROM public.clinic_services
        ORDER BY service_id
    """,
}


def run_query(sql: str, retries: int = 4) -> list[list[str]]:
    last_err = ""
    for attempt in range(retries):
        proc = subprocess.run(
            ["supabase", "db", "query", "--linked", "-o", "csv"],
            input=sql,
            text=True,
            capture_output=True,
            cwd=ROOT,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            reader = csv.reader(io.StringIO(proc.stdout))
            return list(reader)
        last_err = (proc.stderr or proc.stdout or "empty output").strip()
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"supabase db query failed after {retries} tries: {last_err}")


def write_sheet(ws, rows: list[list[str]]) -> None:
    for r_idx, row in enumerate(rows, start=1):
        for c_idx, val in enumerate(row, start=1):
            if val == "" or val == "NULL":
                ws.cell(row=r_idx, column=c_idx, value=None)
            else:
                ws.cell(row=r_idx, column=c_idx, value=val)


def export(out_path: Path) -> None:
    import openpyxl

    qa_sql = (ROOT / "scripts/one_off/export_promo_offer_workbook.sql").read_text()
    qa_rows = run_query(qa_sql)
    if not qa_rows:
        raise RuntimeError("QA query returned no rows")

    # Replace SQL header names with template header labels (keep blank cols).
    qa_rows[0] = [h if h is not None else "" for h in QA_HEADERS[: len(qa_rows[0])]]

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    ws_qa = wb.create_sheet("promo_offer_master_qa")
    write_sheet(ws_qa, qa_rows)

    for sheet_name, sql in TABLE_QUERIES.items():
        rows = run_query(sql)
        ws = wb.create_sheet(sheet_name)
        write_sheet(ws, rows)
        if sheet_name == "promo_offer_master_rows":
            ws.sheet_state = "hidden"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--out",
        type=Path,
        default=ROOT / "output" / "promo_offer_master_rows.xlsx",
    )
    args = ap.parse_args()
    export(args.out)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
