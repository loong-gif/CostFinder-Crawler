"""Single source for the discovery filter line used when promoting businesses.

review_count >= 50 matches the current catalogue (min 51); the score line of 4.0 was confirmed by
the user. Existing businesses below the score line (3 today, min 2.7) are only flagged.
"""
from __future__ import annotations

from typing import Any, Mapping

MIN_REVIEW_COUNT = 50
MIN_SCORE: float | None = 4.0  # confirmed by the user on 2026-10-15; below-line existing rows are flagged, never removed
REQUIRE_WEBSITE = True
# A below-line place must pass this many consecutive observations before promotion (anti-flap).
CONSECUTIVE_PASSES_TO_PROMOTE = 2


def passes_filter(candidate: Mapping[str, Any]) -> bool:
    """Return True when a discovered place is on or above the filter line."""
    review_count = candidate.get("review_count")
    if review_count is None or int(review_count) < MIN_REVIEW_COUNT:
        return False
    score = candidate.get("score")
    if MIN_SCORE is not None and (score is None or float(score) < MIN_SCORE):
        return False
    if REQUIRE_WEBSITE and not str(candidate.get("website") or "").strip():
        return False
    return True
