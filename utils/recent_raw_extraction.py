from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Sequence
from urllib.parse import urlparse


SHARED_HOSTS = {"facebook.com", "zoca.com"}
GENERIC_LOCATION_WORDS = {
    "location",
    "locations",
    "membership",
    "memberships",
    "offer",
    "offers",
    "price",
    "pricing",
    "program",
    "promo",
    "promotions",
    "service",
    "services",
    "special",
    "specials",
}
SIGNAL_LINE = re.compile(
    r"(?:[$€£]\s?\d|\d+(?:\.\d+)?\s*%|\b(?:off|membership|member|per unit|/unit)\b)",
    re.IGNORECASE,
)
PROMOTION_NOISE = re.compile(
    r"\b(?:consent to receive|reply stop|message and data rates|privacy policy|google recaptcha|book now|shop here|bottom of page|skip to main content)\b",
    re.IGNORECASE,
)
PROMOTION_OCR_GARBAGE = re.compile(
    r"(?:"
    r"\b(?:GETFREEA|PACKAGEOF\d+|BUYANY\d+|BUY\s*4O|20%0FF|REFININGFOAM|REGNERATION|BUYANY2FILLER|GET3RDFILLER)\b"
    r"|\b(?:INTRODUCING|PROTECTION|COLLECTION OF FILLERS|SCAN THE QR)\b"
    r")",
    re.IGNORECASE,
)
PROMOTION_SMASHED_WORD = re.compile(r"[a-z][A-Z]{2,}[a-zA-Z]*")


@dataclass(frozen=True)
class GateDecision:
    accepted: bool
    business_id: int | None
    reason: str


def _parse_url(url: str):
    value = str(url or "").strip()
    return urlparse(value if "://" in value else f"https://{value}")


def normalize_host(url: str) -> str:
    return (_parse_url(url).hostname or "").lower().removeprefix("www.")


def _identity_blob(source: dict) -> str:
    return " ".join(
        str(source.get(key) or "") for key in ("url", "title", "description", "text")
    ).casefold()


def _contains_phrase(text: str, phrase: str) -> bool:
    return bool(
        phrase
        and re.search(
            rf"(?<!\w){re.escape(phrase.casefold())}(?!\w)",
            text.casefold(),
        )
    )


def _normalize_words(value: str) -> str:
    return re.sub(
        r"[\W_]+",
        " ",
        str(value or "").casefold().replace("&", " and "),
    ).strip()


def _is_generic_location_label(value: str) -> bool:
    words = set(_normalize_words(value).split())
    return bool(words) and words <= GENERIC_LOCATION_WORDS


def _has_strong_identity(source: dict, business: dict) -> bool:
    blob = _identity_blob(source)
    city = str(business.get("city") or "").strip()
    address = _normalize_words(business.get("address", ""))
    return _contains_phrase(blob, city) or bool(
        address and _contains_phrase(_normalize_words(blob), address)
    )


def _has_business_name_identity(source: dict, business: dict) -> bool:
    name = _normalize_words(business.get("name", ""))
    blob = _normalize_words(_identity_blob(source))
    return bool(name and _contains_phrase(blob, name))


def _has_conflicting_location(source: dict, business: dict) -> bool:
    city = str(business.get("city") or "").strip()
    segments = _parse_url(source.get("url", "")).path.strip("/").split("/")
    path_identity = " ".join(segments).replace("-", " ").replace("_", " ")
    if (
        any(segment and not _is_generic_location_label(segment) for segment in segments)
        and not _contains_phrase(path_identity, city)
    ):
        return True

    title = str(source.get("title") or "").strip()
    name = str(business.get("name") or "").strip()
    if name and title.casefold().startswith(name.casefold()):
        suffix = title[len(name) :].strip(" -|:,")
        if suffix and not _is_generic_location_label(suffix) and not _contains_phrase(title, city):
            return True
    return False


