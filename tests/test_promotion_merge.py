from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from utils import pipeline_db, promotion_merge
from utils.page_title import PageGone, TitleUnavailable
from utils.promotion_rules import today

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def promo(pid, url, *, active, offers_end=None, content=("20% off Botox",), title="Old", updated=T0):
    return {"promotion_id": pid, "business_id": 1, "promotion_title": title, "source_url": url,
            "campaign_start_date": None, "campaign_end_date": offers_end, "is_active": active,
            "promotion_content": list(content), "is_offer_extracted": True, "is_ocr_required": False,
            "screenshot": None, "requires_cf_verification": False, "updated_at": updated}


def plan_with(monkeypatch, promotions, offers=(), fetch_title=lambda url, name: "Specials | Glow"):
    snapshot = {"promotions": list(promotions), "businesses": [{"business_id": 1, "name": "Glow"}],
                "offers": list(offers)}
    return promotion_merge.build_plan(snapshot, fetch_title)


def test_same_page_rows_merge_into_active_keeper(monkeypatch):
    rows = [promo(1, "https://www.glow.com/specials/", active=False),
            promo(2, "https://glow.com/specials", active=True, content=("Save $50 on filler",))]
    offers = [{"id": 10, "promotion_id": 1, "is_active": False}, {"id": 11, "promotion_id": 2, "is_active": True}]
    plan = plan_with(monkeypatch, rows, offers)
    [group] = plan["groups"]
    assert group["keeper_id"] == 2 and group["loser_ids"] == [1] and group["moved_offer_ids"] == [10]
    assert group["new_values"]["promotion_title"] == "Specials | Glow"
    assert group["new_values"]["promotion_content"] == ["Save $50 on filler"]
    assert group["status"] == "ready"


def test_hub_children_create_missing_hub_row(monkeypatch):
    rows = [promo(5, "https://glow.com/specials/botox", active=True),
            promo(6, "https://glow.com/specials/filler", active=False)]
    [group] = plan_with(monkeypatch, rows)["groups"]
    assert group["keeper_id"] is None and group["target_url"] == "https://glow.com/specials"
    assert sorted(group["loser_ids"]) == [5, 6]


def test_single_non_hub_row_is_left_alone(monkeypatch):
    assert plan_with(monkeypatch, [promo(1, "https://glow.com/specials", active=True)])["groups"] == []


def test_title_failure_blocks_group_with_active_rows(monkeypatch):
    rows = [promo(1, "https://glow.com/specials", active=True), promo(2, "https://glow.com/specials/", active=False)]

    def no_title(url, name):
        raise TitleUnavailable("ego_timeout")

    [group] = plan_with(monkeypatch, rows, fetch_title=no_title)["groups"]
    assert group["status"] == "blocked_title" and "ego_timeout" in group["title_error"]


def test_gone_page_merges_the_group_inactive_with_the_existing_title(monkeypatch):
    rows = [promo(1, "https://glow.com/specials", active=True, title="Specials | Glow"),
            promo(2, "https://glow.com/specials/", active=False)]

    def gone(url, name):
        raise PageGone("page_gone")

    [group] = plan_with(monkeypatch, rows, fetch_title=gone)["groups"]
    assert group["status"] == "ready" and group["rule_deactivated"] == "page_gone"
    assert group["new_values"]["is_active"] is False and group["new_values"]["promotion_title"] == "Specials | Glow"
    assert "update public.clinic_promotions set promotion_title='Specials | Glow', is_active=false" in \
        promotion_merge.render_apply(json.loads(promotion_merge.dump_plan(plan_with(monkeypatch, rows,
                                                                                    fetch_title=gone))))


def test_inactive_group_on_unavailable_page_keeps_existing_title(monkeypatch):
    rows = [promo(1, "https://glow.com/specials", active=False, title="Specials | Glow"),
            promo(2, "https://glow.com/specials/", active=False, title="October Specials", updated=T0 + timedelta(1))]

    def no_title(url, name):
        raise TitleUnavailable("http_404")

    [group] = plan_with(monkeypatch, rows, fetch_title=no_title)["groups"]
    assert group["status"] == "ready" and group["keeper_id"] == 2
    assert group["new_values"]["promotion_title"] == "October Specials" and "http_404" in group["title_error"]


def test_merged_row_that_breaks_active_rules_is_written_inactive(monkeypatch):
    expired = today() - timedelta(days=3)
    rows = [promo(1, "https://glow.com/botox-pricing", active=True, offers_end=expired, content=("Botox $12/unit",)),
            promo(2, "https://glow.com/botox-pricing/", active=False)]
    [group] = plan_with(monkeypatch, rows)["groups"]
    assert group["new_values"]["is_active"] is False and group["rule_deactivated"] == "expired_campaign"


def test_apply_sql_checks_rows_then_merges_in_one_do_block(monkeypatch):
    rows = [promo(1, "https://glow.com/specials/", active=False), promo(2, "https://glow.com/specials", active=True)]
    plan = plan_with(monkeypatch, rows, [{"id": 10, "promotion_id": 1, "is_active": False}])
    text = promotion_merge.render_apply(json.loads(promotion_merge.dump_plan(plan)))
    assert text.count("do $pipeline$") == 1 and text.rstrip().endswith("$pipeline$;")
    assert "'2026-10-01T00:00:00+00:00'::timestamptz" in text
    assert "v_keeper := 2;" in text
    assert "is_active = is_active and true" in text and "promotion_id = any('{1}'::int2[]::bigint[])" in text
    assert text.index("raise exception 'stale_plan") < text.index("update public.clinic_promotions")
    assert "delete from public.clinic_promotions" in text


def test_apply_sql_inserts_missing_hub_row(monkeypatch):
    rows = [promo(5, "https://glow.com/specials/botox", active=True, content=("Botox 20% off",))]
    text = promotion_merge.render_apply(plan_with(monkeypatch, rows))
    assert "insert into public.clinic_promotions" in text and "returning promotion_id into v_keeper" in text
    # promotion_id 没有默认序列：锁表后取最大值加一
    assert text.index("lock table public.clinic_promotions") < text.index("insert into public.clinic_promotions")
    assert "(select coalesce(max(promotion_id), 0) + 1 from public.clinic_promotions)" in text
    assert "'https://glow.com/specials'" in text and "'[\"Botox 20% off\"]'::jsonb" in text


def test_snapshot_loader_rejects_truncated_tables(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"promotions": {"count": 2, "rows": [{"promotion_id": 1}]},
                                "businesses": {"count": 0, "rows": []}, "offers": {"count": 0, "rows": []}}))
    with pytest.raises(pipeline_db.SnapshotIncomplete, match="read 1 of 2"):
        pipeline_db.load_snapshot(path, promotion_merge.SNAPSHOT_TABLES)


def test_snapshot_loader_types_dates_and_timestamps(tmp_path):
    path = tmp_path / "snapshot.json"
    row = {"promotion_id": 1, "campaign_end_date": "2026-11-30", "updated_at": "2026-10-01T08:00:00.123456+00:00"}
    path.write_text(json.dumps({"promotions": {"count": 1, "rows": [row]}, "businesses": {"count": 0, "rows": []},
                                "offers": {"count": 0, "rows": []}}))
    loaded = pipeline_db.load_snapshot(path, promotion_merge.SNAPSHOT_TABLES)["promotions"][0]
    assert loaded["campaign_end_date"].isoformat() == "2026-11-30"
    assert loaded["updated_at"].isoformat() == "2026-10-01T08:00:00.123456+00:00"

