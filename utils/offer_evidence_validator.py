"""Evidence gate for AI-extracted promo offers.

The validator runs immediately before persistence.  It keeps source-backed
facts, clears unsupported optional values, and quarantines an offer when its
quoted raw text or its promotional fact is not supported by the scraped page.
It does not add fields to any database payload or change the extraction schema.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Any, Mapping

from utils.clinic_service_extraction import is_promo_offer
from utils.offer_price_evidence import extract_price_evidence


_CURRENCY = re.compile(r"\$\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)")
_PROMO_SIGNAL = re.compile(
    r"\b(?:special|promo|promotion|offer|sale|member|membership|vip|"
    r"new\s+(?:patient|client)|first[-\s]?time|introductory|only|just|save|off)\b",
    re.IGNORECASE,
)
_MEMBERSHIP_SIGNAL = re.compile(r"\b(?:member|membership|vip|club|plan)\b", re.IGNORECASE)
_NEW_CUSTOMER_SIGNAL = re.compile(
    r"\b(?:new\s+(?:patient|client|customer)|first[-\s]?time|first\s+visit)\b",
    re.IGNORECASE,
)
_BOOLEAN_CONDITION_SIGNAL = re.compile(
    r"(?:membership\s+required|new\s+(?:patient|client|customer)s?\s+only|"
    r"first[-\s]?time\s+(?:patient|client|customer)s?\s+only)",
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
class OfferEvidenceIssue:
    field: str
    reason: str
    value: Any

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field, "reason": self.reason, "value": self.value}


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


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"[^\w$%./+-]+", " ", text).strip()


def sanitize_conditions(
    conditions: Any,
    source: str,
) -> tuple[list[str] | None, tuple["OfferEvidenceIssue", ...]]:
    """Validate source evidence and return only requirement strings.

    Extraction still supplies ``requirement`` + ``evidence`` objects so the
    evidence gate can reject unsupported claims.  Persistence deliberately
    keeps only the requirement text because the database column is ``text[]``.
    """
    if conditions is None:
        return None, (OfferEvidenceIssue("conditions", "missing_conditions", None),)
    if not isinstance(conditions, list):
        return None, (OfferEvidenceIssue("conditions", "must_be_array", conditions),)

    normalized_source = _normalize_text(source)
    output: list[str] = []
    seen: set[str] = set()
    issues: list[OfferEvidenceIssue] = []
    for index, raw in enumerate(conditions):
        field = f"conditions[{index}]"
        if not isinstance(raw, Mapping):
            issues.append(OfferEvidenceIssue(field, "must_be_object", raw))
            continue
        extra_fields = set(raw) - {"requirement", "evidence"}
        if extra_fields:
            issues.append(OfferEvidenceIssue(field, "unexpected_fields", sorted(extra_fields)))
            continue
        raw_requirement = raw.get("requirement")
        raw_evidence = raw.get("evidence")
        if not isinstance(raw_requirement, str):
            issues.append(OfferEvidenceIssue(f"{field}.requirement", "must_be_string", raw_requirement))
            continue
        if not isinstance(raw_evidence, str):
            issues.append(OfferEvidenceIssue(f"{field}.evidence", "must_be_string", raw_evidence))
            continue
        requirement = raw_requirement.strip()
        evidence = raw_evidence.strip()
        if not requirement:
            issues.append(OfferEvidenceIssue(f"{field}.requirement", "missing_requirement", raw.get("requirement")))
            continue
        if not evidence:
            issues.append(OfferEvidenceIssue(f"{field}.evidence", "missing_evidence", raw.get("evidence")))
            continue
        if _BOOLEAN_CONDITION_SIGNAL.search(requirement):
            issues.append(OfferEvidenceIssue(f"{field}.requirement", "duplicates_boolean_field", requirement))
            continue
        if not normalized_source or _normalize_text(evidence) not in normalized_source:
            issues.append(OfferEvidenceIssue(f"{field}.evidence", "not_supported_by_source", evidence))
            continue
        key = _normalize_text(requirement)
        if key in seen:
            continue
        seen.add(key)
        output.append(requirement)
    return output, tuple(issues)


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
    parsed = extract_price_evidence(raw_text).get("regular_price")
    if _same_number(parsed, value):
        return True
    for prefix, suffix in _currency_contexts(raw_text, value):
        if re.search(
            r"\b(?:was|regular(?:ly)?|original|list\s+price|value)\b\s*[:=]?\s*$",
            prefix,
            re.I,
        ) or re.match(
            r"^\s*[,;:()\-]*\s*(?:regular(?:ly)?|original|list\s+price|value)\b",
            suffix,
            re.I,
        ):
            return True
    mentions = _currency_mentions(raw_text)
    target = _number(value)
    return bool(
        target is not None
        and len(mentions) >= 2
        and target == max(amount for amount, _start, _end in mentions)
        and _PROMO_SIGNAL.search(raw_text)
    )


def _discount_price_supported(value: Any, raw_text: str) -> bool:
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
    if len(valid_amounts) >= 2 and target == min(valid_amounts):
        return True
    return bool(len(valid_amounts) == 1 and _PROMO_SIGNAL.search(raw_text))


def _quantity_supported(value: Any, raw_text: str, *, minimum: bool = False) -> bool:
    number = _number(value)
    if number is None:
        return False
    shown = f"{number:g}"
    unit_pattern = "|".join(_UNIT_WORDS.values())
    if minimum:
        return bool(
            re.search(
                rf"\b{re.escape(shown)}\s*(?:{unit_pattern})\b.{{0,24}}?"
                r"(?:minimum|at\s+least|or\s+more|\+)",
                raw_text,
                re.I,
            )
        )
    return bool(re.search(rf"\b{re.escape(shown)}\s*(?:{unit_pattern})\b", raw_text, re.I))


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
    return _same_number(extract_price_evidence(raw_text).get("discount_percent"), value)


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
) -> OfferEvidenceDecision:
    """Return an immutable evidence decision and a sanitized offer copy."""
    candidate = dict(offer)
    source = str(evidence or "")
    raw_text = str(candidate.get("offer_raw_text") or "").strip()
    if not source.strip():
        return OfferEvidenceDecision(False, "missing_source_evidence", candidate)
    if not raw_text or not _raw_text_supported(raw_text, source):
        return OfferEvidenceDecision(False, "offer_raw_text_not_supported", candidate)

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
        supported=_discount_price_supported(candidate.get("discount_price"), raw_text),
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
        supported=_quantity_supported(candidate.get("min_quantity"), raw_text, minimum=True),
    )
    _clear_unsupported(
        candidate,
        issues,
        field="is_membership_required",
        supported=(
            not bool(candidate.get("is_membership_required"))
            or bool(_MEMBERSHIP_SIGNAL.search(raw_text))
        ),
        replacement=False,
    )
    _clear_unsupported(
        candidate,
        issues,
        field="is_new_customer_required",
        supported=(
            not bool(candidate.get("is_new_customer_required"))
            or bool(_NEW_CUSTOMER_SIGNAL.search(raw_text))
        ),
        replacement=False,
    )

    raw_items = candidate.get("items") if isinstance(candidate.get("items"), list) else []
    sanitized_items: list[dict[str, Any]] = []
    for index, raw_item in enumerate(raw_items):
        item = dict(raw_item) if isinstance(raw_item, Mapping) else {}
        prefix = f"items[{index}]"
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
        if service_area and _normalize_text(service_area) not in _normalize_text(raw_text):
            issues.append(OfferEvidenceIssue(f"{prefix}.service_area", "not_supported_by_offer_raw_text", service_area))
            item["service_area"] = None
        sanitized_items.append(item)
    candidate["items"] = sanitized_items

    if not is_promo_offer(candidate):
        return OfferEvidenceDecision(
            False,
            "no_supported_promo_fact",
            candidate,
            tuple(issues),
        )
    reason = "validated_with_sanitization" if issues else "validated"
    return OfferEvidenceDecision(True, reason, candidate, tuple(issues))