def detect_multilocation_hosts(candidates: Sequence[dict]) -> set[str]:
    paths: dict[str, set[str]] = {}
    for candidate in candidates:
        host = normalize_host(candidate.get("url", ""))
        segment = next(
            (
                segment.casefold()
                for segment in _parse_url(candidate.get("url", "")).path.strip("/").split("/")
                if segment and not _is_generic_location_label(segment)
            ),
            "",
        )
        if host and segment:
            paths.setdefault(host, set()).add(segment)
    return {host for host, segments in paths.items() if len(segments) >= 3}


def resolve_business(
    source: dict,
    businesses: Sequence[dict],
    multilocation_hosts: set[str],
) -> GateDecision:
    host = normalize_host(source.get("url", ""))
    matches = [row for row in businesses if normalize_host(row.get("website", "")) == host]
    if len(matches) != 1:
        return GateDecision(False, None, "ambiguous_host" if matches else "unmatched_host")
    business = matches[0]
    if host in multilocation_hosts and _has_conflicting_location(source, business):
        return GateDecision(False, None, "multilocation_without_target_identity")
    has_identity = _has_strong_identity(source, business)
    if host in SHARED_HOSTS and not has_identity:
        return GateDecision(False, None, "ambiguous_host")
    if host in SHARED_HOSTS and not _has_business_name_identity(source, business):
        return GateDecision(False, None, "ambiguous_host")
    if host in multilocation_hosts and not has_identity:
        return GateDecision(False, None, "multilocation_without_target_identity")
    return GateDecision(True, int(business["business_id"]), "matched")


def _normalize_compare_text(text: str) -> str:
    value = str(text or "").replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", value).strip()


def validate_service(
    item: dict,
    evidence: str,
    *,
    source_url: str = "",
) -> GateDecision:
    from utils.service_price_guard import normalize_service_catalog_item

    decision = normalize_service_catalog_item(
        item,
        source_url=source_url,
        evidence=evidence,
    )
    if not decision.accepted:
        return GateDecision(False, None, decision.reason)
    return GateDecision(True, None, "validated")


def validate_membership(item: dict, evidence: str) -> GateDecision:
    del evidence
    name = str(item.get("membership_name") or "").strip()
    if not name or item.get("membership_price") is None:
        return GateDecision(False, None, "missing_membership_or_price")
    return GateDecision(True, None, "validated")


def _promotion_content_title_only(segments: Sequence[str], title: str) -> bool:
    if len(segments) != 1:
        return False
    return (
        _normalize_compare_text(segments[0]).casefold()
        == _normalize_compare_text(title).casefold()
    )


def _markdown_section_bounds(lines: list[str], title_idx: int) -> tuple[int, int]:
    header_level = 1
    for i in range(title_idx, -1, -1):
        match = re.match(r"^(#{1,3})\s", lines[i].strip())
        if match:
            header_level = len(match.group(1))
            section_start = i + 1
            break
    else:
        section_start = 0
    section_end = len(lines)
    for i in range(title_idx + 1, len(lines)):
        match = re.match(r"^(#{1,3})\s", lines[i].strip())
        if match and len(match.group(1)) <= header_level:
            section_end = i
            break
    return section_start, section_end


