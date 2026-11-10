from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_FIRECRAWL_URL = "http://192.168.1.68:3002"


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def clean_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def source_key(value: Any) -> str:
    """Preserve the database's raw URL identity for the 111-row audit scope."""
    return str(value or "").strip()


def normalize_benefits(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = [value]
    if not isinstance(parsed, list):
        parsed = [parsed]
    result: List[str] = []
    seen: set[str] = set()
    for item in parsed:
        text = str(item or "").strip()
        key = re.sub(r"\s+", " ", text).casefold()
        if text and key not in seen:
            result.append(text)
            seen.add(key)
    return result


def token_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def same_number(left: Any, right: Any) -> bool:
    a, b = number(left), number(right)
    return a is not None and b is not None and abs(a - b) <= 0.011


def compact_offer(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "service_name": str(raw.get("service_name") or "").strip(),
        "offer_raw_text": str(raw.get("offer_raw_text") or "").strip(),
        "discount_price": number(raw.get("discount_price")),
        "regular_price": number(raw.get("regular_price")),
        "discount_percent": number(raw.get("discount_percent")),
        "discount_amount": number(raw.get("discount_amount")),
        "unit_type": str(raw.get("unit_type") or "").strip(),
    }


def page_title(payload: Dict[str, Any]) -> str:
    metadata = payload.get("metadata") or {}
    return str(metadata.get("title") or metadata.get("og:title") or "").strip()


def page_status(payload: Dict[str, Any]) -> Optional[int]:
    metadata = payload.get("metadata") or {}
    try:
        return int(metadata.get("statusCode")) if metadata.get("statusCode") is not None else None
    except (TypeError, ValueError):
        return None
