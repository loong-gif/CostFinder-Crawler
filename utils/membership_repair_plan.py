from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from utils.membership_plan_lookup import normalize_plan_name
from utils.membership_repair_common import (
    DEFAULT_FIRECRAWL_URL,
    clean_url,
    normalize_benefits,
    number,
    same_number,
    source_key,
    token_text,
)
from utils.membership_repair_extraction import extraction_plans, page_classification

VALID_ACTIONS = {
    "KEEP",
    "UPDATE_CONTENT",
    "UPDATE_SOURCE",
    "NORMALIZE",
    "MERGE_DUPLICATE",
    "DELETE_NOT_MEMBERSHIP",
    "REVIEW_UNVERIFIED",
}
KNOWN_DUPLICATE_PAIRS = (
    (303, 441),
    (289, 325),
    (307, 440),
    (319, 504),
    (481, 509),
    (525, 567),
    (304, 442),
    (323, 511),
    (360, 361),
    (428, 429),
    (454, 455),
    (620, 621),
    (622, 623),
)


def plan_match_score(old: Dict[str, Any], new: Dict[str, Any]) -> float:
    old_name = normalize_plan_name(str(old.get("membership_name") or ""))
    new_name = normalize_plan_name(str(new.get("membership_name") or ""))
    if not old_name or not new_name:
        return 0.0
    if old_name == new_name:
        return 1.0
    old_tokens, new_tokens = set(token_text(old_name).split()), set(token_text(new_name).split())
    overlap = len(old_tokens & new_tokens) / max(1, len(old_tokens | new_tokens))
    fuzzy = SequenceMatcher(None, old_name, new_name).ratio()
    price_bonus = 0.1 if same_number(old.get("membership_price"), new.get("membership_price")) else 0.0
    return max(overlap, fuzzy * 0.85) + price_bonus


