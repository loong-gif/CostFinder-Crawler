from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from utils.clinic_service_extraction import is_promo_offer
from utils.extraction_persist_pricing import build_master_from_offer, route_offer
from utils.offer_conditions import card_conditions
from utils.offer_evidence_validator import _NEW_CUSTOMER_SIGNAL, validate_offer_evidence
from utils.offer_purchase_semantics import normalize_purchase_offers
from utils.offer_scope_filter import (
    _TERMS_PARAGRAPH_RE, clean_item_name, exclude_reason, item_name_problem, multiple_services_reason,
    non_promotable_reason, short_item_label)
from utils.offer_stage_spec import (
    MASTER_COLUMNS, MEMBERSHIPS, PROMOTIONS, SOURCE_SPEC, SYSTEM_PROMPT, TASK_PROMPTS,
    _GIFT_SIGNAL, _REGULAR_PRICE_CARD)
from utils.promo_offer_items_db import is_blanket_offer_text
from utils.render_review import claim_anchors


def build_messages(table: str, row: dict[str, Any], evidence: str) -> list[dict[str, str]]:
    header = f"Source URL: {row['source_url']}\n"
    if table == MEMBERSHIPS:
        header += (f"Membership plan: {row['membership_name']} (fee {row['membership_price']}, "
                   f"{row['billing_period']})\n")
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"{TASK_PROMPTS[table]}\n{header}\nCONTENT:\n{evidence}"}]


