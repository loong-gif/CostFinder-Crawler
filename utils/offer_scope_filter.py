"""Exclude consultation and membership-plan rows from promo_offer_master."""
from __future__ import annotations

import re
from typing import Any, Dict, List

from utils.membership_plans import _is_pure_membership_offer
from utils.skincare_products import is_skincare_product_offer

_CONSULTATION_RE = re.compile(r"\bconsultation\b", re.IGNORECASE)
_FREE_ZERO_RE = re.compile(r"\bfree\b|\$0(?:\.\d+)?\b|0\.00", re.IGNORECASE)
_SERVICE_UNIT_RE = re.compile(
    r"\b(botox|dysport|filler|tox|unit|syringe|juvederm|xeomin|jeuveau|daxxify|sculptra|kybella|hydrafacial|laser|microneedling)\b",
    re.IGNORECASE,
)


def _text_blob(offer: Dict[str, Any]) -> str:
    parts = [
        offer.get("service_name"),
        offer.get("raw_service_name"),
        offer.get("display_service_name"),
        offer.get("offer_raw_text"),
    ]
    content = offer.get("offer_content")
    if isinstance(content, str):
        parts.append(content)
    return " ".join(str(part or "").strip() for part in parts if str(part or "").strip())


def is_consultation_offer(offer: Dict[str, Any]) -> bool:
    service = str(offer.get("service_name") or "").strip().lower()
    if service in {"free consultation", "consultation"}:
        return True
    if "consultation" in service.replace("_", " "):
        return True
    if _CONSULTATION_RE.search(str(offer.get("service_name") or "")):
        return True
    if _CONSULTATION_RE.search(str(offer.get("raw_service_name") or "")):
        return True
    if _CONSULTATION_RE.search(str(offer.get("display_service_name") or "")):
        return True
    text = _text_blob(offer)
    raw = str(offer.get("offer_raw_text") or "")
    if _CONSULTATION_RE.search(raw) and _FREE_ZERO_RE.search(raw):
        if not re.search(r"\$\s*\d+(?:\.\d+)?\s*/\s*(?:unit|syringe|area)\b", raw, re.IGNORECASE):
            return True
    if not _CONSULTATION_RE.search(text):
        return False
    if _FREE_ZERO_RE.search(text) and not _SERVICE_UNIT_RE.search(text):
        return True
    if service in {"", "others"} and _CONSULTATION_RE.search(text):
        return True
    return False


def is_membership_plan_offer(offer: Dict[str, Any]) -> bool:
    """Membership tier/plan fee — not a concrete treatment SKU price."""
    service = str(offer.get("service_name") or "").strip()
    if service == "Membership":
        return True
    if offer.get("membership_plan_id"):
        return False
    template = str(offer.get("template_type") or "").strip().lower()
    if template == "membership":
        return True
    return _is_pure_membership_offer(offer)


def should_exclude_from_offer_master(offer: Dict[str, Any]) -> bool:
    return (
        is_consultation_offer(offer)
        or is_membership_plan_offer(offer)
        or is_skincare_product_offer(offer)
    )


def exclude_reason(offer: Dict[str, Any]) -> str:
    if is_consultation_offer(offer):
        return "consultation"
    if is_membership_plan_offer(offer):
        return "membership_plan"
    if is_skincare_product_offer(offer):
        return "skincare_product"
    return ""


ITEM_NAME_MAX_LENGTH = 60


def short_item_label(text: Any) -> str:
    """整句原文充当 item 名称时的短标签：折叠空白，超长时在单词边界截断，截断后括号不成对则去掉未闭合部分。"""
    label = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(label) <= ITEM_NAME_MAX_LENGTH:
        return label
    label = label[:ITEM_NAME_MAX_LENGTH].rsplit(" ", 1)[0]
    if label.count("(") > label.count(")"):
        label = label[: label.rindex("(")]
    return label.strip(" ,;:-—|")


def _services_in(text: Any) -> set[str]:
    """文本中出现的枚举服务名；延迟导入以避免与 service_linking 循环依赖。"""
    from utils.service_recognition import scan_services
    return scan_services(text)


def multiple_services_reason(offer: Dict[str, Any]) -> str:
    """非 package 的 offer 一条只对应一个服务；原文里并列多个服务时返回原因，应拆成独立 offer。"""
    if offer.get("is_package") or offer.get("purchase_type") == "bundle":
        return ""
    return "multiple_services_in_offer" if len(_services_in(offer.get("offer_raw_text"))) > 1 else ""


_PRICE_WORD_RE = re.compile(r"[$%]|\b(?:off|free|save|discount|savings?|percent)\b", re.IGNORECASE)


