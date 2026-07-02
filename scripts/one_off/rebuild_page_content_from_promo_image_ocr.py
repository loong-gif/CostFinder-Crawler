#!/usr/bin/env python3
"""
Rebuild page_content from promo image via OCR.
Pipeline:
1) Capture likely promo image element from page with Playwright
2) Image preprocessing for OCR robustness
3) Tesseract OCR with line extraction
4) Price-anchored offer segmentation (service title + detail + price)
5) Export markdown-like segmented content and CSV row
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

try:
    from playwright.async_api import async_playwright
except ImportError as playwright_import_error:
    async_playwright = None
    _PLAYWRIGHT_IMPORT_ERROR = playwright_import_error
else:
    _PLAYWRIGHT_IMPORT_ERROR = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT_ROOT / "output" / "results"


PRICE_RE = re.compile(r"\$\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?")
CTA_RE = re.compile(r"\b(book now|learn more|contact us|call now|shop now)\b", re.I)
NAV_RE = re.compile(
    r"\b(about us|services|promotions|patients|blog|contact us|online store|privacy policy|quick links|hours)\b",
    re.I,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Rebuild segmented page_content from promo image OCR")
    p.add_argument("--url", required=True, help="Target page URL")
    p.add_argument("--output-prefix", default="promo_image_ocr", help="Output filename prefix")
    return p.parse_args()


def ensure_playwright_available() -> None:
    if async_playwright is None:
        raise RuntimeError(
            "该脚本属于可选浏览器补抓工具。请先执行 "
            "`uv pip install -r requirements_browser_tools.txt`，再执行 "
            "`PLAYWRIGHT_BROWSERS_PATH=.playwright_browsers playwright install chromium`。"
        ) from _PLAYWRIGHT_IMPORT_ERROR


async def capture_promo_image(url: str, raw_img_path: Path, fallback_fullpage: Path) -> Dict[str, str]:
    ensure_playwright_available()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="chrome")
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(2500)

        # Candidate: largest visible image in upper page area.
        img_meta = await page.evaluate(
            """
            () => {
              const imgs = [...document.querySelectorAll('img')];
              const candidates = imgs.map((img, idx) => {
                const r = img.getBoundingClientRect();
                return {
                  idx,
                  src: img.currentSrc || img.src || '',
                  x: r.x, y: r.y, w: r.width, h: r.height,
                  area: Math.max(0, r.width) * Math.max(0, r.height),
                  visible: r.width > 200 && r.height > 200 && r.bottom > 0
                };
              }).filter(x => x.visible && x.y < window.innerHeight * 1.2);
              candidates.sort((a, b) => b.area - a.area);
              return candidates[0] || null;
            }
            """
        )

        if img_meta and img_meta.get("idx") is not None:
            locator = page.locator("img").nth(int(img_meta["idx"]))
            await locator.screenshot(path=str(raw_img_path))
            capture_mode = "hero_img_element"
        else:
            await page.screenshot(path=str(fallback_fullpage), full_page=True)
            raw_img_path.write_bytes(fallback_fullpage.read_bytes())
            capture_mode = "full_page_fallback"

        final_url = page.url
        await context.close()
        await browser.close()
        return {"final_url": final_url, "capture_mode": capture_mode}


def preprocess_image_for_ocr(src: Path, dst: Path) -> None:
    img = Image.open(src).convert("RGB")
    # Upscale for better OCR of small text
    img = img.resize((img.width * 2, img.height * 2), Image.Resampling.LANCZOS)
    gray = ImageOps.grayscale(img)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    gray = ImageEnhance.Contrast(gray).enhance(1.6)
    gray = gray.filter(ImageFilter.SHARPEN)
    # Mild threshold keeps glyph edges while suppressing background textures.
    bw = gray.point(lambda p: 255 if p > 160 else 0)
    bw.save(dst)


def extract_ocr_lines_tsv(image_path: Path) -> List[str]:
    cmd = ["tesseract", str(image_path), "stdout", "-l", "eng", "--psm", "6", "tsv"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:600])

    lines = proc.stdout.splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    col = {name: i for i, name in enumerate(header)}

    grouped: Dict[str, List[str]] = {}
    order: List[str] = []
    for row in lines[1:]:
        parts = row.split("\t")
        if len(parts) <= max(col.get("text", 0), col.get("conf", 0), col.get("line_num", 0), col.get("par_num", 0), col.get("block_num", 0)):
            continue
        text = parts[col["text"]].strip()
        if not text:
            continue
        try:
            conf = float(parts[col["conf"]])
        except Exception:
            conf = -1.0
        if conf >= 0 and conf < 35:
            continue
        key = f"{parts[col['block_num']]}-{parts[col['par_num']]}-{parts[col['line_num']]}"
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(text)

    joined = [" ".join(grouped[k]).strip() for k in order]
    return [x for x in joined if x]


def is_title_candidate(line: str) -> bool:
    s = " ".join(line.split()).strip()
    if not s:
        return False
    if PRICE_RE.search(s):
        return False
    if CTA_RE.search(s):
        return False
    if NAV_RE.search(s):
        return False
    words = s.split()
    if not (2 <= len(words) <= 12):
        return False
    alpha = sum(1 for ch in s if ch.isalpha())
    return alpha >= 6


def segment_by_price_anchor(ocr_lines: List[str]) -> List[str]:
    cleaned = []
    for raw in ocr_lines:
        s = " ".join(raw.replace("\xa0", " ").split()).strip()
        if not s:
            continue
        if NAV_RE.search(s) and not PRICE_RE.search(s):
            continue
        cleaned.append(s)

    offers: List[str] = []
    seen = set()
    for i, line in enumerate(cleaned):
        if not PRICE_RE.search(line):
            continue
        title = ""
        details: List[str] = []
        for back in range(1, 6):
            j = i - back
            if j < 0:
                break
            prev = cleaned[j]
            if PRICE_RE.search(prev):
                break
            if is_title_candidate(prev):
                title = prev
                # Optional detail between title and price.
                for k in range(j + 1, i):
                    mid = cleaned[k]
                    if PRICE_RE.search(mid):
                        continue
                    if CTA_RE.search(mid):
                        continue
                    if is_title_candidate(mid):
                        continue
                    if len(mid.split()) >= 5:
                        details.append(mid)
                break
        if not title:
            continue
        part = "\n".join([title, *details, line]).strip()
        key = part.casefold()
        if key in seen:
            continue
        seen.add(key)
        offers.append(part)
    return offers


def build_segmented_markdown(offers: List[str]) -> str:
    return "\n\n".join(f"[SEGMENT {i}]\n{o}" for i, o in enumerate(offers))


def main() -> None:
    args = parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = f"{args.output_prefix}_{ts}"

    raw_img = OUT_DIR / f"{prefix}_raw.png"
    full_img = OUT_DIR / f"{prefix}_fullpage.png"
    prep_img = OUT_DIR / f"{prefix}_prep.png"
    json_out = OUT_DIR / f"{prefix}.json"
    csv_out = OUT_DIR / f"{prefix}.csv"
    md_out = OUT_DIR / f"{prefix}.md"

    import asyncio

    capture_meta = asyncio.run(capture_promo_image(args.url, raw_img, full_img))
    preprocess_image_for_ocr(raw_img, prep_img)
    ocr_lines = extract_ocr_lines_tsv(prep_img)
    offers = segment_by_price_anchor(ocr_lines)
    page_content = build_segmented_markdown(offers)

    payload = {
        "url": args.url,
        "final_url": capture_meta["final_url"],
        "capture_mode": capture_meta["capture_mode"],
        "raw_image": str(raw_img),
        "preprocessed_image": str(prep_img),
        "ocr_line_count": len(ocr_lines),
        "offer_count": len(offers),
        "page_content": page_content,
        "ocr_lines_preview": ocr_lines[:120],
    }
    json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_out.write_text(page_content, encoding="utf-8")

    with csv_out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["url", "final_url", "capture_mode", "ocr_line_count", "offer_count", "page_content", "raw_image", "preprocessed_image"],
        )
        w.writeheader()
        w.writerow(
            {
                "url": args.url,
                "final_url": capture_meta["final_url"],
                "capture_mode": capture_meta["capture_mode"],
                "ocr_line_count": len(ocr_lines),
                "offer_count": len(offers),
                "page_content": page_content,
                "raw_image": str(raw_img),
                "preprocessed_image": str(prep_img),
            }
        )

    print(
        json.dumps(
            {
                "json": str(json_out),
                "csv": str(csv_out),
                "md": str(md_out),
                "offer_count": len(offers),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
