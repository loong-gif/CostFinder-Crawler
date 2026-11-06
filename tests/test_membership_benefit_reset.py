from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from utils.membership_benefit_reset import build_plan, render_apply

AT = datetime(2026, 10, 28, tzinfo=timezone.utc)
SNAPSHOT = {"memberships": [
    {"plan_id": 7, "membership_name": "VIP", "source_url": "https://a.com/m", "benefits": ["Sign-up gift listed on page"],
     "is_offer_extracted": False, "updated_at": AT},
    {"plan_id": 8, "membership_name": "Gold", "source_url": "https://a.com/m", "benefits": None,
     "is_offer_extracted": False, "updated_at": None},
    {"plan_id": 9, "membership_name": "Old", "source_url": "https://a.com/m", "benefits": ["10% off"],
     "is_offer_extracted": False, "updated_at": None},
]}


def test_plan_clears_listed_rows_and_skips_rows_already_unknown():
    plan = build_plan(SNAPSHOT, {7: "benefits_not_visible", 8: "benefits_not_visible", 9: "benefits_not_visible"})
    assert [e["plan_id"] for e in plan["entries"]] == [7, 9] and plan["already_null"] == [8]
    with pytest.raises(RuntimeError, match="unknown_plan_ids"):
        build_plan(SNAPSHOT, {10: "benefits_not_visible"})


def test_apply_checks_the_row_is_unchanged_before_clearing_benefits():
    plan = json.loads(json.dumps(build_plan(SNAPSHOT, {7: "x", 9: "x"}), default=str))
    text = render_apply(plan)
    assert text.count("do $pipeline$") == 1
    first = text.index("where plan_id = 7 for update")
    assert first < text.index("raise exception 'stale_plan: clinic_memberships 7'") < text.index(
        "set benefits = null, is_offer_extracted = false, updated_at = now() where plan_id = 7")
    # updated_at 为 NULL 的旧行按 NULL 比较
    assert "v_updated is distinct from NULL::timestamptz" in text
