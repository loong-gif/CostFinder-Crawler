"""
站内价格/促销页发现爬虫
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qsl, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from crawler.jina_reader_client import JinaReaderClient
from utils.logger import log

CANDIDATE_KEYWORD_WEIGHTS = {
    "pricing": 5,
    "price": 5,
    "membership": 5,
    "memberships": 5,
    "specials": 4,
    "special": 4,
    "promotions": 4,
    "promotion": 4,
    "promo": 4,
    "offers": 4,
    "offer": 4,
    "deals": 4,
    "deal": 4,
    "services": 3,
    "service": 3,
    "booking": 2,
    "book": 2,
    "injectables": 3,
    "botox": 3,
    "filler": 3,
}
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
NEGATIVE_KEYWORDS = {
    "about",
    "contact",
    "blog",
    "privacy",
    "terms",
    "career",
    "careers",
    "job",
    "jobs",
    "news",
    "article",
    "press",
    "login",
    "signin",
    "sign-in",
    "account",
    "cart",
    "checkout",
    "faq",
}
TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "srsltid",
    "wgu",
    "wgexpiry",
    "clickid",
}
COMMON_DISCOVERY_PATHS = [
    "/pricing",
    "/price-list",
    "/membership",
    "/memberships",
    "/specials",
    "/promotions",
    "/offers",
    "/services",
    "/service-menu",
    "/book",
    "/booking",
    "/injectables",
    "/botox",
    "/filler",
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


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class SiteTarget:
    master_id: Optional[int]
    business_id: Optional[int]
    name: str
    website: str
    website_clean: str
    process_flag: str
    domain_name: str


@dataclass(frozen=True)
class CandidateLink:
    url: str
    score: int
    source: str
    anchor_text: str = ""


@dataclass
class CrawlStats:
    target_sites: int = 0
    successful_sites: int = 0
    failed_sites: int = 0
    zero_hit_sites: int = 0
    hit_pages: int = 0
    page_failures: int = 0
    skipped_missing_domain: int = 0


def normalize_process_flag(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def is_filtered_process_flag(value: Optional[str]) -> bool:
    return normalize_process_flag(value) == "filtered"


def normalize_domain(value: Optional[str]) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""

    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(candidate)
    host = parsed.netloc or parsed.path.split("/")[0]
    host = host.split("@")[-1].split(":")[0].strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host.strip("/")


def build_start_url(site: SiteTarget) -> str:
    website = (site.website or "").strip()
    if website.startswith("http://") or website.startswith("https://"):
        return website
    if website:
        return f"https://{website.lstrip('/')}"
    if site.domain_name:
        return f"https://{site.domain_name}"
    return ""


def clean_url_for_dedupe(url: str) -> str:
    parsed = urlparse(url.strip())
    filtered_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS
    ]
    clean = parsed._replace(
        fragment="",
        query="&".join(f"{key}={value}" if value else key for key, value in filtered_query),
    )
    return urlunparse(clean)


def is_same_site_domain(candidate_url: str, target_domain: str) -> bool:
    candidate_domain = normalize_domain(candidate_url)
    if not candidate_domain or not target_domain:
        return False
    return (
        candidate_domain == target_domain
        or candidate_domain.endswith(f".{target_domain}")
        or target_domain.endswith(f".{candidate_domain}")
    )


def score_candidate_link(url: str, anchor_text: str = "") -> int:
    haystack = f"{url} {anchor_text}".lower()
    score = 0
    for keyword, weight in CANDIDATE_KEYWORD_WEIGHTS.items():
        if keyword in haystack:
            score += weight
    return score


def should_exclude_candidate(url: str, anchor_text: str = "") -> bool:
    haystack = f"{url} {anchor_text}".lower()
    return any(keyword in haystack for keyword in NEGATIVE_KEYWORDS)


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


def analyze_page_content(url: str, title: str, text: str, candidate_score: int = 0) -> Dict[str, Any]:
    haystack = f"{url} {title} {text}".lower()
    has_price = any(pattern.search(text) for pattern in PRICE_PATTERNS)
    has_promo = any(pattern.search(haystack) for pattern in PROMO_SIGNAL_PATTERNS)
    strong_signal = any(keyword in haystack for keyword in STRONG_SIGNAL_KEYWORDS)
    service_signal = any(keyword in haystack for keyword in SERVICE_SIGNAL_KEYWORDS)
    should_export = (candidate_score > 0 or strong_signal or service_signal) and (has_price or has_promo)
    return {
        "has_price": has_price,
        "has_promo": has_promo,
        "strong_signal": strong_signal,
        "service_signal": service_signal,
        "candidate_score": candidate_score,
        "should_export": should_export,
    }


def build_export_row(
    site: SiteTarget,
    subpage_url: str,
    page_content: str,
    *,
    page_segments_raw: Optional[Iterable[Dict[str, Any]]] = None,
    page_segments_filtered: Optional[Iterable[Dict[str, Any]]] = None,
    page_content_llm: str = "",
    content_quality_flags: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    return {
        "promo_website_id": "",
        "crawl_timestamp": datetime.now(timezone.utc).isoformat(),
        "subpage_url": subpage_url,
        "page_content": page_content,
        "page_segments_raw": _compact_json(list(page_segments_raw or [])),
        "page_segments_filtered": _compact_json(list(page_segments_filtered or [])),
        "page_content_llm": page_content_llm,
        "content_quality_flags": _compact_json(list(content_quality_flags or [])),
        "domain_name": site.domain_name,
        "processed_status": "false",
        "name": site.name,
    }


def build_target_sites(
    master_rows: Iterable[Dict[str, Any]],
    promo_domains: Iterable[str],
) -> List[SiteTarget]:
    existing_domains = {normalize_domain(value) for value in promo_domains if normalize_domain(value)}
    targets: List[SiteTarget] = []

    for row in master_rows:
        if is_filtered_process_flag(row.get("process_flag")):
            continue

        domain_name = normalize_domain(row.get("website_clean") or row.get("website"))
        if not domain_name or domain_name in existing_domains:
            continue

        targets.append(
            SiteTarget(
                master_id=row.get("id"),
                business_id=row.get("business_id"),
                name=(row.get("name") or "").strip(),
                website=(row.get("website") or "").strip(),
                website_clean=(row.get("website_clean") or "").strip(),
                process_flag=(row.get("process_flag") or "").strip(),
                domain_name=domain_name,
            )
        )

    targets.sort(key=lambda item: (item.master_id is None, item.master_id or 0, item.domain_name))
    return targets


class PromoSiteCrawler:
    """站内发现价格/促销页的 Jina Reader 爬虫"""

    def __init__(
        self,
        *,
        headless: Optional[bool] = None,
        concurrency: int = 3,
        max_candidate_pages: int = 12,
    ):
        self._headless = headless
        self.reader_client = JinaReaderClient()
        self.semaphore = asyncio.Semaphore(concurrency)
        self.max_candidate_pages = max_candidate_pages

    async def start(self):
        if self._headless is not None:
            log.info("Jina Reader 模式下已忽略 headless 参数")

    async def close(self):
        return None

    async def crawl_sites(self, sites: List[SiteTarget]) -> tuple[List[Dict[str, Any]], CrawlStats]:
        stats = CrawlStats(target_sites=len(sites))
        all_hits: List[Dict[str, Any]] = []

        async def worker(site: SiteTarget):
            async with self.semaphore:
                return await self.crawl_site(site)

        results = await asyncio.gather(*(worker(site) for site in sites), return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                stats.failed_sites += 1
                log.error(f"站点任务异常: {result}")
                continue

            site_hits, site_stats = result
            all_hits.extend(site_hits)
            stats.successful_sites += int(site_stats["site_success"])
            stats.failed_sites += int(site_stats["site_failed"])
            stats.zero_hit_sites += int(site_stats["zero_hit"])
            stats.hit_pages += site_stats["hit_pages"]
            stats.page_failures += site_stats["page_failures"]

        return all_hits, stats

    async def crawl_site(self, site: SiteTarget) -> tuple[List[Dict[str, Any]], Dict[str, int]]:
        start_url = build_start_url(site)
        if not start_url:
            log.warning(f"站点缺少可用入口URL: {site.domain_name}")
            return [], {
                "site_success": 0,
                "site_failed": 1,
                "zero_hit": 0,
                "hit_pages": 0,
                "page_failures": 0,
            }

        log.info(f"开始站点发现: {site.domain_name} -> {start_url}")
        visited_urls: set[str] = set()
        queued_urls: set[str] = set()
        exported_urls: set[str] = set()
        hits: List[Dict[str, Any]] = []
        page_failures = 0
        site_failed = 0

        queue: List[CandidateLink] = [CandidateLink(url=start_url, score=0, source="entry")]
        queued_urls.add(clean_url_for_dedupe(start_url))

        for guessed_url in self._build_guessed_candidates(site.domain_name):
            dedupe = clean_url_for_dedupe(guessed_url)
            if dedupe in queued_urls:
                continue
            queued_urls.add(dedupe)
            queue.append(
                CandidateLink(
                    url=guessed_url,
                    score=score_candidate_link(guessed_url),
                    source="guessed",
                )
            )

        while queue and len(visited_urls) < self.max_candidate_pages:
            queue.sort(key=lambda item: (-item.score, len(item.url), item.url))
            candidate = queue.pop(0)
            dedupe_url = clean_url_for_dedupe(candidate.url)
            if dedupe_url in visited_urls:
                continue
            visited_urls.add(dedupe_url)

            try:
                page_data = await self._fetch_page(candidate.url)
            except Exception as exc:
                page_failures += 1
                if candidate.source == "entry":
                    site_failed = 1
                    log.error(f"站点入口失败: {candidate.url} - {exc}")
                    break
                log.warning(f"页面抓取失败: {candidate.url} - {exc}")
                continue

            final_url = clean_url_for_dedupe(page_data["final_url"])
            if not is_same_site_domain(final_url, site.domain_name):
                message = f"跳过出站页面: {candidate.url} -> {final_url}"
                if candidate.source == "entry":
                    site_failed = 1
                    log.warning(message)
                    break
                log.debug(message)
                continue

            signals = analyze_page_content(
                final_url,
                page_data["title"],
                page_data["page_content_llm"] or page_data["page_content"],
                candidate.score,
            )
            if signals["should_export"] and final_url not in exported_urls and not should_exclude_candidate(final_url, page_data["title"]):
                hits.append(
                    build_export_row(
                        site,
                        final_url,
                        page_data["page_content"],
                        page_segments_raw=page_data["page_segments_raw"],
                        page_segments_filtered=page_data["page_segments_filtered"],
                        page_content_llm=page_data["page_content_llm"],
                        content_quality_flags=page_data["content_quality_flags"],
                    )
                )
                exported_urls.add(final_url)
                log.info(f"命中价格/促销页: {final_url}")

            if candidate.source == "entry" or candidate.score >= 2:
                new_candidates = self._discover_candidates(
                    base_url=final_url,
                    target_domain=site.domain_name,
                    link_items=page_data["links"],
                )
                for item in new_candidates:
                    item_dedupe = clean_url_for_dedupe(item.url)
                    if item_dedupe in queued_urls or item_dedupe in visited_urls:
                        continue
                    queued_urls.add(item_dedupe)
                    queue.append(item)

        if site_failed:
            return [], {
                "site_success": 0,
                "site_failed": 1,
                "zero_hit": 0,
                "hit_pages": 0,
                "page_failures": page_failures,
            }

        zero_hit = int(not hits)
        if zero_hit:
            log.info(f"站点未发现价格/促销页: {site.domain_name}")

        return hits, {
            "site_success": 1,
            "site_failed": 0,
            "zero_hit": zero_hit,
            "hit_pages": len(hits),
            "page_failures": page_failures,
        }

    async def _fetch_page(self, url: str) -> Dict[str, Any]:
        page = await self.reader_client.fetch(url)
        return {
            "final_url": page.final_url,
            "title": page.title,
            **prepare_page_content(page.content, source_type="markdown"),
            "links": page.links,
        }

    def _build_guessed_candidates(self, domain_name: str) -> List[str]:
        return [f"https://{domain_name}{path}" for path in COMMON_DISCOVERY_PATHS]

    def _discover_candidates(
        self,
        *,
        base_url: str,
        target_domain: str,
        link_items: Iterable[Dict[str, Any]],
    ) -> List[CandidateLink]:
        candidates: Dict[str, CandidateLink] = {}

        for item in link_items:
            href = (item.get("href") or "").strip()
            text = (item.get("text") or "").strip()
            if not href:
                continue

            absolute_url = clean_url_for_dedupe(urljoin(base_url, href))
            if not absolute_url.startswith(("http://", "https://")):
                continue
            if not is_same_site_domain(absolute_url, target_domain):
                continue
            if should_exclude_candidate(absolute_url, text):
                continue

            score = score_candidate_link(absolute_url, text)
            if score <= 0:
                continue

            current = candidates.get(absolute_url)
            if current is None or score > current.score:
                candidates[absolute_url] = CandidateLink(
                    url=absolute_url,
                    score=score,
                    source="discovered",
                    anchor_text=text,
                )

        return list(candidates.values())
