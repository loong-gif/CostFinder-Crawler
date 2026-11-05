"""Keep promotion_content offer-card fields bound in one segment."""
from __future__ import annotations

import re
from typing import Sequence

from utils.recent_raw_extraction import (
    GateDecision,
    SIGNAL_LINE,
    _line_to_promotion_segment,
    _normalize_compare_text,
    filter_promotion_segments,
    is_low_quality_promotion_segment,
)

_CARD_JOIN = " | "
_CURRENCY_RE = re.compile(r"\$\s*\d+(?:\.\d+)?")
_DISCOUNT_RE = re.compile(r"\b\d+(?:\.\d+)?\s*%|\bsave\s+\$?\d", re.IGNORECASE)
_SERVICE_NAME_RE = re.compile(
    r"\b(?:botox|dysport|jeuveau|xeomin|filler|juvederm|restylane|sculptra|kybella|"
    r"microneedling|laser|peel|facial|morpheus|coolsculpt|semaglutide|tox|skinmedica|"
    r"package|syringe|unit)\b",
    re.IGNORECASE,
)
_ORPHAN_PRICE_RE = re.compile(
    r"^\s*(?:\$|regular\s+\$|was\s+\$|now\s+\$|save\s+\$|\d+\s*%\s*off)",
    re.IGNORECASE,
)


def _card_key(text: str) -> str:
    prices = _CURRENCY_RE.findall(text)
    names = _SERVICE_NAME_RE.findall(text)
    return "|".join(sorted(set(p.casefold() for p in prices)) + sorted(set(n.casefold() for n in names)))


def _merge_lines(lines: Sequence[str]) -> str:
    cleaned = [_normalize_compare_text(line) for line in lines if str(line or "").strip()]
    if not cleaned:
        return ""
    out: list[str] = []
    seen: set[str] = set()
    for line in cleaned:
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return _CARD_JOIN.join(out)


def extract_offer_cards_from_markdown(evidence: str) -> list[str]:
    """One promotion_content segment per markdown offer card block."""
    block: list[str] = []
    cards: list[str] = []

    def flush() -> None:
        merged = _merge_lines(block)
        block.clear()
        if merged and (SIGNAL_LINE.search(merged) or _DISCOUNT_RE.search(merged)):
            cards.append(merged)

    for raw in str(evidence or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if re.match(r"^#{1,3}\s", stripped):
            flush()
            continue
        if stripped.startswith("!["):
            flush()
            alt = _line_to_promotion_segment(stripped)
            if alt:
                block.append(alt)
            continue
        segment = _line_to_promotion_segment(stripped)
        if not segment or is_low_quality_promotion_segment(segment):
            continue
        if not block:
            block.append(segment)
            continue
        block.append(segment)
    flush()
    return cards


def _cluster_flat_segments(segments: Sequence[str]) -> list[str]:
    clustered: list[str] = []
    bucket: list[str] = []

    def flush_bucket() -> None:
        nonlocal bucket
        if bucket:
            clustered.append(_merge_lines(bucket))
        bucket = []

    for segment in segments:
        text = _normalize_compare_text(segment)
        if not text:
            continue
        if not bucket:
            bucket.append(text)
            continue
        merged = _merge_lines(bucket)
        same_card = _card_key(text) and _card_key(text) == _card_key(merged)
        price_tail = _ORPHAN_PRICE_RE.match(text) or (
            _CURRENCY_RE.search(text) and not _SERVICE_NAME_RE.search(text)
        )
        if same_card or price_tail or not _SERVICE_NAME_RE.search(text):
            bucket.append(text)
            if SIGNAL_LINE.search(_merge_lines(bucket)):
                flush_bucket()
            continue
        flush_bucket()
        bucket.append(text)
    flush_bucket()
    return clustered


def bind_promotion_content_segments(
    segments: Sequence[str],
    evidence: str = "",
) -> list[str]:
    """Merge fragmented offer lines into bound card strings."""
    cleaned = filter_promotion_segments(segments, evidence) if evidence else [
        _normalize_compare_text(value) for value in segments if str(value or "").strip()
    ]
    if not cleaned:
        return []
    if len(cleaned) == 1:
        return cleaned

    from_markdown = extract_offer_cards_from_markdown(evidence) if evidence else []
    if from_markdown and len(from_markdown) < len(cleaned):
        return from_markdown

    clustered = _cluster_flat_segments(cleaned)
    if len(clustered) < len(cleaned):
        return clustered
    if len(cleaned) >= 3 and validate_promotion_content_binding(clustered).accepted:
        return clustered
    return cleaned


def validate_promotion_content_binding(segments: Sequence[str]) -> GateDecision:
    if not segments:
        return GateDecision(False, None, "missing_promotion_content")
    orphan = 0
    for segment in segments:
        text = _normalize_compare_text(segment)
        if not text:
            continue
        has_price = bool(_CURRENCY_RE.search(text) or _DISCOUNT_RE.search(text))
        has_service = bool(_SERVICE_NAME_RE.search(text))
        if has_price and not has_service and len(text.split()) <= 4:
            orphan += 1
    if orphan >= 2:
        return GateDecision(False, None, "unbound_promotion_content_fragments")
    if len(segments) >= 10 and orphan / len(segments) >= 0.25:
        return GateDecision(False, None, "excessive_unbound_price_fragments")
    return GateDecision(True, None, "bound")


def _self_check() -> None:
    evidence = (
        "## Monthly Specials\n\n"
        "![Botox $11/unit NakedMD](https://x/a.png)\n\n"
        "Regular $14/unit\n\n"
        "Save 20%\n\n"
        "![Juvederm $599/syringe](https://x/b.png)\n\n"
        "Regular $750\n"
    )
    fragments = ["Botox $11/unit", "Regular $14/unit", "Save 20%", "Juvederm $599/syringe", "Regular $750"]
    cards = extract_offer_cards_from_markdown(evidence)
    assert len(cards) == 2
    bound = bind_promotion_content_segments(fragments, evidence)
    assert len(bound) == 2
    assert validate_promotion_content_binding(bound).accepted
    assert not validate_promotion_content_binding(fragments).accepted


if __name__ == "__main__":
    _self_check()
    print("promotion_content_cards self-check ok")
