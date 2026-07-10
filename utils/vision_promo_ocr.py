"""Vision OCR for image-based promo pages via Gemma 4 (Gemini API)."""
from __future__ import annotations

import base64
import glob
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from utils.offer_extraction_llm import parse_json_payload

DEFAULT_GEMINI_MODEL = "gemma-4-26b-a4b-it"
DEFAULT_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

PROMO_IMG_KEYWORDS = (
    "promo", "special", "offer", "deal", "discount", "sale", "banner", "flyer", "monthly",
)
SKIP_IMG_KEYWORDS = ("icon", "logo", "svg", "widget", "spin", "body_wh", "avatar", "favicon")


def discover_promo_images(page_url: str, *, project_root: Optional[Path] = None) -> List[str]:
    """Use Firecrawl scrape with images format to list promo-related image URLs."""
    from utils.firecrawl_client import get_firecrawl_client

    client = get_firecrawl_client(project_root=project_root)
    doc = client.scrape(page_url, formats=["images"], wait_for=3000)
    images = getattr(doc, "images", None) or []
    return _filter_promo_images(images)


def _filter_promo_images(urls: List[str]) -> List[str]:
    picked: List[str] = []
    for raw in urls:
        u = str(raw or "").strip()
        if not u:
            continue
        lower = u.lower()
        if any(k in lower for k in SKIP_IMG_KEYWORDS):
            continue
        if any(k in lower for k in PROMO_IMG_KEYWORDS):
            picked.append(u)
            continue
        if any(ext in lower for ext in (".png", ".jpg", ".jpeg", ".webp")) and "upload" in lower:
            picked.append(u)
    # ponytail: dedupe preserve order; cap at 3 largest promo candidates.
    seen = set()
    out: List[str] = []
    for u in picked:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out[:3]


def _download_image_bytes(url: str) -> tuple[bytes, str]:
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    mime = resp.headers.get("Content-Type", "").split(";")[0].strip()
    if not mime.startswith("image/"):
        mime = mimetypes.guess_type(url)[0] or "image/png"
    return resp.content, mime


def _gemini_api_key() -> str:
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        val = os.getenv(name, "").strip()
        if val:
            return val
    return ""


def _openai_compatible_vision(
    image_bytes: bytes,
    mime: str,
    prompt: str,
    *,
    api_url: str,
    api_key: str,
    model: str,
) -> str:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"
    body = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    resp = requests.post(
        api_url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=180,
    )
    resp.raise_for_status()
    payload = resp.json()
    return str(payload.get("choices", [{}])[0].get("message", {}).get("content") or "")


def _gemini_vision(image_bytes: bytes, mime: str, prompt: str, *, model: str, api_key: str) -> str:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    endpoint = f"{DEFAULT_GEMINI_BASE}/{model}:generateContent"
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"inline_data": {"mime_type": mime, "data": b64}},
                    {"text": prompt},
                ],
            }
        ],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 1.0},
    }
    resp = requests.post(endpoint, params={"key": api_key}, json=body, timeout=180)
    resp.raise_for_status()
    data = resp.json()
    text = ""
    for cand in data.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            if part.get("text"):
                text += part["text"]
    return text


def _image_bytes_to_vision(
    image_bytes: bytes,
    mime: str,
    prompt: str,
    *,
    model: str = DEFAULT_GEMINI_MODEL,
    source_label: str = "",
) -> str:
    """Run a vision LLM on raw image bytes; return raw response text.

    Supports Gemini API (Gemma 4) or OpenAI-compatible (DeepInfra).
    """
    gemini_key = _gemini_api_key()
    if gemini_key:
        return _gemini_vision(image_bytes, mime, prompt, model=model, api_key=gemini_key)
    api_url = os.getenv("VISION_LLM_API_URL", "https://api.deepinfra.com/v1/openai/chat/completions").strip()
    api_key = os.getenv("VISION_LLM_API_KEY", os.getenv("DEEPINFRA_TOKEN", "")).strip()
    vision_model = os.getenv("VISION_LLM_MODEL", "google/gemma-4-26B-A4B-it").strip()
    if not api_key:
        raise RuntimeError(
            "Missing GEMINI_API_KEY (Google AI Studio) or DEEPINFRA_TOKEN / VISION_LLM_API_KEY for Gemma 4 vision"
        )
    return _openai_compatible_vision(
        image_bytes, mime, prompt, api_url=api_url, api_key=api_key, model=vision_model
    )


VISION_PROMPT = (
    "You are extracting medspa/aesthetic promotional offers from an image. "
    "Read ALL visible text including prices, service names, dates, and terms. "
    'Return strict JSON: {"offers": [{"service_name": "", "offer_raw_text": "", '
    '"discount_price": "", "original_price": "", "unit_type": "", '
    '"template_type": "", "service_category": ""}]}. '
    "Split distinct offers into separate records. Use empty strings for unknown fields. "
)

