from __future__ import annotations

import json

import pytest

from utils import pipeline_db
from utils.discovery_batch import snapshot_dir
from utils.discovery_snapshot import business_contexts, business_rows, known_urls, read_discovery_snapshot
from utils.pipeline_db import SnapshotIncomplete

TABLES = {
    "businesses": [{"business_id": 2, "name": "B", "city": "LA", "review_count": 80, "score": 4.5, "website": "https://b.com"},
                   {"business_id": 1, "name": "A", "city": "LA", "review_count": 90, "score": 4.8, "website": "https://a.com"},
                   {"business_id": 3, "name": "C", "city": "LA", "review_count": 70, "score": 4.1, "website": " "}],
    "promotions": [{"promotion_id": 10, "business_id": 1, "is_active": False, "source_url": "https://a.com//specials"}],
    "memberships": [{"plan_id": 5, "business_id": 2, "source_url": "https://b.com/vip"}],
    "services": [{"service_id": 7, "business_id": 3, "source_url": None}],
    "offers": [],
}


def _reader(tables):
    def read(read_sql):
        # 模拟 Supabase CLI：执行 read.sql，把 snapshot 列写到同目录 snapshot.json
        assert "master_business_info" in read_sql.read_text()
        path = read_sql.parent / "snapshot.json"
        path.write_text(json.dumps({name: {"count": len(rows), "rows": rows} for name, rows in tables.items()}))
        return path
    return read


def test_discovery_reads_the_database_through_an_automatic_read_only_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_db, "read_snapshot", _reader(TABLES))
    snapshot = read_discovery_snapshot(tmp_path)
    assert (snapshot_dir(tmp_path) / "read.sql").exists()
    assert [row["business_id"] for row in business_rows(snapshot)] == [1, 2]
    assert known_urls(snapshot) == {"https://a.com/specials", "https://b.com/vip"}
    contexts = business_contexts(snapshot)
    assert not contexts[1].has_active_offer and not contexts[1].never_processed
    assert contexts[2].has_active_offer
    assert not contexts[3].never_processed  # 已有服务记录


def test_a_truncated_snapshot_stops_the_command(tmp_path, monkeypatch):
    broken = {name: rows for name, rows in TABLES.items() if name != "offers"}
    monkeypatch.setattr(pipeline_db, "read_snapshot", _reader(broken))
    with pytest.raises(SnapshotIncomplete):
        read_discovery_snapshot(tmp_path)
