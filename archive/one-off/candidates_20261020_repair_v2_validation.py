import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "one-off"))

from candidates_20261020_extract import TASKS, load_schemas, validate_extraction
from utils.monitor_price_store import atomic_json

OUT = ROOT / "one-off/outputs/candidates_20261020"
EMPTY = {
    "memberships": {"explanation": "The source does not expose a positively priced paid membership plan in captured text.", "memberships": []},
    "services": {"explanation": "No regular non-promotional unit price is unambiguously stated in captured text.", "services": []},
    "offers": {"explanation": "The returned offer claims were not supported by the captured page text; no purchasable offer is retained.", "offers": []},
}
REPAIRS = {
    (340, "scrape_v2_retry_000.json"): {
        "promotions": {"explanation": "The captured page contains only an image title for the event; offer terms and dates are not visible in the captured text.", "promotions": [
            {"promotion_title": "Swan Med Spa - Fall Into Radiance Event", "promotion_content": ["Swan Med Spa - Fall Into Radiance Event"], "campaign_start_date": None, "campaign_end_date": None}
        ]},
    },
    (340, "scrape_v2_retry_001.json"): {
        "promotions": {"explanation": "The page is labeled as a promotions page, but its captured text contains general service pricing without explicit campaign terms.", "promotions": []},
    },
    (340, "scrape_v2_retry_006.json"): {
        "promotions": {"explanation": "The page describes Laser Skin Therapy without a promotion or discount in captured text.", "promotions": []},
    },
    (340, "scrape_v2_retry_007.json"): {
        "promotions": {"explanation": "The page describes Microneedling without a promotion or discount in captured text.", "promotions": []},
    },
    (410, "scrape_v2_retry_000.json"): {
        "promotions": {"explanation": "The captured text exposes four promotion image titles but no prices, eligibility or offer terms; each remains for source review.", "promotions": [
            {"promotion_title": "Valentine's Day Couples & Besties Duo Promotion", "promotion_content": ["Valentine's Day Couples & Besties Duo Promotion"], "campaign_start_date": None, "campaign_end_date": None},
            {"promotion_title": "Valentine's Day Membership & La Mer Gift Promotion", "promotion_content": ["Valentine's Day Membership & La Mer Gift Promotion"], "campaign_start_date": None, "campaign_end_date": None},
            {"promotion_title": "Glowme Med Spa Top-up Bonus Promotions", "promotion_content": ["Glowme Med Spa Top-up Bonus Promotions"], "campaign_start_date": None, "campaign_end_date": None},
            {"promotion_title": "Glowme Med Spa Facial Promotions", "promotion_content": ["Glowme Med Spa Facial Promotions"], "campaign_start_date": None, "campaign_end_date": None},
        ]},
    },
    (747, "scrape_v2_retry_000.json"): {
        "promotions": {"explanation": "The page describes financing options but no clinic promotion in captured text.", "promotions": []},
    },
}


def main():
    schemas = load_schemas()
    fixed = []
    for (row, capture_name), changes in REPAIRS.items():
        capture_path = OUT / "requests" / str(row) / capture_name
        extraction_path = OUT / "extraction" / str(row) / capture_name
        capture = json.loads(capture_path.read_text())
        extraction = json.loads(extraction_path.read_text())
        markdown = capture["response"]["data"]["markdown"]
        extraction["extractions"].update(EMPTY)
        extraction["extractions"].update(changes)
        for kind in TASKS:
            validate_extraction(kind, extraction["extractions"][kind], schemas[kind], markdown)
        extraction["status"] = "dry_run_extracted"
        extraction["source_review_required"] = True
        extraction.setdefault("review_notes", []).append("manual_source_review_removed_unsupported_firecrawl_claims")
        extraction["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_json(extraction_path, extraction)
        fixed.append({"input_row": row, "capture": capture_name, "status": extraction["status"]})

    for extraction_path in (OUT / "extraction").glob("*/scrape_v2_retry_*.json"):
        extraction = json.loads(extraction_path.read_text())
        if extraction.get("status") == "dry_run_extracted":
            extraction["source_review_required"] = True
            notes = extraction.setdefault("review_notes", [])
            if "manual_content_review_required" not in notes:
                notes.append("manual_content_review_required")
            atomic_json(extraction_path, extraction)
    print(json.dumps({"repaired_validation_failures": fixed, "all_v2_retry_extractions_require_review": True,
                      "database_writes": 0, "schema_changed": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
