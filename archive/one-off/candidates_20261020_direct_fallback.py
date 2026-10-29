import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "one-off"))

from candidates_20261020_extract import load_schemas, validate_extraction
from candidates_20261020_parallel import load_rows
from utils.monitor_price_store import atomic_json

OUT = ROOT / "one-off/outputs/candidates_20261020"


def promotion(title, content):
    return {"promotion_title": title, "promotion_content": content,
            "campaign_start_date": None, "campaign_end_date": None}


def no_promotions(reason):
    return {"explanation": reason, "promotions": []}


def no_memberships(reason):
    return {"explanation": reason, "memberships": []}


PAGES = [
    {
        "row": 389, "index": 0, "url": "https://trifectamedspanyc.com/specials",
        "promotions": {
            "explanation": "Direct HTTPS fallback source contains three October offers. Campaign year is not stated in the offer dates, so both date fields remain null.",
            "promotions": [
                promotion("Brow Lift", [
                    "Sofwave or Ultherapy brow treatment · $450",
                    "Choose between Sofwave or Ultherapy to lift and define your brows without surgery. A precise, non-invasive upgrade for the season ahead.",
                    "Sofwave Brow Lift", "$450", "Ultherapy Brow Lift", "$450",
                    "Offers end October 31. Complimentary toxin units apply to same-day service. Offers cannot be combined with other discounts. Subject to medical consultation.",
                ]),
                promotion("Post-Summer Skin Refresh", [
                    "2 for $400", "Two IPL Treatments · $400",
                    "Explore IPL for the appearance of sun spots, uneven tone, and redness. Two sessions designed to restore clarity and even skin tone after summer exposure.",
                    "Two IPL Treatments", "$400",
                    "Offers end October 31. Complimentary toxin units apply to same-day service. Offers cannot be combined with other discounts. Subject to medical consultation.",
                ]),
                promotion("October Toxin Bonus", [
                    "Purchase 30–49 units and receive 5 complimentary units, or purchase 50+ units and receive 10 complimentary units. Complimentary toxin units apply to same-day service.",
                    "Purchase 30–49 units", "Receive 5 complimentary units",
                    "Purchase 50+ units", "Receive 10 complimentary units",
                    "Offers end October 31. Complimentary toxin units apply to same-day service. Offers cannot be combined with other discounts. Subject to medical consultation.",
                ]),
            ],
        },
        "memberships": no_memberships("The specials page contains no paid membership plan."),
    },
    {
        "row": 389, "index": 1, "url": "https://trifectamedspanyc.com/offers/new-client",
        "promotions": {
            "explanation": "This URL redirects to /specials. The distinct header welcome offer is retained here; the October offers are recorded on the canonical specials URL.",
            "promotions": [promotion("Welcome Offer", ["Welcome Offer: 20% Off Your First Visit"])],
        },
        "memberships": no_memberships("The redirected specials page contains no paid membership plan."),
    },
    {
        "row": 389, "index": 2, "url": "https://trifectamedspanyc.com/membership",
        "promotions": no_promotions("This page describes paid membership plans and member benefits; it has no separate campaign offer."),
        "memberships": {
            "explanation": "Two paid plans have explicit fees. The six-month minimum applies to the monthly plan only; the annual minimum commitment is left unknown.",
            "memberships": [
                {
                    "membership_name": "Annual Plan", "membership_price": 299,
                    "billing_period": "annual", "minimum_commitment_months": None,
                    "membership_description": "Annual Plan $299 /year Save $49 per year with annual billing",
                    "benefits": [
                        "30% Off One Treatment / Year", "Botox / Dysport $14 / unit",
                        "Xeomin $12 / unit", "Daxxify $7 / unit",
                        "Services & Products 10% Everyday Discount", "VIP Booking Access",
                        "Birthday Gift $50 Aesthetic Credit", "Flash Sales & VIP Previews",
                    ],
                },
                {
                    "membership_name": "Monthly Plan", "membership_price": 29,
                    "billing_period": "monthly", "minimum_commitment_months": 6,
                    "membership_description": "Monthly Plan $29 /mo Flexible monthly billing (6-month commitment)",
                    "benefits": [
                        "30% Off One Treatment / Year", "Botox / Dysport $14 / unit",
                        "Xeomin $12 / unit", "Daxxify $7 / unit",
                        "Services & Products 10% Everyday Discount", "VIP Booking Access",
                        "Birthday Gift $50 Aesthetic Credit", "Flash Sales & VIP Previews",
                    ],
                },
            ],
        },
    },
    {
        "row": 389, "index": 3, "url": "https://trifectamedspanyc.com/pricelist",
        "promotions": {
            "explanation": "Three explicitly priced filler bundles include stated savings and are retained as promotion-page content. Regular service catalog prices are outside these two dry-run tables.",
            "promotions": [promotion("Dermal Filler Packages", [
                "Multi-syringe bundled packages designed for maximum harmony & savings",
                "Any TWO fillers ($500 savings) + add-on Botox $14 per unit + Skincare products 10% off",
                "$1500 package total",
                "Any THREE fillers ($900 savings) / add-on Botox $13.5 per unit / Skincare products 10% off / Diamond Glow $200",
                "$2100 package total",
                "Any FOUR fillers ($1500 savings) / add-on Botox $13 per unit / Skincare products 10% off / Diamond Glow $200",
                "$2700 package total",
            ])],
        },
        "memberships": no_memberships("The price list page contains no paid membership plan."),
    },
    {
        "row": 389, "index": 4, "url": "https://trifectamedspanyc.com/location/park-avenue",
        "promotions": no_promotions("The location page lists clinic details and services without a concrete promotion.") ,
        "memberships": no_memberships("The location page contains no paid membership plan."),
    },
    {
        "row": 389, "index": 5, "url": "https://trifectamedspanyc.com/body/rf-skin-tightening",
        "promotions": {
            "explanation": "The page explicitly states a six-treatment package discount. The percentage is retained; no package total is inferred.",
            "promotions": [promotion("RF Skin Tightening Package", [
                "At Trifecta Med Spa in New York, we charge $300 per RF Skin Tightening session.",
                "We offer 20% off package discount with a purchase of six treatments.",
            ])],
        },
        "memberships": no_memberships("The treatment page contains no paid membership plan."),
    },
    {
        "row": 389, "index": 6, "url": "https://trifectamedspanyc.com/location/57th-street",
        "promotions": no_promotions("The location page lists clinic details and services without a concrete promotion."),
        "memberships": no_memberships("The location page contains no paid membership plan."),
    },
    {
        "row": 396, "index": 7, "url": "https://redcarpetaesthetics.com/offer/flat-rate-filler",
        "promotions": {
            "explanation": "The page states a limited-time introductory filler offer for new patients. Upgrade amounts are retained in source text without inferring final prices.",
            "promotions": [promotion("Dermal Filler Flat Rate", [
                "Dermal Filler Flat Rate - From $550 per syringe. New patients only.",
                "From $550* per 1cc syringe · RHA® Collection",
                "Limited-Time Offer",
                "Juvéderm® Ultra / Ultra Plus +$100",
                "Volbella® / Vollure® +$150",
                "Voluma® XC +$200",
                "*Subject to clinical evaluation, anatomy, and informed consent.",
            ])],
        },
        "memberships": no_memberships("The introductory offer page contains no paid membership plan."),
    },
    {
        "row": 396, "index": 8, "url": "https://redcarpetaesthetics.com/offer/flat-rate-neurotoxin",
        "promotions": {
            "explanation": "The page states a limited-time introductory $420 flat rate for three areas and restricts it to new patients. The optional product upgrade remains a separate stated amount.",
            "promotions": [promotion("Neurotoxin Flat Rate", [
                "Neurotoxin Flat Rate - $420 for three areas. New patients only.",
                "$420 Flat Rate · 3 FDA Areas", "Forehead, Glabella & Crow’s Feet",
                "Botox® or Daxxify® available for an additional investment of $150",
                "Complimentary refinement assessment within 14 days",
                "LIMITED-TIME INTRODUCTORY OFFER",
            ])],
        },
        "memberships": no_memberships("The introductory offer page contains no paid membership plan."),
    },
    {
        "row": 396, "index": 9, "url": "https://redcarpetaesthetics.com/services/glo2facial",
        "promotions": {
            "explanation": "The service page lists a three-session series with stated savings and a priced facial plus neurotoxin combination.",
            "promotions": [
                promotion("Glo2 Series of 3", [
                    "Glo2 Series of 3", "$675", "$225 / session · Save $75",
                    "Three complete Signature Glo2Facial sessions",
                    "Recommended for clinical transformation. Monthly sessions progressively stimulate collagen renewal, smooth skin tone, and maintain long-term glow.",
                ]),
                promotion("Glow & Tox Duo", [
                    "Glow & Tox Duo", "$475", "Glo2Facial + Face Tox Combo",
                    "Complete 3-in-1 Glo2Facial paired with targeted upper-face neurotoxin (Xeomin) for glass-skin tone and smoothed dynamic lines.",
                ]),
            ],
        },
        "memberships": no_memberships("The treatment page contains no paid membership plan."),
    },
    {
        "row": 396, "index": 10, "url": "https://redcarpetaesthetics.com/services/rf-skin-tightening",
        "promotions": {
            "explanation": "The page lists two packages with explicit savings and treatment counts. Single-session rates remain regular service pricing.",
            "promotions": [
                promotion("Face & Neck RF Lift", [
                    "Face & Neck RF Lift", "$350", "single session · Save $100",
                    "Comprehensive upper and lower rejuvenation covering full face, jawline contour, submental double chin, and horizontal neck bands.",
                ]),
                promotion("Series of 6 (Full Face)", [
                    "Series of 6 (Full Face)", "$1,200", "$200 / session · Save $300",
                    "Six full-face TriPollar® RF + DMA™ sessions",
                    "The recommended clinical protocol for long-lasting structural remodeling. Weekly sessions progressively tone facial muscles and maximize collagen density.",
                ]),
            ],
        },
        "memberships": no_memberships("The treatment page contains no paid membership plan."),
    },
]


