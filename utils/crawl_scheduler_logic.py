"""Pure scheduling rules: due selection, tier adaptation and monitor bucket building."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Set

from utils.crawl_schedule_state import TIER_INTERVAL_DAYS
from utils.firecrawl_scrape_raw_db import canonical_scrape_url

TIER_ORDER = ("A", "B", "C")
MAX_BACKOFF_DAYS = 180
FAILURES_TO_BACKOFF = 3
NO_CHANGE_TO_DEMOTE = 3
BUCKET_SIZE = 50


def jitter_days(business_id: int) -> int:
    """Deterministic -3..+3 day spread so businesses do not all fall due on the same day."""
    return (int(business_id) * 7919) % 7 - 3


def collect_business_urls(
    businesses: Iterable[Mapping[str, Any]],
    *source_rows: Iterable[Mapping[str, Any]],
) -> Dict[int, Set[str]]:
    """Map business_id to canonical evidence URLs; the website is used only when nothing else exists."""
    urls: Dict[int, Set[str]] = {}
    for rows in source_rows:
        for row in rows:
            business_id = row.get("business_id")
            url = canonical_scrape_url(str(row.get("source_url") or ""))
            if business_id is not None and url:
                urls.setdefault(int(business_id), set()).add(url)
    for row in businesses:
        business_id = int(row["business_id"])
        website = canonical_scrape_url(str(row.get("website") or ""))
        if website and not urls.get(business_id):
            urls[business_id] = {website}
    return urls


def select_due(
    schedule: Mapping[str, Mapping[str, Any]],
    now: datetime,
    cap: int,
    urls_by_business: Mapping[int, Set[str]],
) -> List[Dict[str, Any]]:
    """Due businesses ordered by tier then due date, bounded by ``cap``; each carries its URLs."""
    rank = {tier: index for index, tier in enumerate(("A", "B", "C", "D"))}
    due = []
    for key, entry in schedule.items():
        raw = entry.get("next_crawl_at")
        if not raw:
            continue
        when = datetime.fromisoformat(str(raw))
        if when > now:
            continue
        business_id = int(key)
        urls = sorted(urls_by_business.get(business_id, ()))
        if not urls:
            continue
        due.append(
            {
                "business_id": business_id,
                "tier": entry.get("tier", "B"),
                "next_crawl_at": str(raw),
                "urls": urls,
                "_sort": (rank.get(entry.get("tier", "B"), 1), when, business_id),
            }
        )
    due.sort(key=lambda item: item["_sort"])
    for item in due:
        item.pop("_sort", None)
    return due[: max(0, cap)]


def _shift(tier: str, step: int) -> str:
    index = TIER_ORDER.index(tier) if tier in TIER_ORDER else 1
    return TIER_ORDER[max(0, min(len(TIER_ORDER) - 1, index + step))]


def apply_result(
    entry: Mapping[str, Any],
    *,
    business_id: int,
    ok: bool,
    changed: bool,
    now: datetime,
    expiring_campaign: bool = False,
) -> Dict[str, Any]:
    """Return the entry after one crawl: tier adaptation, failure backoff and the next due date."""
    new = dict(entry)
    tier = str(new.get("tier") or "B")
    pinned = bool(new.get("pinned"))
    new["last_crawled_at"] = now.isoformat()

    if not ok:
        failures = int(new.get("consecutive_failures") or 0) + 1
        new["consecutive_failures"] = failures
        new["last_status"] = "failed"
        if failures >= FAILURES_TO_BACKOFF:
            if not pinned:
                tier = "D"
            interval = min(MAX_BACKOFF_DAYS, 30 * 2 ** (failures - FAILURES_TO_BACKOFF))
        else:
            interval = int(new.get("interval_days") or TIER_INTERVAL_DAYS.get(tier, 30))
        new["tier"] = tier
        new["interval_days"] = interval
        new["next_crawl_at"] = (now + timedelta(days=interval)).isoformat()
        return new

    new["consecutive_failures"] = 0
    new["last_status"] = "changed" if changed else "ok"
    if tier == "D" and not pinned:
        tier = "B"
    if changed:
        new["last_change_at"] = now.isoformat()
        new["consecutive_no_change"] = 0
        if not pinned:
            tier = _shift(tier, -1)
    else:
        no_change = int(new.get("consecutive_no_change") or 0) + 1
        if no_change >= NO_CHANGE_TO_DEMOTE and not pinned and not expiring_campaign:
            tier = _shift(tier, +1)
            no_change = 0
        new["consecutive_no_change"] = no_change
    new["tier"] = tier
    interval = TIER_INTERVAL_DAYS[tier]
    new["interval_days"] = interval
    new["next_crawl_at"] = (now + timedelta(days=interval + jitter_days(business_id))).isoformat()
    return new


def build_buckets(urls: Iterable[str], size: int = BUCKET_SIZE) -> List[List[str]]:
    ordered = sorted(dict.fromkeys(urls))
    return [ordered[index:index + size] for index in range(0, len(ordered), size)]
