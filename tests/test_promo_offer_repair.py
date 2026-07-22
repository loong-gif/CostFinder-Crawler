"""Tests for utils.promo_offer_repair."""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.promo_offer_repair import (
    build_business_lookup,
    plan_membership_repairs,
    plan_p0_repairs,
    resolve_business_id,
    should_swap_prices,
)


def test_should_swap_prices_when_text_inverted():
    offer = {
        "regular_price": 12,
        "discount_price": 15,
        "offer_raw_text": "Regular price $15 Sale price $12",
    }
    assert should_swap_prices(offer)


def test_p0_ends_expired_active():
    actions = plan_p0_repairs(
        [{"id": 1, "is_active": True, "end_date": "2020-01-01"}],
        today=date(2026, 7, 15),
    )
    assert actions and actions[0]["fields"]["is_active"] is False


def test_resolve_business_id_unique_domain():
    url_map, dom_map = build_business_lookup(
        [{"business_id": 9, "website": "https://clinic.example.com"}],
        [],
    )
    bid, method = resolve_business_id(
        {"source_url": "https://clinic.example.com/pricing", "source_name": "clinic.example.com"},
        url_map=url_map,
        dom_map=dom_map,
    )
    assert bid == 9
    assert method == "unique_domain"


def test_membership_plan_candidates():
    actions = plan_membership_repairs(
        [
            {
                "id": 10,
                "service_name": "Membership",
                "offer_raw_text": "VIP $199/month",
                "discount_price": 199,
                "membership_plan_id": None,
            }
        ]
    )
    assert len(actions) == 1
    assert actions[0]["action"] in {"migrate_plan_then_delete_offer", "delete"}
