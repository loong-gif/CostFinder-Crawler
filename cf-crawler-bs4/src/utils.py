# src/utils.py

from __future__ import annotations

import io
import json
import hashlib
import re
import shutil
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qsl, urlparse, urlunparse
from xml.etree import ElementTree

import httpx
from PIL import Image, ImageOps

try:
    import pytesseract
except Exception:  # pragma: no cover - optional runtime dependency
    pytesseract = None

try:
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover - optional runtime dependency
    async_playwright = None

TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "srsltid",
}
SITEMAP_CANDIDATES = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/wp-sitemap.xml",
]
SEGMENT_CONTAINER_TAGS = {"main", "section", "article", "div", "li", "tr", "p"}
REMOVE_SELECTOR = "script, style, noscript, svg, nav, footer, header, form, button"
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
    "treatment",
    "treatments",
    "membership",
    "package",
    "packages",
}
STRONG_SIGNAL_KEYWORDS = {
    "pricing",
    "price",
    "special",
    "specials",
    "promotion",
    "promotions",
    "promo",
    "offer",
    "offers",
    "discount",
    "deal",
    "deals",
    "membership",
    "memberships",
}
URL_KEYWORD_WEIGHTS = {
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
    "discount": 4,
    "deals": 3,
    "deal": 3,
    "services": 3,
    "service": 3,
    "treatment": 3,
    "treatments": 3,
    "package": 3,
    "packages": 3,
    "botox": 3,
    "filler": 3,
    "fillers": 3,
}
URL_NEGATIVE_KEYWORDS = {
    "login",
    "sign-in",
    "signin",
    "account",
    "cart",
    "checkout",
    "privacy",
    "terms",
    "blog",
    "news",
    "career",
    "careers",
    "before-and-after",
    "gallery",
}
URL_ALLOWLIST_KEYWORDS = {
    "monthly-specials",
    "specials",
    "special",
    "pricing",
    "price",
    "membership",
    "memberships",
    "offers",
    "offer",
    "services",
    "service",
    "packages",
    "package",
    "promotions",
    "promotion",
    "promo",
}
URL_HARD_EXCLUDE_PATTERNS = [
    re.compile(r"/blogs?/", re.IGNORECASE),
    re.compile(r"/learn(?:/|$)", re.IGNORECASE),
    re.compile(r"/news(?:/|$)", re.IGNORECASE),
    re.compile(r"/article(?:s)?(?:/|$)", re.IGNORECASE),
    re.compile(r"/about(?:-us)?(?:/|$)", re.IGNORECASE),
    re.compile(r"/contact(?:-us)?(?:/|$)", re.IGNORECASE),
    re.compile(r"/polic(?:y|ies)(?:/|$)", re.IGNORECASE),
    re.compile(r"/what-(?:is|are)-", re.IGNORECASE),
    re.compile(r"/(?:\d+-)?reasons?-to-", re.IGNORECASE),
    re.compile(r"/benefits?-of-", re.IGNORECASE),
    re.compile(r"/.+-vs-.+", re.IGNORECASE),
    re.compile(r"/offering-the-best-.+-in-[a-z0-9-]+/?$", re.IGNORECASE),
]
PRICE_PATTERNS = [
    re.compile(r"\$\s*\d+(?:,\d{3})*(?:\.\d{2})?"),
    re.compile(r"\bUSD\s*\d+(?:,\d{3})*(?:\.\d{2})?\b", re.IGNORECASE),
]
PROMO_PATTERNS = [
    re.compile(r"\b\d{1,3}%\s+off\b", re.IGNORECASE),
    re.compile(r"\bsave\s+\$?\d+", re.IGNORECASE),
    re.compile(r"\blimited[-\s]?time\b", re.IGNORECASE),
    re.compile(r"\bspecial offer\b", re.IGNORECASE),
    re.compile(r"\bmonthly\b", re.IGNORECASE),
    re.compile(r"\bmember(ship)?\b", re.IGNORECASE),
]
DATE_PATTERNS = [
    re.compile(
        r"\b(jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(valid|expires?|through|thru|until|ends?)\b", re.IGNORECASE),
]
NOISE_SEGMENT_PATTERNS = {
    "commerce": re.compile(r"\b(cart|checkout|shop now|view product|add to cart|buy now)\b", re.IGNORECASE),
    "account": re.compile(r"\b(login|log in|sign in|sign up|my account)\b", re.IGNORECASE),
    "cta": re.compile(r"\b(book now|book online|schedule now|learn more|read more|get started|call now)\b", re.IGNORECASE),
    "review": re.compile(r"\b(review|reviews|testimonial|testimonials|star\s+star)\b", re.IGNORECASE),
    "social": re.compile(r"\b(facebook|instagram|tiktok|youtube|follow us)\b", re.IGNORECASE),
}
GENERIC_SLOGAN_PATTERNS = [
    re.compile(r"\b(welcome to|our story|about us|patient care|confidence starts here)\b", re.IGNORECASE),
]
TESTIMONIAL_LANGUAGE_PATTERN = re.compile(
    r"\b(wonderful|professional|skillful|very satisfied|so happy|come back|thanks|thank you|gentle)\b",
    re.IGNORECASE,
)
UI_NOISE_PATTERNS = {
    "nav": re.compile(
        r"\b(skip to content|open menu|close menu|main menu|menu)\b",
        re.IGNORECASE,
    ),
    "ui": re.compile(
        r"\b(filter availability|sort by|quick view|\d+\s+selected)\b",
        re.IGNORECASE,
    ),
    "cart": re.compile(
        r"\b(your cart is currently empty|subtotal|check out|checkout|add to cart|item added to your cart)\b",
        re.IGNORECASE,
    ),
    "legal": re.compile(
        r"\b(privacy policy|cookie policy|terms(?:\s*&\s*conditions)?)\b",
        re.IGNORECASE,
    ),
    "form": re.compile(
        r"\b(first name|last name|email|phone|zip code|postal code)\b",
        re.IGNORECASE,
    ),
    "footer": re.compile(
        r"(©\s*\d{4}|site map|web accessibility|hipaa)",
        re.IGNORECASE,
    ),
}
CTA_PHRASE_PATTERN = re.compile(
    r"\b(book now|learn more|shop now|claim this offer|call now|get started|book online|schedule now|read more)\b",
    re.IGNORECASE,
)
PRICE_PATTERN_SIMPLE = re.compile(r"\$\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?")
HIDDEN_CLASS_TOKENS = {
    "hidden",
    "d-none",
    "is-hidden",
    "u-hidden",
    "visually-hidden",
    "sr-only",
}
HIDDEN_STYLE_PATTERNS = [
    re.compile(r"display\s*:\s*none", re.IGNORECASE),
    re.compile(r"visibility\s*:\s*hidden", re.IGNORECASE),
]
MIN_EXPORT_TOTAL_CHARS = 200
JACCARD_DUPLICATE_THRESHOLD = 0.72


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def normalize_url(url: str) -> str:
    if not url:
        return ""
    raw = url.strip()
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS
    ]
    normalized = parsed._replace(
        scheme=parsed.scheme or "https",
        fragment="",
        query="&".join(f"{key}={value}" if value else key for key, value in query),
    )
    return urlunparse(normalized).rstrip("/") or raw.rstrip("/")


