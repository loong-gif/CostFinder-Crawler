from __future__ import annotations

from typing import Any

from utils.schema_contract import TABLE_PROMO_OFFER_MASTER
from utils.offer_purchase_semantics import canonical_offer_semantics


def _verify_offer_write(client: Any, offer_id: int, master: dict, items: list[dict]) -> bool:
    rows = client.fetch_rows(TABLE_PROMO_OFFER_MASTER,
        "id,business_id,promotion_id,membership_plan_id,offer_raw_text,is_active,regular_price,discount_price,"
        "discount_amount,discount_percent,price_model,is_package,is_membership_required,is_new_customer_required,conditions,"
        "promo_offer_items(offer_item_id,service_id,item_name,quantity:delivered_units,unit_price)",
        filters={"id": f"eq.{offer_id}"}, limit=1)
    if len(rows) != 1 or rows[0].get("is_active") is not True:
        return False
    expected = dict(master)
    # Trigger arithmetic is permitted only for the source-stated comparable pair.
    regular, discount = expected.get("regular_price"), expected.get("discount_price")
    if regular is not None and discount is not None and regular > discount:
        for field in ("discount_amount", "discount_percent"):
            if expected.get(field) is None:
                expected[field] = rows[0].get(field)
    return canonical_offer_semantics(rows[0], rows[0].get("promo_offer_items") or []) == canonical_offer_semantics(expected, items)
