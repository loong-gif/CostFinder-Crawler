from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

from utils.source_url import source_url_key


def _campaign_date(value: str | None) -> date | None:
    """抽取结果的日期已由 promo_extraction 校验为 YYYY-MM-DD 或 null。"""
    return date.fromisoformat(value) if value else None


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value else None


def _merged_dates(items: list[Mapping[str, Any]]) -> tuple[date | None, date | None]:
    starts = [i["start"] for i in items]
    ends = [i["end"] for i in items]
    return (None if None in starts else min(starts)), (None if None in ends else max(ends))


def _rejection(kind: str, business_id: int, url: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "business_id": business_id, "source_url": url,
            "target_key": {"business_id": business_id, "source_url_key": source_url_key(url)},
            "action": "rejected", "reason": reason, **extra}


def index_manifest(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """可见性清单按 URL 身份键索引。"""
    return {source_url_key(url): value for url, value in manifest.items()}


def _visibility_for(manifest: Mapping[str, Any] | None, url: str) -> Mapping[str, Any] | None:
    if manifest is None:
        return None
    return manifest.get(source_url_key(url))