def find_plan_match(old: Dict[str, Any], plans: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not plans:
        return None
    scored = sorted(((plan_match_score(old, plan), plan) for plan in plans), key=lambda item: item[0], reverse=True)
    score, best = scored[0]
    if score >= 0.55 or (len(plans) == 1 and score >= 0.35):
        return best
    old_price = number(old.get("membership_price"))
    if old_price is not None:
        same_price = [plan for plan in plans if same_number(old_price, plan.get("membership_price"))]
        if len(same_price) == 1:
            return same_price[0]
    return None


def choose_effective_page(source_url: str, main: Dict[str, Any], main_extraction: Dict[str, Any], candidates: Sequence[Tuple[Dict[str, Any], Dict[str, Any]]], rows: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    related = [row for row in rows if source_key(row.get("source_url")) == source_key(source_url)]
    best_page, best_extraction = main, main_extraction
    best_score = -1.0
    for page, extraction in [(main, main_extraction), *candidates]:
        plans = extraction_plans(extraction)
        if not plans:
            continue
        score = 1.0 if not page.get("fallback") else 0.2
        for row in related:
            score = max(score, max((plan_match_score(row, plan) for plan in plans), default=0.0))
        if "/membership" in urlparse(str(page.get("final_url") or "")).path.casefold() or "club" in str(page.get("final_url") or "").casefold():
            score += 0.15
        if score > best_score:
            best_page, best_extraction, best_score = page, extraction, score
    return best_page, best_extraction


def evidence_text(page: Dict[str, Any], extraction: Dict[str, Any], plan: Optional[Dict[str, Any]] = None) -> str:
    result = extraction.get("result") or {}
    markdown = str(page.get("markdown") or "")
    terms = []
    if plan:
        terms.extend([str(plan.get("membership_name") or ""), str(plan.get("membership_price") or "")])
    snippets: List[str] = []
    lower = markdown.casefold()
    for term in terms:
        key = str(term).strip().casefold()
        if not key:
            continue
        index = lower.find(key)
        if index >= 0:
            snippets.append(re.sub(r"\s+", " ", markdown[max(0, index - 160) : index + 420]).strip())
    return " | ".join(
        part
        for part in [
            f"Firecrawl status={page.get('http_status')}; requested={page.get('requested_url')}; final={page.get('final_url')}; title={page.get('page_title')}",
            f"classification={page_classification(extraction)}",
            str(result.get("explanation") or "").strip(),
            *snippets[:3],
        ]
        if part
    )[:5000]


def build_offer_actions(old_offers: Sequence[Dict[str, Any]], desired: Sequence[Dict[str, Any]], *, plan_id: int, changed: bool) -> List[Dict[str, Any]]:
    if not changed:
        return []
    desired = [
        item
        for item in desired
        if any(item.get(field) is not None for field in ("discount_price", "discount_percent", "discount_amount"))
    ]
    active = [row for row in old_offers if row.get("is_active")]
    actions: List[Dict[str, Any]] = []
    used: set[int] = set()
    for item in desired:
        match = None
        for row in active:
            if int(row.get("id")) in used:
                continue
            if token_text(row.get("offer_raw_text")) == token_text(item.get("offer_raw_text")):
                match = row
                break
        if match:
            used.add(int(match["id"]))
            actions.append({"action": "UPDATE_OFFER", "offer_id": int(match["id"]), "desired": item})
        else:
            actions.append({"action": "INSERT_OFFER", "offer_id": None, "desired": item})
    for row in active:
        if int(row.get("id")) not in used:
            actions.append({"action": "DEACTIVATE_OFFER", "offer_id": int(row["id"]), "desired": None})
    return actions


def build_dry_run(snapshot: Dict[str, Any], pages: Sequence[Dict[str, Any]], extractions: Sequence[Dict[str, Any]], *, artifact_dir: Path) -> Dict[str, Any]:
    rows = snapshot["memberships"]
    offers = snapshot["offers"]
    by_source: Dict[str, List[Dict[str, Any]]] = {}
    for page in pages:
        by_source.setdefault(source_key(page.get("requested_url")), []).append(page)
    ex_by_key = {(source_key(item.get("requested_url")), bool(item.get("fallback"))): item for item in extractions}

    plans: List[Dict[str, Any]] = []
    for row in rows:
        source = source_key(row.get("source_url"))
        variant_pages = [
            page
            for raw_source, source_pages in by_source.items()
            if clean_url(raw_source) == clean_url(source)
            for page in source_pages
        ]
        main_variants = [page for page in variant_pages if not page.get("fallback")]
        main_variants.sort(
            key=lambda page: (
                bool(extraction_plans(ex_by_key.get((source_key(page.get("requested_url")), False), {}))),
                page.get("http_status") == 200,
                bool(page.get("markdown")),
                not bool(page.get("error")),
            ),
            reverse=True,
        )
        main_page = main_variants[0] if main_variants else {"requested_url": source, "final_url": source, "http_status": None, "markdown": "", "error": "missing_scrape"}
        main_extraction = ex_by_key.get((source_key(main_page.get("requested_url")), False), {"result": {}, "error": "missing_extraction"})
        candidates = []
        for page in variant_pages:
            if page is main_page:
                continue
            extraction = ex_by_key.get((source_key(page.get("requested_url")), bool(page.get("fallback"))), {"result": {}, "error": "missing_extraction"})
            candidates.append((page, extraction))
        page, extraction = choose_effective_page(source, main_page, main_extraction, candidates, rows)
        current_plans = extraction_plans(extraction)
        match = find_plan_match(row, current_plans)
        old_benefits = normalize_benefits(row.get("benefits"))
        classification = page_classification(extraction)
        record: Dict[str, Any] = {
            "plan_id": row.get("plan_id"),
            "business_id": row.get("business_id"),
            "requested_url": row.get("source_url"),
            "final_url": page.get("final_url"),
            "membership_name_old": row.get("membership_name"),
            "membership_name_new": match.get("membership_name") if match else None,
            "price_old": row.get("membership_price"),
            "price_new": match.get("membership_price") if match else None,
            "billing_old": row.get("billing_period"),
            "billing_new": match.get("billing_period") if match else None,
            "commitment_old": row.get("minimum_commitment_months"),
            "commitment_new": match.get("minimum_commitment_months") if match else None,
            "benefits_old": row.get("benefits"),
            "benefits_new": match.get("benefits") if match else None,
            "new_priced_offers": match.get("priced_offers", []) if match else [],
            "dependent_offer_count": sum(1 for offer in offers if offer.get("membership_plan_id") == row.get("plan_id")),
            "classification": classification,
            "evidence": evidence_text(page, extraction, match),
            "page_error": page.get("error") or extraction.get("error"),
            "survivor_plan_id": None,
        }
        if match:
            semantic_changed = any(
                [
                    str(row.get("membership_name") or "").strip() != str(match.get("membership_name") or "").strip(),
                    not same_number(row.get("membership_price"), match.get("membership_price")),
                    row.get("billing_period") != match.get("billing_period"),
                    row.get("minimum_commitment_months") != match.get("minimum_commitment_months"),
                    old_benefits != normalize_benefits(match.get("benefits")),
                ]
            )
            source_changed = clean_url(row.get("source_url")) != clean_url(page.get("final_url"))
            if semantic_changed:
                record["action"] = "UPDATE_CONTENT"
            elif source_changed:
                record["action"] = "UPDATE_SOURCE"
            elif normalize_benefits(row.get("benefits")) != row.get("benefits"):
                record["action"] = "NORMALIZE"
            else:
                record["action"] = "KEEP"
        elif classification in {"service", "package", "promotion", "loyalty"}:
            record["action"] = "DELETE_NOT_MEMBERSHIP"
        elif (
            "package" in token_text(row.get("membership_name"))
            and re.search(r"treatment\s+package|purchase\s+\d+\s+.*treatment", str(page.get("markdown") or ""), re.IGNORECASE)
        ):
            record["action"] = "DELETE_NOT_MEMBERSHIP"
            record["reason"] = "current page evidence describes a treatment package, not the formal membership plan"
        else:
            record["action"] = "REVIEW_UNVERIFIED"
        plans.append(record)

    by_id = {int(plan["plan_id"]): plan for plan in plans}
    for left, right in KNOWN_DUPLICATE_PAIRS:
        if left not in by_id or right not in by_id:
            continue
        left_row = next(row for row in rows if int(row["plan_id"]) == left)
        right_row = next(row for row in rows if int(row["plan_id"]) == right)
        if left_row.get("business_id") != right_row.get("business_id"):
            continue
        left_plan, right_plan = by_id[left], by_id[right]
        if left_plan.get("classification") != "membership" and right_plan.get("classification") != "membership":
            continue
        if not left_plan.get("membership_name_new") or not right_plan.get("membership_name_new"):
            continue
        name_similarity = SequenceMatcher(None, token_text(left_plan["membership_name_new"]), token_text(right_plan["membership_name_new"])).ratio()
        if name_similarity < 0.55 and left_plan["membership_name_new"] != right_plan["membership_name_new"]:
            continue
        left_url_score = 1 if "/membership" in urlparse(str(left_plan.get("final_url") or "")).path.casefold() else 0
        right_url_score = 1 if "/membership" in urlparse(str(right_plan.get("final_url") or "")).path.casefold() else 0
        survivor = left if (left_url_score, len(normalize_benefits(left_plan.get("benefits_old")) or []), -left) >= (right_url_score, len(normalize_benefits(right_plan.get("benefits_old")) or []), -right) else right
        loser = right if survivor == left else left
        by_id[loser]["action"] = "MERGE_DUPLICATE"
        by_id[loser]["survivor_plan_id"] = survivor
        by_id[loser]["reason"] = f"same business_id and current plan identity as survivor {survivor}"
        by_id[survivor].setdefault("reason", "current canonical survivor for duplicate plan")

    for plan in plans:
        if plan["action"] in {"UPDATE_CONTENT", "DELETE_NOT_MEMBERSHIP"}:
            plan["offer_actions"] = build_offer_actions(
                [offer for offer in offers if offer.get("membership_plan_id") == plan["plan_id"]],
                plan.get("new_priced_offers") or [],
                plan_id=int(plan["plan_id"]),
                changed=True,
            )
        elif plan["action"] == "MERGE_DUPLICATE":
            plan["offer_actions"] = [
                {"action": "REPOINT_OFFER", "offer_id": int(offer["id"]), "survivor_plan_id": plan["survivor_plan_id"]}
                for offer in offers
                if offer.get("membership_plan_id") == plan["plan_id"]
            ]
        else:
            plan["offer_actions"] = []

    action_counts: Dict[str, int] = {}
    for plan in plans:
        action_counts[plan["action"]] = action_counts.get(plan["action"], 0) + 1
    dry_run = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_plan": "CostFinder clinic_memberships 全表修复执行方案.md",
        "firecrawl_base_url": os.getenv("COSTFINDER_FIRECRAWL_URL", DEFAULT_FIRECRAWL_URL),
        "schema_note": "live clinic_memberships does not expose is_offer_extracted; no schema change was made",
        "membership_count": len(plans),
        "action_counts": action_counts,
        "plans": sorted(plans, key=lambda item: int(item["plan_id"])),
        "artifacts": {"directory": str(artifact_dir)},
    }
    if not plans or sum(action_counts.values()) != len(plans) or not set(action_counts).issubset(VALID_ACTIONS):
        raise RuntimeError(f"dry-run completeness gate failed: count={len(plans)} actions={action_counts}")
    return dry_run
