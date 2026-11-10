from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from utils.pipeline_db import validate_do_block
from utils.promotion_enforcement import build_plan, render_apply

UPDATED = datetime(2026, 11, 1, tzinfo=timezone.utc)


def promotion(pid, url, content, end=None):
    return {"promotion_id": pid, "business_id": 1, "source_url": url, "promotion_title": "Specials",
            "promotion_content": content, "campaign_end_date": end, "is_active": True, "updated_at": UPDATED}


SNAPSHOT = {
    "promotions": [promotion(1, "https://a.com/specials", ["Botox $10/unit"]),
                   promotion(2, "https://a.com/pricing", ["Botox $12/unit"]),
                   promotion(3, "https://a.com/specials", ["Filler 20% off"], end=date(2020, 1, 1))],
    "offers": [{"id": 10, "promotion_id": 1, "is_active": True}],
}


def test_rule_review_deactivates_price_page_and_expired_campaign():
    plan = build_plan(SNAPSHOT)
    assert [(v["promotion_id"], v["rule"]) for v in plan["violations"]] == [
        (2, "price_page_without_discount"), (3, "expired_campaign")]
    validate_do_block(render_apply(plan))


def test_rule_review_has_no_manual_deactivation_mode():
    with pytest.raises(TypeError):
        build_plan(SNAPSHOT, {1: "offers not visible on rendered page"})
