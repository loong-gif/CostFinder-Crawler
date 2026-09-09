"""
站内价格/促销页发现爬虫
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

from bs4 import BeautifulSoup

STRONG_SIGNAL_KEYWORDS = {
    "pricing",
    "price",
    "membership",
    "special",
    "specials",
    "promotion",
    "promotions",
    "promo",
    "offer",
    "offers",
    "deal",
    "deals",
}
SERVICE_SIGNAL_KEYWORDS = {
    "service",
    "services",
    "botox",
    "dysport",
    "xeomin",
    "daxxify",
    "filler",
    "fillers",
    "injectable",
    "injectables",
    "laser",
    "facial",
    "facials",
    "package",
    "packages",
    "treatment",
    "treatments",
    "monthly",
    "membership",
}
PROMO_SIGNAL_PATTERNS = [
    re.compile(r"\b\d{1,3}%\s+off\b", re.IGNORECASE),
    re.compile(r"\bsave\s+\$?\d+", re.IGNORECASE),
    re.compile(r"\bdiscount\b", re.IGNORECASE),
    re.compile(r"\blimited[-\s]?time\b", re.IGNORECASE),
    re.compile(r"\bspecial offer\b", re.IGNORECASE),
    re.compile(r"\bfree consultation\b", re.IGNORECASE),
    re.compile(r"\bmonthly\b", re.IGNORECASE),
    re.compile(r"\bmember(ship)?\b", re.IGNORECASE),
]
PRICE_PATTERNS = [
    re.compile(r"\$\s*\d+(?:,\d{3})*(?:\.\d{2})?"),
    re.compile(r"\bUSD\s*\d+(?:,\d{3})*(?:\.\d{2})?\b", re.IGNORECASE),
    re.compile(r"\b\d+(?:,\d{3})*(?:\.\d{2})?\s*USD\b", re.IGNORECASE),
]
SEGMENT_CONTAINER_TAGS = {
    "main",
    "section",
    "article",
    "div",
    "ul",
    "ol",
    "li",
    "table",
    "tbody",
    "tr",
    "td",
    "p",
}
SEGMENT_SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "svg",
    "img",
    "picture",
    "video",
    "audio",
    "iframe",
    "form",
    "input",
    "button",
    "footer",
}
NOISE_SEGMENT_PATTERNS = {
    "navigation": re.compile(r"\b(home|about|contact|locations?|menu|faq|privacy|terms)\b", re.IGNORECASE),
    "commerce": re.compile(r"\b(cart|checkout|shop now|view product|add to cart|buy now)\b", re.IGNORECASE),
    "account": re.compile(r"\b(login|log in|sign in|sign up|my account)\b", re.IGNORECASE),
    "cta": re.compile(r"\b(book now|book online|schedule now|learn more|read more|call now|get started)\b", re.IGNORECASE),
    "review": re.compile(r"\b(star\s+star|review|reviews|testimonial|testimonials)\b", re.IGNORECASE),
    "social": re.compile(r"\b(facebook|instagram|tiktok|youtube|follow us)\b", re.IGNORECASE),
}
PROMO_SEGMENT_PATTERNS = {
    "price": PRICE_PATTERNS,
    "discount": PROMO_SIGNAL_PATTERNS,
    "date": [
        re.compile(
            r"\b(jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\b",
            re.IGNORECASE,
        ),
        re.compile(r"\b(valid|expires?|through|thru|until|ends?)\b", re.IGNORECASE),
    ],
}
GENERIC_SLOGAN_PATTERNS = [
    re.compile(r"\b(welcome to|our story|about us|patient care|confidence starts here)\b", re.IGNORECASE),
]
TESTIMONIAL_LANGUAGE_PATTERN = re.compile(
    r"\b(wonderful|professional|skillful|very satisfied|so happy|come back|thanks|thank you|gentle)\b",
    re.IGNORECASE,
)
MARKDOWN_NAV_KEYWORDS = {
    "skip to content",
    "open menu",
    "close menu",
    "about",
    "services",
    "treatments",
    "specials",
    "resources",
    "blog",
    "book a service",
    "folder:",
    "back",
    "shop",
}
MARKDOWN_FOOTER_PATTERN = re.compile(
    r"\b(navigate|get in touch|privacy policy|terms of use|copyright|brandwell|all rights reserved)\b",
    re.IGNORECASE,
)
MARKDOWN_OFFER_LINE_PATTERN = re.compile(
    r"(\$\s*\d+|\b\d+%\s*off\b|\b(spend|buy|get|save)\b|\bfree\b)",
    re.IGNORECASE,
)
MARKDOWN_OFFER_DETAIL_PATTERN = re.compile(
    r"(\$\s*\d+|\bvalue\b|\bwhile supplies last\b|\blimited\b|\bthrough\b|\buntil\b|\bmother[’']?s day\b)",
    re.IGNORECASE,
)
MARKDOWN_CTA_LINE_PATTERN = re.compile(
    r"\b(shop|purchase|book|learn more|apply)\b",
    re.IGNORECASE,
)
MARKDOWN_VALUE_ONLY_PATTERN = re.compile(r"^\$\s*\d+(?:\.\d{1,2})?\s+value\b", re.IGNORECASE)
MARKDOWN_PRICE_PATTERN = re.compile(r"\$\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?")
MARKDOWN_SECTION_HINT_PATTERN = re.compile(
    r"\b("
    r"lasers?|sofwave|ultraclear|resurfacing|miracle|pigment|vascular|hair removal|"
    r"injectables?|neurotoxins?|fillers?|prfm|hair restoration|body|contouring|thread lifts?|"
    r"skin treatments?|microneedling|prx|weight loss|peptides?|hormone therapy|skincare|pricing varies|"
    r"complimentary growth factors|pronox|no downtime|safe for all skin types|best for first visits|"
    r"masters laser repair"
    r")\b",
    re.IGNORECASE,
)
MARKDOWN_TOP_LEVEL_CATEGORIES = (
    "lasers & sofwave",
    "injectables",
    "thread lifts",
    "skin treatments",
    "medical weight loss",
    "peptides",
    "hormones",
    "skincare",
)
MARKDOWN_UI_LINE_PATTERN = re.compile(
    r"^\s*(home|about|services?|treatment finder|blog|promos?|gift cards?|products?|contact|"
    r"quick links|address|our socials|call us|message|book now|directions|privacy policy|"
    r"all rights reserved|copyrights?)\s*$",
    re.IGNORECASE,
)


def normalize_process_flag(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def is_filtered_process_flag(value: Optional[str]) -> bool:
    return normalize_process_flag(value) == "filtered"


def normalize_segment_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _normalize_markdown_text(text: str) -> str:
    lines: List[str] = []
    for raw_line in text.replace("\xa0", " ").splitlines():
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        lines.append(line)
    normalized = "\n".join(lines)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _clean_markdown_content(content: str) -> str:
    stripped = re.sub(r"`{1,3}", "", content)
    stripped = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", stripped)
    stripped = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", stripped)
    stripped = re.sub(r"<https?://[^>]+>", "", stripped)
    stripped = re.sub(r"https?://[^\s)]+", "", stripped)
    return _normalize_markdown_text(stripped)


def _is_markdown_ui_block(text: str) -> bool:
    normalized = _normalize_markdown_text(text)
    if not normalized:
        return True

    lower_text = normalized.casefold()
    if MARKDOWN_FOOTER_PATTERN.search(normalized):
        return True

    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    nav_hits = sum(1 for keyword in MARKDOWN_NAV_KEYWORDS if keyword in lower_text)
    short_line_hits = sum(1 for line in lines if len(line.split()) <= 4)

    if nav_hits >= 6:
        return True
    if nav_hits >= 4 and short_line_hits >= 6:
        return True

    return False


def _is_markdown_ui_line(text: str) -> bool:
    normalized = _normalize_offer_line_text(text)
    if not normalized:
        return True
    if MARKDOWN_FOOTER_PATTERN.search(normalized):
        return True
    if MARKDOWN_UI_LINE_PATTERN.search(normalized):
        return True
    if normalized in {"*", "-", "•", "|"}:
        return True
    return False


def _filter_markdown_lines(content: str) -> str:
    kept_lines: List[str] = []
    for raw_line in content.splitlines():
        candidate = raw_line.strip()
        if not candidate:
            continue
        normalized_candidate = _normalize_offer_line_text(candidate)
        if not normalized_candidate:
            continue
        if _is_markdown_ui_line(normalized_candidate):
            continue
        kept_lines.append(candidate)
    return _normalize_markdown_text("\n".join(kept_lines))


def _filter_markdown_blocks(content: str) -> str:
    cleaned = _clean_markdown_content(content)
    raw_blocks = [block for block in re.split(r"\n{2,}", cleaned) if block.strip()]
    use_line_fallback = len(raw_blocks) <= 2

    if use_line_fallback:
        return _filter_markdown_lines(cleaned)

    kept_blocks: List[str] = []
    for block in raw_blocks:
        candidate = block.strip()
        if not candidate:
            continue
        if _is_markdown_ui_block(candidate):
            continue
        kept_blocks.append(candidate)
    block_result = "\n\n".join(kept_blocks)
    # Some pages come as one giant mixed nav+content block; block-level filter can over-drop.
    if len(block_result) < 200 and MARKDOWN_PRICE_PATTERN.search(cleaned):
        return _filter_markdown_lines(cleaned)
    return block_result


def _dedupe_markdown_block_lines(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    seen: set[str] = set()
    deduped: List[str] = []

    for line in lines:
        key = re.sub(r"[*_`~>#-]+", " ", line)
        key = re.sub(r"\s+", " ", key).strip().casefold()
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        deduped.append(line)

    return _normalize_markdown_text("\n".join(deduped))


def _normalize_offer_line_text(text: str) -> str:
    cleaned = re.sub(r"[*_`~#>\-]+", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _is_heading_or_context_line(text: str) -> bool:
    if not text:
        return False
    if text.startswith("#"):
        return True
    normalized = _normalize_offer_line_text(text)
    if not normalized:
        return False
    if MARKDOWN_PRICE_PATTERN.search(normalized):
        return False
    if MARKDOWN_SECTION_HINT_PATTERN.search(normalized):
        lowered = normalized.casefold()
        category_hits = sum(1 for token in MARKDOWN_TOP_LEVEL_CATEGORIES if token in lowered)
        if category_hits >= 3:
            return False
        return True
    word_count = len(normalized.split())
    # short title-like lines, e.g. "Pigment Removal Laser", "Vascular Laser"
    if 2 <= word_count <= 8 and MARKDOWN_PRICE_PATTERN.search(normalized) is None:
        title_like = normalized == normalized.title() or normalized.upper() == normalized
        if title_like:
            return True
    return False


def _is_major_section_line(text: str) -> bool:
    lowered = _normalize_offer_line_text(text).casefold()
    if not lowered:
        return False
    return any(token in lowered for token in MARKDOWN_TOP_LEVEL_CATEGORIES)


def _is_offer_start_line(text: str) -> bool:
    if not text:
        return False
    if MARKDOWN_VALUE_ONLY_PATTERN.search(text):
        return False
    if MARKDOWN_CTA_LINE_PATTERN.fullmatch(text):
        return False
    if not (
        MARKDOWN_OFFER_LINE_PATTERN.search(text)
        or MARKDOWN_PRICE_PATTERN.search(text)
        or "pricing varies" in text.casefold()
    ):
        return False
    alpha_count = sum(1 for ch in text if ch.isalpha())
    return alpha_count >= 8


def _is_offer_detail_line(text: str) -> bool:
    if not text or text.startswith("#"):
        return False
    if MARKDOWN_OFFER_DETAIL_PATTERN.search(text):
        return True
    if MARKDOWN_CTA_LINE_PATTERN.search(text):
        return True
    return False


def _is_service_title_candidate_line(text: str) -> bool:
    normalized = _normalize_offer_line_text(text)
    if not normalized:
        return False
    if MARKDOWN_PRICE_PATTERN.search(normalized):
        return False
    if MARKDOWN_CTA_LINE_PATTERN.fullmatch(normalized):
        return False
    if _is_major_section_line(normalized):
        return False
    if _is_markdown_ui_line(normalized):
        return False
    if _is_heading_or_context_line(normalized):
        word_count = len(normalized.split())
        alpha_count = sum(1 for ch in normalized if ch.isalpha())
        return 2 <= word_count <= 8 and alpha_count >= 6
    return False


def _has_nearby_price_line(lines: List[str], start_index: int, lookahead: int = 3) -> bool:
    checked = 0
    idx = start_index + 1
    while idx < len(lines) and checked < lookahead:
        candidate = _normalize_offer_line_text(lines[idx])
        idx += 1
        if not candidate:
            continue
        checked += 1
        if MARKDOWN_PRICE_PATTERN.search(candidate):
            return True
        if _is_major_section_line(candidate):
            return False
    return False


def _extract_price_anchored_offer_segments(lines: List[str]) -> List[str]:
    offers: List[str] = []
    seen: set[str] = set()

    for idx, raw_line in enumerate(lines):
        line = _normalize_offer_line_text(raw_line)
        if not line or not MARKDOWN_PRICE_PATTERN.search(line):
            continue

        title_line = ""
        description_line = ""

        for back in range(1, 5):
            prev_idx = idx - back
            if prev_idx < 0:
                break
            prev = _normalize_offer_line_text(lines[prev_idx])
            if not prev:
                continue
            if MARKDOWN_PRICE_PATTERN.search(prev):
                break
            if _is_major_section_line(prev):
                break
            if _is_service_title_candidate_line(prev):
                title_line = prev
                for desc_idx in range(prev_idx + 1, idx):
                    desc = _normalize_offer_line_text(lines[desc_idx])
                    if not desc:
                        continue
                    if _is_markdown_ui_line(desc):
                        continue
                    if MARKDOWN_CTA_LINE_PATTERN.search(desc):
                        continue
                    if MARKDOWN_PRICE_PATTERN.search(desc):
                        continue
                    if _is_service_title_candidate_line(desc):
                        continue
                    if len(desc.split()) >= 6:
                        description_line = desc
                        break
                break

        if not title_line:
            continue

        offer_lines = [title_line]
        if description_line:
            offer_lines.append(description_line)
        offer_lines.append(line)
        offer_text = _dedupe_markdown_block_lines(_normalize_markdown_text("\n".join(offer_lines)))
        offer_key = normalize_segment_text(offer_text).casefold()
        if not offer_key or offer_key in seen:
            continue
        seen.add(offer_key)
        offers.append(offer_text)

    return offers


def _extract_offer_segments_from_markdown(content: str) -> List[str]:
    filtered = _filter_markdown_blocks(content)
    lines = [line.strip() for line in filtered.splitlines()]
    anchored_offers = _extract_price_anchored_offer_segments(lines)
    if len(anchored_offers) >= 3:
        return anchored_offers

    offers: List[str] = []
    seen: set[str] = set()
    context_lines: List[str] = []
    idx = 0

    while idx < len(lines):
        line = lines[idx]
        idx += 1
        if not line:
            continue

        normalized_line = _normalize_offer_line_text(line)
        if not normalized_line:
            continue
        is_offer_start = _is_offer_start_line(normalized_line)
        if not is_offer_start and _is_service_title_candidate_line(normalized_line):
            # Position-based recovery:
            # treat short service-title lines as offer starts when a nearby price line follows.
            is_offer_start = _has_nearby_price_line(lines, idx - 1, lookahead=3)

        if not is_offer_start:
            if _is_heading_or_context_line(line):
                if _is_major_section_line(line):
                    context_lines = [normalized_line]
                else:
                    context_lines.append(normalized_line)
                context_lines = context_lines[-4:]
            continue

        offer_lines: List[str] = [*context_lines, normalized_line]
        cta_added = False
        look_ahead = idx
        while look_ahead < len(lines):
            candidate = lines[look_ahead].strip()
            if not candidate:
                break
            normalized_candidate = _normalize_offer_line_text(candidate)
            if not normalized_candidate:
                look_ahead += 1
                continue

            if _is_major_section_line(candidate):
                candidate_context = _normalize_offer_line_text(candidate)
                context_lines = [candidate_context]
                context_lines = context_lines[-4:]
                break

            if _is_offer_start_line(normalized_candidate):
                break
            if _is_service_title_candidate_line(normalized_candidate):
                context_lines.append(normalized_candidate)
                context_lines = context_lines[-4:]
                break

            if _is_offer_detail_line(normalized_candidate):
                offer_lines.append(normalized_candidate)
                if MARKDOWN_CTA_LINE_PATTERN.search(normalized_candidate):
                    cta_added = True
            look_ahead += 1

        idx = max(idx, look_ahead)
        offer_text = _dedupe_markdown_block_lines(_normalize_markdown_text("\n".join(offer_lines)))
        offer_key = normalize_segment_text(offer_text).casefold()
        if not offer_key or offer_key in seen:
            continue
        seen.add(offer_key)
        offers.append(offer_text)

    return offers


def clean_page_text(content: str, source_type: str = "html") -> str:
    if source_type == "markdown":
        filtered = _filter_markdown_blocks(content)
        deduped_blocks: List[str] = []
        for block in re.split(r"\n{2,}", filtered):
            candidate = _dedupe_markdown_block_lines(block)
            if candidate:
                deduped_blocks.append(candidate)
        return "\n\n".join(deduped_blocks)

    soup = BeautifulSoup(content, "lxml")
    for tag in soup(SEGMENT_SKIP_TAGS):
        tag.decompose()
    text = " ".join(s.strip() for s in soup.stripped_strings if s.strip())
    return normalize_segment_text(text)


def extract_page_segments(content: str, source_type: str = "html") -> List[Dict[str, Any]]:
    if source_type == "markdown":
        offer_segments = _extract_offer_segments_from_markdown(content)
        if offer_segments:
            return [
                {
                    "index": idx,
                    "tag": "markdown_offer",
                    "text": text,
                    "text_length": len(text),
                }
                for idx, text in enumerate(offer_segments)
            ]

        cleaned_markdown = _filter_markdown_blocks(content)
        segments: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for idx, block in enumerate(re.split(r"\n{2,}", cleaned_markdown)):
            text = _dedupe_markdown_block_lines(block)
            if len(normalize_segment_text(text)) < 8:
                continue
            if _is_markdown_ui_block(text):
                continue
            normalized = normalize_segment_text(text).casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            segments.append(
                {
                    "index": idx,
                    "tag": "markdown_block",
                    "text": text,
                    "text_length": len(text),
                }
            )
        return segments

    soup = BeautifulSoup(content, "lxml")
    for tag in soup(SEGMENT_SKIP_TAGS):
        tag.decompose()

    root = soup.body or soup
    segments: List[Dict[str, Any]] = []
    seen_texts: set[str] = set()

    for idx, node in enumerate(root.find_all(SEGMENT_CONTAINER_TAGS)):
        if not getattr(node, "get_text", None):
            continue
        text = normalize_segment_text(node.get_text(" ", strip=True))
        if len(text) < 8:
            continue
        normalized = text.casefold()
        if normalized in seen_texts:
            continue
        seen_texts.add(normalized)
        segments.append(
            {
                "index": idx,
                "tag": node.name,
                "text": text,
                "text_length": len(text),
            }
        )

    if not segments:
        raw_text = clean_page_text(content, source_type=source_type)
        if raw_text:
            segments.append(
                {
                    "index": 0,
                    "tag": "document",
                    "text": raw_text,
                    "text_length": len(raw_text),
                }
            )
    return segments


def score_page_segment(text: str) -> Dict[str, Any]:
    normalized = normalize_segment_text(text)
    lower_text = normalized.casefold()
    word_count = len(normalized.split())
    flags: List[str] = []
    score = 0

    price_hits = sum(1 for pattern in PROMO_SEGMENT_PATTERNS["price"] if pattern.search(normalized))
    discount_hits = sum(1 for pattern in PROMO_SEGMENT_PATTERNS["discount"] if pattern.search(lower_text))
    date_hits = sum(1 for pattern in PROMO_SEGMENT_PATTERNS["date"] if pattern.search(normalized))
    service_hits = sum(1 for keyword in SERVICE_SIGNAL_KEYWORDS if keyword in lower_text)
    strong_hits = sum(1 for keyword in STRONG_SIGNAL_KEYWORDS if keyword in lower_text)

    score += price_hits * 5
    score += discount_hits * 4
    score += date_hits * 2
    score += service_hits * 2
    score += strong_hits * 3

    noise_hits = 0
    for label, pattern in NOISE_SEGMENT_PATTERNS.items():
        if pattern.search(normalized):
            flags.append(f"noise:{label}")
            noise_hits += 1
    score -= noise_hits * 4

    if any(pattern.search(normalized) for pattern in GENERIC_SLOGAN_PATTERNS):
        flags.append("generic:slogan")
        score -= 3

    if TESTIMONIAL_LANGUAGE_PATTERN.search(normalized) and not (service_hits or strong_hits or discount_hits):
        flags.append("noise:testimonial")
        score -= 6

    if word_count <= 4 and not price_hits:
        flags.append("drop:short_fragment")
        score -= 6

    if len(normalized) > 1200:
        flags.append("shape:long_block")
        score += 1

    if "noise:review" in flags and not (service_hits or strong_hits or discount_hits):
        flags.append("drop:review_only")
        score -= 8

    if "noise:testimonial" in flags and not (service_hits or strong_hits or discount_hits):
        flags.append("drop:testimonial_only")
        score -= 8

    if any(marker in flags for marker in {"noise:cta", "noise:account", "noise:commerce"}) and not (
        service_hits or strong_hits or discount_hits
    ):
        flags.append("drop:action_only")
        score -= 8

    if not (price_hits or discount_hits or service_hits or strong_hits) and noise_hits:
        flags.append("drop:noise_only")
        score -= 8

    keep = score > 0 and not any(
        flag in flags
        for flag in {
            "drop:noise_only",
            "drop:short_fragment",
            "drop:review_only",
            "drop:action_only",
            "drop:testimonial_only",
        }
    )
    return {
        "score": score,
        "keep": keep,
        "flags": flags,
        "signals": {
            "price_hits": price_hits,
            "discount_hits": discount_hits,
            "date_hits": date_hits,
            "service_hits": service_hits,
            "strong_hits": strong_hits,
            "noise_hits": noise_hits,
            "word_count": word_count,
        },
    }


def filter_page_segments(raw_segments: Iterable[Dict[str, Any]], max_segments: int = 18) -> tuple[List[Dict[str, Any]], List[str]]:
    scored_segments: List[Dict[str, Any]] = []
    content_quality_flags: List[str] = []

    for segment in raw_segments:
        if str(segment.get("tag", "")).strip() == "markdown_offer":
            scored_segments.append(
                {
                    **segment,
                    "score": 100,
                    "keep": True,
                    "flags": [],
                    "signals": {},
                }
            )
            continue
        scored = score_page_segment(segment["text"])
        candidate = {**segment, **scored}
        if candidate["keep"]:
            scored_segments.append(candidate)
        else:
            content_quality_flags.extend(candidate["flags"])

    scored_segments.sort(key=lambda item: (-item["score"], -item["text_length"], item["index"]))
    kept_segments: List[Dict[str, Any]] = []
    kept_norms: List[str] = []

    for segment in scored_segments:
        normalized = segment["text"].casefold()
        if any(normalized == existing for existing in kept_norms):
            content_quality_flags.append("drop:exact_duplicate")
            continue
        if any(
            len(normalized) >= 8
            and normalized in existing
            and segment["score"] <= kept_segments[idx]["score"] + 2
            for idx, existing in enumerate(kept_norms)
        ):
            content_quality_flags.append("drop:contained_by_parent")
            continue
        kept_segments.append(segment)
        kept_norms.append(normalized)

    kept_segments.sort(key=lambda item: item["index"])
    has_markdown_offer = any(str(item.get("tag", "")).strip() == "markdown_offer" for item in kept_segments)
    if len(kept_segments) > max_segments and not has_markdown_offer:
        content_quality_flags.append(f"trim:top_{max_segments}_segments")
        kept_segments = sorted(kept_segments, key=lambda item: (-item["score"], item["index"]))[:max_segments]
        kept_segments.sort(key=lambda item: item["index"])

    return kept_segments, sorted(set(content_quality_flags))


def build_llm_ready_content(filtered_segments: Iterable[Dict[str, Any]], max_chars: int = 6000) -> str:
    chunks: List[str] = []
    total_chars = 0
    for output_index, segment in enumerate(filtered_segments):
        chunk = f"[SEGMENT {output_index}]\n{segment['text']}"
        if total_chars and total_chars + len(chunk) + 5 > max_chars:
            break
        chunks.append(chunk)
        total_chars += len(chunk) + 5
    return "\n\n".join(chunks)


def prepare_page_content(content: str, source_type: str = "html") -> Dict[str, Any]:
    raw_text = clean_page_text(content, source_type=source_type)
    raw_segments = extract_page_segments(content, source_type=source_type)
    filtered_segments, content_quality_flags = filter_page_segments(raw_segments)
    has_markdown_offer = any(str(item.get("tag", "")).strip() == "markdown_offer" for item in filtered_segments)
    llm_max_chars = 30000 if has_markdown_offer else 6000
    page_content_llm = build_llm_ready_content(filtered_segments, max_chars=llm_max_chars)
    if not page_content_llm and raw_text:
        page_content_llm = raw_text[:6000]
        content_quality_flags = sorted(set(content_quality_flags + ["fallback:raw_page_content"]))
    return {
        "page_content": raw_text,
        "page_segments_raw": raw_segments,
        "page_segments_filtered": filtered_segments,
        "page_content_llm": page_content_llm,
        "content_quality_flags": content_quality_flags,
    }
