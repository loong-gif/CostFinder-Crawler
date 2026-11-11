"""File-backed crawl scheduling state (no database schema is involved).

Files live under ``state/crawl_schedule/`` (override with ``COSTFINDER_CRAWL_STATE_DIR``):
``cities.json`` keyed by ``"<city>|<state>"`` and ``schedule.json`` keyed by ``business_id``.
"""
from __future__ import annotations

import json
import fcntl
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict

DEFAULT_STATE_DIR = Path(__file__).resolve().parents[1] / "state" / "crawl_schedule"

TIER_INTERVAL_DAYS = {"A": 15, "B": 30, "C": 90}
LARGE_CITY_MIN_BUSINESSES = 25
SMALL_CITY_MAX_BUSINESSES = 4


def state_dir() -> Path:
    override = (os.getenv("COSTFINDER_CRAWL_STATE_DIR") or "").strip()
    return Path(override) if override else DEFAULT_STATE_DIR


@contextmanager
def schedule_lock(directory: Path | None = None):
    target = directory or state_dir()
    target.mkdir(parents=True, exist_ok=True)
    with (target / ".lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def load_state(name: str, directory: Path | None = None) -> Dict[str, Any]:
    path = (directory or state_dir()) / f"{name}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(name: str, data: Dict[str, Any], directory: Path | None = None) -> Path:
    target_dir = directory or state_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{name}.json"
    fd, tmp_name = tempfile.mkstemp(dir=target_dir, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path


def city_key(city: str, state: str) -> str:
    return f"{city.strip()}|{state.strip()}"


def discovery_interval_days(business_count: int) -> int:
    """14 days for large cities, 60 for the long tail, 30 otherwise."""
    if business_count >= LARGE_CITY_MIN_BUSINESSES:
        return 14
    if business_count <= SMALL_CITY_MAX_BUSINESSES:
        return 60
    return 30
