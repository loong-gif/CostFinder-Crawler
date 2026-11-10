from utils.item_name_rename import build_plan, render_apply, rename_problem
from utils.offer_scope_filter import item_name_problem


class FakeClient:
    model = "fake"

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def create_json_response(self, messages, *, json_schema=None):
        self.calls += 1
        return {"items": self.answers.pop(0)}


def row(item_id, offer_id, name, raw):
    return {"offer_item_id": item_id, "offer_id": offer_id, "item_name": name, "service_id": None, "offer_raw_text": raw}


def test_item_name_problem_covers_each_defect():
    assert item_name_problem("Botox") == ""
    assert item_name_problem("") == "empty"
    assert item_name_problem("x" * 61) == "too_long"
    assert item_name_problem("Filler $75 off") == "price_or_discount_words"
    assert item_name_problem("member events") == "member_event_or_gift_card"
    assert item_name_problem("services (including injectables") == "unbalanced_parenthesis"
    assert item_name_problem("and lab work") == "fragment_start"
    assert item_name_problem("Botox;") == "trailing_punctuation"


def test_rename_requires_words_from_the_offer_text_and_a_specific_name(tmp_path):
    raw = "Botox $13/unit with senior injectors"
    assert rename_problem("Botox", raw) == ""
    assert rename_problem("Botox and Filler", raw) == "multiple_services"
    assert rename_problem("Dysport", raw) == "not_in_offer_text"
    assert rename_problem("Units", "120 Units at only $3.50 per unit") == "generic_name"


def test_plan_renames_valid_answers_and_retries_invalid_ones_once(tmp_path):
    raw = "Dermal filler $75 off"
    snapshot = {"items": [row(1, 10, "Dermal filler $75 off", raw)]}
    client = FakeClient([[{"offer_item_id": 1, "item_name": "Dermal filler $75"}],
                         [{"offer_item_id": 1, "item_name": "Dermal filler"}]])
    plan = build_plan(snapshot, client, tmp_path)
    assert client.calls == 2 and plan["skipped"] == []
    assert plan["entries"] == [{"offer_item_id": 1, "offer_id": 10, "old_name": "Dermal filler $75 off",
                                "new_name": "Dermal filler"}]


def test_plan_skips_blanket_offers_and_keeps_names_that_stay_invalid(tmp_path):
    blanket = "15% off all products"
    raw = "10% off tox, fillers, biostimulators, lasers, nonprescription retail, add-ons and more extras"
    snapshot = {"items": [row(1, 10, blanket, blanket), row(2, 11, raw, raw)]}
    too_long = raw.replace("10% off ", "")
    client = FakeClient([[{"offer_item_id": 2, "item_name": too_long}], [{"offer_item_id": 2, "item_name": too_long}]])
    plan = build_plan(snapshot, client, tmp_path)
    assert plan["entries"] == []
    assert {s["offer_item_id"]: s["reason"] for s in plan["skipped"]} == {1: "blanket_offer", 2: "too_long"}


def test_apply_checks_the_old_name_and_row_count():
    plan = {"entries": [{"offer_item_id": 7, "offer_id": 3, "old_name": "Filler $75 off", "new_name": "Filler"}],
            "skipped": []}
    sql = render_apply(plan)
    assert "item_name = 'Filler' where offer_item_id = 7 and item_name = 'Filler $75 off'" in sql
    assert "get diagnostics v_rows = row_count;" in sql and "stale_plan: promo_offer_items 7" in sql
