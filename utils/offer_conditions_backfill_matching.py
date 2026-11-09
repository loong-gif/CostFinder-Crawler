from __future__ import annotations

import re
from typing import Any

from utils.offer_conditions import sanitize_conditions

_STOP_WORDS = {
    "a", "an", "and", "at", "for", "from", "get", "in", "of", "on", "or", "per",
    "again", "buy", "customer", "discount", "free", "minimum", "member", "members",
    "non-member", "off", "only", "patient", "patients", "price", "purchase", "regular",
    "required", "save", "special", "syringe", "treatment", "the", "to", "unit", "units",
    "vial", "vials", "with", "your",
}
_NUMBER = r"\d+(?:\.\d+)?"
_UNIT = r"units?|u(?:nit)?s?|syringes?|vials?|sessions?|treatments?|days?|weeks?|months?|years?"


def _source_segments(source_text: str) -> list[str]:
    text = source_text.replace("<br>", "\n").replace("<br/>", "\n")
    lines = [
        re.sub(r"\s+", " ", line).strip(" -*\"'")
        for line in re.split(r"\n+|(?<=[.!?])\s+|[→•|]+", text)
    ]
    return [line for line in lines if line]


def _offer_terms(offer: dict[str, Any]) -> tuple[list[str], list[str]]:
    raw = str(offer.get("offer_raw_text") or "").casefold()
    items = offer.get("promo_offer_items") or []
    if isinstance(items, dict):
        items = [items]
    names = [str(item.get("item_name") or "").casefold() for item in items if isinstance(item, dict)]
    terms = set(names)
    terms.update(
        token
        for token in re.findall(r"[a-z][a-z0-9-]{2,}", raw)
        if token not in _STOP_WORDS
    )
    numbers = re.findall(_NUMBER, raw.replace(",", ""))
    return sorted(terms), numbers


def _number_hit_count(text: str, numbers: list[str]) -> int:
    normalized = re.sub(r"\s+", " ", text).casefold()
    return sum(
        1
        for number in dict.fromkeys(numbers)
        if re.search(rf"(?<![\d,]){re.escape(number)}(?![\d,])", normalized)
    )


def _match_source_segments(offer: dict[str, Any], source_text: str) -> list[str]:
    raw = str(offer.get("offer_raw_text") or "")
    normalized_raw = re.sub(r"\s+", " ", raw).casefold().strip()
    segments = _source_segments(source_text)
    exact = [segment for segment in segments if normalized_raw and normalized_raw in re.sub(r"\s+", " ", segment).casefold()]
    if exact:
        return exact[:2] if len(exact) <= 2 else []
    terms, numbers = _offer_terms(offer)
    anchor_terms = [term for term in terms if len(term) >= 4 and term not in _STOP_WORDS]
    scored: list[tuple[int, int, str]] = []
    scores_by_index: dict[int, tuple[int, int, int]] = {}
    for index, segment in enumerate(segments):
        normalized = re.sub(r"\s+", " ", segment).casefold()
        term_hits = sum(1 for term in anchor_terms if term and term in normalized)
        number_hits = _number_hit_count(segment, numbers)
        score = (3 * term_hits) + (2 * number_hits)
        if term_hits and score >= 3:
            scored.append((score, index, segment))
            scores_by_index[index] = (score, term_hits, number_hits)
    if not scored:
        return []
    best = max(score for score, _index, _segment in scored)
    best_indices = [index for score, index, _segment in scored if score == best]
    if len(best_indices) > 2:
        return []
    selected = set(best_indices)
    condition_signal = re.compile(
        r"minimum|at\s+least|after|within|every|anniversary|renewal|birthday|"
        r"pre[- ]?book|consult|coupon|combined|supplies\s+last|limited|"
        r"purchase|buy|free|complimentary|used\s+within",
        re.IGNORECASE,
    )
    # Pull in only an immediately adjacent clause that shares an offer anchor
    # or one of the offer's numeric values. This captures split source rows
    # without broadcasting a neighboring offer's restriction.
    for index in best_indices:
        for neighbor in (index - 1, index + 1):
            if neighbor < 0 or neighbor >= len(segments):
                continue
            segment = segments[neighbor]
            normalized = re.sub(r"\s+", " ", segment).casefold()
            term_hits = sum(1 for term in anchor_terms if term and term in normalized)
            number_hits = _number_hit_count(segment, numbers)
            if condition_signal.search(segment) and (term_hits or number_hits):
                selected.add(neighbor)
    return [segments[index] for index in sorted(selected)]