def normalize_domain(value: str) -> str:
    normalized = normalize_url(value)
    if not normalized:
        return ""
    host = urlparse(normalized).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def is_root_url(url: str) -> bool:
    parsed = urlparse(normalize_url(url))
    return parsed.path in ("", "/")


def is_same_domain(candidate_url: str, target_domain: str) -> bool:
    candidate_domain = normalize_domain(candidate_url)
    return bool(candidate_domain and target_domain and candidate_domain == target_domain)


def should_skip_url(url: str) -> bool:
    normalized = normalize_url(url)
    if not normalized:
        return True
    lower_url = normalized.casefold()
    if lower_url.endswith((".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp")):
        return True
    if "test-" in lower_url or "/test" in lower_url:
        return True
    parsed = urlparse(normalized)
    slug_tokens = [token for token in re.split(r"[-_/]+", parsed.path.strip("/").casefold()) if token]
    if slug_tokens:
        has_allowlisted_intent = any(keyword in lower_url for keyword in URL_ALLOWLIST_KEYWORDS)
        if len(slug_tokens) >= 5 and not has_allowlisted_intent:
            return True
    return any(pattern.search(normalized) for pattern in URL_HARD_EXCLUDE_PATTERNS)


def score_candidate_url(url: str) -> int:
    normalized = normalize_url(url)
    if not normalized:
        return -100
    if should_skip_url(normalized):
        return -100
    lower_url = normalized.casefold()
    parsed = urlparse(normalized)
    score = 0

    if parsed.path in ("", "/"):
        score += 2

    for keyword, weight in URL_KEYWORD_WEIGHTS.items():
        if keyword in lower_url:
            score += weight

    for keyword in URL_NEGATIVE_KEYWORDS:
        if keyword in lower_url:
            score -= 6

    if parsed.query:
        score -= 2

    return score


