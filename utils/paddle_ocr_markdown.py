"""PaddleOCR: download promo images and assemble markdown_ocr text."""
from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Sequence
from urllib.parse import urlparse

import requests

SKIP_IMG_KEYWORDS = ("icon", "logo", "svg", "widget", "spin", "avatar", "favicon", "emoji")
PROMO_IMG_KEYWORDS = ("special", "promo", "offer", "deal", "discount", "sale", "banner", "flyer", "monthly", "fill")


def _image_name(url: str) -> str:
    return Path(urlparse(url).path).name or "image"


def filter_promo_image_urls(urls: Sequence[str]) -> list[str]:
    picked: list[str] = []
    for raw in urls:
        url = str(raw or "").strip()
        if not url:
            continue
        lower = url.lower()
        if any(k in lower for k in SKIP_IMG_KEYWORDS):
            continue
        if any(k in lower for k in PROMO_IMG_KEYWORDS):
            picked.append(url)
            continue
        if any(ext in lower for ext in (".png", ".jpg", ".jpeg", ".webp")) and (
            "upload" in lower or "wixstatic.com/media" in lower
        ):
            picked.append(url)
    seen: set[str] = set()
    out: list[str] = []
    for url in picked:
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def image_urls_from_markdown(markdown: str) -> list[str]:
    urls = re.findall(r"!\[[^\]]*\]\((https?://[^)]+)\)", str(markdown or ""))
    return filter_promo_image_urls(urls)


def _download_image(url: str, *, session: requests.Session | None = None) -> bytes:
    client = session or requests.Session()
    if session is None:
        client.trust_env = False
    resp = client.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content


def _ocr_lines(image_bytes: bytes, *, ocr_engine: object | None = None) -> list[str]:
    from paddleocr import PaddleOCR

    engine = ocr_engine or PaddleOCR(lang="en")
    with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as tmp:
        tmp.write(image_bytes)
        path = tmp.name
    try:
        result = engine.ocr(path)
    finally:
        Path(path).unlink(missing_ok=True)
    lines: list[str] = []
    for block in result or []:
        for item in block or []:
            if not item or len(item) < 2:
                continue
            text = str(item[1][0] or "").strip()
            if text:
                lines.append(text)
    return lines


def build_markdown_ocr(image_urls: Sequence[str], *, ocr_engine: object | None = None) -> tuple[str, list[dict]]:
    session = requests.Session()
    session.trust_env = False
    sections: list[str] = []
    meta: list[dict] = []
    engine = ocr_engine
    for url in image_urls:
        name = _image_name(url)
        try:
            data = _download_image(url, session=session)
            if engine is None:
                from paddleocr import PaddleOCR

                engine = PaddleOCR(lang="en")
            lines = _ocr_lines(data, ocr_engine=engine)
        except Exception as exc:  # noqa: BLE001
            meta.append({"url": url, "file": name, "error": str(exc)})
            continue
        meta.append({"url": url, "file": name, "bytes": len(data), "n_lines": len(lines)})
        if not lines:
            continue
        sections.append(f"## Image: {name}\n\nSource: {url}\n\n" + "\n".join(lines))
    return ("\n\n---\n\n".join(sections), meta)


if __name__ == "__main__":
    sample = build_markdown_ocr([])
    assert sample == ("", [])
    assert filter_promo_image_urls(["https://x.com/logo.svg"]) == []
    print("paddle_ocr_markdown self-check ok")
