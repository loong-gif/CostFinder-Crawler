#!/usr/bin/env python3
"""Create a reproducible, read-only promo-offer price quality report."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.promo_offer_price_quality import audit_offer_prices  # noqa: E402
from utils.supabase_rest import SupabaseRestClient  # noqa: E402
from utils.supabase_access import fetch_all_rows, load_supabase_client  # noqa: E402


SELECT = (
    "id,regular_price,discount_price,discount_amount,discount_percent,price_model,is_package,"
    "offer_raw_text,updated_at,"
    "promo_offer_items(offer_item_id,service_id,quantity:delivered_units,unit_price,updated_at)"
)
PAGE_SIZE = 1000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "output" / "results"))
    return parser.parse_args()


def fetch_all(client: SupabaseRestClient) -> list[dict[str, Any]]:
    return fetch_all_rows(client, "promo_offer_master", SELECT, page_size=PAGE_SIZE, order="id.asc")


def main() -> int:
    args = parse_args()
    report = audit_offer_prices(fetch_all(load_supabase_client(PROJECT_ROOT, role="reader")))
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"promo_offer_price_audit_{stamp}.csv"
    json_path = output_dir / f"promo_offer_price_audit_{stamp}.json"
    rows = [issue.as_row() for issue in report.issues]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["offer_id", "check_category", "rule_name", "severity", "expected", "actual", "raw_text_snippet"])
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "audited_at": datetime.now().isoformat(),
        "issue_count": len(rows),
        "offer_count": len({row["offer_id"] for row in rows}),
        "by_rule": dict(Counter(row["rule_name"] for row in rows)),
        "csv_path": str(csv_path),
    }
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**summary, "json_path": str(json_path)}, ensure_ascii=False))
    return 1 if rows else 0


if __name__ == "__main__":
    raise SystemExit(main())
