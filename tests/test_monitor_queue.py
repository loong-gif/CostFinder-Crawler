import pytest

from utils.monitor_price_store import atomic_json
from utils.monitor_queue import BUCKET_SIZE, build_config, config_kinds, config_urls, queue_kinds


def _snapshot(promotions=(), memberships=()):
    return {"promotions": [{"source_url": url} for url in promotions],
            "memberships": [{"source_url": url} for url in memberships]}


def test_queue_comes_from_both_tables_and_a_shared_page_counts_as_promotion(tmp_path):
    snapshot = _snapshot(["https://a.test/specials", "https://www.b.test/promos/"],
                         ["https://b.test/promos", "https://c.test/membership"])
    assert queue_kinds(snapshot) == {"a.test/specials": "promotion", "b.test/promos": "promotion",
                                     "c.test/membership": "membership"}
    config = build_config(snapshot, tmp_path)
    kinds = config_kinds(config)
    assert sorted(kinds.values()) == ["membership", "promotion", "promotion"]
    assert len(config_urls(config)) == 3
    membership = [m for m in config["monitors"].values() if m["kind"] == "membership"][0]
    assert membership["scan_images"] is False
    assert all(m["scan_images"] for m in config["monitors"].values() if m["kind"] == "promotion")


def test_rows_without_a_usable_url_are_ignored_and_an_empty_queue_fails(tmp_path):
    assert queue_kinds(_snapshot(["", "not a url", "ftp://a.test/x"])) == {}
    with pytest.raises(ValueError, match="empty"):
        build_config(_snapshot(["not a url"]), tmp_path)


def test_pages_keep_their_bucket_and_new_pages_fill_gaps_before_opening_a_bucket(tmp_path):
    atomic_json(tmp_path / "daily_01.json", {"pages": {"https://a.test/old": {}}})
    atomic_json(tmp_path / "daily_02.json", {"pages": {f"https://b.test/{i}": {} for i in range(BUCKET_SIZE)}})
    urls = ["https://a.test/old", "https://b.test/3", "https://new.test/page"]
    config = build_config(_snapshot(urls), tmp_path)
    placed = {name: m["targets"][0]["urls"] for name, m in config["monitors"].items()}
    assert placed["daily_01"] == ["https://a.test/old", "https://new.test/page"]
    assert placed["daily_02"] == ["https://b.test/3"]


def test_a_full_bucket_opens_the_next_numbered_bucket(tmp_path):
    urls = [f"https://site{i:03d}.test/specials" for i in range(BUCKET_SIZE + 1)]
    config = build_config(_snapshot(urls), tmp_path)
    assert sorted(config["monitors"]) == ["daily_01", "daily_02"]
    assert len(config["monitors"]["daily_01"]["targets"][0]["urls"]) == BUCKET_SIZE
    # 重复构建得到同一分桶
    atomic_json(tmp_path / "daily_01.json", {"pages": {u: {} for u in config["monitors"]["daily_01"]["targets"][0]["urls"]}})
    assert build_config(_snapshot(urls), tmp_path) == config


def test_the_url_spelling_recorded_in_state_is_kept_so_the_baseline_still_matches(tmp_path):
    atomic_json(tmp_path / "daily_01.json", {"pages": {"https://www.a.test/specials/": {}}})
    config = build_config(_snapshot(["http://a.test/specials"]), tmp_path)
    assert config_urls(config) == ["https://www.a.test/specials/"]
