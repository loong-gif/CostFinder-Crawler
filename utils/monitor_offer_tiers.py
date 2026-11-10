"""商品规格截图中的 offer，以及新老客户、会员与非会员价格档的合并。"""
from __future__ import annotations

import re
from pathlib import Path

from utils.monitor_image_evidence import source_tokens
from utils.rendered_visibility_gate import normalize_claim


def _visible_key(value) -> str:
    """Ignore punctuation layout while retaining money/percent tokens."""
    tokens = source_tokens(normalize_claim(value))
    return " ".join(tokens)


def _variant_quantity_key(labels: list[str]) -> tuple[int, str] | None:
    quantity_match = re.search(r"\b(\d+)\s*(units?|sessions?|syringes?|treatments?)\b", " ".join(labels), re.I)
    if not quantity_match:
        return None
    return int(quantity_match.group(1)), quantity_match.group(2).lower().rstrip("s")


def _variant_membership(labels: list[str]) -> str:
    joined = " ".join(labels)
    if re.search(r"\bnon[- ]?members?\b", joined, re.I):
        return "non_member"
    if re.search(r"\bmembers?\b", joined, re.I):
        return "member"
    return "none"


def _distinct_compare_at(regular, sale, other_sale) -> bool:
    """True when regular is a strikethrough above both purchasable sale prices."""
    if regular is None or sale is None or other_sale is None:
        return False
    regular, sale, other_sale = float(regular), float(sale), float(other_sale)
    return regular > sale + 0.02 and regular > other_sale + 0.02


def _variant_product_key(url) -> str:
    # One Shopify product is captured as /collections/x/products/h and /products/h?variant=id.
    url = str(url or "")
    match = re.search(r"/products/([^/?#]+)", url)
    return match.group(1).lower() if match else url.split("?", 1)[0].split("#", 1)[0]


def visible_variant_offers(browser: dict) -> list[dict]:
    offers, seen = [], set()
    details = browser.get("product_details", []) or [browser]
    captures: list[tuple[dict, list[str]]] = []
    for detail in details:
        for capture in detail.get("variant_captures", []):
            if not capture.get("purchasable") or not Path(capture.get("screenshot_path", "__missing__")).is_file():
                continue
            labels = capture.get("selected_options") or []
            captures.append((capture, labels))
    non_member_sale: dict[tuple[str, tuple[int, str]], float] = {}
    for capture, labels in captures:
        tier = _variant_membership(labels)
        qty_key = _variant_quantity_key(labels)
        price = capture.get("discount_price")
        if tier != "non_member" or qty_key is None or price is None:
            continue
        non_member_sale[(_variant_product_key(capture.get("url")), qty_key)] = float(price)
    for capture, labels in captures:
        quote = str(capture.get("offer_raw_text") or "")
        key = (capture.get("url"), capture.get("variant_id"), tuple(labels), _visible_key(quote))
        if key in seen:
            continue
        seen.add(key)
        tier = _variant_membership(labels)
        quantity_key = _variant_quantity_key(labels)
        quantity = quantity_key[0] if quantity_key else None
        url = _variant_product_key(capture.get("url"))
        price = capture.get("discount_price")
        paired_member = None
        if tier == "non_member" and quantity_key and price is not None:
            for other, other_labels in captures:
                if (_variant_membership(other_labels) == "member"
                        and _variant_quantity_key(other_labels) == quantity_key
                        and _variant_product_key(other.get("url")) == url):
                    paired_member = other
                    break
            # A two-tier page uses the non-member sale as the member offer's regular_price.
            # A labeled strikethrough above both sales is its own tier, so keep both offers.
            if paired_member is not None and not _distinct_compare_at(
                    paired_member.get("regular_price"), paired_member.get("discount_price"), price):
                continue
        membership = tier == "member"
        regular = capture.get("regular_price")
        if price is None:
            continue
        if membership and quantity_key and (url, quantity_key) in non_member_sale:
            non_member = non_member_sale[(url, quantity_key)]
            if not _distinct_compare_at(regular, price, non_member) and (regular is None or regular <= price):
                regular = non_member
        elif regular is not None and regular <= price:
            regular = None
        name = capture.get("title", "")
        options = [label for label in labels if not re.search(r"members?|\b\d+\s*(?:units?|sessions?|syringes?|treatments?)\b", label, re.I)]
        item_name = " ".join([name, *options]).strip()
        quantity_match = re.search(r"\b(\d+)\s*(units?|sessions?|syringes?|treatments?)\b", " ".join(labels), re.I)
        offers.append({"offer_raw_text": quote, "price_model": "total", "regular_price": regular,
                       "discount_price": price, "discount_percent": None, "discount_amount": None, "is_package": False,
                       "is_membership_required": membership, "is_new_customer_required": False,
                       "quantity_kind": "fixed" if quantity is not None else "variable",
                       "conditions": [{"requirement": label, "evidence": label} for label in labels],
                       "items": [{"item_name": item_name, "quantity": quantity, "delivered_units": quantity,
                                  "unit_price": None, "service_area": None,
                                  "unit_type": quantity_match.group(2) if quantity_match else None}],
                       "visibility": "visible_match", "purchase_variant": True,
                       "variant_evidence": capture})
    return offers

