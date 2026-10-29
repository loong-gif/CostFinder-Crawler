import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "one-off"))

from candidates_20261020_extract import OFFERS_CONTRACT, load_schemas, validate_extraction
from candidates_20261020_parallel import load_rows
from utils.monitor_price_store import atomic_json

OUT = ROOT / "one-off/outputs/candidates_20261020"
NULL_OFFER = {"regular_price": None, "discount_price": None, "discount_percent": None,
              "discount_amount": None, "min_quantity": None, "is_membership_required": False,
              "is_new_customer_required": False, "conditions": []}


def item(name, service=None, quantity=None, unit_price=None, unit_type=None, area=None):
    return {"item_name": name, "service_name": service, "quantity": quantity,
            "unit_price": unit_price, "unit_type": unit_type, "service_area": area}


def offer(name, raw, items, *, purchase="single", package=False, model="total", **values):
    return {"purchase_type": purchase, "is_package": package, "price_model": model,
            **NULL_OFFER, **values, "offer_raw_text": raw, "items": items}


def plan(name, price, quote, benefits, commitment=None):
    return {"membership_name": name, "membership_price": price, "billing_period": "monthly",
            "minimum_commitment_months": commitment, "membership_description": quote,
            "benefits": benefits}


def empty(kind, reason):
    return {"explanation": reason, kind: []}