def extract_source(client: Any, schema: dict[str, Any], table: str, row: dict[str, Any], evidence: str,
                   cache_dir: Path) -> dict[str, Any]:
    """LLM 结果按 (表, 发给 LLM 的消息、schema、模型的哈希) 缓存，不含来源 id：
    连锁分店内容相同的促销共用同一份回答，同一次运行中各分店的 offer 一致。"""
    messages = build_messages(table, row, evidence)
    digest = hashlib.sha256("\n\x00".join([table, json.dumps(messages, ensure_ascii=False),
                                           json.dumps(schema, sort_keys=True), str(client.model)]).encode()).hexdigest()
    path = cache_dir / f"{table}_{digest[:24]}.json"
    if path.exists():
        return json.loads(path.read_text())
    payload = client.create_json_response(messages, json_schema=schema)
    if not isinstance(payload, dict) or not isinstance(payload.get("offers"), list):
        raise RuntimeError(f"llm_payload_invalid:{table}:{row[SOURCE_SPEC[table]['id']]}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def _skip_reason(offer: dict[str, Any]) -> str:
    """route_offer 判定不是促销时给出原因：赠送、credit、无价格的买送都归入 gift_only。"""
    reason = exclude_reason(offer)
    if reason:
        return reason
    if not is_promo_offer(offer):
        return "gift_only" if _GIFT_SIGNAL.search(str(offer.get("offer_raw_text") or "")) else "no_price_fact"
    return "not_promo"


def _finalize(validated: dict[str, Any], master: dict[str, Any], items: list[dict[str, Any]]) -> tuple[dict, list] | str:
    """通过返回 (master, items)，否则返回拒绝原因。"""
    blanket = is_blanket_offer_text(str(master["offer_raw_text"]))
    master["is_package"] = not blanket and (validated.get("is_package") is True
                                            or validated.get("purchase_type") == "bundle")
    # 名称、数量、单价完全相同的 item 是同一项（LLM 把「8 或 10 次」写成两条相同的 item）
    unique: dict[tuple, dict[str, Any]] = {}
    for item in items:
        unique.setdefault((" ".join(str(item.get("item_name") or "").split()).casefold(), item.get("quantity"),
                           item.get("unit_price")), item)
    items = list(unique.values())
    if len(items) > 1 and not master["is_package"]:
        return "unresolved_purchase_composition"
    if master["is_package"]:
        items = [dict(item, unit_price=None) for item in items]
    if (not master["is_package"] and master["price_model"] == "per_unit" and master.get("discount_price") is None
            and (master.get("discount_amount") is not None or master.get("discount_percent") is not None)):
        master["price_model"] = "total"
    if not master["is_package"] and master["price_model"] == "per_unit":
        price = master.get("discount_price")
        if price is None or price <= 0:
            return "per_unit_price_incomplete"
        if any(item.get("unit_price") is None or abs(float(item["unit_price"]) - price) > 0.011 for item in items):
            return "per_unit_item_price_incomplete"
    if not master["is_package"] and master["price_model"] != "per_unit":
        # 与数据库触发器 normalize_promo_offer_item_unit_price 相同：单项 offer 的 unit_price = 成交价 / 交付数量；
        # 计划与写入后的数据一致，重复运行不会产生「改回原值」的更新
        price = master.get("discount_price")
        items = [dict(item, unit_price=None if price is None else round(float(price) / float(item.get("quantity") or 1), 4))
                 for item in items]
    return master, items


def candidates_from_payload(payload: dict[str, Any], table: str, row: dict[str, Any],
                            evidence: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """LLM offers 经拆分、证据校验、gift-only 过滤、blanket 处理后得到候选 offer。"""
    spec = SOURCE_SPEC[table]
    plan_id = row[spec["id"]] if table == MEMBERSHIPS else None
    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def reject(text: Any, reason: str) -> None:
        rejected.append({"offer_raw_text": str(text or "")[:300], "reason": reason})

    cards = [card for card in row[spec["content"]] or [] if isinstance(card, str)]

    def card_of(text: Any) -> str | None:
        # offer 原文所在的那张卡片（含追加的条款段落）；不唯一时不作为条件证据
        key = " ".join(str(text or "").split()).casefold()
        found = [card for card in cards if key and key in " ".join(card.split()).casefold()]
        return found[0] if len(found) == 1 else None

    def trimmed_raw_text(text: Any) -> Any:
        # LLM 给出的原文跨越卡片中连续几行时，只保留到最后一行带金额或百分比的行（之后的说明行属于条件，不属于 offer 原文）
        if not isinstance(text, str):
            return text
        key = " ".join(text.split()).casefold()
        for card in cards:
            lines = [" ".join(line.split()) for line in card.splitlines() if line.strip()]
            for start in range(len(lines)):
                for end in range(start + 1, len(lines)):
                    if " ".join(lines[start:end + 1]).casefold() == key:
                        priced = [i for i in range(start, end + 1) if claim_anchors(lines[i])]
                        if priced and priced[-1] < end:
                            return " ".join(lines[start:priced[-1] + 1])
        return text

    def headline_for_terms(offer: dict[str, Any]) -> dict[str, Any]:
        # LLM 从卡片后的条款段落取了原文时（决策第 4 条：条款只是条件证据），改用同一张卡片中
        # 条款之前、带有同一折扣数值的唯一标题行作为原文；item 名称由原文的短标签确定
        text = offer.get("offer_raw_text")
        if not isinstance(text, str) or not _TERMS_PARAGRAPH_RE.search(text):
            return offer
        anchors = set(claim_anchors(text))
        prefix = " ".join(text.split()[:6]).casefold()
        for card in cards:
            lines = [line.strip() for line in card.splitlines() if line.strip()]
            terms_at = next((i for i, line in enumerate(lines) if prefix and prefix in " ".join(line.split()).casefold()), None)
            if terms_at is None:
                continue
            headlines = [line for line in lines[:terms_at] if set(claim_anchors(line)) & anchors]
            if len(headlines) == 1:
                items = offer.get("items") or [{}]
                return {**offer, "offer_raw_text": headlines[0],
                        "items": [{**items[0], "item_name": None, "quantity": None, "unit_price": None}]}
        return offer

    for original in payload["offers"]:
        original = headline_for_terms(original)
        original = {**original, "offer_raw_text": trimmed_raw_text(original.get("offer_raw_text"))}
        offers, reviews = normalize_purchase_offers([original], evidence)
        for review in reviews:
            if review["reason"] != "generic_offer_scope":
                reject(review.get("offer_raw_text"), review["reason"])
        for offer in offers:
            card = card_of(offer.get("offer_raw_text"))
            if card and _REGULAR_PRICE_CARD.search(card):
                reject(offer.get("offer_raw_text"), "regular_price_not_promotion")
                continue
            decision = validate_offer_evidence(offer, evidence, member_context=table == MEMBERSHIPS, scope_text=card)
            if not decision.accepted:
                reject(offer.get("offer_raw_text"), decision.reason)
                continue
            validated = dict(decision.offer)
            # 卡片中有条件句时条件取自卡片原文（确定性），LLM 挑选的条件只在卡片没有条件句时使用
            stated = card_conditions(card, str(validated.get("offer_raw_text") or "")) if card else []
            if stated:
                validated["conditions"] = stated
            # 新客限制同样由原文确定：offer 原文或卡片条件句写明新客、首次购买时为 true
            if _NEW_CUSTOMER_SIGNAL.search("\n".join([str(validated.get("offer_raw_text") or ""), *stated])):
                validated["is_new_customer_required"] = True
            if table == MEMBERSHIPS:
                validated["is_membership_required"] = True
            unsuitable = non_promotable_reason(validated)
            if unsuitable:
                reject(validated.get("offer_raw_text"), unsuitable)
                continue
            if route_offer(validated) != "promo":
                reject(validated.get("offer_raw_text"), _skip_reason(validated))
                continue
            master, items = build_master_from_offer(
                validated, business_id=row["business_id"], promotion_id=row[spec["id"]] if table == PROMOTIONS else 0,
                source_url=row["source_url"], membership_plan_id=plan_id)
            outcome = _finalize(validated, master, items)
            if isinstance(outcome, str):
                reject(master["offer_raw_text"], outcome)
                continue
            master, items = outcome
            multiple = multiple_services_reason(master)
            if multiple:
                reject(master["offer_raw_text"], multiple)
                continue
            hints = [i.get("service_name") for i in validated.get("items") or []]
            # LLM 给出的 item 名称必须是不超过 ITEM_NAME_MAX_LENGTH 的短语；
            # LLM 未给出 item 名称，或 blanket offer 的名称不合格时，名称取 offer 原文的短标签
            blanket = is_blanket_offer_text(master["offer_raw_text"])

            def named(item: Mapping[str, Any]) -> str:
                given = clean_item_name(item.get("item_name"))
                return given if given and not (blanket and item_name_problem(given)) \
                    else short_item_label(master["offer_raw_text"])

            plan_items = [{"item_name": named(item),
                           "delivered_units": item.get("quantity"),
                           "unit_price": item.get("unit_price"),
                           "service_name_hint": hints[index] if len(hints) == len(items) else None}
                          for index, item in enumerate(items)]
            if any(item_name_problem(item["item_name"]) for item in plan_items if not blanket):
                reject(master["offer_raw_text"], "item_name_invalid")
                continue
            master["promotion_id"] = row[spec["id"]] if table == PROMOTIONS else None
            master["membership_plan_id"] = plan_id
            fingerprint = _fingerprint(master)
            if fingerprint in seen:
                reject(master["offer_raw_text"], "duplicate_in_source")
                continue
            seen.add(fingerprint)
            candidates.append({"fingerprint": fingerprint, "master": _plain(master),
                               "blanket": blanket, "items": plan_items})
    return _merge_restated_offers(candidates, reject), rejected


_DEAL_FIELDS = ("price_model", "regular_price", "discount_price", "discount_percent", "discount_amount",
                "is_package", "is_new_customer_required", "is_membership_required")


def _merge_restated_offers(candidates: list[dict[str, Any]], reject: Callable[[Any, str], None]) -> list[dict[str, Any]]:
    """同一来源中 item、价格字段与客户限制都相同的 offer 是页面对同一优惠的重复表述，只保留条件最多的一条。"""
    def deal(candidate: Mapping[str, Any]) -> str:
        master = candidate["master"]
        items = sorted((" ".join(str(i["item_name"]).split()).casefold(), i["delivered_units"], i["unit_price"])
                       for i in candidate["items"])
        return json.dumps([[master.get(f) for f in _DEAL_FIELDS], items], default=str)

    kept: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        key = deal(candidate)
        other = kept.get(key)
        if other is None:
            kept[key] = candidate
            continue
        more, less = sorted((other, candidate), key=lambda c: len(c["master"].get("conditions") or []), reverse=True)
        kept[key] = more
        reject(less["master"]["offer_raw_text"], "duplicate_in_source")
    return list(kept.values())


def _fingerprint(master: dict[str, Any]) -> str:
    """计划内的 offer 身份：来源 + 规范化原文（保留非 ASCII 字符）；数据库 offer_fingerprint 由触发器另行计算。"""
    if master.get("membership_plan_id") is not None:
        source = f"m:{master['membership_plan_id']}"
    else:
        source = f"p:{master['promotion_id']}"
    text = re.sub(r"[\W_]+", " ", str(master["offer_raw_text"]).casefold()).strip()
    return f"{source}|{text}"


def _plain(master: dict[str, Any]) -> dict[str, Any]:
    return {key: master.get(key) for key in MASTER_COLUMNS}