def _items_signature(offer: dict) -> tuple:
    return tuple(sorted((_visible_key(i.get("item_name")), i.get("quantity", i.get("delivered_units")))
                        for i in offer.get("items") or []))


def merge_new_customer_tiers(offers: list[dict]) -> list[dict]:
    """Fold returning-patient price into the new-patient offer's regular_price."""
    paired: set[int] = set()
    merged: list[dict] = []
    buckets: dict[tuple, list[dict]] = {}
    for offer in offers:
        if offer.get("is_membership_required"):
            continue
        buckets.setdefault((_items_signature(offer), offer.get("price_model")), []).append(offer)
    for offer in offers:
        if id(offer) in paired:
            continue
        peers = buckets.get((_items_signature(offer), offer.get("price_model")), [])
        if offer.get("is_new_customer_required") and offer.get("discount_price") is not None:
            returning = [o for o in peers if not o.get("is_new_customer_required")
                         and o.get("discount_price") is not None and id(o) not in paired]
            if len(returning) == 1:
                ret_price = returning[0]["discount_price"]
                new_price = offer["discount_price"]
                if ret_price is not None and new_price is not None and ret_price > new_price:
                    offer = {**offer, "regular_price": ret_price}
                    paired.add(id(returning[0]))
        merged.append(offer)
    return [o for o in merged if id(o) not in paired]


def merge_member_tiers(offers: list[dict]) -> list[dict]:
    """Fold a non-member sale into regular_price only when the page has no higher strikethrough."""
    non_member = [o for o in offers if not o.get("is_membership_required")
                  and re.search(r"\bnon[- ]?members?\b", str(o.get("offer_raw_text") or ""), re.I)]
    paired = set()
    merged = []
    for offer in offers:
        if offer.get("is_membership_required") and offer.get("discount_price") is not None:
            matches = [o for o in non_member if _items_signature(o) == _items_signature(offer)
                       and o.get("price_model") == offer.get("price_model")
                       and o.get("discount_price") is not None and o["discount_price"] > offer["discount_price"]]
            # Several candidate non-member prices are ambiguous; leave both offers for review.
            if len(matches) == 1:
                sibling = matches[0]
                sibling_price = sibling["discount_price"]
                if _distinct_compare_at(offer.get("regular_price"), offer.get("discount_price"), sibling_price):
                    if not _distinct_compare_at(sibling.get("regular_price"), sibling_price, offer["discount_price"]):
                        sibling["regular_price"] = offer["regular_price"]
                else:
                    if offer.get("regular_price") is None or offer["regular_price"] <= offer["discount_price"]:
                        offer = {**offer, "regular_price": sibling_price}
                    paired.add(id(sibling))
        merged.append(offer)
    return [o for o in merged if id(o) not in paired]