def filter_urls_by_inclusion(urls: list[str]) -> list[str]:
    """
    Legacy helper name kept for compatibility.
    New behavior: normalize, dedupe, and rank candidate URLs instead of removing parent paths.
    """
    ranked = sorted(
        {normalize_url(url) for url in urls if normalize_url(url) and not should_skip_url(url)},
        key=lambda item: (-score_candidate_url(item), len(urlparse(item).path), item),
    )
    return ranked


def _parse_sitemap_xml(content: bytes) -> list[str]:
    urls: list[str] = []
    try:
        root = ElementTree.fromstring(content)
        for child in root.iter():
            if child.tag.endswith("loc") and child.text:
                urls.append(child.text.strip())
    except ElementTree.ParseError:
        return []
    return urls


async def fetch_sitemap_urls(
    domain: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    max_depth: int = 2,
    max_urls: int = 250,
) -> list[str]:
    base_url = normalize_url(domain)
    if not base_url:
        return []

    own_client = client is None
    http_client = client or httpx.AsyncClient(timeout=10.0, follow_redirects=True)
    target_domain = normalize_domain(base_url)
    sitemap_queue = [(f"{base_url}{path}", 0) for path in SITEMAP_CANDIDATES]
    seen_sitemaps = set()
    found_urls = set()

    try:
        while sitemap_queue and len(found_urls) < max_urls:
            sitemap_url, depth = sitemap_queue.pop(0)
            normalized_sitemap = normalize_url(sitemap_url)
            if not normalized_sitemap or normalized_sitemap in seen_sitemaps:
                continue
            seen_sitemaps.add(normalized_sitemap)
            try:
                response = await http_client.get(normalized_sitemap)
            except Exception:
                continue

            if response.status_code != 200 or "xml" not in response.headers.get("content-type", ""):
                continue

            for loc in _parse_sitemap_xml(response.content):
                normalized_loc = normalize_url(loc)
                if not normalized_loc:
                    continue
                if normalize_domain(normalized_loc) != target_domain:
                    continue
                if normalized_loc.endswith(".xml"):
                    if depth < max_depth:
                        sitemap_queue.append((normalized_loc, depth + 1))
                    continue
                if should_skip_url(normalized_loc):
                    continue
                found_urls.add(normalized_loc)
                if len(found_urls) >= max_urls:
                    break
    finally:
        if own_client:
            await http_client.aclose()

    return filter_urls_by_inclusion(list(found_urls)) or [base_url]


