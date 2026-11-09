"""发现结果登记：读取现有商家，分类地点，渲染 apply.sql，更新候选与城市的发现时间（register-businesses 与 discover-businesses --due 共用）。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from utils.crawl_schedule_state import city_key, load_state, save_state
from utils.discovery_logic import classify_places
from utils.pipeline_db import fresh_snapshot, load_snapshot, write_text
from utils.register_businesses import render_register

MASTER_TABLES = {"master": ("master_business_info",
                            "business_id, name, city, address, website, google_place_id, review_count, score", "true")}


def read_master(run_dir: Path, snapshot: Path | None = None) -> list[dict[str, Any]]:
    return load_snapshot(snapshot or fresh_snapshot(run_dir, MASTER_TABLES), MASTER_TABLES)["master"]


def register_places(places: list[dict[str, Any]], master: list[dict[str, Any]], *, city: str | None, state: str,
                    now: datetime, run_dir: Path) -> Path:
    """写 run_dir/apply.sql（经 MCP 执行）与 ambiguous.json，保存候选状态，推进该城市的下次发现时间。"""
    master_by_place = {row["google_place_id"]: row for row in master if row.get("google_place_id")}
    candidates, actions = classify_places(places, master_by_place, load_state("candidates"), now,
                                          unlinked_masters=master)
    print({name: len(items) for name, items in actions.items()})
    apply_path = write_text(run_dir / "apply.sql", render_register(actions))
    write_text(run_dir / "ambiguous.json", json.dumps(actions["ambiguous"], ensure_ascii=False, indent=2))
    cities = load_state("cities")
    entry = cities.get(city_key(city, state)) if city else None
    if entry:
        entry["last_discovery_at"] = now.isoformat()
        entry["next_discovery_at"] = (now + timedelta(days=int(entry["discovery_interval_days"]))).isoformat()
    save_state("candidates", candidates)
    save_state("cities", cities)
    return apply_path
