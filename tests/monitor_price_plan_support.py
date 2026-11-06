SOURCE = {"business_id": 7, "url": "https://clinic.example/specials"}
RAW = "Botox 50 units Regular $450 Special $350"


def _offer(**changes):
    value = {"offer_raw_text": RAW, "regular_price": 450, "discount_price": 350,
             "discount_percent": None, "discount_amount": None, "price_model": "total",
             "is_package": False, "is_membership_required": False,
             "is_new_customer_required": False, "conditions": [],
             "visibility": "visible_match", "quantity_kind": "fixed",
             "items": [{"item_name": "Botox", "service_id": 12, "delivered_units": 50, "unit_price": 7}]}
    return {**value, **changes}


def _evidence(offers=None, **changes):
    offers = [_offer()] if offers is None else offers
    dates = [v for k, v in changes.items() if k in {"campaign_start_date", "campaign_end_date", "promotion_start_date", "promotion_end_date"} and v]
    return {"url": SOURCE["url"], "accessible": True, "complete": True,
            "markdown": "\n".join([*(o.get("offer_raw_text", "") for o in offers), *dates]), "offers": offers, **changes}


def _graph():
    master = {k: v for k, v in _offer().items() if k not in {"visibility", "quantity_kind", "items"}}
    return {"promotions": [{"promotion_id": 3, "business_id": 7, "source_url": SOURCE["url"],
                            "promotion_title": "Specials", "campaign_start_date": None,
                            "campaign_end_date": None, "promotion_content": [RAW],
                            "is_active": True, "is_offer_extracted": True}],
            "offers": [{**master, "id": 4, "business_id": 7, "promotion_id": 3,
                        "membership_plan_id": None, "is_active": True}],
            "items": [{"offer_item_id": 5, "offer_id": 4, "service_id": 12,
                       "item_name": "Botox", "delivered_units": 50, "unit_price": 7}],
            "services": [{"service_id": 12, "business_id": 7, "service_name": "Botox", "service_area": None}]}


def _operation(plan, table, row_id):
    return next(op for op in plan["operations"] if op["table"] == table and op["id"] == row_id)


def _changed_condition_offer():
    return _offer(offer_raw_text=RAW + ". Not valid with other offers", conditions=["Not valid with other offers"])


def _with_extra_offer(graph):
    graph["services"] += [{"service_id": 13, "business_id": 7, "service_name": "Dysport"}]
    graph["offers"] += [{**graph["offers"][0], "id": 6, "offer_raw_text": "Dysport Special $200"}]
    graph["items"] += [{"offer_item_id": 8, "offer_id": 6, "service_id": 13,
                        "item_name": "Dysport", "delivered_units": None, "unit_price": None}]
    return graph