def _line_to_promotion_segment(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return ""
    alt = re.match(r"!\[(.*?)\]", stripped)
    if alt:
        return alt.group(1).strip().replace("%20", " ")
    return re.sub(r"^[-*]\s*", "", stripped).strip()


def is_low_quality_promotion_segment(segment: str) -> bool:
    text = _normalize_compare_text(segment)
    if not text:
        return True
    if PROMOTION_OCR_GARBAGE.search(text):
        return True
    if PROMOTION_SMASHED_WORD.search(text):
        return True
    if len(text) < 10 and not SIGNAL_LINE.search(text):
        return True
    if len(text) > 18 and " " not in text:
        return True
    words = text.split()
    if len(words) == 1 and len(text) < 20 and not SIGNAL_LINE.search(text):
        return True
    return False


def _mostly_low_quality(segments: Sequence[str]) -> bool:
    if not segments:
        return True
    bad = sum(1 for segment in segments if is_low_quality_promotion_segment(segment))
    return bad / len(segments) >= 0.5


def _dedupe_segments(segments: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for segment in segments:
        text = _normalize_compare_text(segment)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _segment_supported_by_evidence(segment: str, evidence: str) -> bool:
    norm = _normalize_compare_text(segment)
    evidence_cf = evidence.casefold()
    if norm.casefold() in evidence_cf:
        return True
    words = [word for word in re.findall(r"\w+", norm) if len(word) > 2]
    if not words:
        return False
    hits = sum(1 for word in words if word.casefold() in evidence_cf)
    return hits >= max(1, len(words) // 2)


def filter_promotion_segments(segments: Sequence[str], evidence: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for segment in segments:
        text = _normalize_compare_text(segment)
        if not text or is_low_quality_promotion_segment(text) or PROMOTION_NOISE.search(text):
            continue
        if len(text) > 12 and not _segment_supported_by_evidence(text, evidence):
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def extract_promotion_content_from_markdown(evidence: str) -> list[str]:
    """Deterministic promotion lines from markdown alt text, headings, and FAQ."""
    segments: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        text = _normalize_compare_text(str(raw or "").replace("%20", " "))
        if not text or is_low_quality_promotion_segment(text) or PROMOTION_NOISE.search(text):
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        segments.append(text)

    for match in re.finditer(r"!\[([^\]]+)\]\(", evidence):
        alt = match.group(1).strip()
        if alt and (
            SIGNAL_LINE.search(alt)
            or re.search(r"\b(?:special|off|promo|learn|gift)\b", alt, re.IGNORECASE)
        ):
            add(alt)

    for line in str(evidence or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("!["):
            continue
        heading = re.match(r"^#{1,3}\s+(.*)$", stripped)
        if heading:
            title = heading.group(1).strip()
            if title and "faq" not in title.casefold():
                add(title)
            continue
        for sentence in re.split(r"(?<=[.?!])\s+", stripped):
            if len(sentence) < 25:
                continue
            if SIGNAL_LINE.search(sentence) and (
                "faq" in stripped.casefold()
                or re.search(r"\b(?:offers|price|between|save|off)\b", sentence, re.IGNORECASE)
            ):
                add(sentence)
        segment = _line_to_promotion_segment(stripped)
        if SIGNAL_LINE.search(segment):
            add(segment)

    return segments


def promotion_evidence_markdown(scrape: dict) -> str:
    """Prefer readable markdown over OCR when the page already has alt/FAQ signals."""
    markdown = str(scrape.get("markdown") or "")
    if extract_promotion_content_from_markdown(markdown):
        return markdown
    ocr = str(scrape.get("markdown_ocr") or "").strip()
    return ocr or markdown


def build_promotion_content(item: dict, evidence: str) -> list[str]:
    """Merge LLM segments with markdown extraction; drop OCR garbage on image-heavy pages."""
    from_markdown = extract_promotion_content_from_markdown(evidence)
    llm_raw = [
        str(value).strip() for value in item.get("promotion_content") or [] if str(value).strip()
    ]
    llm_filtered = filter_promotion_segments(llm_raw, evidence)

    if _mostly_low_quality(llm_raw) and from_markdown:
        merged = from_markdown
    elif len(llm_filtered) >= 2:
        merged = _dedupe_segments(llm_filtered + from_markdown)
    elif from_markdown:
        merged = from_markdown
    else:
        merged = filter_promotion_segments(expand_promotion_content(item, evidence), evidence)

    title = _normalize_compare_text(item.get("promotion_title") or "")
    if title:
        merged = [
            segment
            for segment in merged
            if _normalize_compare_text(segment).casefold() != title.casefold()
        ]
    return merged or from_markdown or llm_filtered


def expand_promotion_content(item: dict, evidence: str) -> list[str]:
    """ponytail: when LLM returns title-only content, pull same-section markdown lines."""
    title = str(item.get("promotion_title") or "").strip()
    segments = [str(value).strip() for value in item.get("promotion_content") or [] if str(value).strip()]
    if len(segments) > 1 or (len(segments) == 1 and not _promotion_content_title_only(segments, title)):
        return filter_promotion_segments(segments, evidence) or segments

    lines = str(evidence or "").splitlines()
    title_norm = _normalize_compare_text(title).casefold()
    title_idx = next(
        (
            index
            for index, line in enumerate(lines)
            if title_norm in _normalize_compare_text(line).casefold()
        ),
        None,
    )
    if title_idx is None:
        return segments

    section_start, section_end = _markdown_section_bounds(lines, title_idx)
    expanded: list[str] = []
    seen: set[str] = set()
    for line in lines[section_start:section_end]:
        segment = _line_to_promotion_segment(line)
        if not segment or PROMOTION_NOISE.search(segment):
            continue
        segment_norm = _normalize_compare_text(segment).casefold()
        if segment_norm in seen:
            continue
        if SIGNAL_LINE.search(segment) or title_norm in segment_norm:
            seen.add(segment_norm)
            expanded.append(segment)

    if _promotion_content_title_only(expanded or segments, title):
        for line in lines[title_idx + 1 : min(title_idx + 8, section_end)]:
            segment = _line_to_promotion_segment(line)
            if not segment or segment.startswith("http") or PROMOTION_NOISE.search(segment):
                continue
            segment_norm = _normalize_compare_text(segment).casefold()
            if segment_norm in seen or len(segment) < 12:
                continue
            seen.add(segment_norm)
            expanded.append(segment)

    if not expanded:
        return filter_promotion_segments(segments, evidence) or segments
    merged: list[str] = []
    seen = set()
    for segment in segments + expanded:
        key = _normalize_compare_text(segment).casefold()
        if key in seen:
            continue
        seen.add(key)
        merged.append(segment)
    return filter_promotion_segments(merged, evidence) or merged


def validate_promotion(item: dict, evidence: str) -> GateDecision:
    title = str(item.get("promotion_title") or "").strip()
    if not title:
        return GateDecision(False, None, "missing_promotion_title")
    segments = [
        str(value).strip() for value in item.get("promotion_content") or [] if str(value).strip()
    ]
    if not segments:
        return GateDecision(False, None, "missing_promotion_content")
    if _mostly_low_quality(segments):
        return GateDecision(False, None, "low_quality_promotion_content")
    if not any(SIGNAL_LINE.search(segment) for segment in segments):
        return GateDecision(False, None, "promotion_content_missing_price_signal")
    return GateDecision(True, None, "validated")


def pricing_template_fingerprint(text: str) -> str:
    lines = [
        re.sub(r"\s*/\s*", "/", re.sub(r"\s+", " ", line)).strip().casefold()
        for line in str(text or "").splitlines()
        if SIGNAL_LINE.search(line)
    ]
    normalized = "\n".join(sorted(set(lines)))
    return hashlib.sha256(normalized.encode()).hexdigest() if normalized else ""


def deduplicate_templates(candidates: Sequence[dict]) -> tuple[list[dict], list[dict]]:
    seen: dict[tuple[str, str], str] = {}
    kept: list[dict] = []
    rejected: list[dict] = []
    for candidate in candidates:
        fingerprint = pricing_template_fingerprint(candidate.get("text", ""))
        key = (normalize_host(candidate.get("url", "")), fingerprint)
        if fingerprint and key in seen:
            rejected.append(
                {
                    **candidate,
                    "reason": "duplicate_template",
                    "template": fingerprint,
                    "template_fingerprint": fingerprint,
                    "kept_url": seen[key],
                }
            )
            continue
        seen[key] = str(candidate.get("url") or "")
        kept.append({**candidate, "template": fingerprint})
    return kept, rejected
