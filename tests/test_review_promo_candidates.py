from __future__ import annotations

import json
import sys

from scripts.review_promo_candidates import load_pages, main
from utils import discovery_snapshot
from utils.discovery_batch import decisions_path, discovered_path


def test_earlier_discovery_results_follow_the_current_discovery_rules(tmp_path):
    discovered = tmp_path / "discovered.json"
    discovered.write_text(json.dumps({"businesses": [{"business_id": 7, "pages": [
        {"url": "https://oubeauty.com//specials", "kind": "promotion"},
        {"url": "https://laseraway.com/offer-terms", "kind": "promotion"},
    ]}]}))
    assert [(p["business_id"], p["url"]) for p in load_pages(discovered)] == [(7, "https://oubeauty.com/specials")]


def test_review_reads_the_batch_discovery_and_writes_the_decisions_every_later_stage_reads(tmp_path, monkeypatch):
    discovered_path(tmp_path).write_text(json.dumps({"businesses": [{"business_id": 7, "pages": [
        {"url": "https://oubeauty.com/specials", "status": "valid", "kind": "promotion",
         "evidence": ["Botox $9 per unit this month", "20% off filler"]}]}]}))

    def read(read_sql):
        tables = {"businesses": [{"business_id": 7, "name": "OU", "city": "LA", "review_count": 200, "score": 4.9,
                                  "website": "https://oubeauty.com"}],
                  "promotions": [], "memberships": [], "services": [], "offers": []}
        path = read_sql.parent / "snapshot.json"
        path.write_text(json.dumps({name: {"count": len(rows), "rows": rows} for name, rows in tables.items()}))
        return path

    monkeypatch.setattr(discovery_snapshot, "read_snapshot", read)
    monkeypatch.setattr(sys, "argv", ["review-pages", "--batch-dir", str(tmp_path), "--today", "2026-11-09"])
    assert main() == 0
    decisions = json.loads(decisions_path(tmp_path).read_text())["decisions"]
    assert [(d["business_id"], d["url"], d["decision"]) for d in decisions] == [(7, "https://oubeauty.com/specials", "add")]
