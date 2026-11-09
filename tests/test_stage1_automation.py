"""阶段 1 自动化：读库走 read-sql + read-snapshot，调度自动补种，城市到期自动重新发现，登记幂等，试点数由代码统计。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pglast
import pytest

from scripts import crawl_scheduler, gmaps_discovery, process_discovery_results, select_next_city
from utils.crawl_schedule_state import load_state, save_state
from utils.discovery_logic import classify_places
from utils.register_businesses import render_register

NOW = datetime.now(timezone.utc)


def snapshot_file(path: Path, **tables) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({name: {"count": len(rows), "rows": rows} for name, rows in tables.items()}))
    return path


def catalogue(path: Path) -> Path:
    return snapshot_file(
        path,
        businesses=[{"business_id": 1, "website": "a.test", "city": "Reno", "address": "1 Main St, Reno, NV 89501"},
                    {"business_id": 2, "website": "b.test", "city": "Reno", "address": "2 Main St, Reno, NV 89501"}],
        promotions=[{"promotion_id": 3, "business_id": 1, "source_url": "https://a.test/specials",
                     "updated_at": "2026-11-01T00:00:00+00:00", "is_active": True}],
        services=[], memberships=[])


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("COSTFINDER_CRAWL_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


def run(monkeypatch, module, *args):
    monkeypatch.setattr("sys.argv", ["cmd", *args])
    return module.main()


def test_plan_seeds_newly_registered_businesses_and_cities(tmp_path, monkeypatch, capsys):
    save_state("schedule", {"1": {"tier": "A", "interval_days": 15, "next_crawl_at": (NOW + timedelta(days=3)).isoformat(),
                                  "consecutive_failures": 0}})
    assert run(monkeypatch, crawl_scheduler, "plan", "--snapshot", str(catalogue(tmp_path / "s.json")), "--apply") == 0
    schedule = load_state("schedule")
    assert set(schedule) == {"1", "2"} and schedule["2"]["tier"] == "B"
    assert load_state("cities")["Reno|NV"]["status"] == "active"
    assert "cities due for re-discovery: 1" in capsys.readouterr().out


def test_plan_reads_the_catalogue_with_read_snapshot(tmp_path, monkeypatch):
    queries = []
    def read_snapshot(read_sql):
        queries.append(Path(read_sql).read_text())
        return catalogue(Path(read_sql).parent / "snapshot.json")
    monkeypatch.setattr("utils.pipeline_db.read_snapshot", read_snapshot)
    monkeypatch.setattr(crawl_scheduler, "OUTPUT_DIR", tmp_path / "runs")
    assert run(monkeypatch, crawl_scheduler, "plan") == 0
    assert "public.master_business_info" in queries[0] and "public.clinic_memberships" in queries[0]


def test_register_renders_every_action_and_repeated_places_are_skipped_in_sql():
    places = [{"google_place_id": "P1", "name": "Spa", "city": "Reno", "review_count": 90, "score": 4.8,
               "website": "https://spa.test"}]
    _, actions = classify_places(places, {}, {}, NOW)
    sql = render_register({**actions, "promote": actions["insert"]})
    pglast.parse_sql(sql)
    assert sql.count("insert into public.master_business_info") == 1
    assert "where not exists (select 1 from public.master_business_info where google_place_id = 'P1')" in sql
    assert "raise exception" not in sql


def test_register_command_writes_sql_candidates_and_moves_the_city_date(tmp_path, monkeypatch):
    save_state("cities", {"Reno|NV": {"city": "Reno", "state": "NV", "status": "active", "discovery_interval_days": 30,
                                      "next_discovery_at": NOW.isoformat(), "last_discovery_at": None}})
    places = tmp_path / "places.json"
    places.write_text(json.dumps([{"google_place_id": "P1", "name": "Spa", "city": "Reno", "review_count": 90,
                                   "score": 4.8, "website": "https://spa.test"}]))
    master = snapshot_file(tmp_path / "m.json", master=[])
    monkeypatch.setattr(process_discovery_results, "OUTPUT_DIR", tmp_path / "runs")
    assert run(monkeypatch, process_discovery_results, "--input", str(places), "--city", "Reno", "--state", "NV",
               "--snapshot", str(master)) == 0
    apply_sql = next((tmp_path / "runs").glob("*/apply.sql")).read_text()
    assert "'P1'" in apply_sql
    assert load_state("candidates")["P1"]["passed_filter"] is True
    assert datetime.fromisoformat(load_state("cities")["Reno|NV"]["next_discovery_at"]) > NOW + timedelta(days=29)


def test_shared_website_is_matched_by_street_and_unresolved_places_wait_for_the_next_round():
    masters = [{"business_id": 7, "name": "Laser A", "city": "Reno", "website": "https://chain.test", "address": "10 Oak St, Reno"},
               {"business_id": 8, "name": "Laser B", "city": "Sparks", "website": "https://chain.test", "address": "5 Elm Ave, Sparks"}]
    places = [{"google_place_id": "X", "name": "Chain Laser", "city": "Reno", "address": "10 Oak St, Reno, NV 89501",
               "review_count": 90, "score": 4.8, "website": "https://chain.test/reno"},
              {"google_place_id": "Y", "name": "Chain Laser", "city": "Carson", "address": "9 Pine Rd, Carson",
               "review_count": 90, "score": 4.8, "website": "https://chain.test/carson"}]
    candidates, actions = classify_places(places, {}, {}, NOW, unlinked_masters=masters)
    assert [(a["business_id"], a["matched_by"]) for a in actions["link"]] == [(7, "host_address")]
    assert actions["ambiguous"][0]["google_place_id"] == "Y" and actions["ambiguous"][0]["business_ids"] == [8]
    assert candidates["Y"]["match_status"] == "ambiguous" and not actions["insert"]


def test_pilot_counts_qualifying_places_itself(monkeypatch, capsys):
    save_state("cities", {"Reno|NV": {"city": "Reno", "state": "NV", "status": "onboarding", "failed_pilots": 0}})
    save_state("candidates", {f"P{i}": {"city": "Reno", "passed_filter": True, "website": "https://x.test"} for i in range(15)}
               | {"Q": {"city": "Reno", "passed_filter": True, "website": ""}})
    assert run(monkeypatch, select_next_city, "pilot", "Reno", "NV") == 0
    assert load_state("cities")["Reno|NV"]["status"] == "active" and "qualifying=15" in capsys.readouterr().out


def test_pick_marks_the_city_in_one_step_with_demand_from_the_snapshot(tmp_path, monkeypatch):
    save_state("cities", {"Reno|NV": {"city": "Reno", "state": "NV", "status": "candidate"},
                          "Boise|ID": {"city": "Boise", "state": "ID", "status": "candidate"}})
    demand = snapshot_file(tmp_path / "d.json", profiles=[{"id": "u1", "location_city": "Boise"}])
    assert run(monkeypatch, select_next_city, "pick", "--snapshot", str(demand)) == 0
    assert load_state("cities")["Boise|ID"]["status"] == "onboarding"


def test_due_cities_are_rediscovered_and_registered_without_operator_input(tmp_path, monkeypatch):
    save_state("cities", {
        "Reno|NV": {"city": "Reno", "state": "NV", "status": "active", "discovery_interval_days": 30,
                    "next_discovery_at": (NOW - timedelta(days=1)).isoformat(), "last_discovery_at": None},
        "Boise|ID": {"city": "Boise", "state": "ID", "status": "active", "discovery_interval_days": 30,
                     "next_discovery_at": (NOW + timedelta(days=9)).isoformat(), "last_discovery_at": NOW.isoformat()}})
    searched = []
    class Client:
        def __init__(self, *a, **k): pass
        def discover(self, name, keywords, **kwargs):
            searched.append(name)
            return [{"google_place_id": "P1", "name": "Spa", "city": "Reno", "review_count": 90, "score": 4.8,
                     "website": "https://spa.test"}]
    monkeypatch.setattr(gmaps_discovery, "GmapsClient", Client)
    monkeypatch.setenv("GMAPS_SCRAPER_URL", "http://scraper.test")
    monkeypatch.setattr(gmaps_discovery, "OUTPUT_DIR", tmp_path / "runs")
    monkeypatch.setattr("utils.pipeline_db.read_snapshot",
                        lambda read_sql: snapshot_file(Path(read_sql).parent / "snapshot.json", master=[]))
    assert run(monkeypatch, gmaps_discovery, "--due") == 0
    assert searched == ["costfinder-Reno-NV"]
    assert next((tmp_path / "runs").glob("*/Reno_NV/apply.sql")).is_file()
    assert datetime.fromisoformat(load_state("cities")["Reno|NV"]["next_discovery_at"]) > NOW
