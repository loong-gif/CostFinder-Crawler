from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import urlsplit

# service_name -> alias regexes (first match wins among candidate lines)
SERVICE_ALIASES: dict[str, list[str]] = {
    "Botox": [r"\bbotox\b", r"\bbotulinum\b"],
    "Dysport": [r"\bdysport\b"],
    "Daxxify": [r"\bdaxxify\b"],
    "Xeomin": [r"\bxeomin\b"],
    "Jeuveau": [r"\bjeuveau\b", r"\bnewtox\b"],
    "Letybo": [r"\bletybo\b"],
    "Juvéderm": [r"\bjuv[eé]derm\b", r"\bjuvederm\b"],
    "Sculptra": [r"\bsculptra\b"],
    "Radiesse": [r"\bradiesse\b"],
    "Dermal Filler": [
        r"\bdermal\s*fillers?\b",
        r"\bfillers?\b",
        r"\brestylane\b",
        r"\bversa\b",
        r"\brevanesse\b",
    ],
}

PROMO_RE = re.compile(
    r"\b(member|membership|vip|special|promo|promotion|sale|discount|off\b|"
    r"new\s*client|first\s*time|intro|flash|limited|coupon|deal)\b",
    re.I,
)
REGULAR_HINT_RE = re.compile(
    r"\b(regular|standard|menu|list\s*price|retail|non[- ]?member|"
    r"original\s*price|everyday)\b",
    re.I,
)
FROM_RE = re.compile(r"\bfrom\b", re.I)
UNIT_HINT_RE = re.compile(
    r"\b(per\s+)?(unit|syringe|vial|area|treatment|session|ml|mg)\b", re.I
)
# Require $ or USD — bare integers like "1/2 syringe" must not count as prices.
PRICE_RE = re.compile(
    r"(?:USD\s*)?\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)\s*"
    r"(?:/\s*|\s+per\s+)?(unit|syringe|vial|area|treatment|session|ml|mg)?",
    re.I,
)
RANGE_RE = re.compile(
    r"\$\s*(\d+(?:\.\d+)?)\s*[-–—to]+\s*\$?\s*(\d+(?:\.\d+)?)", re.I
)
NAV_NOISE_RE = re.compile(
    r"^(skip to|search this|embedded files|cookie|privacy|terms|"
    r"book now|call us|follow us|instagram|facebook)\b",
    re.I,
)


@dataclass
class PriceHit:
    amount: Decimal
    line_idx: int
    line: str
    is_promo: bool
    is_from: bool
    has_unit: bool
    has_regular_hint: bool
    is_range: bool


@dataclass
class ExtractResult:
    proposed_price: Optional[Decimal]
    evidence: str
    verdict_hint: str  # regular | promo_only | not_found
    needs_review: bool
    notes: list[str] = field(default_factory=list)


