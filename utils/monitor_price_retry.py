"""Bound hourly retries per source, using durable files under the monitor lock."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from utils.monitor_price_store import atomic_json, now, read_json
from utils.monitor_price_writer_plan import normalize_source_url

MAX_CONSECUTIVE_FAILURES = 3


class RetryLimits:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "retry_limits.json"
        self.state = read_json(self.path, {"schema_version": 1})
        self.state["max_consecutive_failures"] = MAX_CONSECUTIVE_FAILURES
        for stage in ("scrape", "processing", "recovery"):
            self.state.setdefault(stage, {})

    def save(self):
        atomic_json(self.path, self.state)

    def get(self, stage, url):
        return self.state[stage].get(normalize_source_url(url), {})

    def paused(self, stage, url):
        return self.get(stage, url).get("consecutive_failures", 0) >= MAX_CONSECUTIVE_FAILURES

    def failed(self, stage, url, reason):
        key = normalize_source_url(url)
        previous = self.state[stage].get(key, {})
        self.state[stage][key] = {"url": url,
            "consecutive_failures": previous.get("consecutive_failures", 0) + 1,
            "last_reason": reason, "last_failed_at": now()}
        self.save()

    def succeeded(self, stage, url):
        if self.state[stage].pop(normalize_source_url(url), None) is not None:
            self.save()

    def refresh_scrapes(self, configured):
        """Count failed page runs since last success, not the two HTTP attempts."""
        urls = {normalize_source_url(url): url for url in configured}
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
        states = {}
        for key, attempts in failures.items():
            remaining = [(when, item) for when, item in attempts
                         if when > successes.get(key, datetime.min.replace(tzinfo=timezone.utc))]
            if remaining:
                when, item = max(remaining, key=lambda pair: pair[0])
                states[key] = {"url": urls[key], "consecutive_failures": len(remaining),
                               "last_reason": item.get("error") or "scrape_failed",
                               "last_failed_at": when.isoformat()}
        self.state["scrape"] = states
        self.save()

    def review(self, stage, url):
        return {**self.get(stage, url), "url": url, "status": "review", "retry_stage": stage,
                "category": "retry_limit_reached", "hourly_retry_paused": True,
                "review": [{"reason": "retry_limit_reached", "blocking": True}]}

    def paused_reviews(self):
        return [self.review(stage, value["url"])
                for stage in ("scrape", "processing", "recovery")
                for value in self.state[stage].values()
                if value["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES]
