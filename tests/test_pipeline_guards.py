from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pglast
import pytest

from utils import pipeline_run
from utils.pipeline_db import do_block
from utils.promotion_rules import (
    DISCOUNT_EVIDENCE_PATTERN,
    PromotionRejected,
    check_promotion,
    has_discount_evidence,
    parse_campaign_date,
    today,
)
from utils.source_url import HUB_SEGMENT_PATTERN, TRACKING_PARAM_PATTERN, hub_url, source_url_key
from utils.supabase_rest import SupabaseRestClient

MIGRATION = Path(__file__).resolve().parents[1] / "supabase/migrations/20261103043251_promotion_identity_guards.sql"


@pytest.mark.parametrize("write", [
    lambda c: c.insert_rows("clinic_promotions", [{"business_id": 1}]),
    lambda c: c.upsert_rows("clinic_promotions", [{"business_id": 1}]),
    lambda c: c.update_row("clinic_promotions", 1, {"is_active": False}),
    lambda c: c.delete_rows("clinic_promotions", {"promotion_id": "eq.1"}),
    lambda c: c.rpc("mark_notification_sent", {}),
])
def test_python_never_writes_the_database_directly(write):
    client = SupabaseRestClient("https://kdlpkjzcnbkjcvwsvlwn.supabase.co", "key")
    with pytest.raises(pipeline_run.DirectDatabaseWrite):
        write(client)


def test_source_url_key_ignores_scheme_www_slash_tracking_and_param_order():
    assert source_url_key("HTTP://WWW.Example.com:443/Specials/?utm_source=x&b=2&a=1#top") == "example.com/specials?a=1&b=2"
    assert source_url_key("example.com/specials") == source_url_key("https://example.com/specials/")


def test_hub_url_uses_shallowest_promotion_segment():
    assert hub_url("https://x.com/specials/new-patient-botox-offer") == "https://x.com/specials"
    assert hub_url("https://x.com/promotions/costa-mesa/costa-mesa-promotions") == "https://x.com/promotions"
    assert hub_url("https://x.com/specials") is None
    assert hub_url("https://x.com/ideal-weight/plan") is None


def test_price_page_without_discount_is_rejected_but_promo_path_passes():
    with pytest.raises(PromotionRejected) as exc:
        check_promotion("https://x.com/body/laser-vaginal-rejuvenation", "Laser Vaginal Rejuvenation Cost",
                        ["The regular price is $800 per session."], is_active=True, campaign_end_date=None)
    assert exc.value.code == "price_page_without_discount"
    check_promotion("https://x.com/specials", "Specials | X", ["Botox $10/unit"], is_active=True, campaign_end_date=None)
    check_promotion("https://x.com/botox", "Botox | X", ["20% off Botox this month"], is_active=True,
                    campaign_end_date=None)


def test_hub_child_expired_and_empty_title_are_rejected():
    for url, title, end, code in [
        ("https://x.com/specials/child", "Child", None, "hub_child_url"),
        ("https://x.com/specials", " ", None, "empty_title"),
        ("https://x.com/specials", "Specials", today() - timedelta(days=1), "expired_campaign"),
    ]:
        with pytest.raises(PromotionRejected) as exc:
            check_promotion(url, title, ["20% off"], is_active=True, campaign_end_date=end)
        assert exc.value.code == code


def test_discount_evidence_patterns():
    assert has_discount_evidence(["Save $50 on fillers"])
    assert has_discount_evidence(["~~$400~~ $300"])
    assert has_discount_evidence(["Buy 2 syringes, get 1 half off"])
    assert not has_discount_evidence(["Botox $12/unit. Free consultation"])
    assert not has_discount_evidence(["Package of 3 sessions priced at $2,100"])


def test_campaign_date_month_day_uses_current_year():
    assert parse_campaign_date("8/31", reference=date(2026, 10, 29)) == date(2026, 8, 31)
    assert parse_campaign_date("1.28.21") == date(2021, 1, 28)
    assert parse_campaign_date("") is None


def test_sql_migration_parses_with_postgres_grammar():
    text = MIGRATION.read_text()
    pglast.parse_sql(text)
    # 每个函数体（含 plpgsql 触发器）按 PostgreSQL 语法解析
    assert len(pglast.parse_plpgsql(text)) == text.count("create or replace function")


def test_do_block_rejects_invalid_plpgsql():
    with pytest.raises(pglast.parser.ParseError):
        do_block(["if true then raise exception 'x'; end;"])


def test_sql_migration_uses_the_same_patterns():
    sql = MIGRATION.read_text()
    assert DISCOUNT_EVIDENCE_PATTERN in sql
    assert TRACKING_PARAM_PATTERN in sql
    assert HUB_SEGMENT_PATTERN in sql
    assert re.search(r"create unique index if not exists uq_clinic_promotions_business_source_key", sql)