def is_hidden_container(el: Any) -> bool:
    current = el
    while current is not None:
        has_attr = getattr(current, "has_attr", None)
        if callable(has_attr) and has_attr("hidden"):
            return True

        get_attr = getattr(current, "get", None)
        if callable(get_attr):
            aria_hidden = (get_attr("aria-hidden") or "").strip().lower()
            if aria_hidden == "true":
                return True

            style = get_attr("style") or ""
            if any(pattern.search(style) for pattern in HIDDEN_STYLE_PATTERNS):
                return True

            classes = get_attr("class") or []
            class_tokens = {str(token).strip().lower() for token in classes if str(token).strip()}
            if class_tokens & HIDDEN_CLASS_TOKENS:
                return True

        current = getattr(current, "parent", None)

    return False


def normalize_segment_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def extract_visible_text(element: Any) -> str:
    parts: list[str] = []
    for text_node in element.find_all(string=True):
        parent = getattr(text_node, "parent", None)
        if parent is None:
            continue
        parent_name = (getattr(parent, "name", "") or "").lower()
        if parent_name in {"script", "style", "noscript", "svg"}:
            continue
        if is_hidden_container(parent):
            continue
        text = normalize_segment_text(str(text_node))
        if text:
            parts.append(text)
    return normalize_segment_text(" ".join(parts))


def _dedupe_repeated_sentences(text: str, *, min_chars: int = 30) -> str:
    normalized = normalize_segment_text(text)
    if not normalized:
        return ""
    parts = [part.strip() for part in re.split(r"(?<=[.!?;])\s+", normalized) if part.strip()]
    if not parts:
        return normalized
    kept: list[str] = []
    seen: set[str] = set()
    for part in parts:
        signature = normalize_segment_text(part).casefold()
        if len(signature) >= min_chars and signature in seen:
            continue
        seen.add(signature)
        kept.append(part)
    return normalize_segment_text(" ".join(kept))


def _strip_ui_noise_phrases(text: str) -> tuple[str, list[str]]:
    cleaned = normalize_segment_text(text)
    if not cleaned:
        return "", []
    removed_labels: list[str] = []

    cta_matches = list(CTA_PHRASE_PATTERN.finditer(cleaned))
    if len(cta_matches) > 1:
        first_span = cta_matches[0].span()

        def _cta_replacer(match: re.Match[str]) -> str:
            if match.span() == first_span:
                return match.group(0)
            removed_labels.append("cta")
            return " "

        cleaned = CTA_PHRASE_PATTERN.sub(_cta_replacer, cleaned)

    for label, pattern in UI_NOISE_PATTERNS.items():
        if pattern.search(cleaned):
            removed_labels.append(label)
            cleaned = pattern.sub(" ", cleaned)

    cleaned = _dedupe_repeated_sentences(cleaned)
    cleaned = normalize_segment_text(cleaned)
    return cleaned, sorted(set(removed_labels))


def _safe_json_loads(raw: str) -> Any:
    candidate = (raw or "").strip()
    if not candidate or candidate[0] not in "[{":
        return None
    try:
        return json.loads(candidate)
    except Exception:
        return None


