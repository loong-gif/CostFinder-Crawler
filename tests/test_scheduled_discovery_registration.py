from utils.scheduled_discovery_registration import run_registration


def test_no_active_city_does_not_create_pending_sql(tmp_path, monkeypatch):
    monkeypatch.setenv("COSTFINDER_CRAWL_STATE_DIR", str(tmp_path / "schedule"))
    batch = tmp_path / "batch"
    assert run_registration(batch) == {"status": "idle"}
    assert not batch.exists()
