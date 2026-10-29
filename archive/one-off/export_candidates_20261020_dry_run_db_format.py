import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "one-off/outputs/candidates_20261020"
OUTPUT_DIR = SOURCE_DIR / "db_format_export_20261024"
TABLES = {
    "clinic_memberships": {
        "input": "memberships_dry_run.json",
        "columns": [
            "plan_id", "business_id", "membership_name", "membership_price",
            "billing_period", "minimum_commitment_months", "created_at", "updated_at",
            "benefits", "source_url", "is_ocr_required", "is_offer_extracted", "need_browser",
        ],
    },
    "clinic_promotions": {
        "input": "promotions_dry_run.json",
        "columns": [
            "promotion_id", "business_id", "promotion_title", "source_url",
            "campaign_start_date", "campaign_end_date", "created_at", "updated_at",
            "is_active", "promotion_content", "is_offer_extracted", "is_ocr_required",
            "screenshot", "requires_cf_verification",
        ],
    },
}
AUDIT_COLUMNS = [
    "dry_run_action", "dry_run_reason", "dry_run_evidence_status", "dry_run_record_json",
]


def csv_value(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def export_table(table, config):
    dry_run = json.loads((SOURCE_DIR / config["input"]).read_text())
    assert dry_run["mode"] == "dry_run" and dry_run["database_writes"] == 0
    assert dry_run["table"] == table and len(dry_run["records"]) == dry_run["record_count"]
    columns = config["columns"]
    output = OUTPUT_DIR / f"{table}_dry_run_full.csv"
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns + AUDIT_COLUMNS, extrasaction="raise")
        writer.writeheader()
        for record in dry_run["records"]:
            values = record.get("insert_fields") or {}
            assert set(values) <= set(columns)
            row = {column: csv_value(values.get(column)) for column in columns}
            row.update({
                "dry_run_action": record["action"],
                "dry_run_reason": record.get("reason"),
                "dry_run_evidence_status": record.get("evidence_status"),
                "dry_run_record_json": json.dumps(record, ensure_ascii=False, separators=(",", ":")),
            })
            writer.writerow(row)
    with output.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == dry_run["record_count"]
    assert [row["dry_run_action"] for row in rows].count("review") == dry_run["action_counts"]["review"]
    assert all(
        json.loads(row["dry_run_record_json"]) == record
        for row, record in zip(rows, dry_run["records"])
    )
    assert all(
        row[column] == str(csv_value((record.get("insert_fields") or {}).get(column)))
        for row, record in zip(rows, dry_run["records"])
        for column in columns
    )
    return output, len(rows)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for table, config in TABLES.items():
        output, count = export_table(table, config)
        print(json.dumps({"table": table, "rows": count, "file": str(output)}))


if __name__ == "__main__":
    main()
