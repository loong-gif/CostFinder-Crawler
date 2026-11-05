"""Deterministic semantic QA for promotion_content and membership benefits."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence

from utils.promotion_content_cards import validate_promotion_content_binding
from utils.promotion_source_page import _MEMBERSHIP_CONTENT_RE
from utils.recent_raw_extraction import (
    GateDecision,
    SIGNAL_LINE,
    _normalize_compare_text,
)

Severity = str  # "high" | "medium" | "low"

_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\([^)]+\)")
_RAW_URL_RE = re.compile(r"https?://[^\s\])]+", re.IGNORECASE)
_IMAGE_MARKDOWN_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_SHOPIFY_RESIDUE_RE = re.compile(
    r"\b(?:regular\s+price|sale\s+price|unit\s+price\s*/\s*per|compare\s+at|"
    r"add\s+to\s+cart|your\s+cart|checkout|shopify|collections/)\b",
    re.IGNORECASE,
)
_UI_POLLUTION_RE = re.compile(
    r"\b(?:contact\s+us|book\s+now|subtotal\s*\$?\s*0\.?0{2}|"
    r"your\s+cart\s+is\s+empty|\$\$ImageUrl\$\$)\b",
    re.IGNORECASE,
)
_SOFT_CTA_RE = re.compile(
    r"\b(?:mention\s+this\s+page|gift\s+with\s+purchase|terms\s+and\s+conditions|"
    r"cannot\s+be\s+combined|new\s+patient\s+offer(?:\s+only)?|"
    r"exclusive\s+pricing\s+for\s+residents|learn\s+more|call\s+to\s+book)\b",
    re.IGNORECASE,
)
_MEMBERSHIP_FEE_IN_PROMO_RE = re.compile(
    r"\$\s*\d+(?:\.\d+)?\s*(?:/|\s+per\s+)\s*(?:mo\b|month\b)|"
    r"\b(?:membership|member\s+dues|monthly\s+dues)\b.*\$\s*\d",
    re.IGNORECASE,
)
_TITLE_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){2,}$")
_CURRENCY_RE = re.compile(r"\$\s*\d+(?:\.\d+)?")
_DISCOUNT_ONLY_BENEFIT_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)?\s*%\s*off|\d+\s*%\s*discount|"
    r"\$\s*\d+(?:\.\d+)?\s*off\s+\w+|10\s*%\s*off\s+everything)\s*\.?\s*$",
    re.IGNORECASE,
)
_ORPHAN_PRICE_SEGMENT_RE = re.compile(
    r"^\s*(?:\$|regular\s+\$|was\s+\$|now\s+\$|save\s+\$|\d+\s*%\s*off)\b",
    re.IGNORECASE,
)
_SERVICE_HINT_RE = re.compile(
    r"\b(?:botox|dysport|filler|hydrafacial|microneedling|laser|peel|facial|"
    r"package|syringe|unit|tox|coolsculpt|semaglutide|iv\s|injection)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SemanticIssue:
    severity: Severity
    issue_type: str
    detail: str


def _norm_benefits(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, Sequence):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return []


def _segment_blob(segments: Sequence[str], title: str = "") -> str:
    parts = [str(title or "").strip(), *(str(s).strip() for s in segments)]
    return "\n".join(part for part in parts if part)


def has_markdown_or_shopify_residue(text: str) -> bool:
    blob = str(text or "")
    if _IMAGE_MARKDOWN_RE.search(blob):
        return True
    if _MARKDOWN_LINK_RE.search(blob):
        return True
    if _RAW_URL_RE.search(blob) and "http" in blob.casefold():
        return True
    if _SHOPIFY_RESIDUE_RE.search(blob):
        return True
    if "](http" in blob or "](https" in blob:
        return True
    return False


def has_ui_pollution(segments: Sequence[str]) -> bool:
    return any(_UI_POLLUTION_RE.search(str(segment or "")) for segment in segments)


def title_looks_like_slug(title: str) -> bool:
    cleaned = str(title or "").strip().casefold()
    if not cleaned or " " in cleaned:
        return False
    return bool(_TITLE_SLUG_RE.match(cleaned))


def is_soft_cta_only(title: str, segments: Sequence[str]) -> bool:
    if any(SIGNAL_LINE.search(str(segment or "")) for segment in segments):
        return False
    blob = _segment_blob(segments, title)
    if _CURRENCY_RE.search(blob) or re.search(r"\d+\s*%\s*off", blob, re.IGNORECASE):
        return False
    if not blob.strip():
        return False
    if _SOFT_CTA_RE.search(blob):
        return True
    return len(segments) <= 2 and not any(len(str(s)) > 40 for s in segments)


def looks_like_membership_in_promo(title: str, segments: Sequence[str]) -> bool:
    blob = _segment_blob(segments, title)
    if not _MEMBERSHIP_CONTENT_RE.search(blob) and not _MEMBERSHIP_FEE_IN_PROMO_RE.search(blob):
        return False
    promo_signals = re.search(
        r"\b(?:\d+\s*%\s*off|save\s+\$|buy\s+\d+\s*,?\s*get|limited\s+time|new\s+client|"
        r"first\s+time|special\s+offer|seasonal|stacked\s+savings)\b",
        blob,
        re.IGNORECASE,
    )
    package_deals = re.search(r"\bbuy\s+\d+\s*,?\s*get\b", blob, re.IGNORECASE)
    if package_deals and _MEMBERSHIP_CONTENT_RE.search(blob):
        return True
    return bool(_MEMBERSHIP_FEE_IN_PROMO_RE.search(blob)) and not promo_signals


def detect_fragmented_unbound_prices(segments: Sequence[str]) -> bool:
    binding = validate_promotion_content_binding(segments)
    if not binding.accepted:
        return True
    texts = [_normalize_compare_text(s) for s in segments if str(s or "").strip()]
    orphan = 0
    for index, text in enumerate(texts):
        has_price = bool(_CURRENCY_RE.search(text) or re.search(r"\d+\s*%\s*off", text, re.I))
        has_service = bool(_SERVICE_HINT_RE.search(text))
        if has_price and not has_service and len(text.split()) <= 5:
            orphan += 1
        if has_service and not has_price and index + 1 < len(texts):
            nxt = texts[index + 1]
            if _ORPHAN_PRICE_SEGMENT_RE.match(nxt) or (
                _CURRENCY_RE.search(nxt) and not _SERVICE_HINT_RE.search(nxt)
            ):
                return True
    return orphan >= 2


def audit_promotion_content_semantics(
    *,
    title: str,
    segments: Sequence[str],
    source_url: str = "",
    check_binding: bool = True,
) -> list[SemanticIssue]:
    """check_binding=False 用于单张卡片：卡片各行本身已经绑定，不按行检查价格是否与服务名称同段。"""
    del source_url
    issues: list[SemanticIssue] = []
    cleaned = [str(s).strip() for s in segments if str(s or "").strip()]
    blob = _segment_blob(cleaned, title)

    if has_markdown_or_shopify_residue(blob):
        issues.append(
            SemanticIssue("high", "MARKDOWN_OR_SHOPIFY_RESIDUE", "promotion_content 含 Markdown/URL/Shopify 残留")
        )
    if check_binding and detect_fragmented_unbound_prices(cleaned):
        issues.append(
            SemanticIssue("high", "FRAGMENTED_UNBOUND_PRICES", "价格行未与服务/套餐名称绑定")
        )
    if is_soft_cta_only(title, cleaned):
        issues.append(
            SemanticIssue("high", "SOFT_CTA_ONLY", "仅有软性 CTA/条款，缺少可核实优惠金额")
        )
    if looks_like_membership_in_promo(title, cleaned):
        issues.append(
            SemanticIssue("high", "MEMBERSHIP_AS_PROMO", "会员月费/权益块误入 promotion_content")
        )
    if has_ui_pollution(cleaned):
        issues.append(
            SemanticIssue("medium", "UI_POLLUTION", "promotion_content 含 UI/按钮/购物车文案")
        )
    if title_looks_like_slug(title):
        issues.append(
            SemanticIssue("medium", "TITLE_LOOKS_LIKE_SLUG", f"promotion_title 像 URL slug: {title!r}")
        )
    return issues


def duplicate_benefit_indices(benefits: Sequence[str]) -> list[int]:
    seen: dict[str, int] = {}
    dups: list[int] = []
    for index, raw in enumerate(benefits):
        key = _normalize_compare_text(raw).casefold()
        if not key:
            continue
        if key in seen:
            dups.append(index)
        else:
            seen[key] = index
    return dups


def benefits_are_discount_only(benefits: Sequence[str]) -> bool:
    lines = [str(b).strip() for b in benefits if str(b or "").strip()]
    if not lines:
        return False
    perk_hint = re.compile(
        r"\b(?:consult|appointment|credit|month|free|included|priority|birthday|"
        r"referral|gift|event|access|unlimited|treatment|facial|injection|b12|"
        r"commitment|cancel|rollover)\b",
        re.IGNORECASE,
    )
    if any(perk_hint.search(line) for line in lines):
        return False
    if all(_DISCOUNT_ONLY_BENEFIT_RE.match(line) for line in lines):
        return True
    if len(lines) == 1 and re.search(r"\d+\s*%\s*off", lines[0], re.I) and len(lines[0].split()) <= 6:
        return True
    return False


def audit_membership_benefits_semantics(
    item: Mapping[str, object],
    *,
    evidence: str = "",
    is_ocr_required: bool = False,
) -> list[SemanticIssue]:
    del evidence
    issues: list[SemanticIssue] = []
    benefits = _norm_benefits(item.get("benefits"))
    price = item.get("membership_price")

    if price is not None and not benefits:
        issues.append(
            SemanticIssue("high", "NULL_BENEFITS_PRICED", "有标价但 benefits 为空")
        )

    dupes = duplicate_benefit_indices(benefits)
    if dupes:
        issues.append(
            SemanticIssue(
                "medium",
                "DUPLICATE_BENEFIT_BULLET",
                f"benefits 数组内重复条目（indices={dupes[:5]}）",
            )
        )

    blob = "\n".join(benefits)
    if has_markdown_or_shopify_residue(blob):
        issues.append(
            SemanticIssue("high", "MARKDOWN_OR_SHOPIFY_RESIDUE", "benefits 含 Markdown/URL 残留")
        )

    if benefits and benefits_are_discount_only(benefits):
        issues.append(
            SemanticIssue(
                "high",
                "INCOMPLETE_BENEFITS_DISCOUNT_ONLY",
                "权益几乎只有折扣百分比，缺少页面列出的其他 perk",
            )
        )

    if is_ocr_required and not benefits:
        issues.append(
            SemanticIssue(
                "high",
                "NEEDS_OCR",
                "页面以图片 offer 为主且 is_ocr_required，benefits/content 未完成 OCR",
            )
        )
    return issues


def gate_promotion_semantics(
    item: Mapping[str, object],
    evidence: str = "",
    *,
    check_binding: bool = True,
) -> GateDecision:
    title = str(item.get("promotion_title") or "").strip()
    segments = [
        str(value).strip()
        for value in item.get("promotion_content") or []
        if str(value or "").strip()
    ]
    source_url = str(item.get("source_url") or evidence or "")
    for issue in audit_promotion_content_semantics(
        title=title,
        segments=segments,
        source_url=source_url,
        check_binding=check_binding,
    ):
        if issue.severity == "high":
            return GateDecision(False, None, issue.issue_type.lower())
    return GateDecision(True, None, "semantic_ok")


def gate_membership_semantics(
    item: Mapping[str, object],
    *,
    evidence: str = "",
    is_ocr_required: bool = False,
) -> GateDecision:
    for issue in audit_membership_benefits_semantics(
        item,
        evidence=evidence,
        is_ocr_required=is_ocr_required,
    ):
        if issue.severity == "high":
            return GateDecision(False, None, issue.issue_type.lower())
    return GateDecision(True, None, "semantic_ok")


def _self_check() -> None:
    promo_fail = audit_promotion_content_semantics(
        title="monthly-promos-page",
        segments=[
            "New patient offer; cannot be combined with other offers.",
            "Terms and conditions may apply.",
        ],
    )
    assert any(i.issue_type == "SOFT_CTA_ONLY" for i in promo_fail)

    bound_ok = audit_promotion_content_semantics(
        title="July Specials",
        segments=["Botox $11/unit | Regular $14/unit | Save 20%"],
    )
    assert not any(i.severity == "high" for i in bound_ok)

    memb_fail = audit_membership_benefits_semantics(
        {"membership_price": 150, "benefits": ["10% off everything"]},
    )
    assert any(i.issue_type == "INCOMPLETE_BENEFITS_DISCOUNT_ONLY" for i in memb_fail)

    dup = audit_membership_benefits_semantics(
        {
            "membership_price": 99,
            "benefits": ["Back Bay only.", "Back Bay only."],
        },
    )
    assert any(i.issue_type == "DUPLICATE_BENEFIT_BULLET" for i in dup)


if __name__ == "__main__":
    _self_check()
    print("extraction_semantic_quality self-check ok")
