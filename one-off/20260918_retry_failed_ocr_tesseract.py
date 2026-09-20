#!/usr/bin/env python3
"""Retry rendered promotion-image OCR locally without changing the source manifest."""
from __future__ import annotations

import io
import json
import re
import time
from pathlib import Path
from typing import Any

import requests
import pytesseract
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
MEDIA_PATH = ROOT / "outputs/visibility_review_20260918/visible_image_manifest.json"
OCR_PATH = ROOT / "outputs/visibility_review_20260918/visible_image_ocr_manifest.json"
OUTPUT_PATH = ROOT / "outputs/visibility_review_20260918/visible_image_tesseract_retry.json"
HINT_RE = re.compile(r"special|promo|offer|sale|deal|laser|botox|filler|membership|tox|skin|facial|package|discount|gift|summer|fall|winter|spring|sept|oct|nov|dec", re.I)
SKIP_RE = re.compile(r"logo|icon|avatar|favicon|facebook|instagram|youtube|google|pixel|tracking", re.I)


def candidates(media: dict[str, Any], existing: dict[str, Any]) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for page in media.values():
        count = 0
        for image in page.get("images") or []:
            src = str(image.get("src") or "").strip()
            text = f"{src} {image.get('alt', '')}"
            if count >= 5 or SKIP_RE.search(text) or not src.startswith("https://"):
                continue
            if int(image.get("naturalWidth") or 0) < 250 or int(image.get("naturalHeight") or 0) < 100:
                continue
            if not HINT_RE.search(text) or existing.get(src, {}).get("status") == "ocr_complete":
                continue
            entry = selected.setdefault(src, {"image_url": src, "pages": [], "alts": []})
            entry["pages"].append(page.get("source_url"))
            if image.get("alt"):
                entry["alts"].append(image["alt"])
            count += 1
    return sorted(selected.values(), key=lambda item: item["image_url"])


def ocr_image(data: bytes) -> list[str]:
    image = Image.open(io.BytesIO(data)).convert("RGB")
    image = ImageOps.exif_transpose(image)
    if max(image.size) < 1400:
        scale = 2
        image = image.resize((image.width * scale, image.height * scale))
    text = pytesseract.image_to_string(image, config="--psm 6")
    lines = []
    seen = set()
    for raw in text.splitlines():
        line = " ".join(raw.split()).strip()
        key = line.casefold()
        if line and key not in seen:
            seen.add(key)
            lines.append(line)
    return lines


media = json.loads(MEDIA_PATH.read_text(encoding="utf-8"))
existing = json.loads(OCR_PATH.read_text(encoding="utf-8"))
target = candidates(media, existing)
session = requests.Session()
session.trust_env = False
output: dict[str, Any] = {}
for index, item in enumerate(target, start=1):
    url = item["image_url"]
    record = {**item, "status": "tesseract_failed", "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        response = session.get(url, timeout=30)
        response.raise_for_status()
        lines = ocr_image(response.content)
        record.update({"status": "tesseract_complete", "transcript": lines, "bytes": len(response.content), "mime_type": response.headers.get("Content-Type", "")})
    except Exception as exc:  # noqa: BLE001
        record["error"] = str(exc)[:1000]
    output[url] = record
    print(json.dumps({"index": index, "total": len(target), "url": url, "status": record["status"], "lines": len(record.get("transcript") or [])}, ensure_ascii=False), flush=True)

summary = {
    "target_count": len(target),
    "complete": sum(record.get("status") == "tesseract_complete" for record in output.values()),
    "failed": sum(record.get("status") != "tesseract_complete" for record in output.values()),
}
OUTPUT_PATH.write_text(json.dumps({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "summary": summary, "results": output}, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"output": str(OUTPUT_PATH), **summary}, ensure_ascii=False, indent=2))
