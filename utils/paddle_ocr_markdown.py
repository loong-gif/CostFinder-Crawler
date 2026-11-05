"""Download relevant promotion and membership images and assemble OCR markdown."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Sequence
from urllib.parse import urlparse

import requests

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
    urls = re.findall(r"!\[[^\]]*\]\((https?://[^)]+)\)", str(markdown or ""))
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
        size = int(estimate.headers.get("content-length", "")) or None
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


def _ocr_lines(image_bytes: bytes) -> list[str]:
    image_dir = Path(__file__).resolve().parents[1] / "state" / "pipeline_ocr"
    image_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".img", dir=image_dir, delete=False) as tmp:
        tmp.write(image_bytes)
        path = tmp.name
    try:
        result = subprocess.run(
            ["tesseract", path, "stdout", "--psm", "6"],
            capture_output=True, text=True, check=True,
        )
    finally:
        Path(path).unlink(missing_ok=True)
    lines: list[str] = []
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        text = " ".join(line.split())
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            lines.append(text)
    return lines


def build_markdown_ocr(image_urls: Sequence[str]) -> tuple[str, list[dict]]:
    session = requests.Session()
    session.trust_env = False
    sections: list[str] = []
    meta: list[dict] = []
    for url in image_urls:
        name = _image_name(url)
        try:
            data = _download_image(url, session=session)
            lines = _ocr_lines(data)
        except (requests.RequestException, subprocess.CalledProcessError, FileNotFoundError) as exc:
            meta.append({"url": url, "file": name, "error": str(exc)})
            continue
        meta.append({"url": url, "file": name, "bytes": len(data), "n_lines": len(lines), "engine": "tesseract"})
        if not lines:
            continue
        sections.append(f"## Image: {name}\n\nSource: {url}\n\n" + "\n".join(lines))
    return ("\n\n---\n\n".join(sections), meta)


if __name__ == "__main__":
    assert shutil.which("tesseract")
    sample = build_markdown_ocr([])
    assert sample == ("", [])
    assert filter_promo_image_urls(["https://x.com/logo.svg"]) == []
    print("image OCR self-check ok")
