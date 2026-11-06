from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Mapping

from utils.linked_terms import attach_terms
from utils.page_extraction_common import _campaign_date, _iso, _merged_dates, _rejection, _visibility_for
from utils.page_extraction_snapshot import _cache_name, _cached, _page_evidence
from utils.page_extraction_source_review import _hub_note, _pair_with_source_cards, _page_order
from utils.page_title import TitleUnavailable, choose_title, title_candidates_from_scrape
from utils.promo_extraction import SYSTEM_PROMPT as PROMOTION_SYSTEM_PROMPT
from utils.promo_extraction import comparison_key, llm_extract_promotions, merge_content_blocks, merge_promotion_items
from utils.promotion_rules import PromotionRejected, check_promotion, today
from utils.rendered_visibility_gate import filter_visible_promotion_content, image_claim_verified
from utils.source_url import hub_url, is_hub_url, source_url_key

NO_VALIDATED_PROMOTIONS = "no validated promotions"


def _extract_promotion_page(page: Mapping[str, Any], row: Mapping[str, Any], *, llm: Any, schema: dict[str, Any],
                            cache_dir: Path, manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    """返回 {content,start,end} 或 {reject: code}。标题不在此处理。"""
    if manifest is not None and _visibility_for(manifest, page["url"]) is None:
        return {"reject": "not_in_visibility_manifest"}
    evidence = _page_evidence(row)
    review = page["_source_review"]
    if review["page_type"] not in {"promotion", "mixed"}:
        return {"reject": "source_page_not_promotion", "source_review": review}
    if not review["promotion_quotes"]:
        reason = "promotion_source_quote_not_verbatim" if review["unverified_quotes"] else "source_page_not_promotion"
        return {"reject": reason, "source_review": review}
    note = _hub_note(page["url"])
    name = _cache_name("promotion", row, evidence, llm=llm, schema=schema, prompt=PROMOTION_SYSTEM_PROMPT + note)
    payload = _cached(cache_dir, name, lambda: llm_extract_promotions(
        llm, source_url=page["url"], evidence=evidence, schema=schema, extra_instruction=note.strip()))
    items = payload.get("promotions") or []
    if not items:
        return {"reject": "no_promotions", "source_review": review}
    try:
        merged = merge_promotion_items(items, evidence, source_url=page["url"])
    except ValueError as exc:
        if str(exc) != NO_VALIDATED_PROMOTIONS:
            raise
        return {"reject": "no_validated_promotions", "source_review": review}
    paired = _pair_with_source_cards(merged["promotion_blocks"], review["promotion_quotes"])
    if paired is None or not paired[0]:
        return {"reject": "promotion_source_inventory_mismatch", "source_review": review}
    confirmed, used = paired
    # 条款段落按页面上的链接位置确定所属卡片，由代码追加（可见性清单在条款页上核验这些段落）
    blocks = attach_terms([merged["promotion_blocks"][index] for index in confirmed], row.get("linked_terms"))
    # 卡片按标题在页面上首次出现的位置排序，不随 LLM 返回的顺序变化
    position = _page_order(page["_evidence_key"])
    blocks.sort(key=lambda block: position(block[0]))
    if manifest is not None:
        cards = merge_content_blocks(blocks)
        gate = filter_visible_promotion_content(cards, _visibility_for(manifest, page["url"]))
        if not gate.accepted:
            return {"reject": gate.reason, "source_review": review}
        # 优惠卡片按完整关系验收，任一张卡片不可见时整页进入复核。
        if len(gate.content) != len(cards):
            return {"reject": "promotion_card_not_fully_visible", "source_review": review}
    return {"blocks": blocks, "start": _campaign_date(merged["campaign_start_date"]),
            "end": _campaign_date(merged["campaign_end_date"]), "source_review": review,
            "cards_not_in_source_review": [block[0] for index, block in enumerate(merged["promotion_blocks"])
                                           if index not in confirmed],
            "source_cards_not_extracted": [offer["title"] for index, offer in enumerate(review["promotion_quotes"])
                                           if index not in used]}


def _group_title(target_url: str, is_hub: bool, pages: list[Mapping[str, Any]], scrapes: Mapping[str, Mapping[str, Any]],
                 business_name: str, fetch_title: Callable[[str, str], str]) -> str:
    """标题只取自页面 <title>：hub 取 hub 页面，其他取页面自身；raw 中没有 scrape 的 hub 现抓。"""
    sources = [scrapes.get(source_url_key(target_url))]
    if not is_hub:
        sources += [scrapes.get(source_url_key(p["url"])) for p in pages]
    sources = [s for s in sources if s is not None]
    if not sources:
        return fetch_title(target_url, business_name)
    for scrape in sources:
        try:
            return choose_title(title_candidates_from_scrape(scrape), business_name)
        except TitleUnavailable:
            continue
    raise TitleUnavailable("no_usable_title")


def _superseded(card: str, fresh: list[str], target_key: str, pages: list[tuple[str, str]]) -> bool:
    """库中 hub 行的已有条目是否由本批结果取代。

    条目首行即卡片标题。同一标题出现在新卡片中，或该条目来自本批重新抽取的页面时取代：
    子页面以标题出现在页面上为准；hub 页面本身常列出全部子页面标题，须条目每一行都出现在页面上。
    """
    lines = [key for key in map(comparison_key, card.splitlines()) if key]
    if not lines:
        return True
    if lines[0] in {comparison_key(new.splitlines()[0]) for new in fresh}:
        return True
    return any(all(line in evidence for line in lines) if source_url_key(url) == target_key else lines[0] in evidence
               for url, evidence in pages)


def _plan_promotions(pages: list[dict[str, Any]], *, scrapes, names, existing, llm, schema, cache_dir, manifest,
                     fetch_title) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    extracted: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    # 本批每个目标行涉及的全部页面（包括被拒绝的页面）及其证据比对键，用于判定库中已有卡片的来源页面
    batch_pages: dict[tuple[int, str], list[tuple[str, str]]] = defaultdict(list)
    for page in pages:
        row = scrapes.get(source_url_key(page["url"]))
        if row is None:
            entries.append(_rejection("promotion", page["business_id"], page["url"], "no_scrape"))
            continue
        batch_pages[(page["business_id"], source_url_key(hub_url(page["url"]) or page["url"]))].append(
            (page["url"], page["_evidence_key"]))
        result = _extract_promotion_page(page, row, llm=llm, schema=schema, cache_dir=cache_dir, manifest=manifest)
        if "reject" in result:
            entries.append(_rejection("promotion", page["business_id"], page["url"], result["reject"],
                                      source_review=result.get("source_review")))
            continue
        # 每个页面单独判定过期，过期页面的内容不并入 hub
        if result["end"] is not None and result["end"] < today():
            entries.append(_rejection("promotion", page["business_id"], page["url"], "expired_campaign",
                                      campaign_end_date=_iso(result["end"])))
            continue
        hub = hub_url(page["url"])
        target = hub or page["url"]
        extracted[(page["business_id"], source_url_key(target))].append(
            {**page, **result, "hub": hub, "fingerprint": row["request_fingerprint"]})

    for (business_id, key), members in sorted(extracted.items()):
        members.sort(key=lambda m: m["url"])
        hub = next((m["hub"] for m in members if m["hub"]), None)
        exact = [m for m in members if source_url_key(m["url"]) == key]
        target_url = exact[0]["url"] if exact else hub
        old = existing.get((business_id, key))
        content = merge_content_blocks([block for member in members for block in member["blocks"]])
        start, end = _merged_dates(members)
        if (hub is not None or is_hub_url(target_url)) and old is not None:
            # hub 行由多个子页面组成：本批重新抽取过的页面以新卡片为准，其余子页面的已有卡片与已知日期保留。
            # 保留的已有卡片同样清理链接与标记残留；有可见性清单时，只保留在本组渲染页面上可见的卡片。
            kept = [merge_content_blocks([card.splitlines()])[0] for card in old["promotion_content"] or []
                    if merge_content_blocks([card.splitlines()])
                    and not _superseded(card, content, key, batch_pages[(business_id, key)])]
            if manifest is not None:
                kept = [card for card in kept if _visible_in_group(card, [m["url"] for m in members], manifest)]
            content = [card for card in kept if card not in content] + content
            if exact and len(members) == 1:
                # 本批抽取的就是 hub 页面本身：保留的已有卡片也在这个页面上，与新卡片一起按页面位置排序
                position = _page_order(exact[0]["_evidence_key"])
                content.sort(key=lambda card: position(card.splitlines()[0]))
            start = start or old["campaign_start_date"]
            end = end or old["campaign_end_date"]
        base ={"kind": "promotion", "business_id": business_id, "source_url": target_url,
                "target_key": {"business_id": business_id, "source_url_key": key},
                "source_pages": [m["url"] for m in members], "scrape_fingerprints": [m["fingerprint"] for m in members],
                "source_reviews": [{"url": m["url"], "scrape_id": scrapes[source_url_key(m["url"])].get("id"),
                                    "request_fingerprint": m["fingerprint"],
                                    "updated_at": scrapes[source_url_key(m["url"])].get("updated_at"),
                                    "ocr_meta": scrapes[source_url_key(m["url"])].get("ocr_meta"),
                                    "review": m["source_review"],
                                    "cards_not_in_source_review": m["cards_not_in_source_review"],
                                    "source_cards_not_extracted": m["source_cards_not_extracted"]} for m in members],
                # 卡片链接到的条款页：可见性核验时与促销页一并渲染
                "linked_terms_pages": sorted({t["page"] for m in members
                                              for t in scrapes[source_url_key(m["url"])].get("linked_terms") or []}),
                "campaign_start_date": _iso(start), "campaign_end_date": _iso(end)}
        try:
            title = _group_title(target_url, hub is not None, members, scrapes, names[business_id], fetch_title)
        except TitleUnavailable as exc:
            entries.append({**base, "title": None, "content": content, "action": "rejected", "reason": str(exc)})
            continue
        base = {**base, "title": title, "content": content}
        try:
            check_promotion(target_url, title, content, is_active=True, campaign_end_date=end)
        except PromotionRejected as exc:
            entries.append({**base, "action": "rejected", "reason": exc.code})
            continue
        if old is None:
            entries.append({**base, "action": "insert", "reason": "new_row"})
            continue
        same = (old["promotion_title"] == title and list(old["promotion_content"] or []) == content
                and old["campaign_start_date"] == start and old["campaign_end_date"] == end and old["is_active"])
        entries.append({**base, "action": "unchanged" if same else "update",
                        "reason": "identical" if same else "row_differs",
                        "promotion_id": old["promotion_id"], "expected_updated_at": _iso(old["updated_at"]),
                        "content_changed": list(old["promotion_content"] or []) != content})
    return entries


def _visible_in_group(card: str, urls: list[str], manifest: Mapping[str, Any]) -> bool:
    """卡片在本组某个渲染页面上已核验可见。"""
    for url in urls:
        page = _visibility_for(manifest, url) or {}
        for result in page.get("claimResults") or []:
            if result["claim"] == card and (result["status"] == "visible_match" or (
                    result["status"] == "image_only" and image_claim_verified(card, result, page))):
                return True
    return False