SCREENSHOT_PROMPT = (
    "You are analyzing a full-page screenshot of a promotions/deals page for medspa pricing. "
    "List ALL dollar amounts and prices you see, including:\n"
    "- Any dollar signs like $10, $325, $699, $500, $339, $159\n"
    "- Price-per-unit like $10/unit, $4.25/unit, $8/unit\n"
    "- Discounts like was/now, reg/sale, BOGO deals\n"
    "- Package prices, membership costs\n\n"
    'Return JSON: {"offers": [{"service_name": "name of service or deal", '
    '"offer_raw_text": "full text of the offer including pricing details", '
    '"discount_price": "sale/now price (empty string if none)", '
    '"original_price": "original/regular price (empty string if none)", '
    '"unit_type": "unit, syringe, session, area, vial if applicable, else empty string", '
    '"template_type": "promo|membership|package|bogo", '
    '"service_category": "Injectables|Fillers|Skin|Laser|Body|Membership|Other"}]}. '
    "Each distinct price point or offer is a separate record. Use empty strings for unknown fields."
)


def _unwrap_offers(parsed: Any) -> List[Dict[str, Any]]:
    """Normalize vision LLM JSON variants into a flat offer list."""
    if parsed is None:
        return []
    if isinstance(parsed, dict):
        offers = parsed.get("offers")
        if isinstance(offers, list):
            return [item for item in offers if isinstance(item, dict)]
        if any(key in parsed for key in ("service_name", "offer_raw_text")):
            return [parsed]
        return []
    if isinstance(parsed, list):
        out: List[Dict[str, Any]] = []
        for item in parsed:
            out.extend(_unwrap_offers(item))
        return out
    return []


def _parse_offers_response(text: str, source_label: str) -> Dict[str, Any]:
    """Parse vision LLM JSON response into {offers: [...], raw_text, ...}."""
    parsed = parse_json_payload(text, {"offers": []})
    offers = _unwrap_offers(parsed)
    return {"offers": offers, "raw_text": text, "source": source_label}


def vision_extract_offers_from_image(
    image_url: str,
    *,
    page_url: str = "",
    model: str = DEFAULT_GEMINI_MODEL,
) -> Dict[str, Any]:
    """Run Gemma 4 vision on a promo image URL; return parsed {offers: [...]}."""
    image_bytes, mime = _download_image_bytes(image_url)
    prompt = VISION_PROMPT + f"Page URL context: {page_url or image_url}"
    text = _image_bytes_to_vision(image_bytes, mime, prompt, model=model, source_label=image_url)
    return _parse_offers_response(text, source_label=image_url)


def _parse_data_url(data_url: str) -> tuple[bytes, str]:
    """Parse a data:image/...;base64,... URL into (bytes, mime_type)."""
    mime_match = re.match(r"data:([^;]+)", data_url)
    mime = mime_match.group(1) if mime_match else "image/png"
    b64_part = data_url.split("base64,", 1)[1] if "base64," in data_url else data_url
    return base64.b64decode(b64_part), mime


def _screenshot_engine() -> str:
    return os.getenv("PROMO_SCREENSHOT_ENGINE", "firecrawl").strip().lower() or "firecrawl"


def _default_chromium_path() -> str:
    env_path = os.getenv("PLAYWRIGHT_CHROMIUM_PATH", "").strip()
    if env_path and os.path.isfile(env_path):
        return env_path
    for pattern in (
        os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"),
        "/root/.cache/ms-playwright/chromium-*/chrome-linux*/chrome",
    ):
        matches = sorted(glob.glob(pattern), reverse=True)
        if matches:
            return matches[0]
    return "/root/.cache/ms-playwright/chromium-1124/chrome-linux/chrome"


def _chrome_screenshot_bytes(page_url: str, *, chromium_path: Optional[str] = None) -> tuple[bytes, str]:
    import subprocess
    import tempfile

    chrome = chromium_path or _default_chromium_path()
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        screenshot_path = tmp.name
    try:
        cmd = [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--hide-scrollbars",
            "--disable-software-rasterizer",
            f"--screenshot={screenshot_path}",
            "--window-size=1280,3000",
            "--virtual-time-budget=10000",
            page_url,
        ]
        subprocess.run(cmd, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=120)
        image_bytes = Path(screenshot_path).read_bytes()
        return image_bytes, "image/png"
    finally:
        try:
            os.unlink(screenshot_path)
        except OSError:
            pass


def _firecrawl_screenshot_bytes(page_url: str, *, project_root: Optional[Path] = None) -> tuple[bytes, str, Dict[str, Any]]:
    from utils.firecrawl_client import scrape_screenshot_bytes

    captured = scrape_screenshot_bytes(page_url, project_root=project_root, full_page=True)
    meta = {
        "screenshot_engine": captured.get("engine", "firecrawl"),
        "screenshot_ref": captured.get("screenshot_ref"),
        "warning": captured.get("warning"),
    }
    return captured["image_bytes"], str(captured.get("mime") or "image/png"), meta