def _extract_segments_from_json(value: Any) -> list[str]:
    segments: list[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                normalized = normalize_segment_text(item)
                if normalized:
                    segments.append(normalized)
            elif isinstance(item, dict):
                for key in ("text", "content", "value", "title"):
                    item_value = normalize_segment_text(str(item.get(key, "")))
                    if item_value:
                        segments.append(item_value)
                        break
    elif isinstance(value, dict):
        for key in ("text", "content", "value", "title"):
            normalized = normalize_segment_text(str(value.get(key, "")))
            if normalized:
                segments.append(normalized)
    return segments


def _tokenize_for_similarity(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9$%]+", text.casefold()) if len(token) >= 2}


def _jaccard_similarity(left: str, right: str) -> float:
    left_tokens = _tokenize_for_similarity(left)
    right_tokens = _tokenize_for_similarity(right)
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return intersection / union if union else 0.0


def clean_html_content(soup_element: Any) -> str:
    for tag in soup_element.select(REMOVE_SELECTOR):
        tag.decompose()
    return normalize_segment_text(soup_element.get_text(separator=" ", strip=True))


def extract_page_name(soup: Any, fallback: str = "") -> str:
    selectors = [
        ('meta[property="og:site_name"]', "content"),
        ('meta[name="application-name"]', "content"),
        ('meta[property="og:title"]', "content"),
        ("title", None),
        ("h1", None),
    ]
    for selector, attr in selectors:
        element = soup.select_one(selector)
        if not element:
            continue
        value = element.get(attr, "") if attr else element.get_text(separator=" ", strip=True)
        normalized = normalize_segment_text(value)
        if normalized:
            return normalized[:200]
    return fallback


def extract_page_segments(soup: Any) -> List[Dict[str, Any]]:
    root = soup.body or soup
    segments: List[Dict[str, Any]] = []
    seen_texts: set[str] = set()

    for idx, element in enumerate(root.find_all(SEGMENT_CONTAINER_TAGS)):
        if is_hidden_container(element):
            continue
        text = extract_visible_text(element)
        parsed_json = _safe_json_loads(text)
        json_segments = _extract_segments_from_json(parsed_json) if parsed_json is not None else []
        candidate_texts = json_segments or [text]

        for candidate_text in candidate_texts:
            cleaned_text, _ = _strip_ui_noise_phrases(candidate_text)
            if not 12 <= len(cleaned_text) <= 3200:
                continue
            normalized = cleaned_text.casefold()
            if normalized in seen_texts:
                continue
            seen_texts.add(normalized)
            segments.append(
                {
                    "index": len(segments) if json_segments else idx,
                    "tag": "json" if json_segments else element.name,
                    "text": cleaned_text,
                    "text_length": len(cleaned_text),
                }
            )
    return segments


def score_page_segment(text: str) -> Dict[str, Any]:
    cleaned_text, removed_labels = _strip_ui_noise_phrases(text)
    normalized = normalize_segment_text(cleaned_text)
    lower_text = normalized.casefold()
    word_count = len(normalized.split())
    flags: List[str] = []
    score = 0

    if removed_labels:
        flags.extend([f"cleaned:{label}" for label in removed_labels])
        score += 1

    price_hits = sum(1 for pattern in PRICE_PATTERNS if pattern.search(normalized))
    promo_hits = sum(1 for pattern in PROMO_PATTERNS if pattern.search(normalized))
    date_hits = sum(1 for pattern in DATE_PATTERNS if pattern.search(normalized))
    service_hits = sum(1 for keyword in SERVICE_SIGNAL_KEYWORDS if keyword in lower_text)
    strong_hits = sum(1 for keyword in STRONG_SIGNAL_KEYWORDS if keyword in lower_text)

    score += price_hits * 5
    score += promo_hits * 4
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
    if TESTIMONIAL_LANGUAGE_PATTERN.search(normalized) and not (service_hits or strong_hits or promo_hits):
        flags.append("noise:testimonial")
        score -= 6
    if word_count <= 4 and not price_hits:
        flags.append("drop:short_fragment")
        score -= 6
    if len(normalized) > 1200:
        flags.append("shape:long_block")
        score += 1
    if "noise:review" in flags and not (service_hits or strong_hits or promo_hits):
        flags.append("drop:review_only")
        score -= 8
    if "noise:testimonial" in flags and not (service_hits or strong_hits or promo_hits):
        flags.append("drop:testimonial_only")
        score -= 8
    if any(marker in flags for marker in {"noise:cta", "noise:account", "noise:commerce"}) and not (
        service_hits or strong_hits or promo_hits
    ):
        flags.append("drop:action_only")
        score -= 8
    if not (price_hits or promo_hits or service_hits or strong_hits) and noise_hits:
        flags.append("drop:noise_only")
        score -= 8

    keep = score > 0 and not any(
        flag in flags
        for flag in {
            "drop:short_fragment",
            "drop:review_only",
            "drop:testimonial_only",
            "drop:action_only",
            "drop:noise_only",
        }
    )
    return {
        "score": score,
        "keep": keep,
        "flags": flags,
        "cleaned_text": normalized,
        "signals": {
            "price_hits": price_hits,
            "promo_hits": promo_hits,
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
        scored = score_page_segment(segment["text"])
        candidate = {**segment, **scored}
        candidate_text = normalize_segment_text(str(candidate.get("cleaned_text") or candidate.get("text", "")))
        candidate["text"] = candidate_text
        candidate["text_length"] = len(candidate_text)
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
        if any(
            _jaccard_similarity(normalized, existing) >= JACCARD_DUPLICATE_THRESHOLD
            and segment["text_length"] <= kept_segments[idx]["text_length"] + 120
            for idx, existing in enumerate(kept_norms)
        ):
            content_quality_flags.append("drop:jaccard_near_duplicate")
            continue
        kept_segments.append(segment)
        kept_norms.append(normalized)

    kept_segments.sort(key=lambda item: item["index"])
    if len(kept_segments) > max_segments:
        content_quality_flags.append(f"trim:top_{max_segments}_segments")
        kept_segments = sorted(kept_segments, key=lambda item: (-item["score"], item["index"]))[:max_segments]
        kept_segments.sort(key=lambda item: item["index"])

    return kept_segments, sorted(set(content_quality_flags))


def build_llm_ready_content(filtered_segments: Iterable[Dict[str, Any]], max_chars: int = 6000) -> str:
    chunks: List[str] = []
    total_chars = 0
    for output_index, segment in enumerate(filtered_segments):
        chunk = f"[SEGMENT {output_index}] {segment['text']}"
        if total_chars and total_chars + len(chunk) + 2 > max_chars:
            break
        chunks.append(chunk)
        total_chars += len(chunk) + 2
    return "\n\n".join(chunks)


def build_content_signature(
    filtered_segments: Iterable[Dict[str, Any]],
    *,
    fallback_text: str = "",
    max_chars: int = 4000,
) -> str:
    signature_parts = [
        normalize_segment_text(segment.get("text", "")).casefold()
        for segment in filtered_segments
        if normalize_segment_text(segment.get("text", ""))
    ]
    normalized = "\n".join(signature_parts).strip()
    if not normalized:
        normalized = normalize_segment_text(fallback_text).casefold()
    if not normalized:
        return ""
    compacted = normalized[:max_chars]
    return hashlib.sha1(compacted.encode("utf-8")).hexdigest()


def build_segment_keys(filtered_segments: Iterable[Dict[str, Any]]) -> List[str]:
    keys: List[str] = []
    for segment in filtered_segments:
        normalized = normalize_segment_text(segment.get("text", "")).casefold()
        if normalized:
            keys.append(hashlib.sha1(normalized.encode("utf-8")).hexdigest())
    return keys


def build_text_segments_from_content(text: str, *, source_tag: str = "text") -> List[Dict[str, Any]]:
    normalized_text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n{2,}", normalized_text)
    segments: List[Dict[str, Any]] = []
    seen_texts: set[str] = set()

    for block in blocks:
        raw_block = normalize_segment_text(block)
        parsed_json = _safe_json_loads(raw_block)
        json_segments = _extract_segments_from_json(parsed_json) if parsed_json is not None else []
        candidate_blocks = json_segments or [raw_block]
        for candidate in candidate_blocks:
            clean_block, _ = _strip_ui_noise_phrases(candidate)
            if len(clean_block) < 12:
                continue
            normalized_block = clean_block.casefold()
            if normalized_block in seen_texts:
                continue
            seen_texts.add(normalized_block)
            segments.append(
                {
                    "index": len(segments),
                    "tag": "json" if json_segments else source_tag,
                    "text": clean_block,
                    "text_length": len(clean_block),
                }
            )

    if segments:
        return segments

    fallback = normalize_segment_text(normalized_text)
    if len(fallback) >= 12:
        return [{"index": 0, "tag": source_tag, "text": fallback, "text_length": len(fallback)}]
    return []


def _is_ocr_title_candidate(text: str) -> bool:
    normalized = normalize_segment_text(text)
    if not normalized:
        return False
    if PRICE_PATTERN_SIMPLE.search(normalized):
        return False
    if CTA_PHRASE_PATTERN.search(normalized):
        return False
    if any(pattern.search(normalized) for pattern in UI_NOISE_PATTERNS.values()):
        return False
    words = normalized.split()
    if not (2 <= len(words) <= 12):
        return False
    alpha_count = sum(1 for ch in normalized if ch.isalpha())
    return alpha_count >= 6


def build_price_anchored_ocr_segments(text: str) -> List[Dict[str, Any]]:
    lines = [normalize_segment_text(line) for line in (text or "").replace("\r", "\n").split("\n")]
    lines = [line for line in lines if line]

    segments: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for idx, line in enumerate(lines):
        if not PRICE_PATTERN_SIMPLE.search(line):
            continue

        title = ""
        details: List[str] = []
        for back in range(1, 6):
            prev_idx = idx - back
            if prev_idx < 0:
                break
            prev = lines[prev_idx]
            if PRICE_PATTERN_SIMPLE.search(prev):
                break
            if _is_ocr_title_candidate(prev):
                title = prev
                for detail_idx in range(prev_idx + 1, idx):
                    detail = lines[detail_idx]
                    if not detail:
                        continue
                    if PRICE_PATTERN_SIMPLE.search(detail):
                        continue
                    if CTA_PHRASE_PATTERN.search(detail):
                        continue
                    if _is_ocr_title_candidate(detail):
                        continue
                    if len(detail.split()) >= 5:
                        details.append(detail)
                break

        if not title:
            continue

        offer_text = normalize_segment_text("\n".join([title, *details, line]))
        key = offer_text.casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        segments.append(
            {
                "index": len(segments),
                "tag": "ocr_offer",
                "text": offer_text,
                "text_length": len(offer_text),
            }
        )

    return segments


async def extract_ocr_text_from_page_screenshot(
    url: str,
    *,
    timeout_ms: int = 20_000,
    ocr_lang: str = "eng",
) -> Dict[str, Any]:
    if not url:
        return {"text": "", "error": "empty_url"}
    if async_playwright is None:
        return {"text": "", "error": "missing_playwright"}
    if pytesseract is None:
        return {"text": "", "error": "missing_pytesseract"}
    if shutil.which("tesseract") is None:
        return {"text": "", "error": "missing_tesseract_binary"}

    screenshot_bytes = b""
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                ],
            )
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 3200})
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                await page.wait_for_timeout(500)
                screenshot_bytes = await page.screenshot(full_page=False, type="png")
            finally:
                await browser.close()
    except Exception as exc:
        return {"text": "", "error": f"screenshot_failed:{exc}"}

    try:
        image = Image.open(io.BytesIO(screenshot_bytes)).convert("L")
        processed = ImageOps.autocontrast(image)
        binary = processed.point(lambda px: 255 if px > 165 else 0)
        ocr_text = pytesseract.image_to_string(binary, lang=ocr_lang, config="--psm 6")
    except Exception as exc:
        return {"text": "", "error": f"ocr_failed:{exc}"}

    return {
        "text": ocr_text or "",
        "error": "",
        "screenshot_size_bytes": len(screenshot_bytes),
    }


