"""Shared URL / path helpers for the promotion scripts.

Three different promotion-URL rules exist on purpose and each has its own tests; they are named by what
they accept instead of being merged:

* ``is_promotion_url_loose``  - a whole path segment containing special(s)/promo(s)/promotion(s)/offer(s)/
  deal(s). Used when extracting promotions from stored scrape batches.
* ``is_promotion_url_strict`` - only special(s)/promotion(s) as whole words. Used by the missing-promotion
  audit, which must not treat ``/promo`` or ``/offers`` as proof of a promotion page.
* ``has_promo_substring``     - legacy ``special|promo`` substring match used by the Map-based discovery.

``utils/promo_page_discovery.py`` is a fourth, deliberately broader candidate heuristic that is always
followed by a price-evidence check.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

PROMOTION_PATH_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:specials?|promos?|promotions?|offers?|deals?)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)
PROMOTION_URL_TOKENS = {"special", "specials", "promotion", "promotions"}
PROMO_SUBSTRING_RE = re.compile(r"(?:special|promo)", re.IGNORECASE)
_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalized_host(url: Any) -> str:
    """Lower-cased host without ``www.``; accepts scheme-less input."""
    value = str(url or "").strip()
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").casefold().removeprefix("www.")


def sanitized_output_path(value: str) -> Path:
    """Expand ``~`` and replace spaces in the file name."""
    requested = Path(value).expanduser()
    return requested.with_name(requested.name.replace(" ", "_"))


def _segments(url: Any) -> list[str]:
    raw = str(url or "").strip()
    if not raw:
        return []
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return [unquote(s).strip() for s in parsed.path.split("/") if unquote(s).strip()]


def is_promotion_url_loose(url: Any) -> bool:
    return any(PROMOTION_PATH_RE.search(segment) for segment in _segments(url))


def is_promotion_url_strict(url: Any) -> bool:
    return any(set(_NON_WORD.sub(" ", segment.casefold()).split()) & PROMOTION_URL_TOKENS
               for segment in _segments(url))


def has_promo_substring(path: str) -> bool:
    return bool(PROMO_SUBSTRING_RE.search(path or ""))
