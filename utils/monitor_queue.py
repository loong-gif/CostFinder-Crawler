"""监控队列：由数据库 clinic_promotions / clinic_memberships 的 source_url 动态生成，不再手写 URL 清单。

页面分两类：
- ``promotion``：出现在 clinic_promotions 的页面（同时出现在 clinic_memberships 的页面也按促销处理）
- ``membership``：只出现在 clinic_memberships 的页面，更新慢，轮询档位见 ``utils/monitor_frequency.py``

URL 按类别分桶，桶名 ``daily_NN`` / ``membership_NN``，每桶最多 ``BUCKET_SIZE`` 个。已有桶状态文件里的页面保持原桶，
新页面放进有空位的第一个桶，桶满再开新桶，这样每个桶的基线状态文件始终对应同一批页面。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from utils.monitor_price_store import read_json
from utils.monitor_price_writer_plan import WriterError, normalize_source_url

BUCKET_SIZE = 50
BUCKET_PREFIX = {"promotion": "daily", "membership": "membership"}


def queue_kinds(snapshot: Mapping[str, Any]) -> dict[str, str]:
    """规范化键 -> 类别；``snapshot`` 里 source_url 无法规范化的行被跳过。"""
    kinds: dict[str, set[str]] = {}
    for kind, rows in (("membership", snapshot.get("memberships", [])), ("promotion", snapshot.get("promotions", []))):
        for row in rows:
            try:
                key = normalize_source_url(row.get("source_url"))
            except WriterError:
                continue
            kinds.setdefault(key, set()).add(kind)
    return {key: "promotion" if "promotion" in found else "membership" for key, found in kinds.items()}


def _source_urls(snapshot: Mapping[str, Any]) -> dict[str, str]:
    """规范化键 -> 代表 URL（数据库里字典序最小的原始写法）。"""
    urls: dict[str, str] = {}
    for rows in (snapshot.get("promotions", []), snapshot.get("memberships", [])):
        for row in rows:
            raw = str(row.get("source_url") or "").strip()
            try:
                key = normalize_source_url(raw)
            except WriterError:
                continue
            if key not in urls or raw < urls[key]:
                urls[key] = raw
    return urls


def _existing_assignment(state_dir: Path, prefix: str) -> dict[str, tuple[str, str]]:
    """已有桶状态文件：规范化键 -> (桶名, 状态里记录的 URL 写法)。"""
    assignment: dict[str, tuple[str, str]] = {}
    for path in sorted(state_dir.glob(f"{prefix}_[0-9][0-9]*.json")):
        pages = (read_json(path, {}) or {}).get("pages", {})
        for url in pages:
            try:
                assignment.setdefault(normalize_source_url(url), (path.stem, url))
            except WriterError:
                continue
    return assignment


def build_config(snapshot: Mapping[str, Any], state_dir: Path) -> dict[str, Any]:
    kinds = queue_kinds(snapshot)
    if not kinds:
        raise ValueError("monitor queue is empty")
    urls = _source_urls(snapshot)
    monitors: dict[str, dict[str, Any]] = {}
    for kind, prefix in BUCKET_PREFIX.items():
        keys = sorted(key for key, value in kinds.items() if value == kind)
        existing = _existing_assignment(state_dir, prefix)
        buckets: dict[str, list[str]] = {}
        pending = []
        for key in keys:
            if key in existing:
                name, recorded = existing[key]
                buckets.setdefault(name, []).append(recorded)
            else:
                pending.append(key)
        for key in pending:
            name = next((n for n in sorted(buckets) if len(buckets[n]) < BUCKET_SIZE), None)
            if name is None:
                number = 1 + max((int(n.rsplit("_", 1)[1]) for n in buckets), default=0)
                name = f"{prefix}_{number:02d}"
            buckets.setdefault(name, []).append(urls[key])
        for name, bucket_urls in sorted(buckets.items()):
            monitors[name] = {
                "name": name, "kind": kind, "scan_images": kind == "promotion",
                "targets": [{"id": f"{name}_scrape", "type": "scrape", "urls": bucket_urls}],
            }
    return {"schema_version": 1, "monitors": monitors}


def config_urls(config: Mapping[str, Any]) -> list[str]:
    return [url for monitor in config["monitors"].values()
            for target in monitor.get("targets", []) for url in target.get("urls", [])]


def config_kinds(config: Mapping[str, Any]) -> dict[str, str]:
    """URL -> 类别；没有 kind 字段的监控（手写的静态配置）按促销页处理。"""
    return {url: monitor.get("kind", "promotion") for monitor in config["monitors"].values()
            for target in monitor.get("targets", []) for url in target.get("urls", [])}
