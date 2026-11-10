"""Find new promotion / membership pages from a site map and keep only pages with real price evidence."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from utils.crawler_visibility import assess_page
from utils.firecrawl_scrape_raw_db import canonical_scrape_url

PROMO_PREFIXES = ("special", "promo", "offer", "deal", "discount", "coupon", "saving", "sale", "bundle", "package",
                  "reward", "loyalty", "blackfriday", "cybermonday", "holiday", "seasonal", "vip", "giftwith")
PROMO_TOKENS = frozenset({"event", "events", "summer", "fall", "winter", "spring", "sale", "bogo", "free"})
MEMBERSHIP_PREFIXES = ("member", "subscription", "beautybank", "botoxbank", "toxbank")
MEMBERSHIP_TOKENS = frozenset({"club", "vip"})
PRICING_TOKENS = frozenset({"price", "prices", "pricing", "rate", "rates", "cost", "costs", "menu", "fees", "fee"})
EXCLUDED_PATH_RE = re.compile(
    r"/(tag|category|author|feed|wp-content|wp-json|cart|checkout|account|login|privacy|terms|careers?|jobs?|"
    r"sitemap|cdn-cgi|rsvp|registration|[a-z-]*terms[a-z-]*|conditions|policy|policies)(/|$)|"
    r"(?:rsvp|registration|terms-and-conditions)[a-z-]*$|\.(pdf|jpe?g|png|gif|webp|svg|css|js|xml|zip|mp4)$", re.I)
BLOG_PATH_RE = re.compile(r"/(blog|news|articles?|press|journal|post|posts)(/|$)", re.I)

AMOUNT_RE = re.compile(r"\$\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\b\d{1,5}(?:\.\d{2})?\s?(?:usd|dollars)\b", re.I)
PERCENT_OFF_RE = re.compile(r"\b\d{1,2}\s?%\s?(?:off|discount|savings?)|\bsave\s+(?:up to\s+)?\d{1,2}\s?%", re.I)
AMOUNT_OFF_RE = re.compile(r"\$\s?\d+(?:\.\d{2})?\s?(?:off|discount|savings?|rebate|credit)|"
                           r"\bsave\s+(?:up to\s+)?\$\s?\d+", re.I)
# compare-at pricing: "Regular price ~$600~ Sale price $420", "was $600 now $420", struck-through amounts
SALE_PRICE_RE = re.compile(
    r"sale\s+price\s*:?\s*(?:from\s+)?\$\s?\d|\bwas\s+\$\s?\d+.{0,25}\bnow\s+\$\s?\d+|"
    r"~{1,2}\s*\$\s?\d[\d,.]*[^~\n]{0,12}~{1,2}|"
    r"(?:regular|original|compare[- ]at)\s+price.{0,40}\$\s?\d+.{0,60}\b(?:sale|now)\b", re.I)
BOGO_RE = re.compile(r"\bbuy\s+\d+.{0,30}\bget\b|\bbogo\b|\b2\s?for\s?1\b", re.I)
UNIT_PRICE_RE = re.compile(r"\$\s?\d+(?:\.\d{2})?\s?(?:/|per|each|a)\s?(?:unit|syringe|vial|session|treatment|area|month|mo|visit|"
                           r"cc|ml|week|yr|year)|\b(?:starting|starts?|from|only|just)\s+(?:at\s+)?\$\s?\d+", re.I)
MONTHLY_RE = re.compile(r"\$\s?\d+(?:\.\d{2})?\s?(?:/|per|a)\s?(?:month|mo)\b|\bmonthly\b.{0,40}\$\s?\d+|\$\s?\d+.{0,40}\bmonthly\b", re.I)
SERVICE_RE = re.compile(r"botox|dysport|xeomin|jeuveau|filler|juvederm|restylane|laser|hydrafacial|facial|peel|microneedl|"
                        r"coolsculpt|emsculpt|prp|vampire|hair removal|ipl|photofacial|weight|semaglutide|tirzepatide|"
                        r"massage|waxing|lip|sculptra|kybella|body contouring|iv therapy|vitamin", re.I)
MIN_PLAUSIBLE_AMOUNT = 5
MAX_PLAUSIBLE_AMOUNT = 20000


@dataclass(frozen=True)
class OfferAssessment:
    valid: bool
    kind: str  # "promotion" | "membership" | "pricing" | ""
    needs_review: bool
    evidence: Tuple[str, ...]
    reasons: Tuple[str, ...]


def registrable_root(host: str) -> str:
    parts = [p for p in host.lower().split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


_LANG_PREFIX = re.compile(r"^/([a-z]{2}(?:-[a-z]{2})?)/", re.I)


_NOT_PROMO_WORD = re.compile(r"^special(?:t|i)")


def _promo_word(word: str) -> bool:
    return word.startswith(PROMO_PREFIXES) and not _NOT_PROMO_WORD.match(word)


def _path_kind(path: str) -> str:
    """Classify by whole path words, so that "ideal" never matches "deal"."""
    tokens = [t for t in re.split(r"[^a-z0-9]+", path.lower()) if t]
    joined = [a + b for a, b in zip(tokens, tokens[1:])]  # black-friday -> blackfriday, beauty-bank -> beautybank
    words = tokens + joined
    if any(w.startswith(MEMBERSHIP_PREFIXES) for w in words) or MEMBERSHIP_TOKENS & set(tokens):
        return "membership"
    if any(_promo_word(w) for w in words) or PROMO_TOKENS & set(tokens):
        return "promotion"
    if PRICING_TOKENS & set(tokens):
        return "pricing"
    return ""


def candidate_urls(
    website: str,
    mapped: Iterable[str],
    *,
    known: Set[str] = frozenset(),
    allow_blog: bool = False,
    limit: int = 10,
) -> List[Dict[str, str]]:
    """Same-site URLs whose path suggests a promotion, membership or price page and that are new to us."""
    root = registrable_root(_host(canonical_scrape_url(website)))
    seen: Set[str] = set()
    found: List[Dict[str, str]] = []
    mapped = list(mapped)
    all_urls = {canonical_scrape_url(m) for m in mapped}
    for raw in mapped:
        url = canonical_scrape_url(raw)
        if not url or url in seen:
            continue
        lang = _LANG_PREFIX.match(urlparse(url).path or "")
        if lang:  # /es/pages/deals duplicates /pages/deals when both are mapped
            bare = url.replace(f"/{lang.group(1)}/", "/", 1)
            if bare in all_urls or bare.rstrip("/") in all_urls:
                continue
        host = _host(url)
        if not host or registrable_root(host) != root:
            continue
        path = urlparse(url).path or "/"
        if path == "/" or EXCLUDED_PATH_RE.search(path):
            continue
        if not allow_blog and BLOG_PATH_RE.search(path):
            continue
        kind = _path_kind(path)
        if not kind:
            continue
        seen.add(url)
        if url in known:
            continue
        found.append({"url": url, "kind_hint": kind})
    order = {"promotion": 0, "membership": 1, "pricing": 2}
    found.sort(key=lambda c: (order[c["kind_hint"]], len(c["url"])))
    return found[:limit]


def _amounts(text: str) -> List[float]:
    values = []
    for match in AMOUNT_RE.finditer(text):
        digits = re.sub(r"[^\d.]", "", match.group(0).replace("usd", "").replace("dollars", ""))
        try:
            values.append(float(digits))
        except ValueError:
            continue
    return values


def assess_offer_page(markdown: str, raw_html: Optional[str], *, kind_hint: str = "") -> OfferAssessment:
    """A page is valid only when it states a real discount or price tied to a service or plan.

    Evidence lines must also appear in the page's visible text (hidden-element filtered); pages whose
    visibility cannot be judged from HTML are returned with ``needs_review=True`` instead of valid.
    """
    reasons: List[str] = []
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in str(markdown or "").splitlines()]
    lines = [ln for ln in lines if ln]
    evidence: List[str] = []
    # 同一句话在导航、横幅与正文中重复出现时只差空格、脚注符号或 markdown 转义，按字母数字与 %、$ 去重
    seen_keys: set = set()
    discount = monthly = unit = False
    for line in lines:
        amounts = [a for a in _amounts(line) if MIN_PLAUSIBLE_AMOUNT <= a <= MAX_PLAUSIBLE_AMOUNT]
        has_percent = bool(PERCENT_OFF_RE.search(line))
        off_match = AMOUNT_OFF_RE.search(line)
        off_digits = re.findall(r"\d+(?:\.\d+)?", off_match.group(0)) if off_match else []
        plausible_off = bool(off_digits) and MIN_PLAUSIBLE_AMOUNT <= float(off_digits[0]) <= MAX_PLAUSIBLE_AMOUNT
        sale_ok = bool(SALE_PRICE_RE.search(line)) and any(MIN_PLAUSIBLE_AMOUNT <= a <= MAX_PLAUSIBLE_AMOUNT for a in _amounts(line))
        has_off = plausible_off or bool(BOGO_RE.search(line)) or sale_ok
        has_monthly = bool(MONTHLY_RE.search(line)) and bool(amounts)
        has_unit = bool(UNIT_PRICE_RE.search(line)) and bool(amounts)
        if not (has_percent or has_off or has_monthly or has_unit):
            continue
        if not (SERVICE_RE.search(line) or has_monthly or "membership" in line.lower() or has_off or has_percent):
            continue
        discount |= has_percent or has_off
        monthly |= has_monthly
        unit |= has_unit
        key = re.sub(r"[^0-9a-z%$]+", "", line.casefold())
        if key not in seen_keys:
            seen_keys.add(key)
            evidence.append(line[:240])
    if not evidence:
        reasons.append("no_discount_or_price_evidence")
        return OfferAssessment(False, "", False, (), tuple(reasons))

    visibility = assess_page(markdown, raw_html, facts=())
    needs_review = False
    if "no_html" in visibility.reasons:
        reasons.append("no_html_for_visibility")
        needs_review = True
    else:
        visible = visibility.visible_text.casefold()
        shown = [e for e in evidence if all(tok in visible for tok in re.findall(r"\d+(?:[.,]\d+)*", e))]
        if not shown:
            reasons.append("evidence_not_in_visible_text")
            return OfferAssessment(False, "", False, tuple(evidence[:3]), tuple(reasons))
        evidence = shown
        if {"css_class_hiding_possible", "thin_visible_text", "script_heavy_page"} & set(visibility.reasons):
            reasons.extend(sorted({"css_class_hiding_possible", "thin_visible_text", "script_heavy_page"} & set(visibility.reasons)))
            needs_review = True

    if monthly or (kind_hint == "membership" and (unit or discount)):
        kind = "membership"
    elif discount:
        kind = "promotion"
    else:
        kind = "pricing" if kind_hint == "pricing" else (kind_hint or "promotion")
    return OfferAssessment(True, kind, needs_review, tuple(evidence[:6]), tuple(reasons))


# --- one hop from a promotion / membership hub page --------------------------------------------
HUB_NO_PRICE_REASON = "no_discount_or_price_evidence"
HUB_LINK_PREFIXES = PROMO_PREFIXES + MEMBERSHIP_PREFIXES
HUB_PRODUCT_SEGMENTS = frozenset({"products", "product", "collections", "collection", "shop", "store", "services",
                                  "treatments", "packages", "pages"})
CHROME_TAGS = frozenset({"header", "nav", "footer", "aside", "script", "style", "noscript", "template", "form"})


class _MainLinks(HTMLParser):
    """Collect hrefs from the page body, skipping header, navigation, footer and sidebars."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth_in_chrome = 0
        self.links: List[str] = []

    def handle_starttag(self, tag: str, attrs_list) -> None:
        attrs = {k.lower(): (v or "") for k, v in attrs_list}
        if tag in CHROME_TAGS or attrs.get("role", "").lower() in {"navigation", "banner", "contentinfo"}:
            self.depth_in_chrome += 1
            return
        if tag == "a" and not self.depth_in_chrome and attrs.get("href"):
            self.links.append(attrs["href"])

    def handle_endtag(self, tag: str) -> None:
        if tag in CHROME_TAGS and self.depth_in_chrome:
            self.depth_in_chrome -= 1


