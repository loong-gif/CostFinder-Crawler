"""Write-time gate for promotion evidence verified in a rendered browser page."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class RenderedVisibilityDecision:
    accepted: bool
    content: tuple[str, ...]
    removed: tuple[str, ...]
    reason: str


def normalize_claim(value: Any) -> str:
    text = str(value or "").strip()
    return " ".join(text.replace("\u2013", "-").replace("\u2014", "-").split()).casefold()


def filter_visible_promotion_content(
    content: Sequence[str],
    rendered_visibility: Mapping[str, Any] | None,
) -> RenderedVisibilityDecision:
    """Keep only claims proven visible by the browser gate.

    ``rendered_visibility`` is one page result from the ego visibility report:
    ``{"status": "page_accessible", "claimResults": [...]}``.  Firecrawl
    Markdown, HTML presence, or a missing result is deliberately insufficient.
    """
    values = tuple(str(value).strip() for value in content if str(value).strip())
    if not isinstance(rendered_visibility, Mapping):
        return RenderedVisibilityDecision(False, (), values, "rendered_visibility_required")
    if rendered_visibility.get("status") != "page_accessible":
        return RenderedVisibilityDecision(False, (), values, "page_not_accessible")

    visible = {
        normalize_claim(item.get("claim"))
        for item in rendered_visibility.get("claimResults") or []
        if isinstance(item, Mapping)
        and item.get("recordType") == "promotion"
        and item.get("status") == "visible_match"
        and normalize_claim(item.get("claim"))
    }
    kept = tuple(value for value in values if normalize_claim(value) in visible)
    removed = tuple(value for value in values if normalize_claim(value) not in visible)
    if not kept:
        return RenderedVisibilityDecision(False, (), removed, "no_rendered_visible_promotion_content")
    return RenderedVisibilityDecision(True, kept, removed, "visible_content_filtered")