def should_export_page(filtered_segments: Iterable[Dict[str, Any]], url: str) -> bool:
    if should_skip_url(url):
        return False
    segments = list(filtered_segments)
    if not segments:
        return False

    has_price = any(item.get("signals", {}).get("price_hits", 0) for item in segments)
    has_promo = any(
        item.get("signals", {}).get("promo_hits", 0) or item.get("signals", {}).get("strong_hits", 0)
        for item in segments
    )
    has_service = any(item.get("signals", {}).get("service_hits", 0) for item in segments)
    total_chars = sum(len(normalize_segment_text(str(item.get("text", "")))) for item in segments)
    long_prose_segments = [
        item
        for item in segments
        if item.get("signals", {}).get("word_count", 0) >= 40 and item.get("signals", {}).get("price_hits", 0) == 0
    ]
    short_price_segments = [
        item
        for item in segments
        if item.get("signals", {}).get("price_hits", 0) > 0 and item.get("signals", {}).get("word_count", 0) <= 14
    ]

    if long_prose_segments and not has_price and not has_promo:
        return False
    if len(long_prose_segments) >= 2 and short_price_segments and not has_promo:
        return False
    if total_chars < MIN_EXPORT_TOTAL_CHARS and not (has_price and (has_promo or has_service)):
        return False

    if score_candidate_url(url) >= 4 and has_price:
        return True
    return (has_price and (has_service or has_promo)) or (has_service and has_promo)


