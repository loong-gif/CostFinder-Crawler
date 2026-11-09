"""Evidence gate for AI-extracted promo offers.

The validator runs immediately before persistence.  It keeps source-backed
facts, clears unsupported optional values, and quarantines an offer when its
quoted raw text or its promotional fact is not supported by the scraped page.
It does not add fields to any database payload or change the extraction schema.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re
from typing import Any, Mapping

from utils.clinic_service_extraction import is_promo_offer
from utils.offer_conditions import OfferEvidenceIssue, _normalize_text, sanitize_conditions
from utils.offer_price_evidence import extract_price_evidence
from utils.service_price_guard import quantity_has_bound, infer_explicit_per_unit_price
from utils.offer_purchase_semantics import service_supported_by_source


_CURRENCY = re.compile(r"\$\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)")
# GFM 删除线可以是单个或两个波浪号（~$150~ 与 ~~$150~~）
_STRUCK_PRICE = re.compile(r"(?<!~)~{1,2}\s*\$\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*~{1,2}(?!~)")
# 「Your First Botox Treatment Is $10 Per Unit」这类首次购买价同样是新客促销价
_FIRST_PURCHASE = r"your\s+first\b"
_PROMO_SIGNAL = re.compile(
    r"\b(?:special|promo|promotion|offer|sale|member|membership|vip|"
    r"new\s+(?:patient|client)|first[-\s]?time|introductory|only|just|save|off|free)\b|"
    r"-\s*\d+(?:\.\d+)?\s*%|\b" + _FIRST_PURCHASE,
    re.IGNORECASE,
)
_MEMBERSHIP_SIGNAL = re.compile(r"\b(?:members?|membership|vip|club|plan)\b", re.IGNORECASE)
_NEW_CUSTOMER_SIGNAL = re.compile(
    r"\b(?:new\s+(?:patients?|clients?|customers?)|first[-\s]?time|first\s+visit|" + _FIRST_PURCHASE + r")",
    re.IGNORECASE,
)
_UNIT_WORDS = {
    "unit": r"u(?:nit)?s?",
    "syringe": r"syringes?",
    "half_syringe": r"half[-\s]?syringes?",
    "vial": r"vials?",
    "treatment": r"treatments?",
    "session": r"sessions?",
    "package": r"packages?",
    "area": r"areas?",
    "ml": r"m[lL]",
    "mg": r"m[gG]",
}


@dataclass(frozen=True)
class OfferEvidenceDecision:
    accepted: bool
    reason: str
    offer: dict[str, Any]
    issues: tuple[OfferEvidenceIssue, ...] = ()


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _same_number(left: Any, right: Any, *, tolerance: float = 0.011) -> bool:
    left_number = _number(left)
    right_number = _number(right)
    return (
        left_number is not None
        and right_number is not None
        and abs(left_number - right_number) <= tolerance
    )


def _raw_text_supported(raw_text: str, evidence: str) -> bool:
    raw = _normalize_text(raw_text)
    page = _normalize_text(evidence)
    if not raw or not page:
        return False
    if raw in page:
        return True
    fragments = [
        _normalize_text(part)
        for part in re.split(r"[\n\r|•]+", str(raw_text or ""))
        if _normalize_text(part)
    ]
    return len(fragments) > 1 and all(fragment in page for fragment in fragments)


def _currency_mentions(text: str) -> list[tuple[float, int, int]]:
    mentions: list[tuple[float, int, int]] = []
    for match in _CURRENCY.finditer(str(text or "")):
        value = _number(match.group(1))
        if value is not None:
            mentions.append((value, match.start(), match.end()))
    return mentions


def _currency_windows(text: str, value: Any, *, radius: int = 55) -> list[str]:
    target = _number(value)
    if target is None:
        return []
    source = str(text or "")
    return [
        source[max(0, start - radius) : min(len(source), end + radius)]
        for amount, start, end in _currency_mentions(source)
        if abs(amount - target) <= 0.011
    ]


def _currency_contexts(text: str, value: Any, *, radius: int = 45) -> list[tuple[str, str]]:
    target = _number(value)
    if target is None:
        return []
    source = str(text or "")
    return [
        (
            source[max(0, start - radius) : start],
            source[end : min(len(source), end + radius)],
        )
        for amount, start, end in _currency_mentions(source)
        if abs(amount - target) <= 0.011
    ]


def _regular_price_supported(value: Any, raw_text: str) -> bool:
    badge = _badge_price_pair(raw_text)
    if badge and _same_number(badge[0], value):
        return True
    # markdown 删除线标出的价格是原价
    if any(_same_number(_number(amount), value) for amount in _STRUCK_PRICE.findall(str(raw_text or ""))):
        return True
    parsed = extract_price_evidence(raw_text).get("regular_price")
    if _same_number(parsed, value):
        return True
    for prefix, suffix in _currency_contexts(raw_text, value):
        if re.search(
            r"\b(?:was|reg(?:ular(?:ly)?)?\.?|original|list\s+price|value)(?:\s+price)?\s*[:=]?\s*$",
            prefix,
            re.I,
        ) or (not _CURRENCY.search(suffix) and re.match(
            r"^\s*[,;:()\-]*\s*(?:reg(?:ular(?:ly)?)?\.?|original|list\s+price|value)\b",
            suffix,
            re.I,
        )):
            return True
    return False


def _discount_price_supported(value: Any, raw_text: str, member_context: bool = False,
                              promo_context: str | None = None) -> bool:
    """金额必须出现在 offer 原文中；单个价格是否为促销价的措辞可以来自整张卡片（promo_context）。"""
    badge = _badge_price_pair(raw_text)
    if badge and _same_number(badge[1], value):
        return True
    parsed = extract_price_evidence(raw_text).get("discount_price")
    if _same_number(parsed, value):
        return True
    contexts = _currency_contexts(raw_text, value)
    if not contexts:
        return False
    for prefix, suffix in contexts:
        if re.match(r"^\s*(?:off|discount|savings?)\b", suffix, re.I):
            continue
        if re.search(
            r"\b(?:now|sale(?:\s+price)?|special(?:\s+price)?|member(?:\s+price)?|"
            r"your\s+price|only|just|introductory|for)\b\s*[:=]?\s*$",
            prefix,
            re.I,
        ):
            return True
    mentions = _currency_mentions(raw_text)
    target = _number(value)
    if target is None:
        return False
    valid_amounts = [
        amount
        for amount, start, end in mentions
        if not re.search(
            r"^\s*(?:off|discount|savings?)\b",
            raw_text[end : min(len(raw_text), end + 18)],
            re.I,
        )
    ]
    if target not in valid_amounts:
        return False
    if len(valid_amounts) >= 2:
        regulars = [amount for amount in valid_amounts if _regular_price_supported(amount, raw_text)]
        return len(regulars) == 1 and target not in regulars and len(set(valid_amounts) - set(regulars)) == 1
    # 会员权益列表里的单个价格就是会员价，不需要促销措辞
    return bool(len(valid_amounts) == 1 and (member_context or _PROMO_SIGNAL.search(promo_context or raw_text)))


def _badge_price_pair(raw_text: str) -> tuple[float, float] | None:
    """An ordered price pair with a corroborating discount badge is explicit."""
    match = re.search(r"\$\s*(\d+(?:\.\d+)?)\s+\$\s*(\d+(?:\.\d+)?)\s+-\s*(\d+(?:\.\d+)?)\s*%", raw_text)
    if match:
        regular, discount, percent = map(float, match.groups())
        if regular > discount > 0 and abs((regular-discount)*100/regular-percent) < 1:
            return regular, discount
    return None


def stated_paid_and_delivered(raw_text: str) -> tuple[Decimal, Decimal] | None:
    """Recognize explicitly paid and free quantities of the same stated unit."""
    text = str(raw_text or "")
    for word,value in (("one",1),("two",2),("three",3),("four",4),("five",5),("six",6)):
        text = re.sub(r"\b"+word+r"\b",str(value),text,flags=re.I)
    units = "|".join(_UNIT_WORDS.values())
    forward = re.search(rf"\bbuy\s+(\d+)\s+({units})\b.{{0,60}}?\bget\s+(?:in\s+)?(?:the\s+)?(\d+)(st|nd|rd|th)?\s*(?:({units})\s*)?(?:for\s+)?free\b",text,re.I|re.S)
    if forward:
        paid,free = Decimal(forward[1]),Decimal(forward[3])
        if forward[5] and forward[2].lower().rstrip("s") != forward[5].lower().rstrip("s"):
            return None
        delivered = free if forward[4] else paid+free
        if paid>0 and delivered>paid and (not forward[4] or delivered==paid+1):return paid,delivered
    reverse = re.search(rf"\b(\d+)\s+free\s+({units})\b.{{0,35}}?purchase\s+of\s+(\d+)\s+({units})\b",text,re.I|re.S)
    if reverse and reverse[2].lower().rstrip("s")==reverse[4].lower().rstrip("s"):
        paid,free=Decimal(reverse[3]),Decimal(reverse[1])
        if paid>0 and free>0:return paid,paid+free
    return None


def _quantity_supported(value: Any, raw_text: str, *, minimum: bool = False) -> bool:
    number = _number(value)
    if number is None:
        return False
    shown = re.escape(f"{number:g}")
    units = "|".join(_UNIT_WORDS.values())
    if minimum:
        # 「20 to 300 units」这类区间的下限也是最低购买量
        return bool(re.search(rf"\b(?:minimum|min\.?|at\s+least)\s+{shown}\s*(?:{units})\b|"
            rf"\b{shown}\s*(?:\+|or\s+more)\s*(?:{units})\b|"
            rf"\b{shown}\s*(?:-|–|to)\s*\d+\s*(?:{units})\b|"
            rf"\b{shown}[ -]*(?:{units})\b.{{0,24}}?(?:minimum|at\s+least|or\s+more|\+)",raw_text,re.I))
    counts=stated_paid_and_delivered(raw_text)
    if counts is not None:
        # The paid count is not the delivered count when units are included free.
        if number in (counts[0],counts[1]-counts[0]):return False
        if number == counts[1]:return True
    return any(not quantity_has_bound(raw_text[max(0,match.start()-35):match.start()],raw_text[match.end():])
               for match in re.finditer(rf"\b{shown}\s*(?:{units})\b",raw_text,re.I))


def _unit_type_supported(value: Any, raw_text: str) -> bool:
    unit_type = str(value or "").strip().lower()
    if not unit_type or unit_type == "others":
        return True
    pattern = _UNIT_WORDS.get(unit_type)
    return bool(pattern and re.search(rf"\b(?:{pattern})\b", raw_text, re.I))


def _unit_price_supported(
    value: Any,
    raw_text: str,
    *,
    quantity: Any,
    discount_price: Any,
    single_item: bool,
) -> bool:
    target = _number(value)
    if target is None:
        return False
    for window in _currency_windows(raw_text, target):
        if re.search(
            r"(?:/|per)\s*(?:u(?:nit)?s?|syringes?|vials?|sessions?|treatments?|m[lL]|m[gG])\b",
            window,
            re.I,
        ):
            return True
    qty = _number(quantity)
    total = _number(discount_price)
    return bool(
        single_item
        and qty is not None
        and qty > 0
        and total is not None
        and abs(target * qty - total) <= 0.011
        and _quantity_supported(qty, raw_text)
        and _discount_price_supported(total, raw_text)
    )


def _discount_amount_supported(value: Any, raw_text: str) -> bool:
    return _same_number(extract_price_evidence(raw_text).get("discount_amount"), value)


def _discount_percent_supported(value: Any, raw_text: str) -> bool:
    if _same_number(extract_price_evidence(raw_text).get("discount_percent"), value):
        return True
    return any(_same_number(match[1] or match[2], value) for match in re.finditer(
        r"(?:-\s*|\bsave\s+)(\d+(?:\.\d+)?)\s*%|"
        r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s+(?:off\b|(?:[\w-]+\s+){0,3}(?:savings?|discount)\b)", raw_text, re.I))


def _clear_unsupported(
    candidate: dict[str, Any],
    issues: list[OfferEvidenceIssue],
    *,
    field: str,
    supported: bool,
    replacement: Any = None,
) -> None:
    value = candidate.get(field)
    if value is None or supported:
        return
    issues.append(OfferEvidenceIssue(field, "not_supported_by_offer_raw_text", value))
    candidate[field] = replacement


def validate_offer_evidence(
    offer: Mapping[str, Any],
    evidence: str,
    *,
    member_context: bool = False,
    scope_text: str | None = None,
) -> OfferEvidenceDecision:
    """Return an immutable evidence decision and a sanitized offer copy.

    价格与折扣只按 offer 原文核验；新客、会员要求与最低数量是整张卡片的条件，按 scope_text（offer 所在的完整卡片，
    含追加的条款段落）核验，未给出时同样按 offer 原文核验。
    """
    candidate = dict(offer)
    source = str(evidence or "")
    raw_text = str(candidate.get("offer_raw_text") or "").strip()
    scope = f"{raw_text}\n{scope_text}" if scope_text else raw_text
    if not source.strip():
        return OfferEvidenceDecision(False, "missing_source_evidence", candidate)
    if not raw_text or not _raw_text_supported(raw_text, source):
        return OfferEvidenceDecision(False, "offer_raw_text_not_supported", candidate)
    source_quote = candidate.get("source_offer_raw_text")
    if source_quote and (not _raw_text_supported(str(source_quote), source)
                         or not _raw_text_supported(raw_text, str(source_quote))):
        return OfferEvidenceDecision(False, "purchase_branch_not_supported", candidate)
    if (candidate.get("discount_price") is not None
            and infer_explicit_per_unit_price(raw_text) is None
            and infer_explicit_per_unit_price(raw_text, prefer_first=True) is not None):
        return OfferEvidenceDecision(False, "ambiguous_unit_price_context", candidate)
    if re.search(r"\b(?:from|starting\s+at|starts?\s+(?:at|from))\s+\$", raw_text, re.I):
        candidate["price_model"] = "from"

    issues: list[OfferEvidenceIssue] = []
    sanitized_conditions, condition_issues = sanitize_conditions(
        candidate.get("conditions"),
        source,
    )
    if condition_issues:
        return OfferEvidenceDecision(
            False,
            "invalid_conditions_evidence",
            candidate,
            condition_issues,
        )
    candidate["conditions"] = sanitized_conditions or []
    _clear_unsupported(
        candidate,
        issues,
        field="regular_price",
        supported=_regular_price_supported(candidate.get("regular_price"), raw_text),
    )
    _clear_unsupported(
        candidate,
        issues,
        field="discount_price",
        supported=_discount_price_supported(candidate.get("discount_price"), raw_text, member_context, scope),
    )
    _clear_unsupported(
        candidate,
        issues,
        field="discount_amount",
        supported=_discount_amount_supported(candidate.get("discount_amount"), raw_text),
    )
    _clear_unsupported(
        candidate,
        issues,
        field="discount_percent",
        supported=_discount_percent_supported(candidate.get("discount_percent"), raw_text),
    )
    _clear_unsupported(
        candidate,
        issues,
        field="min_quantity",
        supported=_quantity_supported(candidate.get("min_quantity"), scope, minimum=True),
    )
    _clear_unsupported(
        candidate,
        issues,
        field="is_membership_required",
        supported=(
            not bool(candidate.get("is_membership_required"))
            or bool(_MEMBERSHIP_SIGNAL.search(scope))
        ),
        replacement=False,
    )
    _clear_unsupported(
        candidate,
        issues,
        field="is_new_customer_required",
        supported=(
            not bool(candidate.get("is_new_customer_required"))
            or bool(_NEW_CUSTOMER_SIGNAL.search(scope))
        ),
        replacement=False,
    )

    raw_items = candidate.get("items") if isinstance(candidate.get("items"), list) else []
    sanitized_items: list[dict[str, Any]] = []
    for index, raw_item in enumerate(raw_items):
        item = dict(raw_item) if isinstance(raw_item, Mapping) else {}
        prefix = f"items[{index}]"
        service_name = item.get("service_name")
        if service_name and str(service_name).casefold() != "others" and not service_supported_by_source(service_name, raw_text):
            issues.append(OfferEvidenceIssue(f"{prefix}.service_name", "not_supported_by_offer_raw_text", service_name))
            item["service_name"] = None
            item.pop("service_id", None)
        quantity = item.get("quantity")
        if quantity is not None and not _quantity_supported(quantity, raw_text):
            issues.append(OfferEvidenceIssue(f"{prefix}.quantity", "not_supported_by_offer_raw_text", quantity))
            item["quantity"] = None
        unit_price = item.get("unit_price")
        if unit_price is not None and not _unit_price_supported(
            unit_price,
            raw_text,
            quantity=item.get("quantity"),
            discount_price=candidate.get("discount_price"),
            single_item=len(raw_items) == 1,
        ):
            issues.append(OfferEvidenceIssue(f"{prefix}.unit_price", "not_supported_by_offer_raw_text", unit_price))
            item["unit_price"] = None
        unit_type = item.get("unit_type")
        if unit_type is not None and not _unit_type_supported(unit_type, raw_text):
            issues.append(OfferEvidenceIssue(f"{prefix}.unit_type", "not_supported_by_offer_raw_text", unit_type))
            item["unit_type"] = None
        service_area = str(item.get("service_area") or "").strip()
        if service_area and _normalize_text(service_area) not in _normalize_text(scope):
            issues.append(OfferEvidenceIssue(f"{prefix}.service_area", "not_supported_by_offer_raw_text", service_area))
            item["service_area"] = None
        sanitized_items.append(item)
    candidate["items"] = sanitized_items

    if not is_promo_offer(candidate) and not stated_paid_and_delivered(raw_text):
        return OfferEvidenceDecision(
            False,
            "no_supported_promo_fact",
            candidate,
            tuple(issues),
        )
    reason = "validated_with_sanitization" if issues else "validated"
    return OfferEvidenceDecision(True, reason, candidate, tuple(issues))