def is_hub_page(assessment: OfferAssessment, kind_hint: str) -> bool:
    """A promotion or membership page that states no price itself, so the offers must sit one click away."""
    return (not assessment.valid and HUB_NO_PRICE_REASON in assessment.reasons
            and kind_hint in {"promotion", "membership"})


def _hub_priority(url: str) -> Optional[int]:
    path = urlparse(url).path or "/"
    tokens = [t for t in re.split(r"[^a-z0-9]+", path.lower()) if t]
    words = tokens + [a + b for a, b in zip(tokens, tokens[1:])]
    if any(w.startswith(MEMBERSHIP_PREFIXES) or _promo_word(w) for w in words) or PROMO_TOKENS & set(tokens):
        return 0
    return None  # generic collection / treatment / service pages are navigation, not offers


def hub_links(
    raw_html: str,
    hub_url: str,
    *,
    known: Set[str] = frozenset(),
    visited: Set[str] = frozenset(),
    limit: int = 8,
) -> List[str]:
    """Same-site pages linked from a hub's main content that look like offers, packages or products."""
    parser = _MainLinks()
    parser.feed(raw_html or "")
    root = registrable_root(_host(canonical_scrape_url(hub_url)))
    hub_canonical = canonical_scrape_url(hub_url)
    all_links = {canonical_scrape_url(urljoin(hub_url, href)) for href in parser.links}
    picked: Dict[str, int] = {}
    for href in parser.links:
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = canonical_scrape_url(urljoin(hub_url, href.split("#")[0]))
        if not url or url == hub_canonical or url in picked or url in known or url in visited:
            continue
        host = _host(url)
        if not host or registrable_root(host) != root:
            continue
        path = urlparse(url).path or "/"
        if path == "/" or EXCLUDED_PATH_RE.search(path) or BLOG_PATH_RE.search(path):
            continue
        lang = _LANG_PREFIX.match(path)
        if lang:
            bare = url.replace(f"/{lang.group(1)}/", "/", 1)
            if bare in all_links or bare.rstrip("/") in all_links:
                continue
        priority = _hub_priority(url)
        if priority is not None:
            picked[url] = priority
    ordered = sorted(picked, key=lambda u: (picked[u], len(u)))
    return ordered[:limit]