def main():
    rows = {row["input_row"]: row for row in load_rows()}
    schemas = load_schemas()
    sources = {
        (19, "scrape_ego_browser_000.json"): {
            "promotions": {"explanation": "The page lists past events and Saturday appointments dated September 12 and October 3 without a year; no current priced event offer is stated.", "promotions": [
                {"promotion_title": "NOW BOOKING Saturday Appointments with Loren, NP", "promotion_content": ["NOW BOOKING", "Saturday Appointments with Loren, NP", "September 12th & October 3rd · Because self care is always in", "Select Saturdays are now open with our Nurse Practitioner, Loren. Book your appointment for injectables, skin rejuvenation, or wellness services — Saturday spots are limited and fill quickly."], "campaign_start_date": None, "campaign_end_date": None},
                {"promotion_title": "Drop In & Refresh — A Mother's Day Celebration", "promotion_content": ["PAST EVENT · MAY 2026", "Tuesday, May 5th · 9 A.M. – 5 P.M.", "Drop In & Refresh — A Mother's Day Celebration", "Complimentary B-12 Injections", "Champagne & Sweet Treats", "Fresh Flowers for Our Honored Guests", "Exclusive Same-Day Botox Pricing", "Easy Gift Card Pickup"], "campaign_start_date": None, "campaign_end_date": None},
            ]},
            "memberships": {"explanation": "The page states a $99 monthly Founders Membership, 12-month commitment and 30-day written cancellation notice.", "memberships": [
                plan("Founders Membership", 99, "The Founders Membership\n$99\nPER MONTH", ["15% OFF ALL SERVICES", "50% OFF BIRTHDAY FILLER", "$10 PER UNIT — BIRTHDAY TOX", "Limit 1 syringe per birthday month.", "Up to 25 units per birthday month.", "Retail Skincare & Skincare Services", "All Wellness Services", "All Fillers & Tox", "12 MONTH COMMITMENT", "30 DAY WRITTEN NOTICE", "Membership auto-renews monthly.", "Members must provide written 30-day notice to cancel."], 12)
            ]},
            "offers": empty("offers", "Events mention complimentary items or unspecified exclusive pricing but contain no concrete purchasable price."),
        },
        (19, "scrape_ego_browser_001.json"): {
            "promotions": empty("promotions", "The source is a paid membership page; no separate limited-time campaign is stated."),
            "memberships": {"explanation": "The page states the paid $99 monthly Founders plan, 15% standard-price service discounts, birthday benefits and a 12-month commitment.", "memberships": [
                plan("Founders Membership", 99, "The Founders Membership\n$99\nPER MONTH", ["15% OFF ALL SERVICES", "50% OFF BIRTHDAY FILLER", "$10 PER UNIT — BIRTHDAY TOX", "Limit 1 syringe per birthday month.", "Up to 25 units per birthday month.", "Retail Skincare & Skincare Services", "All Wellness Services", "All Fillers & Tox", "12 MONTH COMMITMENT", "30 DAY WRITTEN NOTICE", "Membership auto-renews monthly.", "Members must provide written 30-day notice to cancel."], 12)
            ]},
            "offers": empty("offers", "Prices on the source are illustrative estimated savings, not purchasable treatment offers."),
        },
        (377, "scrape_ego_browser_000.json"): {
            "promotions": {"explanation": "The page lists a seasonal package with a September 30 expiry but no year; remaining offers have no explicit dates.", "promotions": [
                {"promotion_title": "New Client Welcome Package", "promotion_content": ["NEW CLIENTS", "New Client Welcome Package", "Free skin analysis with advanced imaging", "10% off your first treatment", "Complimentary aftercare kit", "SAVE UP TO $150"], "campaign_start_date": None, "campaign_end_date": None},
                {"promotion_title": "Summer Glow Package", "promotion_content": ["SEASONAL", "Summer Glow Package", "3× HydraFacial Platinum sessions", "3× LED light therapy add-ons", "Complimentary SPF skincare bundle", "Valid through September 30", "$1,200", "$899", "SAVE $301"], "campaign_start_date": None, "campaign_end_date": None},
                {"promotion_title": "Botox & Filler Refresh Bundle", "promotion_content": ["Botox & Filler Refresh Bundle", "20 units Botox + 1 syringe filler", "Starting at $699", "SAVE UP TO $200"], "campaign_start_date": None, "campaign_end_date": None},
                {"promotion_title": "Laser Hair Removal — 6-Session Package", "promotion_content": ["Laser Hair Removal — 6-Session Package", "6 sessions for any single area", "$900+", "Starting at $599", "SAVE 30%+"], "campaign_start_date": None, "campaign_end_date": None},
                {"promotion_title": "RF Microneedling + PRP Combo", "promotion_content": ["RF Microneedling + PRP Combo", "3× RF microneedling sessions", "PRP add-on each session", "Starting at $1,499", "SAVE $400"], "campaign_start_date": None, "campaign_end_date": None},
            ]},
            "memberships": empty("memberships", "The specials page mentions a membership but has no explicit fee; the linked membership page has priced plans."),
            "offers": {"explanation": "Extracted concrete purchasable offers. Exact end year and starting-price totals remain unresolved as shown on the page.", "offers": [
                offer("New Client Welcome Package", "10% off your first treatment", [item("first treatment", "Others")], discount_percent=10, is_new_customer_required=True, conditions=[{"requirement": "First treatment only", "evidence": "10% off your first treatment"}]),
                offer("Summer Glow Package", "$1,200\n$899", [item("HydraFacial Platinum", "Others", 3, unit_type="session"), item("LED light therapy", "Others", 3, unit_type="session"), item("SPF skincare bundle", "Others")], purchase="bundle", package=True, discount_price=899, regular_price=1200, conditions=[{"requirement": "Valid through September 30; year not stated", "evidence": "Valid through September 30"}]),
                offer("Botox & Filler Refresh Bundle", "20 units Botox + 1 syringe filler\nStarting at $699", [item("Botox", "Botox", 20, unit_type="unit"), item("filler", "Dermal Filler", 1, unit_type="syringe")], purchase="bundle", package=True, model="from", discount_price=699),
                offer("Laser Hair Removal — 6-Session Package", "6 sessions for any single area\nStarting at $599", [item("Laser Hair Removal", "Others", 6, unit_type="session", area="any single area")], purchase="bundle", package=True, model="from", regular_price=None, discount_price=599),
                offer("RF Microneedling + PRP Combo", "3× RF microneedling sessions\nPRP add-on each session\nStarting at $1,499", [item("RF microneedling", "Others", 3, unit_type="session"), item("PRP add-on", "Others", 3, unit_type="session")], purchase="bundle", package=True, model="from", discount_price=1499),
            ]},
        },
        (377, "scrape_ego_browser_001.json"): {
            "promotions": empty("promotions", "This page describes the ongoing paid membership tiers; no separate campaign dates are stated."),
            "memberships": {"explanation": "Three paid monthly tiers are explicitly priced. The page does not state a minimum commitment.", "memberships": [
                plan("Essential", 199, "Essential\n$199\n/month", ["10% off all individual treatments", "1 complimentary HydraFacial every 12 months", "Birthday month bonus treatment", "Monthly Complimentary Service", "one complimentary compression massage per month"]),
                plan("Premium", 299, "Premium\n$299\n/month", ["15% off all individual treatments", "2 complimentary HydraFacials every 12 months", "Same-day booking availability", "Birthday month bonus treatment", "Free skincare consultation every quarter", "10% off treatment packages", "one complimentary compression massage per month"]),
                plan("VIP", 399, "VIP\n$399\n/month", ["20% off all individual treatments", "4 complimentary HydraFacials every 12 months", "Same-day booking availability", "Birthday month bonus treatment", "Free skincare consultation every quarter", "15% off treatment packages", "Complimentary product samples quarterly", "First access to new treatments", "one complimentary compression massage per month"]),
            ]},
            "offers": empty("offers", "The page is a membership plan page; its benefits are recorded with the membership tiers."),
        },
        (529, "scrape_ego_browser_000.json"): {
            "promotions": {"explanation": "The page gives Fall campaigns but no year or exact campaign dates.", "promotions": [
                {"promotion_title": "Fall for Collagen", "promotion_content": ["Fall Physician Promotion", "Fall for Collagen", "Buy 2 syringes of Radiesse, get the 3rd on us.", "Investment: $1,800", "($2,700 value)", "available through november"], "campaign_start_date": None, "campaign_end_date": None},
                {"promotion_title": "3 Ways to Tackle Uneven Skin Tone", "promotion_content": ["3 Ways to Tackle Uneven Skin Tone", "Bundle includes:", "The Perfect Derma™ Chemical Peel", "Limelight Laser Facial", "Ourself Brightening Complexion Serum", "Investment: $500 ($789 value)"], "campaign_start_date": None, "campaign_end_date": None},
            ]},
            "memberships": empty("memberships", "Seasonal specials page contains no paid membership fee."),
            "offers": {"explanation": "Two bundled promotions have explicit package prices. Campaign year is not stated.", "offers": [
                offer("Fall for Collagen", "Buy 2 syringes of Radiesse, get the 3rd on us.\nInvestment: $1,800\n($2,700 value)", [item("Radiesse", "Radiesse", 3, unit_type="syringe")], purchase="bundle", package=True, discount_price=1800, min_quantity=2, conditions=[{"requirement": "Purchase 2 syringes and receive the 3rd", "evidence": "Buy 2 syringes of Radiesse, get the 3rd on us."}]),
                offer("3 Ways to Tackle Uneven Skin Tone", "Bundle includes:\nThe Perfect Derma™ Chemical Peel\nLimelight Laser Facial\nOurself Brightening Complexion Serum\nInvestment: $500 ($789 value)", [item("The Perfect Derma™ Chemical Peel", "Others"), item("Limelight Laser Facial", "Others"), item("Ourself Brightening Complexion Serum", "Others")], purchase="bundle", package=True, discount_price=500),
            ]},
        },
        (529, "scrape_ego_browser_001.json"): {
            "promotions": empty("promotions", "This membership page lists paid plans and ongoing member benefits, with no time-limited campaign."),
            "memberships": {"explanation": "The page explicitly lists three paid monthly clubs and their benefits; no minimum commitment is stated.", "memberships": [
                plan("Facial Club", 100, "facial club $100/mo", ["60 min customized facial", "option to add advance exfolation for $50", "$1 off per tox", "10% off all esthetician services", "10% off all retail skincare", "50% off one PerfectDermaTM peel per year", "$50 off any service during birthday month"]),
                plan("Laser Club", 225, "laser club $225/mo", ["limelight signature laser facial", "$1 off per tox", "10% off all esthetician services", "10% off all retail skincare", "50% off one PerfectDermaTM peel per year", "$50 off any service during birthday month"]),
                plan("Tox Club", 130, "tox club $130/mo", ["tox pricing locked at $11.50/unit ($1.50 savings)", "$1 off per tox", "10% off all esthetician services", "10% off all retail skincare", "50% off one PerfectDermaTM peel per year", "$50 off any service during birthday month"]),
            ]},
            "offers": empty("offers", "The page's tox and treatment pricing is part of membership benefits; no standalone promotion is listed."),
        },
        (529, "scrape_ego_browser_002.json"): {
            "promotions": empty("promotions", "The article explains the same paid Skin Club plans and does not describe a separate promotion."),
            "memberships": {"explanation": "The March 4, 2026 article restates three paid monthly Skin Club plans. They duplicate plans on the membership page, so source identity review is retained.", "memberships": [
                plan("Facial Club", 100, "Facial Club\n$100 per month", ["One 60-minute customized facial by advanced esthetician Logan Paszek each month", "Optional advanced exfoliation add-on for just $50", "$1 off per tox (wrinkle relaxer) unit", "10% off all esthetician services", "10% off all retail skincare", "50% off one PerfectDerma™ peel per year", "$50 off any service during your birthday month", "No long-term contracts or hidden fees"]),
                plan("Laser Club", 225, "Laser Club\n$225 per month", ["Includes the Limelight Signature Laser Facial", "$1 off per tox (wrinkle relaxer) unit", "10% off all esthetician services", "10% off all retail skincare", "50% off one PerfectDerma™ peel per year", "$50 off any service during your birthday month", "No long-term contracts or hidden fees"]),
                plan("Tox Club", 130, "Tox Club\n$130 per month", ["Tox (wrinkle relaxer) pricing locked at $11.50 per unit", "$1 off per tox (wrinkle relaxer) unit", "10% off all esthetician services", "10% off all retail skincare", "50% off one PerfectDerma™ peel per year", "$50 off any service during your birthday month", "No long-term contracts or hidden fees"]),
            ]},
            "offers": empty("offers", "This article restates the Skin Club membership benefits and contains no separate standalone promotion."),
        },
    }
    written = 0
    already_present = 0
    for (row_number, capture_name), extractions in sources.items():
        capture_path = OUT / "requests" / str(row_number) / capture_name
        extraction_path = OUT / "extraction" / str(row_number) / capture_name
        assert capture_path.exists(), f"capture_missing:{row_number}:{capture_name}"
        capture = json.loads(capture_path.read_text())
        if extraction_path.exists():
            existing = json.loads(extraction_path.read_text())
            assert existing["captured_at"] == capture["captured_at"] and existing["source_url"] == capture["request"]["url"]
            already_present += 1
            continue
        markdown = capture["response"]["data"]["markdown"]
        for kind, payload in extractions.items():
            validate_extraction(kind, payload, schemas[kind], markdown)
        source_url = capture["request"]["url"]
        record = {"business_id": rows[row_number]["business_id"], "input_row": row_number,
                  "source_url": source_url, "captured_at": capture["captured_at"],
                  "capture_path": str(capture_path.relative_to(ROOT)), "api_url": "ego_browser_rendered_dom",
                  "status": "dry_run_extracted", "extractions": extractions,
                  "extraction_contracts": {"offers": OFFERS_CONTRACT},
                  "source_review_required": True,
                  "review_notes": ["ego_browser_rendered_dom", "current_visibility_receipt_not_bound", "source_identity_or_campaign_date_review"],
                  "extraction_method": "manual_from_rendered_dom", "updated_at": datetime.now(timezone.utc).isoformat()}
        atomic_json(extraction_path, record)
        written += 1
    assert written + already_present == 7
    print(json.dumps({"extractions_written": written, "already_present": already_present, "input_rows": sorted({row for row, _ in sources}),
                      "database_writes": 0, "schema_changed": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
