#!/usr/bin/env python3
"""
Use Inference.net schematron-v2-small API structured outputs to extract clinic offers.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawler.fetch_engine import FirecrawlFetchEngine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="调用 Inference.net schematron-v2-small 做结构化 offer 抽取")
    parser.add_argument("--url", required=True, help="目标页面 URL")
    parser.add_argument("--source-name", required=True, help="诊所/品牌名")
    parser.add_argument("--model", default="inference-net/schematron-v2-small", help="Inference.net 模型名")
    parser.add_argument(
        "--api-url",
        default="https://api.inference.net/v1/chat/completions",
        help="Inference.net chat completions URL",
    )
    parser.add_argument(
        "--api-key-file",
        default=str(PROJECT_ROOT / "api_key.txt"),
        help="包含 SCHEMATRON_API_KEY 的文件路径",
    )
    parser.add_argument(
        "--schema-file",
        default=str(PROJECT_ROOT / "config" / "readerlm_offer_schema.json"),
        help="单条 offer JSON Schema 文件",
    )
    parser.add_argument("--timeout", type=int, default=180, help="请求超时秒数")
    parser.add_argument("--max-markdown-chars", type=int, default=12000, help="送入模型的 markdown 最大长度")
    parser.add_argument("--max-completion-tokens", type=int, default=1800, help="最大输出 token")
    parser.add_argument(
        "--include-price-list",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否保留价格表型输出（默认保留，可用 --no-include-price-list 关闭）",
    )
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "output" / "results" / "schematron_v2_small_result.json"),
        help="输出 JSON 路径",
    )
    return parser.parse_args()


def _load_api_key(path: Path) -> str:
    raw = path.read_text(encoding="utf-8")
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            if k.strip() == "SCHEMATRON_API_KEY":
                return v.strip()
    raise RuntimeError(f"在 {path} 中未找到 SCHEMATRON_API_KEY")


def _extract_price_ints(text: str) -> List[int]:
    prices: List[int] = []
    for m in re.findall(r"\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text or ""):
        cleaned = m.replace(",", "")
        try:
            prices.append(int(round(float(cleaned))))
        except ValueError:
            continue
    return prices


def _extract_discount_percent(text: str) -> float:
    if not text:
        return 0.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if not m:
        return 0.0
    try:
        return float(m.group(1))
    except ValueError:
        return 0.0


def _extract_choice_items(text: str) -> List[str]:
    raw = re.sub(r"\s+", " ", text or "").strip()
    m = re.search(r"(?i)\byour\s+pick\s*:\s*(.+?)(?:\.|;|\\||must be|$)", raw)
    if not m:
        return []
    tail = m.group(1)
    tail = re.sub(r"(?i)\band\b", ",", tail)
    parts = [re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9®+]+$", "", p.strip()) for p in tail.split(",")]
    out: List[str] = []
    seen: set[str] = set()
    for p in parts:
        if len(p) < 2:
            continue
        key = p.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _infer_or_options(service_name: str, raw_text: str) -> List[str]:
    name = re.sub(r"\s+", " ", service_name or "").strip()
    text = re.sub(r"\s+", " ", raw_text or "").strip()
    if not name:
        return []
    # Treat comma-separated service lists as alternatives in this pipeline.
    has_or_signal = bool(re.search(r"(?i)\bor\b|your\s+pick\s*:", text))
    has_comma_list = "," in name
    if not (has_or_signal or has_comma_list):
        return []

    normalized = re.sub(r"\s*(?:,|\bor\b|/|\|)\s*", "|", name, flags=re.IGNORECASE)
    parts = [p.strip(" -") for p in normalized.split("|") if p.strip(" -")]
    dedup: List[str] = []
    seen: set[str] = set()
    for p in parts:
        key = p.casefold()
        if key in seen:
            continue
        seen.add(key)
        dedup.append(p)
    return dedup if len(dedup) >= 2 else []


def _merge_parallel_choice_offers(offers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if len(offers) <= 1:
        return offers

    grouped: Dict[tuple, List[Dict[str, Any]]] = {}
    passthrough: List[Dict[str, Any]] = []
    for o in offers:
        raw = str(o.get("offer_raw_text", "") or "")
        if not re.search(r"(?i)\byour\s+pick\s*:", raw):
            passthrough.append(o)
            continue
        key = (
            str(o.get("source_url", "") or ""),
            re.sub(r"\s+", " ", raw).strip().casefold(),
            float(o.get("original_price", 0) or 0),
            float(o.get("discount_price", 0) or 0),
            float(o.get("discount_percent", 0) or 0),
        )
        grouped.setdefault(key, []).append(o)

    merged: List[Dict[str, Any]] = []
    for _k, items in grouped.items():
        base = dict(items[0])
        raw = str(base.get("offer_raw_text", "") or "")
        choices = _extract_choice_items(raw)
        if not choices:
            choices = []
            for it in items:
                name = str(it.get("service_name", "") or "").strip()
                if name and name not in choices:
                    choices.append(name)
        if len(choices) <= 1:
            merged.append(base)
            continue
        for choice in choices:
            item = dict(base)
            item["service_name"] = choice
            item["offer_content"] = {choice: 1}
            item["delivered_unit"] = 1
            item["is_package"] = False
            merged.append(item)

    return passthrough + merged


def _clean_service_name(name: str) -> str:
    s = re.sub(r"\s+", " ", str(name or "")).strip(" -")
    if not s:
        return s
    # Strip common UI/header noise that can leak into service names.
    s = re.sub(r"(?i)\bfilters?\b", "FILTER_SPLIT", s)
    if "FILTER_SPLIT" in s:
        s = s.split("FILTER_SPLIT")[-1].strip(" -")
    s = re.sub(r"(?i)^(spring|summer|fall|winter|holiday)\s+specials?\s*", "", s).strip(" -")
    s = re.sub(r"(?i)^specials?\s*", "", s).strip(" -")
    return s


def _normalize_offer_content(value: Any, service_name: str, delivered_unit: int) -> Dict[str, Any]:
    generic_keys = {"any service", "service", "any treatment", "treatment"}
    default_key = (service_name or "service").strip() or "service"
    qty = int(delivered_unit or 1)
    qty = max(qty, 1)

    if not isinstance(value, dict) or not value:
        return {default_key: qty}

    normalized: Dict[str, Any] = {}
    for k, v in value.items():
        key = str(k or "").strip()
        if not key:
            continue
        normalized[key] = v

    if not normalized:
        return {default_key: qty}

    if len(normalized) == 1:
        only_key = next(iter(normalized.keys())).strip().lower()
        if only_key in generic_keys:
            return {default_key: qty}

    return normalized


def _sanitize_offer_content_values(
    offer_content: Dict[str, Any],
    service_name: str,
    delivered_unit: int,
    original_price: float,
    discount_price: float,
    discount_percent: float,
    raw_text: str,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    default_key = (service_name or "service").strip() or "service"
    default_qty = max(int(delivered_unit or 1), 1)
    has_percent_offer = "%" in (raw_text or "")

    for key, value in (offer_content or {}).items():
        k = str(key or "").strip() or default_key
        v = value

        numeric_v: float | None = None
        if isinstance(v, (int, float)):
            numeric_v = float(v)
        elif isinstance(v, str):
            cleaned = re.sub(r"[,$%]", "", v).strip()
            try:
                numeric_v = float(cleaned)
            except ValueError:
                numeric_v = None

        if numeric_v is not None:
            if original_price > 0 and abs(numeric_v - original_price) < 0.01:
                v = default_qty
            elif discount_price > 0 and abs(numeric_v - discount_price) < 0.01:
                v = default_qty
            elif has_percent_offer and discount_percent > 0 and abs(numeric_v - discount_percent) < 0.01:
                v = default_qty

        out[k] = v

    if not out:
        out[default_key] = default_qty
    return out


def _extract_pattern_snippets(text: str, max_items: int = 24) -> List[str]:
    raw = re.sub(r"\s+", " ", text or "").strip()
    if not raw:
        return []

    patterns = [
        r"[A-Za-z][A-Za-z0-9®&+'\-\s]{2,48}\s*[—\-:]\s*[^$]{0,80}\$[0-9][0-9,]*(?:\.[0-9]{1,2})?(?:\s*/\s*[A-Za-z]+)?",
        r"[A-Za-z][A-Za-z0-9®&+'\-\s]{2,48}\s+\$[0-9][0-9,]*(?:\.[0-9]{1,2})?(?:\s*/\s*(?:unit|syringe|session|area|ml))?",
        r"[A-Z][A-Z'&\-\s]{3,}?\s+Price:\s*\$[0-9][0-9,]*(?:\.[0-9]{1,2})?\s+Original Price:\s*\$[0-9][0-9,]*(?:\.[0-9]{1,2})?",
        r"[A-Z][A-Z'&\-\s]{3,}?\s*-\s*Includes[^$]{0,120}\$[0-9][0-9,]*(?:\.[0-9]{1,2})?",
        r"GIFT CARD\s+from\s+\$[0-9][0-9,]*(?:\.[0-9]{1,2})?",
    ]
    snippets: List[str] = []
    seen: set[str] = set()
    for pat in patterns:
        for m in re.finditer(pat, raw, flags=re.IGNORECASE):
            s = re.sub(r"\s+", " ", m.group(0)).strip(" -")
            if len(s) < 12:
                continue
            key = s.casefold()
            if key in seen:
                continue
            seen.add(key)
            snippets.append(s)
            if len(snippets) >= max_items:
                return snippets
    return snippets


def _is_price_pair_ambiguous(raw_text: str, prices: List[int]) -> bool:
    text = (raw_text or "").lower()
    if len(set(prices)) < 2:
        return False
    if re.search(r"\b(original|reg\.?|regular|was|from|save)\b", text):
        return False
    if re.search(r"\bprice\s*:\s*\\$|\boriginal price\s*:\s*\\$", text):
        return False
    # Generic rollup blocks often mix multiple unrelated offers.
    if re.search(r"\b(specials?|monthly|shop all|products?)\b", text):
        return True
    # Too many prices in one text chunk is usually multi-offer contamination.
    return len(prices) >= 3


def _has_explicit_discount_cue(text: str) -> bool:
    t = (text or "").lower()
    return bool(
        re.search(
            r"\b(original|reg\.?|regular|was|save|discount|deal|promo|off|from)\b",
            t,
        )
    )


def _is_non_promotional_price_list(service_name: str, raw_text: str) -> bool:
    text = (raw_text or "").lower()
    svc = (service_name or "").lower()
    price_count = len(re.findall(r"\$\s*[0-9]", raw_text or ""))
    has_discount_signal = _has_explicit_discount_cue(text) or bool(re.search(r"\d+\s*%\s*off", text))
    pricing_like = "pricing" in svc or "price list" in svc or "per unit" in text
    # Long multi-row menus frequently get merged into one pseudo-offer.
    return price_count >= 8 and pricing_like and not has_discount_signal


def _titleize_area(area: str) -> str:
    tokens = re.split(r"(\s+|&)", area.strip())
    out: List[str] = []
    for tok in tokens:
        if not tok or tok.isspace() or tok == "&":
            out.append(tok)
            continue
        out.append(tok.capitalize())
    return "".join(out).strip()


def _parse_dual_column_package_offers(page_text: str, source_url: str, source_name: str) -> List[Dict[str, Any]]:
    text = re.sub(r"\s+", " ", page_text or "").strip()
    if not text:
        return []
    upper = text.upper()
    if "MULTI-AREA STARTER PACKAGES" not in upper or "PER TREATMENT" not in upper:
        return []

    # OCR often flattens 2-column grids into:
    # AREA_A AREA_B 4-TREATMENT PACKAGE 4-TREATMENT PACKAGE only $x only $y PER TREATMENT PER TREATMENT
    # Use area whitelist to avoid matching noisy banner tokens as area names.
    known_areas = [
        "UNDERARMS",
        "BRAZILIAN",
        "FACE",
        "LOWER LEGS",
        "BIKINI LINE",
        "UPPER LIP",
        "FULL LEGS",
        "FACE & NECK",
        "LIP & CHIN",
        "HAPPY TRAIL",
        "CHEST & ABS",
        "FULL BACK",
    ]
    area_pat = "(?:" + "|".join(re.escape(a) for a in sorted(known_areas, key=len, reverse=True)) + ")"
    pat = re.compile(
        rf"({area_pat})\s+({area_pat})\s+"
        rf"(?:4|A)-TREATMENT PACKAGE\s+(?:4|A)-TREATMENT PACKAGE\s+"
        rf"only\s+\$([0-9]+(?:\.[0-9]{{1,2}})?)\s+only\s+\$([0-9]+(?:\.[0-9]{{1,2}})?)\s+"
        rf"PER TREATMENT\s+PER TREATMENT",
        flags=re.IGNORECASE,
    )

    out: List[Dict[str, Any]] = []
    seen: set[tuple[str, float]] = set()
    has_30_off = "30% OFF" in upper
    discount_percent = 30.0 if has_30_off else 0.0

    for m in pat.finditer(text):
        left_area = _titleize_area(m.group(1))
        right_area = _titleize_area(m.group(2))
        left_price = float(m.group(3))
        right_price = float(m.group(4))
        raw_chunk = re.sub(r"\s+", " ", m.group(0)).strip()

        pairs = [(left_area, left_price), (right_area, right_price)]
        for area, price in pairs:
            key = (area.casefold(), price)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "source_url": source_url,
                    "source_name": source_name,
                    "service_category": "Facials & Lasers Services",
                    "service_name": f"Laser Hair Removal - {area} (4-Treatment Package)",
                    "offer_raw_text": raw_chunk,
                    "is_package": True,
                    "is_membership_required": False,
                    "eligibility": "Open to all",
                    "offer_content": {area: 4},
                    "original_price": 0,
                    "discount_price": price,
                    "discount_percent": discount_percent,
                    "unit_type": "treatment",
                    "service_area": area.lower(),
                    "delivered_unit": 4,
                }
            )
    return out


def _extract_context(text: str, start: int, end: int, pad: int = 80) -> str:
    s = max(0, start - pad)
    e = min(len(text), end + pad)
    return re.sub(r"\s+", " ", text[s:e]).strip()


def _parse_category_percent_offers(page_text: str, source_url: str, source_name: str) -> List[Dict[str, Any]]:
    text = page_text or ""
    if not text:
        return []

    patterns = [
        ("Single-Area Starter Packages", r"(single-?area.{0,80}?(\d{1,2})%\s*off|(\d{1,2})%\s*off.{0,80}?single-?area)"),
        ("Multi-Area Starter Packages", r"(multi-?area.{0,80}?(\d{1,2})%\s*off|(\d{1,2})%\s*off.{0,80}?multi-?area)"),
        ("1-5 Areas", r"(1\s*[-–]\s*5\s*areas?.{0,80}?(\d{1,2})%\s*off|(\d{1,2})%\s*off.{0,80}?1\s*[-–]\s*5\s*areas?)"),
        ("Full Body", r"(full\s*body.{0,80}?(\d{1,2})%\s*off|(\d{1,2})%\s*off.{0,80}?full\s*body)"),
    ]

    out: List[Dict[str, Any]] = []
    seen: set[tuple[str, float]] = set()
    for label, pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE | re.DOTALL)
        if not m:
            continue
        pct_str = m.group(2) or m.group(3) or "0"
        try:
            pct = float(pct_str)
        except ValueError:
            continue
        key = (label.casefold(), pct)
        if key in seen:
            continue
        seen.add(key)
        raw = _extract_context(text, m.start(), m.end())
        out.append(
            {
                "source_url": source_url,
                "source_name": source_name,
                "service_category": "Facials & Lasers Services",
                "service_name": f"Laser Hair Removal - {label}",
                "offer_raw_text": raw,
                "is_package": False,
                "is_membership_required": False,
                "eligibility": "Open to all",
                "offer_content": {label: 1},
                "original_price": 0,
                "discount_price": 0.0,
                "discount_percent": pct,
                "unit_type": "percent",
                "service_area": "body",
                "delivered_unit": 1,
            }
        )
    return out


def _parse_neurotoxin_price_list_offers(page_text: str, source_url: str, source_name: str) -> List[Dict[str, Any]]:
    text = re.sub(r"\s+", " ", page_text or "").strip()
    if not text:
        return []
    lower = text.lower()
    if "per unit" not in lower and "botox" not in lower and "dysport" not in lower and "daxxify" not in lower:
        return []

    out: List[Dict[str, Any]] = []
    seen: set[tuple[str, float]] = set()

    def clean_area_label(area: str) -> str:
        s = re.sub(r"\s+", " ", area or "").strip(" -")
        s = re.sub(r"(?i)^(?:\d+\s*-\s*\d+\s*months?\)|months?\))\s*", "", s).strip(" -")
        s = re.sub(r"^[^A-Za-z]+", "", s).strip(" -")
        return s

    # Pattern like:
    # Forehead Lines Botox I Dysport: $430 ... Daxxify: $430 ...
    combo_pat = re.compile(
        r"([A-Za-z][A-Za-z ()&/\-]{2,80}?)\s+Botox®?\s*[|I/]\s*Dysport®?\s*:\s*\$([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
        r"(?:[^$]{0,120}?)Daxxify®?\s*:\s*\$([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
        flags=re.IGNORECASE,
    )

    for m in combo_pat.finditer(text):
        area = clean_area_label(m.group(1))
        if not area:
            continue
        try:
            botox_price = float(m.group(2).replace(",", ""))
            daxx_price = float(m.group(3).replace(",", ""))
        except ValueError:
            continue
        raw_chunk = _extract_context(text, m.start(), m.end(), pad=20)

        entries = [
            ("Botox", botox_price),
            ("Dysport", botox_price),
            ("Daxxify", daxx_price),
        ]
        for product, price in entries:
            service_name = f"{area} - {product}"
            key = (service_name.casefold(), price)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "source_url": source_url,
                    "source_name": source_name,
                    "service_category": "Neurotoxins",
                    "service_name": service_name,
                    "offer_raw_text": raw_chunk,
                    "is_package": False,
                    "is_membership_required": False,
                    "eligibility": "Open to all",
                    "offer_content": {product: 1},
                    "original_price": price,
                    "discount_price": price,
                    "discount_percent": 0.0,
                    "unit_type": "unit",
                    "service_area": area.lower(),
                    "delivered_unit": 1,
                }
            )

    # Per-unit lines like: Botox Per Unit $13 Dysport Per Unit $4.33 Daxxify Per Unit $7.00
    for product in ("Botox", "Dysport", "Daxxify", "Xeomin", "Jeuveau"):
        pat = re.compile(
            rf"{re.escape(product)}\s+Per\s+Unit\s*\$([0-9][0-9,]*(?:\.[0-9]{{1,2}})?)",
            flags=re.IGNORECASE,
        )
        for m in pat.finditer(text):
            try:
                price = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            service_name = f"{product} Per Unit"
            key = (service_name.casefold(), price)
            if key in seen:
                continue
            seen.add(key)
            raw_chunk = _extract_context(text, m.start(), m.end(), pad=20)
            out.append(
                {
                    "source_url": source_url,
                    "source_name": source_name,
                    "service_category": "Neurotoxins",
                    "service_name": service_name,
                    "offer_raw_text": raw_chunk,
                    "is_package": False,
                    "is_membership_required": False,
                    "eligibility": "Open to all",
                    "offer_content": {product: 1},
                    "original_price": price,
                    "discount_price": price,
                    "discount_percent": 0.0,
                    "unit_type": "unit",
                    "service_area": "face",
                    "delivered_unit": 1,
                }
            )

    return out


def _dedupe_offers(offers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen: set[tuple] = set()
    for o in offers:
        key = (
            str(o.get("source_url", "")).strip().casefold(),
            re.sub(r"\s+", " ", str(o.get("service_name", "") or "")).strip().casefold(),
            str(o.get("unit_type", "") or "").strip().casefold(),
            round(float(o.get("original_price", 0) or 0), 2),
            round(float(o.get("discount_price", 0) or 0), 2),
            round(float(o.get("discount_percent", 0) or 0), 2),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(o)
    return deduped


def _extract_offer_snippets(markdown: str, max_items: int = 24) -> List[str]:
    snippets: List[str] = []
    seen: set[str] = set()
    signal = re.compile(
        r"(\$\s*\d+|\d+\s*/\s*(unit|syringe|area|ml)|\b\d+%\s*off\b|\boff\b|\bbook now\b)",
        re.IGNORECASE,
    )
    markdown_link = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

    pattern_snippets = _extract_pattern_snippets(markdown, max_items=max_items)
    if pattern_snippets:
        return pattern_snippets

    for text, _href in markdown_link.findall(markdown or ""):
        normalized = re.sub(r"\s+", " ", text).strip()
        if len(normalized) < 12 or not signal.search(normalized):
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        snippets.append(normalized)
        if len(snippets) >= max_items:
            break

    if snippets:
        return snippets

    for line in (markdown or "").splitlines():
        normalized = re.sub(r"\s+", " ", line).strip(" -*")
        if len(normalized) < 12 or not signal.search(normalized):
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        snippets.append(normalized)
        if len(snippets) >= max_items:
            break
    return snippets


def _coerce_value(value: Any, expected_type: str) -> Any:
    if expected_type == "string":
        return "" if value is None else str(value)
    if expected_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "y"}:
                return True
            if lowered in {"false", "0", "no", "n", ""}:
                return False
        return False
    if expected_type == "integer":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            cleaned = re.sub(r"[,$]", "", value).strip()
            try:
                return int(float(cleaned))
            except ValueError:
                return 0
        return 0
    if expected_type == "number":
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            cleaned = re.sub(r"[,$]", "", value).strip()
            try:
                return float(cleaned)
            except ValueError:
                return 0.0
        return 0.0
    if expected_type == "object":
        return value if isinstance(value, dict) else {}
    if expected_type == "array":
        return value if isinstance(value, list) else []
    return value


def _normalize_offer(item: Dict[str, Any], schema: Dict[str, Any], source_url: str, source_name: str) -> Dict[str, Any]:
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    req = schema.get("required", []) if isinstance(schema, dict) else []
    out = dict(item)

    # Repair common alias drift from model output.
    if "membership_required" in out and "is_membership_required" not in out:
        out["is_membership_required"] = out.get("membership_required")
    out.pop("membership_required", None)

    out["source_url"] = out.get("source_url") or source_url
    out["source_name"] = out.get("source_name") or source_name
    out["service_name"] = _clean_service_name(str(out.get("service_name", "") or ""))

    # Coerce all known schema properties first.
    for key, meta in props.items():
        expected = meta.get("type") if isinstance(meta, dict) else None
        if key in out and isinstance(expected, str):
            out[key] = _coerce_value(out.get(key), expected)

    for key in req:
        expected = props.get(key, {}).get("type") if isinstance(props.get(key), dict) else None
        if key not in out:
            if expected == "boolean":
                out[key] = False
            elif expected == "integer":
                out[key] = 0
            elif expected == "number":
                out[key] = 0.0
            elif expected == "object":
                out[key] = {}
            elif expected == "array":
                out[key] = []
            else:
                out[key] = ""
        elif isinstance(expected, str):
            out[key] = _coerce_value(out.get(key), expected)

    raw_text = str(out.get("offer_raw_text", "") or "")
    prices = _extract_price_ints(raw_text)
    service_name = str(out.get("service_name", "") or "")

    # Prefer nearest price to service token for compact "name + $price" rows.
    nearest_price: int | None = None
    if service_name and prices:
        m = re.search(re.escape(service_name), raw_text, flags=re.IGNORECASE)
        if m:
            matches = []
            for pm in re.finditer(r"\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", raw_text):
                cleaned = pm.group(1).replace(",", "")
                try:
                    price_i = int(round(float(cleaned)))
                except ValueError:
                    continue
                dist = abs(pm.start() - m.end())
                matches.append((dist, price_i))
            if matches:
                nearest_price = sorted(matches, key=lambda x: x[0])[0][1]

    if prices:
        if int(out.get("original_price", 0) or 0) <= 0:
            out["original_price"] = nearest_price if nearest_price is not None else prices[0]
        if int(out.get("discount_price", 0) or 0) <= 0:
            out["discount_price"] = nearest_price if nearest_price is not None else prices[-1]

    # For standard product listings with a single visible price and no discount cue,
    # avoid fabricating an original-vs-discount pair.
    if len(prices) == 1 and not _has_explicit_discount_cue(raw_text):
        out["original_price"] = 0
        if float(out.get("discount_price", 0) or 0) <= 0:
            out["discount_price"] = float(prices[0])
        out["discount_percent"] = 0.0
        out["discount_amount"] = 0.0

    if not out.get("discount_amount"):
        op = float(out.get("original_price", 0) or 0)
        dp = float(out.get("discount_price", 0) or 0)
        if op > dp > 0:
            out["discount_amount"] = round(op - dp, 2)

    if float(out.get("discount_percent", 0) or 0) <= 0:
        op = float(out.get("original_price", 0) or 0)
        dp = float(out.get("discount_price", 0) or 0)
        if op > dp > 0:
            out["discount_percent"] = round((op - dp) * 100.0 / op, 2)
        else:
            out["discount_percent"] = round(_extract_discount_percent(raw_text), 2)
    else:
        # Recompute when both prices exist to avoid cross-offer price leakage.
        op = float(out.get("original_price", 0) or 0)
        dp = float(out.get("discount_price", 0) or 0)
        if op > dp > 0:
            computed = round((op - dp) * 100.0 / op, 2)
            reported = float(out.get("discount_percent", 0) or 0)
            if abs(computed - reported) > 1.0:
                out["discount_percent"] = computed

    # Ambiguous multi-price blocks should not emit a fake price pair.
    if _is_price_pair_ambiguous(raw_text, prices):
        out["original_price"] = 0
        out["discount_price"] = 0.0
        out["discount_amount"] = 0.0
        out["discount_percent"] = round(_extract_discount_percent(raw_text), 2)

    # Prevent cross-offer price leakage: if this text has no dollar figure, clear inferred prices.
    if not re.search(r"\$\s*[0-9]", raw_text):
        out["original_price"] = 0
        out["discount_price"] = 0.0
        out["discount_amount"] = 0.0
        out["discount_percent"] = round(_extract_discount_percent(raw_text), 2)

    normalized_offer_content = _normalize_offer_content(
        out.get("offer_content"),
        str(out.get("service_name", "") or ""),
        int(out.get("delivered_unit", 1) or 1),
    )
    out["offer_content"] = _sanitize_offer_content_values(
        normalized_offer_content,
        str(out.get("service_name", "") or ""),
        int(out.get("delivered_unit", 1) or 1),
        float(out.get("original_price", 0) or 0),
        float(out.get("discount_price", 0) or 0),
        float(out.get("discount_percent", 0) or 0),
        raw_text,
    )
    # offer_content values should represent quantities, not prices.
    cleaned_offer_content: Dict[str, Any] = {}
    for k, v in out.get("offer_content", {}).items():
        if isinstance(v, (int, float)):
            numeric_v = float(v)
            if numeric_v > 200:
                cleaned_offer_content[str(k)] = int(max(1, int(out.get("delivered_unit", 1) or 1)))
            else:
                cleaned_offer_content[str(k)] = v
        else:
            cleaned_offer_content[str(k)] = v
    out["offer_content"] = cleaned_offer_content

    return out


def _expand_or_relation_offers(offers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    expanded: List[Dict[str, Any]] = []
    for offer in offers:
        service_name = str(offer.get("service_name", "") or "")
        raw_text = str(offer.get("offer_raw_text", "") or "")
        options = _infer_or_options(service_name, raw_text)
        if not options:
            expanded.append(offer)
            continue
        for option in options:
            item = dict(offer)
            item["service_name"] = option
            item["offer_content"] = {option: 1}
            item["delivered_unit"] = 1
            item["is_package"] = False
            expanded.append(item)
    return expanded


def _build_strict_offer_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    if not isinstance(properties, dict):
        raise RuntimeError("schema properties 非法")
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(schema.get("required", [])),
        "properties": properties,
    }


async def fetch_markdown(url: str) -> Dict[str, Any]:
    page = await FirecrawlFetchEngine().fetch(url)
    return {"url": page.final_url, "title": page.title, "content": page.content}


def _parse_content_to_json(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(raw[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("模型内容不是可解析的 JSON 对象")


def call_inference_api(
    *,
    api_url: str,
    api_key: str,
    model: str,
    timeout: int,
    max_completion_tokens: int,
    messages: list[dict[str, str]],
    response_schema: Dict[str, Any],
) -> tuple[Dict[str, Any], str]:
    session = requests.Session()
    session.trust_env = True
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": max(128, int(max_completion_tokens)),
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "clinic_offers",
                "strict": True,
                "schema": response_schema,
            },
        },
    }
    response = session.post(
        api_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Inference API 请求失败: HTTP {response.status_code}.\n"
            f"Response body (first 2000 chars): {(response.text or '')[:2000]}"
        )
    obj = response.json()
    choices = obj.get("choices", [])
    if not choices:
        raise RuntimeError("API 返回缺少 choices")
    message = choices[0].get("message", {})
    content = message.get("content", "")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not str(content).strip():
        raise RuntimeError("API 返回空 content")
    parsed = _parse_content_to_json(str(content))
    if not isinstance(parsed, dict):
        raise RuntimeError("结构化输出不是 JSON 对象")
    finish_reason = str(choices[0].get("finish_reason", "") or "")
    return parsed, finish_reason


def main() -> None:
    args = parse_args()
    key_file = Path(args.api_key_file).expanduser().resolve()
    schema_file = Path(args.schema_file).expanduser().resolve()
    output_file = Path(args.output).expanduser().resolve()
    output_file.parent.mkdir(parents=True, exist_ok=True)

    api_key = _load_api_key(key_file)
    single_offer_schema = json.loads(schema_file.read_text(encoding="utf-8"))
    strict_offer_schema = _build_strict_offer_schema(single_offer_schema)
    extraction_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["offers"],
        "properties": {
            "offers": {
                "type": "array",
                "items": strict_offer_schema,
            }
        },
    }

    source_doc = asyncio.run(fetch_markdown(args.url))
    markdown = source_doc.get("content", "") or ""
    if args.max_markdown_chars > 0 and len(markdown) > args.max_markdown_chars:
        markdown = markdown[: args.max_markdown_chars]
    offer_snippets = _extract_offer_snippets(markdown)
    focused_markdown = "\n".join(f"- {s}" for s in offer_snippets) if offer_snippets else markdown

    instruction = (
        "Extract clinic offers from the page into strict JSON. "
        "Extract ALL distinct offers present in the content; do not merge neighboring offers. "
        "Return only a JSON object with top-level key offers (array). "
        "Each item must follow the provided schema exactly. "
        "Use source_url and source_name in every offer. "
        "Use offer_snippets as primary evidence and ignore navigation noise. "
        "Do not use generic keys like 'any service' in offer_content. "
        "offer_content must map concrete service names to delivered quantity. "
        "discount_percent must be numeric percent value (0-100)."
    )

    messages = [
        {
            "role": "system",
            "content": "You are a precise data extraction engine. Respond in JSON format.",
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "instruction": instruction,
                    "source_url": source_doc.get("url", args.url),
                    "source_name": args.source_name,
                    "page_title": source_doc.get("title", ""),
                    "offer_snippets": offer_snippets,
                    "page_markdown": focused_markdown,
                },
                ensure_ascii=False,
            ),
        },
    ]

    payload: Dict[str, Any] | None = None
    finish_reason = ""
    max_tokens = int(args.max_completion_tokens)
    last_error: Exception | None = None
    for _attempt in range(3):
        try:
            payload, finish_reason = call_inference_api(
                api_url=args.api_url,
                api_key=api_key,
                model=args.model,
                timeout=args.timeout,
                max_completion_tokens=max_tokens,
                messages=messages,
                response_schema=extraction_schema,
            )
            if finish_reason != "length":
                break
            max_tokens = min(6000, int(max_tokens * 1.8))
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            max_tokens = min(6000, int(max_tokens * 1.8))

    if payload is None:
        raise RuntimeError(f"连续重试后仍未拿到可解析 JSON: {last_error}")

    offers_raw = payload.get("offers", []) if isinstance(payload, dict) else []
    if isinstance(offers_raw, dict):
        offers_raw = [offers_raw]
    offers: List[Dict[str, Any]] = []
    for item in offers_raw:
        if isinstance(item, dict):
            offers.append(
                _normalize_offer(
                    item,
                    schema=single_offer_schema,
                    source_url=source_doc.get("url", args.url),
                    source_name=args.source_name,
                )
            )
    offers = _merge_parallel_choice_offers(offers)
    offers = _expand_or_relation_offers(offers)

    # Deterministic rescue for OCR-flattened 2-column package tables (e.g. simplicitylaser sale page).
    table_offers = _parse_dual_column_package_offers(
        source_doc.get("content", "") or "",
        source_doc.get("url", args.url),
        args.source_name,
    )
    generic_pkg_count = sum(1 for o in offers if "treatment package" in str(o.get("service_name", "")).lower())
    if table_offers and (not offers or len(table_offers) >= max(4, generic_pkg_count)):
        offers = table_offers

    category_percent_offers = _parse_category_percent_offers(
        source_doc.get("content", "") or "",
        source_doc.get("url", args.url),
        args.source_name,
    )
    if category_percent_offers:
        existing = {
            (
                str(o.get("service_name", "")).casefold(),
                float(o.get("discount_percent", 0) or 0),
            )
            for o in offers
        }
        for item in category_percent_offers:
            key = (
                str(item.get("service_name", "")).casefold(),
                float(item.get("discount_percent", 0) or 0),
            )
            if key in existing:
                continue
            offers.append(item)
            existing.add(key)

    # Deterministic rescue for neurotoxin pricing menus:
    # split pricing table into multiple per-service offers.
    neuro_price_offers = _parse_neurotoxin_price_list_offers(
        source_doc.get("content", "") or "",
        source_doc.get("url", args.url),
        args.source_name,
    )
    if neuro_price_offers:
        offers = neuro_price_offers

    # Drop merged full pricing menus that are not promotional offers.
    if not args.include_price_list:
        offers = [
            o
            for o in offers
            if not _is_non_promotional_price_list(
                str(o.get("service_name", "") or ""),
                str(o.get("offer_raw_text", "") or ""),
            )
        ]

    offers = _dedupe_offers(offers)

    output_payload = {
        "meta": {
            "source_url": source_doc.get("url", args.url),
            "source_title": source_doc.get("title", ""),
            "source_name": args.source_name,
            "model": args.model,
            "api_url": args.api_url,
            "offer_snippet_count": len(offer_snippets),
            "finish_reason": finish_reason,
        },
        "offers": offers,
    }
    output_file.write_text(json.dumps(output_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output_file)


if __name__ == "__main__":
    main()
