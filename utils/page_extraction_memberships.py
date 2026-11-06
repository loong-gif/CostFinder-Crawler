from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from utils.domain_pipeline_search import llm_extract
from utils.membership_evidence import prepare_membership_write
from utils.page_extraction_common import _iso, _rejection, _visibility_for
from utils.page_extraction_snapshot import _cache_name, _cached, _page_evidence
from utils.promo_extraction import comparison_key
from utils.rendered_visibility_gate import image_claim_verified
from utils.source_url import source_url_key
from utils.visibility_manifest import membership_fee_claim

MEMBERSHIP_TASK = """Extract every purchasable membership tier separately, using the plan name and fee exactly as shown; never invent, rename or combine a plan, and skip a tier whose name is not shown. For each tier, list the monthly fee and credit, included service choices and limits, gifts and frequency, discounts, duration, rollover, cancellation, exclusions, and eligibility, one fact per benefit. Rephrasing is allowed, but every service, price, percentage and quantity in a benefit must appear on the page for that tier. Do not join statements from different places on the page, borrow terms from another tier, add explanations, advice, inferences, computed values or placeholders, or infer content from missing images. Use null for benefits when the page states none for the tier."""


def _name_and_fee_visible(page: Mapping[str, Any], name: str, price: float) -> bool:
    """可见性清单中方案名称与“名称 $费用”两条声明都已核验可见。"""
    results = {c["claim"]: c for c in page.get("claimResults") or []}
    for claim in (name, membership_fee_claim(name, price)):
        result = results.get(claim)
        if result is None or not (result["status"] == "visible_match" or (
                result["status"] == "image_only" and image_claim_verified(claim, result, page))):
            return False
    return True


