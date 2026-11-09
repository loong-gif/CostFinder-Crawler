"""discover-pages 的站内搜索后端：自托管 /v1/search 的健康状态与 Firecrawl 云端 /v2/search 的额度控制。"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

import requests


class SearchHealth:
    """Tracks whether the search backend answers. An empty result is ambiguous (no hits, or the
    upstream engines are throttled); a canary query decides, and a down backend is not hammered."""

    CANARY = "wikipedia"

    def __init__(self, empty_limit: int = 6, cooldown: float = 90.0) -> None:
        self.lock = threading.Lock()
        self.empty_streak = 0
        self.down = False
        self.skipped = 0
        self.empty_limit = empty_limit
        self.cooldown = cooldown

    def record(self, results: int) -> None:
        with self.lock:
            self.empty_streak = 0 if results else self.empty_streak + 1


class BudgetExhausted(RuntimeError):
    pass


class CloudSearch:
    """Firecrawl cloud ``/v2/search`` with a hard credit budget (each search costs 2 credits, any limit)."""

    COST = 2

    def __init__(self, api_key: str, *, budget: int, reserve: int = 150, interval: float = 15.0,
                 session: Optional[requests.Session] = None, api_url: str = "https://api.firecrawl.dev") -> None:
        self.api_url = api_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.interval = interval  # the plan allows about 4 requests a minute
        self.reserve = reserve
        self.budget = budget
        self.spent = 0
        self.searches = 0
        self._lock = threading.Lock()
        self._last = 0.0

    def remaining_credits(self) -> int:
        for attempt in range(4):
            try:
                r = self.session.get(f"{self.api_url}/v2/team/credit-usage", headers=self.headers, timeout=60)
                r.raise_for_status()
                return int(r.json()["data"]["remainingCredits"])
            except requests.exceptions.SSLError:
                time.sleep(3 * (attempt + 1))
        raise RuntimeError("credit_usage_unavailable")

    def effective_budget(self) -> int:
        """Never spend into the reserve, whatever --credit-budget says."""
        return max(0, min(self.budget, self.remaining_credits() - self.reserve))

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            if self.spent + self.COST > self.budget:
                raise BudgetExhausted(f"spent {self.spent} of {self.budget} credits")
            wait = self.interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            body = {"query": query, "limit": min(limit, 10)}
            response = None
            for attempt in range(5):
                try:
                    response = self.session.post(f"{self.api_url}/v2/search", headers=self.headers, json=body, timeout=90)
                except requests.exceptions.SSLError:
                    time.sleep(3 * (attempt + 1))
                    continue
                if response.status_code == 429:
                    time.sleep(float(response.json().get("retry_after_seconds", 30)) + 2)
                    continue
                break
            self._last = time.monotonic()
            if response is None:
                raise RuntimeError("cloud_search_unreachable")
            if response.status_code == 402:
                self.budget = self.spent  # out of credits: stop spending
                raise BudgetExhausted("insufficient credits")
            response.raise_for_status()
            payload = response.json()
            self.spent += int(payload.get("creditsUsed") or self.COST)
            self.searches += 1
        data = payload.get("data")
        items = data.get("web") if isinstance(data, dict) else data
        return [i for i in items or [] if isinstance(i, dict)]
