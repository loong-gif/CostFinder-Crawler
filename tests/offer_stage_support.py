from __future__ import annotations

from datetime import datetime, timezone

from utils.offer_stage_extract import candidates_from_payload
from utils.offer_stage_spec import PROMOTIONS

ENUM = ["Botox", "Dysport", "Dermal Filler", "Others"]
ENUM_ROWS = [{"enumlabel": name, "enumsortorder": float(i)} for i, name in reversed(list(enumerate(ENUM)))]
NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)
CONTENT = "Botox special $10/unit"


def llm_offer(raw, **overrides):
    offer = {"purchase_type": "single", "is_package": False, "price_model": "per_unit", "regular_price": None,
             "discount_price": 10, "discount_percent": None, "discount_amount": None, "min_quantity": None,
             "is_membership_required": False, "is_new_customer_required": False, "conditions": [],
             "offer_raw_text": raw,
             "items": [{"item_name": "Botox", "service_name": "Botox", "quantity": None, "unit_price": 10,
                        "unit_type": "unit", "service_area": None}]}
    offer.update(overrides)
    return offer


def promo_row(lines, promotion_id=7):
    return {"promotion_id": promotion_id, "business_id": 3, "source_url": "https://a.test/specials",
            "promotion_title": "Specials", "promotion_content": lines, "is_active": True, "is_offer_extracted": False,
            "updated_at": NOW}


def existing_offer(offer_id, raw, *, active=True, promotion_id=7, plan_id=None, **extra):
    row = {"id": offer_id, "business_id": 3, "promotion_id": promotion_id, "membership_plan_id": plan_id,
           "is_active": active, "updated_at": NOW, "is_new_customer_required": False,
           "is_membership_required": False, "conditions": [], "offer_raw_text": raw, "price_model": "per_unit",
           "regular_price": None, "discount_price": 10, "discount_percent": None, "discount_amount": None,
           "is_package": False}
    row.update(extra)
    return row


def existing_item(item_id, offer_id, name="Botox", service_id=None, units=None, price=10):
    return {"offer_item_id": item_id, "offer_id": offer_id, "item_name": name, "service_id": service_id,
            "delivered_units": units, "unit_price": price}


def one_candidate(raw="Botox special $10/unit", **kw):
    row = promo_row([raw])
    candidates, _ = candidates_from_payload({"offers": [llm_offer(raw, **kw)]}, PROMOTIONS, row, raw)
    return candidates


class FakeLLM:
    model = "fake"

    def __init__(self, offers):
        self.offers, self.calls, self.schemas = offers, 0, []

    def create_json_response(self, messages, *, json_schema=None):
        self.calls += 1
        self.schemas.append(json_schema)
        return {"explanation": "", "offers": self.offers}
