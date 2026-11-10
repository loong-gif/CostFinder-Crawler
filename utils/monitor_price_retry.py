"""Bound hourly retries per source, using durable files under the monitor lock."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from utils.monitor_price_store import atomic_json, now, read_json
from utils.monitor_price_writer_plan import normalize_source_url

MAX_CONSECUTIVE_FAILURES = 3
STAGES = ("scrape", "processing")


class RetryLimits:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "retry_limits.json"
        self.state = read_json(self.path, {"schema_version": 1})
        self.state["max_consecutive_failures"] = MAX_CONSECUTIVE_FAILURES
        # 已没有代码写入的 recovery 阶段：读取时丢弃，下次保存即从文件中清除
        self.state.pop("recovery", None)
        for stage in STAGES:
            # 早于记录首次失败时间的条目：已知最早的失败就是最后一次失败
            for value in self.state.setdefault(stage, {}).values():
                value.setdefault("first_failed_at", value["last_failed_at"])

    def save(self):
        atomic_json(self.path, self.state)

    def get(self, stage, url):
        return self.state[stage].get(normalize_source_url(url), {})

    def paused(self, stage, url):
        return self.get(stage, url).get("consecutive_failures", 0) >= MAX_CONSECUTIVE_FAILURES

    def failed(self, stage, url, reason):
        key = normalize_source_url(url)
        previous = self.state[stage].get(key, {})
        failed_at = now()
        self.state[stage][key] = {"url": url,
            "consecutive_failures": previous.get("consecutive_failures", 0) + 1,
            "last_reason": reason, "first_failed_at": previous.get("first_failed_at", failed_at),
            "last_failed_at": failed_at}
        self.save()

    def succeeded(self, stage, url):
        if self.state[stage].pop(normalize_source_url(url), None) is not None:
            self.save()

    def capture_history(self, urls):
        """每个来源的最后成功时间与全部失败记录。"""
        successes, failures = {}, {}
        for directory, failed in (("evidence", False), ("failed_evidence", True)):
            for path in (self.root / directory).glob("*.json"):
                try:
                    item = read_json(path)
                    key = normalize_source_url(item["url"])
                    when = datetime.fromisoformat(item["captured_at"].replace("Z", "+00:00"))
                    if key not in urls or when.tzinfo is None:
                        continue
                    if failed:
                        failures.setdefault(key, []).append((when, item))
                    elif when > successes.get(key, datetime.min.replace(tzinfo=timezone.utc)):
                        successes[key] = when
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        return successes, failures

    def refresh_scrapes(self, configured):
        """Count failed page runs since last success, not the two HTTP attempts."""
        urls = {normalize_source_url(url): url for url in configured}
        successes, failures = self.capture_history(urls)
        states = {}
        for key, attempts in failures.items():
            remaining = [(when, item) for when, item in attempts
                         if when > successes.get(key, datetime.min.replace(tzinfo=timezone.utc))]
            if remaining:
                when, item = max(remaining, key=lambda pair: pair[0])
                states[key] = {"url": urls[key], "consecutive_failures": len(remaining),
                               "last_reason": item.get("error") or "scrape_failed",
                               "first_failed_at": min(pair[0] for pair in remaining).isoformat(),
                               "last_failed_at": when.isoformat()}
        self.state["scrape"] = states
        self.save()

    def paused_sources(self):
        """暂停小时重试的来源（通知用）；它们由每日任务继续抓取重试，不进入审查。"""
        return [{**value, "retry_stage": stage, "hourly_retry_paused": True}
                for stage in STAGES
                for value in self.state[stage].values()
                if value["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES]

    def paused_urls(self):
        return {row["url"] for row in self.paused_sources()}