def parse_decimal(val: Any) -> Optional[Decimal]:
    if val is None or val == "":
        return None
    try:
        return Decimal(str(val).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def prices_equal(a: Optional[Decimal], b: Optional[Decimal]) -> bool:
    if a is None or b is None:
        return False
    return a.quantize(Decimal("0.01")) == b.quantize(Decimal("0.01"))


def service_alias_res(service_name: str) -> list[re.Pattern[str]]:
    pats = SERVICE_ALIASES.get(service_name, [re.escape(service_name)])
    return [re.compile(p, re.I) for p in pats]


def line_matches_service(line: str, service_name: str) -> bool:
    low = line.lower()
    # Avoid cross-talk: Botox line shouldn't match when looking for Dermal Filler via "filler"
    # if a more specific brand is named on the same line alone — still OK for Dermal Filler.
    for rx in service_alias_res(service_name):
        if rx.search(line):
            # For generic filler aliases, require service_name == Dermal Filler (caller ensures).
            if service_name == "Dermal Filler" and rx.pattern in (r"\bfillers?\b",):
                # Skip lines that are clearly a named brand already covered elsewhere
                if re.search(
                    r"\b(juv[eé]derm|juvederm|sculptra|radiesse|restylane|versa|revanesse)\b",
                    low,
                ):
                    # Still allow if "dermal filler" framing + brand — keep if filler word present
                    if "dermal" not in low and "filler" in low:
                        return True
                    return True
            return True
    return False


def classify_line(line: str) -> tuple[bool, bool, bool]:
    is_promo = bool(PROMO_RE.search(line))
    is_from = bool(FROM_RE.search(line))
    has_regular = bool(REGULAR_HINT_RE.search(line))
    return is_promo, is_from, has_regular


def parse_price_hits_on_line(
    line: str,
    line_idx: int,
    *,
    is_promo: bool,
    is_from: bool,
    has_regular: bool,
) -> list[PriceHit]:
    hits: list[PriceHit] = []
    for m in RANGE_RE.finditer(line):
        a, b = parse_decimal(m.group(1)), parse_decimal(m.group(2))
        if a is None or b is None:
            continue
        hits.append(
            PriceHit(
                amount=a,
                line_idx=line_idx,
                line=line.strip(),
                is_promo=is_promo,
                is_from=True,
                has_unit=bool(UNIT_HINT_RE.search(line)),
                has_regular_hint=has_regular,
                is_range=True,
            )
        )
    if hits:
        return hits
    for m in PRICE_RE.finditer(line):
        amt = parse_decimal(m.group(1))
        if amt is None or amt <= 0:
            continue
        unit = (m.group(2) or "").lower()
        has_unit = bool(unit) or bool(UNIT_HINT_RE.search(line))
        hits.append(
            PriceHit(
                amount=amt,
                line_idx=line_idx,
                line=line.strip(),
                is_promo=is_promo and not has_regular,
                is_from=is_from,
                has_unit=has_unit,
                has_regular_hint=has_regular,
                is_range=False,
            )
        )
    return hits


def extract_hits_from_line(line: str, line_idx: int, service_name: str) -> list[PriceHit]:
    if not line_matches_service(line, service_name):
        return []
    is_promo, is_from, has_regular = classify_line(line)
    return parse_price_hits_on_line(
        line, line_idx, is_promo=is_promo, is_from=is_from, has_regular=has_regular
    )


def page_implies_service(url: str, lines: list[str], service_name: str) -> bool:
    """True when URL path or top heading clearly centers this service."""
    path = urlsplit(url).path.lower()
    for rx in service_alias_res(service_name):
        # strip regex noise for crude path check
        token = re.sub(r"[\\^$?*+\[\](){}.|]", "", rx.pattern)
        token = token.replace("b", "").strip()
        # use first literal-ish alias word
    alias_words: list[str] = []
    for raw in SERVICE_ALIASES.get(service_name, [service_name]):
        # pick simple word tokens from alias patterns
        alias_words.extend(re.findall(r"[a-zA-Z]{4,}", raw))
    alias_words.append(service_name.lower().replace("é", "e"))
    for w in alias_words:
        w = w.lower()
        if w and w in path.replace("-", "").replace("_", ""):
            return True
        if w and re.search(rf"/{re.escape(w)}\b", path):
            return True
    head = "\n".join(lines[:25])
    return line_matches_service(head, service_name) and not any(
        line_matches_service(head, other)
        for other in SERVICE_ALIASES
        if other != service_name and other not in ("Dermal Filler",)
        and service_name not in ("Dermal Filler",)
    )


def score_hit(h: PriceHit) -> tuple:
    """Higher is better for regular-price selection."""
    return (
        0 if h.is_promo else 1,
        1 if h.has_regular_hint else 0,
        0 if h.is_from else 1,
        1 if h.has_unit else 0,
        0 if h.is_range else 1,
        # prefer typical tox unit prices over huge package totals when unit present
        float(h.amount) if h.amount < 200 else -float(h.amount),
    )