def _capture_screenshot_bytes(
    page_url: str,
    *,
    project_root: Optional[Path] = None,
    chromium_path: Optional[str] = None,
) -> tuple[bytes, str, Dict[str, Any]]:
    engine = _screenshot_engine()
    if engine == "chrome":
        image_bytes, mime = _chrome_screenshot_bytes(page_url, chromium_path=chromium_path)
        return image_bytes, mime, {"screenshot_engine": "chrome"}
    try:
        return _firecrawl_screenshot_bytes(page_url, project_root=project_root)
    except Exception as exc:
        err = str(exc)
        if engine == "auto" and ("screenshot_unsupported" in err or "screenshot_missing" in err):
            image_bytes, mime = _chrome_screenshot_bytes(page_url, chromium_path=chromium_path)
            return image_bytes, mime, {"screenshot_engine": "chrome", "firecrawl_error": err}
        raise


def _ocr_screenshot_bytes(
    image_bytes: bytes,
    page_url: str,
    *,
    model: str = DEFAULT_GEMINI_MODEL,
    mime: str = "image/png",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    screenshot_size = len(image_bytes)
    base = {
        "source": f"screenshot:{page_url}",
        "screenshot_size": screenshot_size,
        **(extra or {}),
    }
    if screenshot_size < 500:
        return {**base, "offers": [], "raw_text": "", "error": f"screenshot_too_small ({screenshot_size} bytes)"}
    if screenshot_size < 50_000:
        return {
            **base,
            "offers": [],
            "raw_text": "",
            "error": f"screenshot_suspiciously_small ({screenshot_size} bytes)",
        }
    prompt = SCREENSHOT_PROMPT + f"\n\nWebsite URL: {page_url}"
    text = _image_bytes_to_vision(image_bytes, mime, prompt, model=model, source_label=f"screenshot:{page_url}")
    result = _parse_offers_response(text, source_label=f"screenshot:{page_url}")
    result.update(base)
    return result


def screenshot_extract_offers(
    page_url: str,
    *,
    model: str = DEFAULT_GEMINI_MODEL,
    project_root: Optional[Path] = None,
    chromium_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Capture a full-page screenshot via Firecrawl, then vision-OCR it.

    Default engine is Firecrawl (``PROMO_SCREENSHOT_ENGINE=firecrawl``).
    Set ``PROMO_SCREENSHOT_ENGINE=chrome`` to use local headless Chromium.
    Set ``PROMO_SCREENSHOT_ENGINE=auto`` to fall back to Chrome when Firecrawl
    screenshot is unsupported (e.g. self-hosted without Fire Engine).

    Optional ``FIRECRAWL_SCREENSHOT_API_URL`` / ``FIRECRAWL_SCREENSHOT_API_KEY``
    point screenshot capture at a cloud instance while keeping crawl on self-hosted.

    Returns::

        {"offers": [...], "raw_text": "...", "source": "screenshot:page_url"}
    """
    image_bytes, mime, meta = _capture_screenshot_bytes(
        page_url, project_root=project_root, chromium_path=chromium_path
    )
    return _ocr_screenshot_bytes(image_bytes, page_url, model=model, mime=mime, extra=meta)


def extract_offers_from_page(
    page_url: str,
    *,
    model: str = DEFAULT_GEMINI_MODEL,
    project_root: Optional[Path] = None,
    prefer_screenshot: bool = False,
) -> Dict[str, Any]:
    """Combined pipeline: try image-URL extraction first, fall back to screenshot.

    When *prefer_screenshot* is True, skip image-URL discovery and go straight
    to the full-page screenshot approach.
    """
    if prefer_screenshot:
        return screenshot_extract_offers(page_url, model=model, project_root=project_root)

    images = discover_promo_images(page_url, project_root=project_root)
    if images:
        all_offers: List[Dict[str, Any]] = []
        raw_texts: List[str] = []
        for img_url in images:
            try:
                result = vision_extract_offers_from_image(img_url, page_url=page_url, model=model)
                all_offers.extend(result.get("offers") or [])
                raw_texts.append(result.get("raw_text", ""))
            except Exception:  # noqa: BLE001
                continue
        if all_offers:
            return {
                "offers": all_offers,
                "raw_text": "\n---\n".join(raw_texts),
                "source": "images",
                "image_urls": images,
            }

    return screenshot_extract_offers(page_url, model=model, project_root=project_root)

if __name__ == "__main__":
    from utils.firecrawl_client import decode_screenshot_payload

    belle_raw = (
        '[{"offers": [{"service_name": "Nefertiti Lift", "offer_raw_text": "BOGO", '
        '"discount_price": "", "original_price": "", "unit_type": "", '
        '"template_type": "bogo", "service_category": "Injectables"}]}]'
    )
    assert len(_parse_offers_response(belle_raw, "test")["offers"]) == 1
    assert len(_parse_offers_response('{"offers": [{"service_name": "Botox"}]}', "test")["offers"]) == 1
    assert len(_parse_offers_response('[{"service_name": "Botox", "offer_raw_text": "$10/unit"}]', "test")["offers"]) == 1
    # 1x1 PNG
    tiny_png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    decoded, mime = decode_screenshot_payload(tiny_png_b64)
    assert mime == "image/png" and len(decoded) > 0
    print("vision_promo_ocr self-check ok")
