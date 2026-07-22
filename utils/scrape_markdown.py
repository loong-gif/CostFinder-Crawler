"""Denoise and truncate Firecrawl scrape markdown before storage / LLM."""
from __future__ import annotations

import os
import re

DEFAULT_SCRAPE_MARKDOWN_MAX_CHARS = int(os.getenv("FIRECRAWL_SCRAPE_MAX_MARKDOWN_CHARS", "120000"))

_SKIP_LINE_PREFIXES = (
    "[skip to content]",
    "[call now button]",
)

_NOISE_SECTION_HEADING = re.compile(
    r"^(?:growth99\+?|tracking debug|lead capture form|recaptcha|"
    r"how could we have made your experience|captcha verification failed|"
    r"any questions\?|thank you!|unable to submit form)$",
    re.IGNORECASE,
)

_TRAILING_CUT_MARKERS = (
    "\ngrowth99",
    "\n![lou lou medspa]",
    "\nbook an appointment",
    "\nany questions?",
)


def _normalize_blank_lines(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _trim_leading_chrome(text: str) -> str:
    match = re.search(r"(?m)^#\s+\S", text)
    if not match or match.start() == 0:
        return text
    prefix = text[: match.start()]
    if (
        "[skip to content]" in prefix.lower()
        or prefix.count("](") >= 2
        or len(prefix.strip()) < 800
    ):
        return text[match.start() :].lstrip()
    return text


def _cut_trailing_widget_blocks(text: str) -> str:
    lower = text.lower()
    cut_at = len(text)
    for marker in _TRAILING_CUT_MARKERS:
        idx = lower.find(marker)
        if idx >= 0:
            cut_at = min(cut_at, idx)
    if cut_at < len(text):
        return text[:cut_at].rstrip()
    return text


def _drop_noise_sections(text: str) -> str:
    parts = re.split(r"(?m)(?=^#{1,6}\s+)", text)
    if len(parts) <= 1:
        return text
    kept: list[str] = []
    for part in parts:
        if not part.strip():
            continue
        heading = part.lstrip("#").split("\n", 1)[0].strip()
        if _NOISE_SECTION_HEADING.match(heading):
            continue
        kept.append(part)
    return _normalize_blank_lines("".join(kept))


def _drop_skip_lines(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip().lower()
        if any(stripped.startswith(prefix) for prefix in _SKIP_LINE_PREFIXES):
            continue
        lines.append(line)
    return "\n".join(lines)


def denoise_scrape_markdown(text: str) -> str:
    """Remove common widget/footer noise while keeping pricing/membership body."""
    raw = str(text or "").replace("\xa0", " ").strip()
    if not raw:
        return ""
    out = _drop_skip_lines(raw)
    out = _trim_leading_chrome(out)
    out = _drop_noise_sections(out)
    out = _cut_trailing_widget_blocks(out)
    return _normalize_blank_lines(out)


def truncate_scrape_markdown(text: str, *, max_chars: int | None = None) -> str:
    limit = max_chars if max_chars is not None else DEFAULT_SCRAPE_MARKDOWN_MAX_CHARS
    if limit <= 0 or len(text) <= limit:
        return text
    clipped = text[:limit].rstrip()
    if "\n" in clipped:
        clipped = clipped.rsplit("\n", 1)[0].rstrip()
    return clipped + "\n\n<!-- ponytail: markdown truncated -->"


def prepare_scrape_markdown(text: str, *, max_chars: int | None = None) -> str:
    return truncate_scrape_markdown(denoise_scrape_markdown(text), max_chars=max_chars)


if __name__ == "__main__":
    sample = (
        "[Skip to content](https://example.com/#content)\n\n"
        "# Membership\n\n### The Works $295/month\n\n"
        "**PLUS** choose ONE complimentary treatment per month.\n\n"
        "Growth99+\n\nUnable to submit form\n\n"
        "## Tracking Debug\n\n1. Source URL\n\nN/A\n\n"
        "BOOK AN APPOINTMENT\n\n[Call Now Button](tel:123)"
    )
    cleaned = prepare_scrape_markdown(sample, max_chars=500)
    assert "Membership" in cleaned and "295" in cleaned
    assert "Growth99" not in cleaned and "Tracking Debug" not in cleaned
    assert "Call Now Button" not in cleaned
