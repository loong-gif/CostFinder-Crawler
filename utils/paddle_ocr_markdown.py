"""Download relevant promotion and membership images and assemble OCR markdown."""
from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from markdown_it import MarkdownIt

from utils.service_endpoints import OCR_URL

SKIP_IMG_KEYWORDS = ("icon", "logo", "svg", "widget", "spin", "avatar", "favicon", "emoji")
PROMO_IMG_KEYWORDS = ("special", "promo", "offer", "deal", "discount", "sale", "banner", "flyer", "monthly", "fill", "membership", "benefit", "plan")
_LAST_DOWNLOAD_SPEED = 0.0


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
    urls = [token.attrGet("src") for block in MarkdownIt().parse(str(markdown or ""))
            for token in (block.children or []) if token.type == "image" and token.attrGet("src")
            and urlparse(token.attrGet("src")).scheme in ("http", "https")]
    return filter_promo_image_urls(urls)


def _download_image(url: str, *, session: requests.Session | None = None) -> bytes:
    global _LAST_DOWNLOAD_SPEED
    client = session or requests.Session()
    if session is None:
        client.trust_env = False
    size = None
    try:
        estimate = client.head(url, timeout=15, allow_redirects=True)
        estimate.raise_for_status()
        length = estimate.headers.get("content-length", "")
        size = int(length) if length.isdecimal() and int(length) > 0 else None
    except requests.RequestException as exc:
        print(f"OCR download estimate unavailable for {url}: {exc}")
    eta = f"{size / _LAST_DOWNLOAD_SPEED:.1f}s" if size and _LAST_DOWNLOAD_SPEED else "unknown"
    print(f"OCR download starting: url={url} bytes={size if size else 'unknown'} prior_speed={_LAST_DOWNLOAD_SPEED:.0f}B/s ETA={eta}")
    started = time.monotonic()
    resp = client.get(url, timeout=60, stream=True)
    resp.raise_for_status()
    chunks = []
    received = 0
    reported = 0
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        chunks.append(chunk)
        received += len(chunk)
        elapsed = time.monotonic() - started
        _LAST_DOWNLOAD_SPEED = received / elapsed if elapsed else 0
        if received - reported >= 1024 * 1024:
            remaining = max((size or received) - received, 0)
            print(f"OCR download progress: {received}/{size if size else 'unknown'} bytes speed={_LAST_DOWNLOAD_SPEED:.0f}B/s ETA={remaining / _LAST_DOWNLOAD_SPEED:.1f}s")
            reported = received
    elapsed = time.monotonic() - started
    _LAST_DOWNLOAD_SPEED = received / elapsed if elapsed else 0
    print(f"OCR download complete: {received} bytes in {elapsed:.1f}s speed={_LAST_DOWNLOAD_SPEED:.0f}B/s")
    return b"".join(chunks)


OCR_SERVICE_URL = OCR_URL
OCR_ATTEMPTS = 4
OCR_BACKOFF_S = 10.0


def ocr_lines(image_bytes: bytes, *, attempts: int = OCR_ATTEMPTS, backoff: float = OCR_BACKOFF_S) -> list[str]:
    """图片 OCR 的唯一入口：抽取流水线的 markdown_ocr 与可见性清单的图片转写都用它。

    直接调用监控服务器上的 PaddleOCR 服务（utils/ocr_service.py，地址见 utils.service_endpoints）。
    同一行的服务名与价格由服务合并在同一行，图片中逐字出现的声明能作为连续原文匹配。
    服务暂时不可用时按递增间隔重试，重试用尽仍失败即终止命令，不把识别失败当作图片没有文字。
    """
    for attempt in range(1, attempts):
        try:
            return _ocr_request(image_bytes)
        except requests.RequestException:
            time.sleep(backoff * attempt)
    return _ocr_request(image_bytes)


def _ocr_request(image_bytes: bytes) -> list[str]:
    response = requests.post(f"{OCR_SERVICE_URL}/ocr", data=image_bytes, timeout=300,
                             headers={"Content-Type": "application/octet-stream"},
                             proxies={"http": None, "https": None})
    response.raise_for_status()
    return response.json()["lines"]


def build_markdown_ocr(image_urls: Sequence[str]) -> tuple[str, list[dict]]:
    session = requests.Session()
    session.trust_env = False
    sections: list[str] = []
    meta: list[dict] = []
    for url in image_urls:
        name = _image_name(url)
        try:
            data = _download_image(url, session=session)
        except requests.RequestException as exc:
            meta.append({"url": url, "file": name, "error": str(exc)})
            continue
        lines = ocr_lines(data)
        meta.append({"url": url, "file": name, "bytes": len(data), "n_lines": len(lines), "engine": "paddleocr"})
        if not lines:
            continue
        sections.append(f"## Image: {name}\n\nSource: {url}\n\n" + "\n".join(lines))
    return ("\n\n---\n\n".join(sections), meta)


if __name__ == "__main__":
    sample = build_markdown_ocr([])
    assert sample == ("", [])
    assert filter_promo_image_urls(["https://x.com/logo.svg"]) == []
    print("image OCR self-check ok")