def _plan_memberships(pages: list[dict[str, Any]], *, scrapes, existing, llm, schema, cache_dir,
                      manifest) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    accepted: dict[tuple[int, str, str], dict[str, Any]] = {}
    conflicted: set[tuple[int, str, str]] = set()
    for page in pages:
        row = scrapes.get(source_url_key(page["url"]))
        if row is None:
            entries.append(_rejection("membership", page["business_id"], page["url"], "no_scrape"))
            continue
        if manifest is not None and _visibility_for(manifest, page["url"]) is None:
            entries.append(_rejection("membership", page["business_id"], page["url"], "not_in_visibility_manifest"))
            continue
        evidence = _page_evidence(row)
        review = page["_source_review"]
        if review["page_type"] not in {"membership", "mixed"} or not review["memberships"]:
            entries.append(_rejection("membership", page["business_id"], page["url"], "source_page_not_membership",
                                      source_review=review))
            continue
        name = _cache_name("membership", row, evidence, llm=llm, schema=schema, prompt=MEMBERSHIP_TASK)
        payload = _cached(cache_dir, name, lambda: llm_extract(
            llm, schema, task=MEMBERSHIP_TASK, source_url=page["url"], markdown=evidence))
        items = payload.get("memberships") or []
        if not items:
            entries.append(_rejection("membership", page["business_id"], page["url"], "no_memberships"))
            continue
        source_url = page["url"].strip().rstrip("/")
        key = source_url_key(source_url)
        for item in items:
            name = str(item.get("membership_name") or "").strip()
            matches = [plan for plan in review["memberships"] if comparison_key(plan["name"]) == comparison_key(name)]
            if len(matches) > 1:
                # 来源清单中同名计划不止一个，会员身份有歧义，进入复核
                entries.append(_rejection("membership", page["business_id"], page["url"],
                                          "ambiguous_membership_source_inventory", membership_name=name,
                                          source_review=review))
                continue
            source_plan = matches[0] if matches else None
            source_claims = source_plan["source_claims"] if source_plan else []
            ocr_meta = row.get("ocr_meta") or []
            image_urls = row.get("images") or []
            image_ocr_complete = (not image_urls or
                                  bool(str(row.get("markdown_ocr") or "").strip()) and
                                  len(ocr_meta) == len(image_urls) and
                                  all(isinstance(image, dict) and image.get("n_lines", 0) > 0 and not image.get("error")
                                      for image in ocr_meta))
            decision = prepare_membership_write(
                item, evidence, _visibility_for(manifest, page["url"]) if manifest is not None else None,
                source_claims=source_claims,
                ocr_complete=image_ocr_complete)
            if not decision["accepted"]:
                entries.append(_rejection("membership", page["business_id"], page["url"], decision["reason"],
                                          membership_name=name, source_review=review, ocr_meta=ocr_meta))
                continue
            if manifest is not None and not _name_and_fee_visible(
                    _visibility_for(manifest, page["url"]), name, decision["membership_price"]):
                # 方案名称或费用在渲染页面上看不到：名称可能是编造的，或费用属于其他方案
                entries.append(_rejection("membership", page["business_id"], page["url"],
                                          "membership_name_or_fee_not_visible", membership_name=name,
                                          membership_price=decision["membership_price"], source_review=review))
                continue
            identity = (page["business_id"], key, name.casefold())
            candidate = {"kind": "membership", "business_id": page["business_id"], "source_url": source_url,
                         "target_key": {"business_id": page["business_id"], "source_url_key": key,
                                        "membership_name_key": name.casefold()},
                         "membership_name": name, "membership_price": decision["membership_price"],
                         "source_claims": source_claims,
                         "verified_benefits": decision.get("verified_benefits"),
                         "missing_claims": decision.get("missing_claims", []),
                         "benefits_quality": "pass" if decision["complete"] else "review",
                         "benefits_review_reason": decision["reason"] if source_plan else "membership_missing_from_source_inventory",
                         "ocr_status": "complete" if image_ocr_complete else "pending",
                         "source_review": {"url": page["url"], "scrape_id": row.get("id"),
                                           "request_fingerprint": row.get("request_fingerprint"),
                                           "updated_at": row.get("updated_at"),
                                           "ocr_meta": row.get("ocr_meta"), "review": review},
                         "billing_period": item.get("billing_period"),
                         "minimum_commitment_months": item.get("minimum_commitment_months"),
                         "benefits": decision["benefits"], "source_pages": [page["url"]],
                         "scrape_fingerprints": [row["request_fingerprint"]]}
            if identity in conflicted:
                entries.append({**candidate, "action": "rejected", "reason": "conflicting_duplicate_membership"})
                continue
            previous = accepted.get(identity)
            if previous is None:
                accepted[identity] = candidate
                continue
            same_terms = all(previous[f] == candidate[f] for f in
                             ("membership_price", "billing_period", "minimum_commitment_months"))
            if not same_terms:
                # 同一会员出现互相矛盾的条款时全部拒绝，结果与页面顺序无关
                conflicted.add(identity)
                accepted.pop(identity)
                entries.extend({**c, "action": "rejected", "reason": "conflicting_duplicate_membership"}
                               for c in (previous, candidate))
                continue
            if candidate["benefits"] is not None:
                merged = list(previous["benefits"] or [])
                merged.extend(b for b in candidate["benefits"] if b not in merged)
                previous["benefits"] = merged
            previous["source_pages"].extend(candidate["source_pages"])
            previous["scrape_fingerprints"].extend(candidate["scrape_fingerprints"])

    for identity, entry in sorted(accepted.items()):
        old = existing.get(identity)
        if old is None:
            entries.append({**entry, "action": "insert", "reason": "new_row"})
            continue
        old_price = None if old["membership_price"] is None else float(old["membership_price"])
        same = (old_price == entry["membership_price"]
                and old["billing_period"] == entry["billing_period"]
                and old["minimum_commitment_months"] == entry["minimum_commitment_months"]
                and (entry["benefits"] is None or old["benefits"] == entry["benefits"]))
        entries.append({**entry, "action": "unchanged" if same else "update",
                        "reason": "identical" if same else "row_differs", "plan_id": old["plan_id"],
                        "expected_updated_at": _iso(old["updated_at"])})
    return entries