_TRAILING_PRICE_PHRASE_RE = re.compile(r"\s+at\s+(?:standard|regular|member|everyday)\s+prices?\s*$", re.IGNORECASE)
_LEADING_QUANTITY_RE = re.compile(
    r"^(?:one|two|three|four|five|a|an|\d+)\s+(?:full\s+|single\s+)?(?:syringes?|vials?|units?|sessions?)\s+of\s+",
    re.IGNORECASE)


def clean_item_name(name: Any) -> str:
    """去掉名称两端的价格措辞与数量前缀（如 "at standard prices"、"one full syringe of"），保留服务或商品本身。"""
    text = str(name or "").strip()
    text = _TRAILING_PRICE_PHRASE_RE.sub("", text)
    return _LEADING_QUANTITY_RE.sub("", text).strip()


def item_name_problem(name: Any) -> str:
    """item_name 必须是服务、产品或项目的短名称；有问题时返回原因，否则返回空串。"""
    text = str(name or "")
    if not text.strip():
        return "empty"
    if len(text) > ITEM_NAME_MAX_LENGTH:
        return "too_long"
    if re.search(r"[\r\n]", text):
        return "multiline"
    if _PRICE_WORD_RE.search(text):
        return "price_or_discount_words"
    if len(_services_in(text)) > 1:
        return "multiple_services"
    if _NON_SERVICE_TARGET_RE.search(text):
        return "member_event_or_gift_card"
    if text.count("(") != text.count(")"):
        return "unbalanced_parenthesis"
    if _CONJUNCTION_START_RE.match(text.strip()) or re.match(r"^\s*(?:\d+\)|[\)\(;:|.,-])", text):
        return "fragment_start"
    if re.search(r"[\s,;:|.-]$", text):
        return "trailing_punctuation"
    # 从条款句中截下的片段（"a package of 8"、"JEUVEAU is available"）是句子成分，不是名称
    if _CLAUSE_FRAGMENT_RE.search(text.strip()):
        return "clause_fragment"
    return ""


# 脚注符号常紧贴在 Valid 前（"ǂValid"），ǂ 属于 Unicode 字母，所以只排除前面紧跟 ASCII 字母的情况
_TERMS_PARAGRAPH_RE = re.compile(r"(?<![A-Za-z])Valid (?:on|for) (?:all )?purchases made\b", re.IGNORECASE)
# 小写冠词开头的是句子成分（"a package of 8"）；大写的 "A E Skin" 是品牌名
_CLAUSE_FRAGMENT_RE = re.compile(r"^(?:a|an)\s+[a-z]|\b(?:is|are)\s+(?:available|offered|included)\b")


_CONJUNCTION_START_RE = re.compile(r"^(?:and|or|plus|including|excluding|except)\b", re.IGNORECASE)
_MEMBERSHIP_TERM_RE = re.compile(
    r"\badditional\s+months?\s+free\b|"
    r"\b(?:purchase|pay|paying|prepay)\b[^.]{0,40}\b\d+\s+months?\b[^.]{0,30}\b(?:upfront|up\s+front|in\s+advance)\b",
    re.IGNORECASE,
)
_GIFT_CARD_RE = re.compile(r"\bgift\s*cards?\b", re.IGNORECASE)
_NON_SERVICE_TARGET_RE = re.compile(
    r"\b(?:members?|memberships?|vip|events?|parties|party|gift\s*cards?)\b", re.IGNORECASE)


def non_promotable_reason(offer: Dict[str, Any]) -> str:
    """offer 带有价格或折扣数值，但原文是展平后的碎片，或折扣对象不是服务或商品时给出原因，否则返回空串。"""
    raw = str(offer.get("offer_raw_text") or "")
    bracketed = re.sub(r"(?m)^\s*\d+\)\s*", "", raw)
    if bracketed.count("(") != bracketed.count(")"):
        return "fragment_offer_text"
    if any(_CONJUNCTION_START_RE.match(part.strip()) for part in raw.split("|")[1:]):
        return "fragment_offer_text"
    if _MEMBERSHIP_TERM_RE.search(raw):
        return "membership_term_offer"
    # 卡片后追加的条款段落是条件证据（决策第 4 条），offer 原文取自卡片正文
    if _TERMS_PARAGRAPH_RE.search(raw):
        return "terms_paragraph_offer_text"
    if _GIFT_CARD_RE.search(raw):
        return "gift_card_offer"
    names = [str(item.get("item_name") or "").strip() for item in offer.get("items") or []]
    if any(_NON_SERVICE_TARGET_RE.search(name) for name in names):
        return "non_service_target"
    return ""


def filter_service_offers(offers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [offer for offer in offers if not should_exclude_from_offer_master(offer)]
