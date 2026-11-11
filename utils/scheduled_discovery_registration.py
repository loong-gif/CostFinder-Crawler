from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.gmaps_discovery import discover_city, scraper_client
from utils.city_selection import due_for_rediscovery
from utils.crawl_schedule_state import city_key, load_state, save_state, schedule_lock
from utils.discovery_logic import classify_places
from utils.discovery_register import read_master
from utils.pending_sql import APPLIED_DIR_NAME, REJECTED_DIR_NAME, pending_dir
from utils.pipeline_db import literal, validate_do_block, write_text
from utils.pipeline_run import PROJECT_ROOT
from utils.register_businesses import render_register


def _write_json(path: Path, value: object) -> None:
    staging = path.with_suffix(path.suffix + ".new")
    write_text(staging, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.replace(staging, path)


def _guarded_sql(actions: dict, master: list[dict]) -> str:
    rows = {row["business_id"]: row for row in master}
    affected = {item["business_id"] for kind in ("link", "refresh") for item in actions[kind]}
    guards = ["lock table public.master_business_info in share row exclusive mode;"]
    unlinked = [[row[key] for key in ("business_id", "name", "city", "address", "website")]
                for row in sorted(master, key=lambda row: row["business_id"]) if not row.get("google_place_id")]
    expected = literal(json.dumps(unlinked, ensure_ascii=False))
    guards.append("if (select coalesce(jsonb_agg(jsonb_build_array(business_id, name, city, address, website) "
                  "order by business_id), '[]'::jsonb) from public.master_business_info "
                  f"where coalesce(google_place_id, '') = '') <> {expected}::jsonb then "
                  "raise exception 'stale_unlinked_businesses'; end if;")
    for business_id in sorted(affected):
        row = rows[business_id]
        checks = " and ".join(f"{key} is not distinct from {literal(value)}" for key, value in row.items())
        guards.append(f"if not exists (select 1 from public.master_business_info where {checks}) then "
                      f"raise exception 'stale_business: {business_id}'; end if;")
    for kind in ("insert", "promote", "link"):
        for item in actions[kind]:
            guards.append("if exists (select 1 from public.master_business_info where google_place_id = "
                          f"{literal(item['google_place_id'])}) then raise exception 'stale_place'; end if;")
    sql = render_register(actions).replace("begin\n", "begin\n  " + "\n  ".join(guards) + "\n", 1)
    validate_do_block(sql)
    return sql


def _prepare(batch: Path, record: dict) -> dict:
    now = datetime.fromisoformat(record["observed_at"])
    places = json.loads((batch / "places.json").read_text(encoding="utf-8"))
    master = read_master(batch / "snapshot")
    by_place = {row["google_place_id"]: row for row in master if row.get("google_place_id")}
    touched = {str(row.get("google_place_id") or "").strip() for row in places}
    with schedule_lock():
        previous = load_state("candidates")
        candidates, actions = classify_places(places, by_place, previous, now, unlinked_masters=master)
    record["previous_candidates"] = {key: previous.get(key) for key in touched if key}
    record["candidates"] = {key: value for key, value in candidates.items() if key in touched}
    record["expected"] = {item["google_place_id"]: item.get("business_id")
                          for kind in ("insert", "promote", "link") for item in actions[kind]}
    record["expected_updates"] = {str(item["business_id"]): item["fields"]
                                  for kind in ("link", "refresh") for item in actions[kind]}
    record["expected_inserts"] = {item["google_place_id"]: {key: value for key, value in item.items()
                                   if key != "google_place_id" and value is not None}
                                  for kind in ("insert", "promote") for item in actions[kind]}
    record["place_ids"] = sorted(touched - {""})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    record["pending"] = f"{stamp}_city_registration.sql"
    _write_json(batch / "ambiguous.json", actions["ambiguous"])
    write_text(batch / "registration.sql", _guarded_sql(actions, master))
    save_state("registration", record, batch)
    return record


def _finish(batch: Path, record: dict) -> dict:
    master = read_master(batch / "confirmation")
    by_place = {row["google_place_id"]: row for row in master if row.get("google_place_id")}
    for place_id, expected_id in record["expected"].items():
        actual = by_place.get(place_id)
        if not actual or (expected_id is not None and actual["business_id"] != expected_id):
            raise RuntimeError(f"registration_not_confirmed: {place_id}")
    for place_id, fields in record["expected_inserts"].items():
        if any(by_place[place_id].get(key) != value for key, value in fields.items()):
            raise RuntimeError(f"registration_insert_not_confirmed: {place_id}")
    by_id = {row["business_id"]: row for row in master}
    for business_id, fields in record["expected_updates"].items():
        actual = by_id.get(int(business_id), {})
        if any(actual.get(key) != value for key, value in fields.items()):
            raise RuntimeError(f"registration_update_not_confirmed: {business_id}")
    with schedule_lock():
        candidates = load_state("candidates")
        for key, value in record["candidates"].items():
            if candidates.get(key) not in (record["previous_candidates"].get(key), value):
                raise RuntimeError(f"candidate_changed: {key}")
        cities = load_state("cities")
        entry = cities[city_key(record["city"], record["state"])]
        observed_at = datetime.fromisoformat(record["observed_at"])
        last_discovery = entry.get("last_discovery_at")
        if last_discovery and datetime.fromisoformat(last_discovery) > observed_at:
            raise RuntimeError(f"city_already_rediscovered: {record['city']}")
        candidates.update(record["candidates"])
        save_state("candidates", candidates)
        entry["last_discovery_at"] = observed_at.isoformat()
        entry["next_discovery_at"] = (observed_at + timedelta(days=int(entry["discovery_interval_days"]))).isoformat()
        save_state("cities", cities)
    ids = sorted({by_place[key]["business_id"] for key in record["place_ids"] if key in by_place})
    record["business_ids"] = ids
    record["completed"] = True
    save_state("registration", record, batch)
    return {"status": "ready", "business_ids": ids}


def run_registration(batch: Path) -> dict:
    batch = Path(batch)
    record = load_state("registration", batch)
    if record.get("completed"):
        return {"status": "ready", "business_ids": record["business_ids"]}
    if not record:
        now = datetime.now(timezone.utc)
        with schedule_lock():
            cities = load_state("cities")
            due = due_for_rediscovery(cities, now)
        if not due:
            return {"status": "idle"}
        entry = cities[due[0]]
        record = {"city": entry["city"], "state": entry["state"], "observed_at": now.isoformat()}
        batch.mkdir(parents=True, exist_ok=True)
        save_state("registration", record, batch)
    if not (batch / "places.json").exists():
        args = argparse.Namespace(keyword=None, depth=1, max_time=900, wait_timeout=None)
        places = discover_city(scraper_client(), record["city"], record["state"], args)
        _write_json(batch / "places.json", places)
    root = pending_dir(PROJECT_ROOT / "state")
    if record.get("pending") and (root / APPLIED_DIR_NAME / record["pending"]).is_file():
        return _finish(batch, record)
    if not record.get("pending") or (root / REJECTED_DIR_NAME / record["pending"]).is_file():
        record = _prepare(batch, record)
    root.mkdir(parents=True, exist_ok=True)
    target = root / record["pending"]
    if not target.exists():
        staging = root / ("." + record["pending"])
        write_text(staging, (batch / "registration.sql").read_text(encoding="utf-8"))
        os.replace(staging, target)
    return {"status": "waiting"}