def main():
    rows = {row["input_row"]: row for row in load_rows()}
    schemas = load_schemas()
    written = []
    for page in PAGES:
        row_number = page["row"]
        source_row = rows[row_number]
        html_path = OUT / "requests" / str(row_number) / f"http_fallback_{page['index']:03d}.html"
        capture_path = OUT / "requests" / str(row_number) / f"scrape_http_fallback_{page['index']:03d}.json"
        extraction_path = OUT / "extraction" / str(row_number) / capture_path.name
        assert source_row["business_id"] in {3828, 3835}
        assert html_path.exists() and not capture_path.exists() and not extraction_path.exists()
        soup = BeautifulSoup(html_path.read_bytes(), "html.parser")
        body = soup.body or soup
        for node in body.select("script,style,svg,noscript"):
            node.decompose()
        markdown = body.get_text("\n", strip=True)
        assert markdown
        captured_at = datetime.fromtimestamp(html_path.stat().st_mtime, timezone.utc).isoformat()
        for kind, payload in (("promotions", page["promotions"]), ("memberships", page["memberships"])):
            validate_extraction(kind, payload, schemas[kind], markdown)
        relative_capture = str(capture_path.relative_to(ROOT))
        atomic_json(capture_path, {
            "captured_at": captured_at,
            "capture_method": "direct_https_fallback_after_self_hosted_firecrawl_500",
            "source_html_path": str(html_path.relative_to(ROOT)),
            "request": {"url": page["url"], "method": "GET"},
            "response": {"success": True, "status_code": 200,
                         "final_url": "https://trifectamedspanyc.com/specials" if page["row"] == 389 and page["index"] == 1 else page["url"],
                         "data": {"markdown": markdown}},
        })
        atomic_json(extraction_path, {
            "business_id": source_row["business_id"], "input_row": row_number,
            "source_url": page["url"], "captured_at": captured_at,
            "capture_path": relative_capture, "api_url": "direct_https_fallback",
            "status": "dry_run_extracted", "extractions": {
                "promotions": page["promotions"], "memberships": page["memberships"],
            }, "source_review_required": True,
            "review_notes": [
                "direct_https_fallback_after_self_hosted_firecrawl_500",
                "current_dom_visibility_receipt_not_bound",
                "services_and_offers_kinds_not_in_this_two_table_fallback",
            ], "extraction_method": "direct_http_manual_review",
        })
        written.append({"input_row": row_number, "source_url": page["url"],
                        "capture_path": relative_capture,
                        "promotions": len(page["promotions"]["promotions"]),
                        "memberships": len(page["memberships"]["memberships"])})
    print(json.dumps({"pages": len(written), "written": written}, ensure_ascii=False))


if __name__ == "__main__":
    main()
