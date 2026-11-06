import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "one-off"))

from candidates_20261028_208_parallel import load_rows
from utils.clinic_service_taxonomy import live_clinic_service_payload
from utils.extraction_persist_pricing import infer_price_model
from utils.membership_paths import is_membership_page_url
from utils.membership_evidence import prepare_membership_write
from utils.monitor_price_store import atomic_json, now
from utils.offer_evidence_validator import _raw_text_supported, validate_offer_evidence
from utils.offer_purchase_semantics import normalize_purchase_offers
from utils.promo_extraction import merge_promotion_items
from utils.promo_paths import is_promotion_url_loose
from utils.promo_offer_items_db import is_blanket_offer_text
from utils.recent_raw_extraction import build_promotion_content, validate_promotion
from utils.rendered_visibility_gate import normalize_claim
from utils.service_price_guard import prepare_service_catalog_write

OUT = ROOT / "one-off/outputs/candidates_20261028_208"
KINDS = ("promotions", "memberships", "services", "offers")


def read(path):
    return json.loads(path.read_text())


def main():
    schema = read(ROOT / "schema/live_schema_snapshot.json")
    rows = load_rows()
    selected = {
        row["input_row"]
        for row in rows
        if row.get("master_verified_at") and not row.get("identity_excluded")
    }
    businesses = {row["input_row"]: row for row in rows if row["input_row"] in selected}
    records, pages = [], []
    memberships = {}

    def payload(table, values):
        if table == "clinic_services":
            values = live_clinic_service_payload(values)
        assert set(values) <= set(schema[table]), f"unknown_snapshot_fields:{table}:{set(values) - set(schema[table])}"
        return values

    for row_number, business in businesses.items():
        for capture_path in sorted((OUT / "requests" / str(row_number)).glob("scrape_*.json")):
            capture = read(capture_path)
            if not (capture.get("response", {}).get("data", {}).get("markdown") or "").strip():
                continue
            extraction_path = OUT / "extraction" / str(row_number) / capture_path.name
            receipt_path = OUT / "visibility" / str(row_number) / capture_path.name
            base = {"input_row": row_number, "business_id": business["business_id"],
                    "source_url": capture["request"]["url"], "captured_at": capture["captured_at"],
                    "capture_path": str(capture_path.relative_to(ROOT)),
                    "extraction_path": str(extraction_path.relative_to(ROOT)),
                    "visibility_path": str(receipt_path.relative_to(ROOT))}
            page = {**base, "status": "pending", "reason": "extraction_pending"}
            pages.append(page)
            if not extraction_path.exists():
                continue
            extraction = read(extraction_path)
            base["extraction_updated_at"] = extraction.get("updated_at")
            base["source_lifecycle"] = extraction.get("source_lifecycle")
            base["source_review_required"] = bool(extraction.get("source_review_required"))
            assert extraction["business_id"] == base["business_id"]
            assert extraction["captured_at"] == base["captured_at"]
            assert extraction["source_url"] == base["source_url"]
            missing = [kind for kind in KINDS if kind not in extraction.get("extractions", {})]
            if missing:
                page["reason"] = "incomplete_extraction:" + ",".join(missing)
            offers_current = extraction.get("extraction_contracts", {}).get("offers") == "source_item_name_v1"
            receipt = read(receipt_path) if receipt_path.exists() else {}
            base["visibility_captured_at"] = receipt.get("captured_at")
            bound = (receipt.get("business_id") == base["business_id"]
                     and receipt.get("source_url") == base["source_url"]
                     and receipt.get("source_captured_at") == base["captured_at"]
                     and receipt.get("status") == "page_accessible")
            visible = "\n".join(str(node.get("text") or "") for node in receipt.get("capture", {}).get("nodes", []) if node.get("visible") is True) if bound else ""
            capture_markdown = str((capture.get("response") or {}).get("data", {}).get("markdown") or "")
            # Named-fee evidence may use scrape markdown when scrolled visibility is not bound yet.
            membership_evidence = visible if bound else capture_markdown
            source = normalize_claim(visible)

            def supported(value):
                return bool(normalize_claim(value)) and normalize_claim(value) in source

            def add(table, original, values, ready, reason, **details):
                if base["source_lifecycle"] == "historical" or base["source_review_required"]:
                    ready, reason = False, "source_lifecycle_or_identity_requires_review"
                record = {**base, "plan_name": f"{row_number}:{capture_path.stem}:{table}:{len(records)}",
                          "table": table, "status": "ready" if ready else "review", "reason": reason,
                          "original": original, "insert_fields": payload(table, values), **details}
                records.append(record)
                return record

            extracted = {kind: extraction.get("extractions", {}).get(kind, {kind: []}) for kind in KINDS}
            page.update(status="planned" if offers_current and not missing else "pending",
                        reason=("incomplete_extraction:" + ",".join(missing)) if missing else (("visibility_bound" if bound else "current_visibility_pending") if offers_current else "offers_source_item_name_v1_pending"),
                        kind_status={kind: "pending" if kind in missing or kind == "offers" and not offers_current else "evaluated" for kind in KINDS},
                        extraction_counts={kind: None if kind in missing or kind == "offers" and not offers_current else len(extracted[kind][kind]) for kind in KINDS})
            common = {"business_id": base["business_id"], "source_url": base["source_url"]}
            for item in extracted["services"]["services"]:
                assert set(item) <= set(schema["clinic_services"]) | {"service_name_raw", "service_category_raw"}
                decision = prepare_service_catalog_write(item, source_url=base["source_url"], evidence=visible)
                quote = item.get("service_name_raw") or item.get("service_name")
                local = "\n".join(line for line in visible.splitlines() if normalize_claim(quote) in normalize_claim(line))
                price = item.get("regular_price")
                amount = re.escape(f"{float(price):g}") if price is not None else ""
                price_visible = price is not None and bool(local) and bool(re.search(r"\$\s*" + amount + r"(?:\.0{1,2})?(?![\d.])", local.replace(",", "")))
                ready = decision.accepted and supported(quote) and price_visible
                service = decision.normalized_item or item
                fields = {field: service.get(field) for field in ("service_name", "regular_price", "unit_type", "service_area", "service_category")}
                add("clinic_services", item, {**common, **fields}, ready,
                    decision.reason if ready or not decision.accepted else "service_name_price_layout_unverified")
            promotion_items = extracted["promotions"]["promotions"]
            membership_source = is_membership_page_url(base["source_url"]) and bool(extracted["memberships"]["memberships"])
            verified = []
            for item in promotion_items:
                if membership_source:
                    page["promotion_route"] = "membership"
                    continue
                claims = [item.get("promotion_title"), *(item.get("promotion_content") or [])]
                candidate = dict(item)
                commercial = " ".join(str(claim or "") for claim in claims)
                if not is_promotion_url_loose(base["source_url"]) and not re.search(r"\b(?:off|save|saving|special|promo|package|bundle|free|complimentary|buy)\b", commercial, re.I):
                    page["promotion_route"] = "catalogue_or_membership"
                    continue
                if all(supported(claim) for claim in claims) and validate_promotion(candidate, visible).accepted:
                    verified.append(item)
                else:
                    add("clinic_promotions", item, {**common, **item}, False, "complete_promotion_claims_unverified")
            if verified:
                scoped_evidence = "\n".join(str(value) for item in verified for value in [item["promotion_title"], *item["promotion_content"]])
                merged = merge_promotion_items(verified, scoped_evidence, source_url=base["source_url"])
                decision = validate_promotion(merged, visible)
                add("clinic_promotions", verified, {**common, **merged, "is_active": True}, decision.accepted,
                    decision.reason, anchor={"business_id": base["business_id"], "source_url": base["source_url"]})
            for item in extracted["memberships"]["memberships"]:
                assert set(item) <= set(schema["clinic_memberships"]) | {"membership_description"}
                membership_receipt = {**receipt, "claimResults": [
                    {"recordType": "membership", "claim": claim,
                     "status": "visible_match" if supported(claim) else "not_found"}
                    for claim in item.get("benefits") or []]}
                decision = prepare_membership_write(item, membership_evidence, membership_receipt if bound else None)
                ready = decision.get("accepted")
                complete = ready and decision.get("complete")
                values = {**common, **{field: item.get(field) for field in ("membership_name", "membership_price", "billing_period", "minimum_commitment_months")}, "benefits": decision.get("benefits")}
                record = add("clinic_memberships", item, values, ready, decision["reason"] if ready else "membership_named_fee_quote_unverified:" + decision["reason"],
                             is_offer_extracted=bool(complete))
                key = (base["business_id"], normalize_claim(item["membership_name"]), item["membership_price"])
                existing = memberships.get(key)
                if existing:
                    comparable = ("billing_period", "minimum_commitment_months", "benefits")
                    if all(existing["insert_fields"].get(field) == record["insert_fields"].get(field) for field in comparable):
                        record.update(action="reuse", reuse_plan_name=existing["plan_name"])
                    else:
                        record.update(status="review", reason="membership_duplicate_conflict")
                        existing.update(status="review", reason="membership_duplicate_conflict")
                else:
                    memberships[key] = record
            if not offers_current:
                page["offers_status"] = "pending_source_item_name_v1"
                continue
            offers, reviews = [], []
            for original_offer in extracted["offers"]["offers"]:
                normalized, findings = normalize_purchase_offers([original_offer], visible)
                source_names = {normalize_claim(item.get("item_name")) for item in original_offer.get("items") or []}
                changed_identity = any(not is_blanket_offer_text(item.get("offer_raw_text", "")) and any(
                    normalize_claim(component.get("item_name")) not in source_names for component in item.get("items") or [])
                    for item in normalized)
                if changed_identity:
                    components = original_offer.get("items") or []
                    single_named_item = (original_offer.get("purchase_type") == "single" and len(components) == 1
                                         and not re.search(r"\bor\b|或", components[0].get("item_name", ""), re.I))
                    if single_named_item:
                        offers.append(original_offer)
                    else:
                        reviews.append({**original_offer, "reason": "purchase_normalization_changed_commercial_identity"})
                else:
                    offers.extend(normalized)
                    reviews.extend(findings)
            for review in reviews:
                add("promo_offer_master", review, {}, False, review["reason"])
            for offer in offers:
                assert set(offer) <= set(schema["promo_offer_master"]) | {"purchase_type", "is_package", "items", "min_quantity", "source_offer_raw_text", "membership_name"}
                decision = validate_offer_evidence(offer, visible)
                candidate = decision.offer
                ready = decision.accepted and not decision.issues and _raw_text_supported(offer.get("source_offer_raw_text") or offer.get("offer_raw_text"), visible)
                blanket = is_blanket_offer_text(offer.get("offer_raw_text", ""))
                items = []
                for item in candidate.get("items") or []:
                    ready = ready and supported(item.get("item_name"))
                    items.append(payload("promo_offer_items", {"item_name": item.get("item_name"), "service_id": None,
                                 "delivered_units": item.get("quantity"), "unit_price": item.get("unit_price")}))
                offer_fields = {field: candidate.get(field) for field in ("regular_price", "discount_price", "discount_percent", "discount_amount", "is_membership_required", "offer_raw_text", "is_new_customer_required", "price_model", "conditions")}
                offer_fields["price_model"] = infer_price_model(candidate)
                offer_fields["conditions"] = [value["requirement"] if isinstance(value, dict) else value for value in candidate.get("conditions") or []]
                if offer_fields["price_model"] == "per_unit" and (not offer_fields.get("discount_price") or any(item.get("unit_price") is None for item in items)):
                    ready = False
                add("promo_offer_master", offer, {**offer_fields, "business_id": base["business_id"],
                    "membership_plan_id": None, "promotion_id": None, "is_active": True}, bool(ready),
                    decision.reason if ready else "complete_offer_claims_unverified:" + decision.reason,
                    item_insert_fields=items, blanket=blanket, is_offer_extracted=False,
                    source_reference={"business_id": base["business_id"], "source_url": base["source_url"],
                                      "membership_name": offer.get("membership_name")},
                    validation_issues=[asdict(issue) for issue in decision.issues])
            anchored = any(record["table"] == "clinic_promotions" and record["status"] == "ready" and record["business_id"] == base["business_id"] and record["source_url"] == base["source_url"] for record in records)
            verified_offers = [record for record in records if record["table"] == "promo_offer_master" and record["status"] == "ready" and record["business_id"] == base["business_id"] and record["source_url"] == base["source_url"]]
            title = receipt.get("capture", {}).get("title")
            if not anchored and verified_offers and title and not membership_source:
                content = list(dict.fromkeys(record["insert_fields"]["offer_raw_text"] for record in verified_offers))
                add("clinic_promotions", [record["original"] for record in verified_offers], {**common, "promotion_title": title, "promotion_content": content, "campaign_start_date": None, "campaign_end_date": None, "is_active": True}, True, "verified_offer_page_anchor")
    services = defaultdict(list)
    for record in records:
        if record["table"] == "clinic_services" and record["status"] == "ready":
            services[(record["business_id"], record["insert_fields"]["service_name"])].append(record)
    for group in services.values():
        identities = {(record["insert_fields"].get("regular_price"), record["insert_fields"].get("unit_type"), record["insert_fields"].get("service_area")) for record in group}
        if len(identities) > 1:
            for record in group:
                record.update(status="review", reason="catalogue_service_tier_price_conflict")
        else:
            for record in group[1:]:
                record.update(action="reuse", reuse_plan_name=group[0]["plan_name"])
    official = [record for record in records if record["table"] == "clinic_promotions" and record["status"] == "ready" and is_promotion_url_loose(record["source_url"])]
    for record in records:
        if record["table"] != "clinic_promotions" or record["status"] != "ready" or is_promotion_url_loose(record["source_url"]):
            continue
        facts = {normalize_claim(value) for value in record["insert_fields"]["promotion_content"] if re.search(r"[$%]\s*\d|\d\s*%", value)}
        matches = [other for other in official if other["business_id"] == record["business_id"] and facts and facts <= {normalize_claim(value) for value in other["insert_fields"]["promotion_content"]}]
        if len(matches) == 1:
            record.update(status="duplicate_source", reason="shared_promotion_component", canonical_source_url=matches[0]["source_url"], reuse_plan_name=matches[0]["plan_name"])
        elif len(matches) > 1:
            record.update(status="review", reason="shared_promotion_source_ambiguous")
    result = {"generated_at": now(), "mode": "dry_run", "external_requests": 0, "database_writes": 0,
              "pilot_rows": sorted(selected), "pages": pages, "records": records,
              "page_counts": dict(Counter(page["status"] for page in pages)),
              "record_counts": dict(Counter(record["status"] for record in records))}
    for record in records:
        assert set(record["insert_fields"]) <= set(schema[record["table"]])
        assert "is_offer_extracted" not in record["insert_fields"]
        assert "is_package" not in record["insert_fields"]
        if record.get("blanket"):
            assert all(item["service_id"] is None for item in record["item_insert_fields"])
    atomic_json(OUT / "full_plan.json", result)
    print(json.dumps({key: result[key] for key in ("page_counts", "record_counts", "external_requests", "database_writes")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
