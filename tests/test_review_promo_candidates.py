from __future__ import annotations

import json

from scripts.review_promo_candidates import load_pages


def test_earlier_discovery_results_follow_the_current_discovery_rules(tmp_path):
    discovered = tmp_path / "discovered.json"
    discovered.write_text(json.dumps({"businesses": [{"business_id": 7, "pages": [
        {"url": "https://oubeauty.com//specials", "kind": "promotion"},
        {"url": "https://laseraway.com/offer-terms", "kind": "promotion"},
    ]}]}))
    assert [(p["business_id"], p["url"]) for p in load_pages([discovered])] == [(7, "https://oubeauty.com/specials")]