def _condition_candidates(segment: str) -> list[dict[str, str]]:
    text = re.sub(r"\s+", " ", segment).strip(" -*\"'")
    lowered = text.casefold()
    candidates: list[dict[str, str]] = []

    def add(requirement: str) -> None:
        requirement = re.sub(r"\s+", " ", requirement).strip(" -*\"'")
        if requirement:
            candidates.append({"requirement": requirement, "evidence": text})

    # New/first-time eligibility is stored in the boolean column. Do not copy
    # it into conditions, but keep other restrictions in the same sentence.
    if re.search(r"\breturn(?:ing)?\s+(?:patients?|clients?|customers?)\b|patients?\s+who\s+have\s+had", lowered):
        add("Returning customer required")
    minimum = re.search(
        rf"\b(?:minimum|at\s+least)\s+({_NUMBER})\s+({_UNIT})\b|\b({_NUMBER})\s+({_UNIT})\s+(?:minimum|required|or\s+more|\+)",
        text,
        re.IGNORECASE,
    )
    if minimum:
        number = minimum.group(1) or minimum.group(3)
        unit = minimum.group(2) or minimum.group(4)
        add(f"Minimum {number} {unit} required")
    for match in re.finditer(
        rf"\b(?:after|within|every|per)\s+({_NUMBER})\s+({_UNIT})\b[^.!?\n]*",
        text,
        re.IGNORECASE,
    ):
        add(match.group(0))
    if re.search(r"\b(?:anniversary|renewal|birthday)\b", lowered):
        add("Available on the stated anniversary or renewal")
    prebook = re.search(r"\bpre[- ]?book\b[^.!?\n]*", text, re.IGNORECASE)
    if prebook:
        add(prebook.group(0))
    consultation = re.search(r"\b(?:consultation|consult)\b[^.!?\n]{0,30}\b(?:required|must)\b", text, re.IGNORECASE)
    if consultation:
        add(consultation.group(0))
    if "coupon" in lowered and re.search(r"activate|redeem|manufacturer", lowered):
        add("Manufacturer coupon activation or redemption required")
    if re.search(r"\b(?:cannot|can't|can not|not)\s+be\s+combined\b|\bnot\s+combinable\b", lowered):
        add("Cannot be combined with other offers")
    if re.search(r"while\s+supplies\s+last|limited\s+(?:time|quantity|availability)", lowered):
        add("Limited availability")
    purchase = re.search(r"\b(?:with|upon)\s+purchase\s+of\b[^.!?\n]*|\bbuy\s+\d+\s+[^.!?\n]*", text, re.IGNORECASE)
    if purchase and re.search(r"free|complimentary|discount|save|only", lowered):
        add(f"Purchase requirement: {purchase.group(0)}")
    used = re.search(r"\bmust\s+be\s+used\s+within\s+\d+\s+(?:days?|weeks?|months?|years?)\b", text, re.IGNORECASE)
    if used:
        add(used.group(0))
    return candidates


def _extract_local_conditions(offer: dict[str, Any], source_text: str) -> tuple[list[str] | None, str | None]:
    matched = _match_source_segments(offer, source_text)
    if not matched:
        return None, "offer_not_located"
    segments = list(matched)
    conditions: list[dict[str, str]] = []
    for segment in segments:
        conditions.extend(_condition_candidates(segment))
    sanitized, issues = sanitize_conditions(conditions, source_text)
    if issues:
        return None, "invalid_conditions_evidence"
    raw = str(offer.get("offer_raw_text") or "").casefold()
    if re.search(r"\bminimum\b|\bat\s+least\b|\bafter\b|\bwithin\b|\bevery\b|"
                 r"\banniversary\b|\brenewal\b|\bpre[- ]?book\b|\bcoupon\b|"
                 r"\b(?:cannot|can't)\s+be\s+combined\b|\bnot\s+combinable\b|"
                 r"\b(?:with|upon)\s+purchase\b", raw):
        if not sanitized:
            return None, "condition_not_located"
        if "minimum" in raw and not any("minimum" in item.casefold() for item in sanitized):
            return None, "condition_not_located"
    return sanitized or [], None
