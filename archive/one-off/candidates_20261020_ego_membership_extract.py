import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "one-off"))

from candidates_20261020_extract import load_schemas, validate_extraction
from utils.monitor_price_store import atomic_json

OUT = ROOT / "one-off/outputs/candidates_20261020"
ROW = 64
CAPTURE_NAME = "scrape_ego_browser_000.json"


def main():
    capture_path = OUT / "requests" / str(ROW) / CAPTURE_NAME
    extraction_path = OUT / "extraction" / str(ROW) / CAPTURE_NAME
    assert capture_path.exists() and not extraction_path.exists()
    capture = json.loads(capture_path.read_text())
    markdown = capture["response"]["data"]["markdown"]
    schemas = load_schemas()
    memberships = {
        "explanation": "Rendered membership page lists three explicitly priced monthly plans. The page states that all plans renew monthly and can be cancelled anytime; minimum commitment is recorded as zero.",
        "memberships": [
            {
                "membership_name": "GLOW",
                "membership_price": 99,
                "billing_period": "monthly",
                "minimum_commitment_months": 0,
                "membership_description": "PERFECT START\nYour essential monthly ritual\nGLOW\n$99/mo",
                "benefits": [
                    "INCLUDES 1 MONTHLY SERVICE (CHOOSE ONE)", "Classic Facial",
                    "Laser Hair Removal – Small Area", "INJECTABLE CREDIT", "$50/mo (3-mo rollover)",
                    "Botox, Fillers, Sculptra & More", "DISCOUNTS", "10% Off Skincare Products",
                    "$20 Off Hydrafacial® Upgrade", "ADDITIONAL BENEFITS", "$25 Birthday Credit",
                    "Priority Event Access", "All plans renew monthly. Cancel anytime. Credits roll over for 3 months.",
                ],
            },
            {
                "membership_name": "RADIANT",
                "membership_price": 179,
                "billing_period": "monthly",
                "minimum_commitment_months": 0,
                "membership_description": "MOST POPULAR\nVisible results, real savings\nRADIANT\n$179/mo",
                "benefits": [
                    "INCLUDES 1 MONTHLY SERVICE (CHOOSE ONE)", "Hydrafacial® Signature",
                    "Laser Hair Removal – Medium Area", "INJECTABLE CREDIT", "$100/mo (3-mo rollover)",
                    "Botox, Fillers, Sculptra & More", "DISCOUNTS", "15% Off Skincare Products",
                    "10% Off Skincare Treatments", "10% Off Laser Packages", "ADDITIONAL BENEFITS",
                    "$50 Birthday Credit", "Priority Booking (7 Days)", "VIP Event Invites",
                    "All plans renew monthly. Cancel anytime. Credits roll over for 3 months.",
                ],
            },
            {
                "membership_name": "ELITE ULALA",
                "membership_price": 299,
                "billing_period": "monthly",
                "minimum_commitment_months": 0,
                "membership_description": "BEST VALUE\nThe full VIP experience\nELITE ULALA\n$299/mo",
                "benefits": [
                    "INCLUDES 1 LUXURY SERVICE (CHOOSE ONE)", "Hydrafacial® Platinum",
                    "Laser Hair Removal – Large Area", "INJECTABLE CREDIT", "$150/mo (3-mo rollover)",
                    "Botox, Fillers, Sculptra & More", "DISCOUNTS", "20% Off Skincare Products",
                    "15% Off Skincare Treatments", "20% Off Laser Packages", "ADDITIONAL BENEFITS",
                    "$75 Birthday Credit", "VIP Booking (3–5 Days)", "Exclusive Event Access",
                    "ANNUAL VIP PERK · CHOOSE ONE", "Lip Flip", "$100 Off Fillers", "$150 Off Biostimulators",
                    "All plans renew monthly. Cancel anytime. Credits roll over for 3 months.",
                ],
            },
        ],
    }
    promotions = {"explanation": "The rendered page describes paid membership tiers and does not list a separate limited-time promotion.", "promotions": []}
    validate_extraction("memberships", memberships, schemas["memberships"], markdown)
    validate_extraction("promotions", promotions, schemas["promotions"], markdown)
    atomic_json(extraction_path, {
        "business_id": 3515,
        "input_row": ROW,
        "source_url": "https://ulalamedspa.com/memberships",
        "captured_at": capture["captured_at"],
        "capture_path": str(capture_path.relative_to(ROOT)),
        "api_url": "ego_browser_rendered_dom",
        "status": "dry_run_extracted",
        "extractions": {"promotions": promotions, "memberships": memberships},
        "source_review_required": True,
        "review_notes": ["ego_browser_rendered_dom", "current_visibility_receipt_not_bound", "direct_fallback_after_firecrawl_scrape_failure"],
        "extraction_method": "manual_from_rendered_dom",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    print(json.dumps({"input_row": ROW, "memberships": len(memberships["memberships"]), "database_writes": 0, "capture_path": str(capture_path.relative_to(ROOT)), "extraction_path": str(extraction_path.relative_to(ROOT))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
