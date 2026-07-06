"""Vision OCR for image-based promo pages via Gemma 4 (Gemini API)."""
from __future__ import annotations

import base64
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


def vision_extract_offers_from_image(
    image_url: str,
    *,
    page_url: str = "",
    model: str = DEFAULT_GEMINI_MODEL,
) -> Dict[str, Any]:
    """Run Gemma 4 vision on a promo image; return parsed {offers: [...]}."""
    image_bytes, mime = _download_image_bytes(image_url)
    prompt = (
        "You are extracting medspa/aesthetic promotional offers from a flyer image. "
        "Read ALL visible text including prices, service names, dates, and terms. "
        "Return strict JSON: {\"offers\": [{\"service_name\": \"\", \"offer_raw_text\": \"\", "
        "\"discount_price\": \"\", \"original_price\": \"\", \"unit_type\": \"\", "
        "\"template_type\": \"\", \"service_category\": \"\"}]}. "
        "Split distinct offers into separate records. Use empty strings for unknown fields. "
        f"Page URL context: {page_url or image_url}"
    )

    gemini_key = _gemini_api_key()
    if gemini_key:
        text = _gemini_vision(image_bytes, mime, prompt, model=model, api_key=gemini_key)
    else:
        api_url = os.getenv("VISION_LLM_API_URL", "https://api.deepinfra.com/v1/openai/chat/completions").strip()
        api_key = os.getenv("VISION_LLM_API_KEY", os.getenv("DEEPINFRA_TOKEN", "")).strip()
        vision_model = os.getenv("VISION_LLM_MODEL", "google/gemma-4-26B-A4B-it").strip()
        if not api_key:
            raise RuntimeError(
                "Missing GEMINI_API_KEY (Google AI Studio) or DEEPINFRA_TOKEN / VISION_LLM_API_KEY for Gemma 4 vision"
            )
        text = _openai_compatible_vision(
            image_bytes, mime, prompt, api_url=api_url, api_key=api_key, model=vision_model
        )

    parsed = parse_json_payload(text, {"offers": []})
    offers = parsed.get("offers") if isinstance(parsed, dict) else []
    if not isinstance(offers, list):
        offers = []
    return {"offers": offers, "raw_text": text, "image_url": image_url}