# --- search results as candidates, and offer end dates ------------------------------------------
import datetime as _dt  # noqa: E402

AGGREGATOR_HOSTS = frozenset({
    "groupon.com", "yelp.com", "facebook.com", "instagram.com", "tiktok.com", "twitter.com", "x.com",
    "youtube.com", "pinterest.com", "google.com", "maps.google.com", "linkedin.com", "reddit.com",
    "realself.com", "healthgrades.com", "zocdoc.com", "yellowpages.com", "mapquest.com", "bbb.org", "nextdoor.com",
    "trustpilot.com", "wellnessliving.com", "mindbodyonline.com", "booksy.com", "vagaro.com",
    "fresha.com", "styleseat.com", "podium.com", "weebly.com", "wixsite.com",
})
SEARCH_PROMO_WORDS = re.compile(r"special|promo|offer|deal|discount|off\b|%|\$\d|save|member|package|bundle|"
                                r"black friday|first (?:time|visit|treatment)|new client|sale|price", re.I)


def is_aggregator(host: str) -> bool:
    root = registrable_root(host)
    return root in AGGREGATOR_HOSTS or host in AGGREGATOR_HOSTS


def search_candidates(
    website: str,
    results: Iterable[Mapping[str, Any]],
    *,
    known: Set[str] = frozenset(),
    allow_blog: bool = False,
    limit: int = 10,
) -> List[Dict[str, str]]:
    """Same-site pages returned by a site-restricted search that look like offers by title or path."""
    root = registrable_root(_host(canonical_scrape_url(website)))
    seen: Set[str] = set()
    found: List[Dict[str, str]] = []
    for item in results:
        raw = str(item.get("url") or "")
        url = canonical_scrape_url(raw)
        if not url or url in seen:
            continue
        host = _host(url)
        if not host or registrable_root(host) != root or is_aggregator(host):
            continue
        path = urlparse(url).path or "/"
        if path == "/" or EXCLUDED_PATH_RE.search(path) or (BLOG_PATH_RE.search(path) and not allow_blog):
            continue
        title = str(item.get("title") or "")
        text = f"{title} {item.get('description') or ''}"
        kind = _path_kind(path)
        if not kind and not SEARCH_PROMO_WORDS.search(text):
            continue
        seen.add(url)
        if url in known:
            continue
        found.append({"url": url, "kind_hint": kind or "promotion", "title": title[:160]})
    return found[:limit]


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
END_DATE_RE = re.compile(
    r"(?:valid\s+(?:through|until|thru)|expires?(?:\s+on)?|good\s+(?:through|until)|offer\s+ends?|ends?(?:\s+on)?|"
    r"ending|through|until|thru)\W{0,8}"
    r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?"
    r"|\d{1,2}/\d{1,2}(?:/\d{2,4})?)", re.I)


def extract_end_dates(text: str, today: Optional[_dt.date] = None) -> List[str]:
    """页面写明的活动结束日期（ISO 格式）。月日没有年份时取当前年份。"""
    today = today or _dt.date.today()
    found: Set[str] = set()
    for match in END_DATE_RE.finditer(str(text or "")):
        raw = match.group(1).lower().replace(".", "")
        try:
            if "/" in raw:
                parts = [int(x) for x in raw.split("/")]
                month, day = parts[0], parts[1]
                year = parts[2] if len(parts) > 2 else None
                if year is not None and year < 100:
                    year += 2000
            else:
                tokens = re.findall(r"[a-z]+|\d+", raw)
                month = _MONTHS[tokens[0][:3]]
                day = int(tokens[1])
                year = int(tokens[2]) if len(tokens) > 2 and len(tokens[2]) == 4 else None
            found.add(_dt.date(year or today.year, month, day).isoformat())
        except (ValueError, KeyError, IndexError):
            continue
    return sorted(found)
