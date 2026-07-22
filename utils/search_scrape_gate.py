"""Gate Scrape targets: only URLs whose Search hit shows price evidence."""
from __future__ import annotations

from typing import Protocol

from utils.caption_price_filter import caption_contains_price_info


class SearchHit(Protocol):
    url: str
    title: str
    markdown: str


def search_hit_has_price(*, title: str = "", markdown: str = "", description: str = "") -> bool:
    """True when title/description/markdown from Search contains price signals."""
    blob = "\n".join(part for part in (title, description, markdown) if str(part or "").strip())
    return caption_contains_price_info(blob)


def search_page_has_price(page: SearchHit) -> bool:
    return search_hit_has_price(title=page.title, markdown=page.markdown)


if __name__ == "__main__":
    assert search_hit_has_price(title="Membership", markdown="The Works $295/month")
    assert not search_hit_has_price(title="About us", markdown="Welcome to our med spa")
    assert search_hit_has_price(description="Botox $12 per unit pricing menu")
