"""监控页面的动态轮询频率：按页面价格内容的实际更新间隔，把每个 URL 分到档位。

促销页（clinic_promotions 的 source_url）分 7 / 15 / 30 天三档；只出现在 clinic_memberships 的会员页更新慢，
分 60 / 90 天两档（三个月内变过的 60 天，更慢的 90 天）。

状态文件 ``<state-dir>/poll_schedule.json`` 以监控队列里的 URL 为键：

- ``first_seen_at``：第一次观察到该页面的时间
- ``last_checked_at``：最近一次成功抓取的时间
- ``last_attempt_at``：最近一次尝试抓取的时间（含失败）
- ``change_times``：最近几次价格行变化（monitor 页面状态 ``changed``）的时间
- ``tier_days`` / ``next_due_at``：当前档位与下次到期时间

更新间隔 = max(相邻两次变化间隔的中位数, 距最近一次变化的天数)；还没有变化记录时取距首次观察的天数。
观察起点也作为一个区间端点参与中位数，所以只变过一次的页面按"观察起点到这次变化"的间隔估计。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping

from utils.monitor_price_store import atomic_json, read_json

SCHEDULE_FILE = "poll_schedule.json"
TIER_DAYS = (7, 15, 30)
# 更新间隔上限（天）→ 轮询档位：两三周内会变的页面每周查，一两个月变一次的半月查，更慢的每月查
TIER_BY_UPDATE_GAP = ((21, 7), (60, 15))
# 会员页（只在 clinic_memberships 出现的页面）更新慢：三个月内变过的每 60 天查，更慢的每 90 天查
MEMBERSHIP_TIER_DAYS = (60, 90)
MEMBERSHIP_TIER_BY_UPDATE_GAP = ((90, 60),)
CHANGE_HISTORY = 6
# monitor 中价格行变化的事件类型；仅正文或截图摘要变化不算网站更新
PRICE_CHANGE_TYPE = "changed"


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def update_gap_days(entry: Mapping[str, Any], now: datetime) -> float:
    points = [_ts(entry["first_seen_at"])] + [_ts(value) for value in entry.get("change_times", [])]
    gaps = [(later - earlier).total_seconds() / 86400 for earlier, later in zip(points, points[1:])]
    since_last = (now - points[-1]).total_seconds() / 86400
    return max(median(gaps) if gaps else 0.0, since_last)


def tier_days(entry: Mapping[str, Any], now: datetime, kind: str = "promotion") -> int:
    gap = update_gap_days(entry, now)
    table, slowest = ((TIER_BY_UPDATE_GAP, TIER_DAYS[-1]) if kind == "promotion"
                      else (MEMBERSHIP_TIER_BY_UPDATE_GAP, MEMBERSHIP_TIER_DAYS[-1]))
    for limit, days in table:
        if gap <= limit:
            return days
    return slowest


def _with_due(entry: dict[str, Any], now: datetime, kind: str = "promotion") -> dict[str, Any]:
    days = tier_days(entry, now, kind)
    anchor = max((_ts(entry[key]) for key in ("last_checked_at", "last_attempt_at") if entry.get(key)), default=None)
    entry["tier_days"] = days
    entry["next_due_at"] = (anchor + timedelta(days=days)).isoformat() if anchor else now.isoformat()
    return entry


def record_check(entry: Mapping[str, Any] | None, *, at: datetime, ok: bool, changed: bool,
                 kind: str = "promotion") -> dict[str, Any]:
    """一次抓取之后的页面条目；失败只推迟到下一档期，变化时间只来自成功抓取。"""
    new = dict(entry or {"first_seen_at": at.isoformat(), "change_times": []})
    new["last_attempt_at"] = at.isoformat()
    if ok:
        new["last_checked_at"] = at.isoformat()
        if changed:
            new["change_times"] = (list(new.get("change_times", [])) + [at.isoformat()])[-CHANGE_HISTORY:]
    return _with_due(new, at, kind)


def due_urls(schedule: Mapping[str, Mapping[str, Any]], urls: Iterable[str], now: datetime) -> set[str]:
    """到期或尚未建档的 URL。"""
    return {url for url in urls if url not in schedule or _ts(schedule[url]["next_due_at"]) <= now}


def seed_schedule(urls: Iterable[str], *, captures: Iterable[tuple[str, str]],
                  changes: Iterable[tuple[str, str]], now: datetime,
                  kinds: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """从 monitor 已有的证据（url, captured_at）与价格变化事件（url, created_at）重建频率档案。"""
    seen: dict[str, list[datetime]] = {}
    for url, captured_at in captures:
        seen.setdefault(url, []).append(_ts(captured_at))
    changed: dict[str, list[datetime]] = {}
    for url, created_at in changes:
        changed.setdefault(url, []).append(_ts(created_at))
    schedule = {}
    for url in urls:
        if url not in seen:
            continue
        stamps = sorted(seen[url])
        entry = {"first_seen_at": stamps[0].isoformat(), "last_checked_at": stamps[-1].isoformat(),
                 "last_attempt_at": stamps[-1].isoformat(),
                 "change_times": [value.isoformat() for value in sorted(changed.get(url, []))][-CHANGE_HISTORY:]}
        schedule[url] = _with_due(entry, now, (kinds or {}).get(url, "promotion"))
    return schedule


def seed_from_state(root: Path, urls: Iterable[str], now: datetime,
                    kinds: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    captures = []
    for path in (root / "evidence").glob("*.json"):
        artifact = read_json(path)
        captures.append((artifact["url"], artifact["captured_at"]))
    changes = []
    for path in (root / "events").glob("*.json"):
        event = read_json(path)
        if event.get("change_type") == PRICE_CHANGE_TYPE:
            changes.append((event["url"], event["created_at"]))
    return seed_schedule(urls, captures=captures, changes=changes, now=now, kinds=kinds)


def load_schedule(root: Path, urls: Iterable[str], now: datetime,
                  kinds: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """读取频率档案；第一次运行时从已有证据与事件重建并保存。"""
    path = root / SCHEDULE_FILE
    if path.exists():
        return read_json(path)
    schedule = seed_from_state(root, urls, now, kinds)
    atomic_json(path, schedule)
    return schedule


def record_reports(root: Path, reports: Iterable[Mapping[str, Any]], at: datetime,
                   kinds: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """把本轮 monitor 报告里每个页面的抓取结果写回频率档案；``kinds`` 给出页面类别（默认按促销页）。"""
    path = root / SCHEDULE_FILE
    schedule = read_json(path, {})
    kinds = kinds or {}
    for report in reports:
        changed = set(report.get("changed_urls", []))
        for url in report.get("successful_urls", []):
            schedule[url] = record_check(schedule.get(url), at=at, ok=True, changed=url in changed,
                                         kind=kinds.get(url, "promotion"))
        for row in report.get("page_errors", []):
            schedule[row["url"]] = record_check(schedule.get(row["url"]), at=at, ok=False, changed=False,
                                                kind=kinds.get(row["url"], "promotion"))
    atomic_json(path, schedule)
    return schedule


def tier_summary(schedule: Mapping[str, Mapping[str, Any]], urls: Iterable[str], now: datetime) -> dict[str, Any]:
    urls = list(urls)
    counts = {str(days): 0 for days in TIER_DAYS + MEMBERSHIP_TIER_DAYS}
    for url in urls:
        if url in schedule:
            counts[str(schedule[url]["tier_days"])] += 1
    return {"pages": len(urls), "untracked": sum(url not in schedule for url in urls), "by_tier_days": counts,
            "due_now": len(due_urls(schedule, urls, now))}