def prepare_ocr_page_export(
    ocr_text: str,
    url: str,
    *,
    max_segments: int = 18,
    max_llm_chars: int = 6000,
) -> Dict[str, Any]:
    normalized_ocr_text = normalize_segment_text(ocr_text)
    raw_segments = build_price_anchored_ocr_segments(ocr_text)
    if not raw_segments:
        raw_segments = build_text_segments_from_content(normalized_ocr_text, source_tag="ocr")
    filtered_segments, content_quality_flags = filter_page_segments(raw_segments, max_segments=max_segments)
    page_content_llm = build_llm_ready_content(filtered_segments, max_chars=max_llm_chars)
    if not page_content_llm and normalized_ocr_text:
        page_content_llm = normalized_ocr_text[:max_llm_chars]
        content_quality_flags = sorted(set(content_quality_flags + ["fallback:raw_ocr_text"]))

    return {
        "page_content": page_content_llm,
        "raw_page_content": normalized_ocr_text,
        "page_segments_raw": raw_segments,
        "page_segments_filtered": filtered_segments,
        "page_content_llm": page_content_llm,
        "content_signature": build_content_signature(filtered_segments, fallback_text=page_content_llm),
        "segment_keys": build_segment_keys(filtered_segments),
        "content_quality_flags": content_quality_flags,
        "should_export": should_export_page(filtered_segments, url),
    }


def prepare_page_export(soup: Any, url: str, *, max_segments: int = 18, max_llm_chars: int = 6000) -> Dict[str, Any]:
    raw_page_content = clean_html_content(soup)
    raw_segments = extract_page_segments(soup)
    filtered_segments, content_quality_flags = filter_page_segments(raw_segments, max_segments=max_segments)
    page_content_llm = build_llm_ready_content(filtered_segments, max_chars=max_llm_chars)
    if not page_content_llm and raw_page_content:
        page_content_llm = raw_page_content[:max_llm_chars]
        content_quality_flags = sorted(set(content_quality_flags + ["fallback:raw_page_content"]))

    return {
        "page_content": page_content_llm,
        "raw_page_content": raw_page_content,
        "page_segments_raw": raw_segments,
        "page_segments_filtered": filtered_segments,
        "page_content_llm": page_content_llm,
        "content_signature": build_content_signature(filtered_segments, fallback_text=page_content_llm),
        "segment_keys": build_segment_keys(filtered_segments),
        "content_quality_flags": content_quality_flags,
        "should_export": should_export_page(filtered_segments, url),
    }
