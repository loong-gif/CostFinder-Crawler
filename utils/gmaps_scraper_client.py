"""Client for the gosom/google-maps-scraper web API (``/api/v1/jobs``) and CSV-to-place mapping."""
from __future__ import annotations

import csv
import io
import json
import re
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import requests

DEFAULT_KEYWORD_TEMPLATES = ("med spa in {city}, {state}", "aesthetic clinic in {city}, {state}", "botox in {city}, {state}")
_STATE_ZIP = re.compile(r",\s*([A-Z]{2})\s+\d{5}")


class GmapsJobError(RuntimeError):
    pass


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def rows_to_places(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Map scraper CSV rows to place dicts, one per ``place_id`` (first row wins); rows without one are dropped."""
    places: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        place_id = str(row.get("place_id") or "").strip()
        if not place_id or place_id in places:
            continue
        try:
            complete = json.loads(row.get("complete_address") or "{}")
        except ValueError:
            complete = {}
        address = str(row.get("address") or "").strip()
        state_match = _STATE_ZIP.search(address)
        places[place_id] = {
            "google_place_id": place_id,
            "name": str(row.get("title") or "").strip() or None,
            "address": address or None,
            "city": (complete.get("city") or "").strip() or None,
            "state": state_match.group(1) if state_match else (complete.get("state") or None),
            "review_count": _to_int(row.get("review_count")),
            "score": _to_float(row.get("review_rating")),
            "category": str(row.get("category") or "").strip() or None,
            "website": str(row.get("website") or "").strip() or None,
            "phone": str(row.get("phone") or "").strip() or None,
        }
    return list(places.values())


def parse_csv(text: str) -> List[Dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text, newline="")))


class GmapsClient:
    def __init__(self, base_url: str, *, proxies: Sequence[str] = (), session: Optional[requests.Session] = None):
        self.base_url = base_url.rstrip("/") + "/api/v1"
        self.proxies = list(proxies)
        self.session = session or requests.Session()
        self.session.trust_env = False  # never inherit a local HTTP(S)_PROXY for the scraper API itself

    def create_job(
        self, name: str, keywords: Sequence[str], *, lang: str = "en", depth: int = 1, max_time: int = 600,
        zoom: Optional[int] = None,
    ) -> str:
        body: Dict[str, Any] = {"name": name, "keywords": list(keywords), "lang": lang, "depth": depth, "max_time": max_time}
        if zoom is not None:
            body["zoom"] = zoom
        if self.proxies:
            body["proxies"] = self.proxies
        response = self.session.post(f"{self.base_url}/jobs", json=body, timeout=30)
        response.raise_for_status()
        return response.json()["id"]

    def wait(self, job_id: str, *, timeout: float = 1800, poll: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while True:
            response = self.session.get(f"{self.base_url}/jobs/{job_id}", timeout=30)
            response.raise_for_status()
            status = str(response.json().get("Status") or "").lower()
            if status == "ok":
                return
            if status == "failed":
                raise GmapsJobError(f"job {job_id} failed")
            if time.monotonic() > deadline:
                raise GmapsJobError(f"job {job_id} still {status!r} after {timeout}s")
            time.sleep(poll)

    def download(self, job_id: str) -> str:
        response = self.session.get(f"{self.base_url}/jobs/{job_id}/download", timeout=120)
        response.raise_for_status()
        return response.text

    def discover(self, name: str, keywords: Sequence[str], **job_options: Any) -> List[Dict[str, Any]]:
        poll = job_options.pop("poll", 20)
        wait_timeout = job_options.pop("wait_timeout", 1800)
        job_id = self.create_job(name, keywords, **job_options)
        self.wait(job_id, timeout=wait_timeout, poll=poll)
        return rows_to_places(parse_csv(self.download(job_id)))
