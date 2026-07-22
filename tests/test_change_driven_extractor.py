import json
from pathlib import Path
from unittest.mock import patch

import pytest

from utils.change_driven_extractor import (
    apply_offer_actions,
    build_change_event_decision_plan,
    build_change_event_payloads,
    build_change_extraction_messages,
    enrich_update_actions_with_diff_prices,
    extract_and_upsert_check_pages,
    extract_diff_payload,
    fetch_candidate_offers,
    persist_change_event_payloads,
    prepare_change_event_insert_rows,
    standardize_offer_service_names,
    validate_offer_actions,
)


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
PROMOTION_ID = 99


@pytest.fixture(autouse=True)
def _mock_promotion_helpers(monkeypatch):
    monkeypatch.setattr(
        "utils.change_driven_extractor.fetch_promotion_by_url",
        lambda client, source_url, **kwargs: {
            "promotion_id": PROMOTION_ID,
            "source_url": source_url,
        },
    )
    monkeypatch.setattr(
        "utils.change_driven_extractor.upsert_promotion",
        lambda client, **kwargs: PROMOTION_ID,
    )
    monkeypatch.setattr(
        "utils.change_driven_extractor.upsert_offer_items",
        lambda client, offer_id, items: items,
    )


class FakeDbClient:
    def __init__(self, rows=None, *, fail_update_ids=None, fail_insert_service_names=None, fail_insert_offer_texts=None):
        self.rows = rows or []
        self.fail_update_ids = set(fail_update_ids or [])
        self.fail_insert_service_names = set(fail_insert_service_names or [])
        self.fail_insert_offer_texts = set(fail_insert_offer_texts or [])
        self.fetch_calls = []
        self.update_calls = []
        self.insert_calls = []
        self.delete_calls = []
        self._next_id = 1000

    def fetch_rows(self, table, select, **kwargs):
        self.fetch_calls.append({"table": table, "select": select, **kwargs})
        rows = list(self.rows)
        filters = kwargs.get("filters") or {}
        for key, expr in filters.items():
            if not isinstance(expr, str) or not expr.startswith("eq."):
                continue
            value = expr[3:]
            if key == "is_active":
                want = value.lower() == "true"
                rows = [row for row in rows if bool(row.get("is_active")) == want]
                continue
            if any(key in row for row in rows):
                rows = [row for row in rows if str(row.get(key, "")) == value]
            elif key == "offer_fingerprint":
                rows = []
        return rows

    def delete_rows(self, table, filters):
        self.delete_calls.append({"table": table, "filters": filters})
        return []

    def update_row(self, table, filters, payload):
        self.update_calls.append({"table": table, "filters": filters, "payload": payload})
        row_id = filters.get("id", "").removeprefix("eq.")
        if row_id in self.fail_update_ids:
            raise RuntimeError(f"boom-update-{row_id}")
        return [{"id": row_id, **payload}]

    def insert_rows(self, table, rows):
        self.insert_calls.append({"table": table, "rows": rows})
        out = []
        for row in rows:
            if row.get("service_name") in self.fail_insert_service_names:
                raise RuntimeError(f"boom-insert-{row['service_name']}")
            if row.get("offer_raw_text") in self.fail_insert_offer_texts:
                raise RuntimeError(f"boom-insert-{row['offer_raw_text']}")
            inserted = dict(row)
            if "id" not in inserted and table == "promo_offer_master":
                inserted["id"] = self._next_id
                self._next_id += 1
            out.append(inserted)
        return out


class FakeLlmClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create_json_response(self, messages, *, json_schema=None):
        self.calls.append(messages)
        return self.response


def test_fetch_candidate_offers_truncates_and_filters_active_query():
    rows = [
        {
            "id": f"id-{idx}",
            "promotion_id": PROMOTION_ID,
            "is_active": True,
            "offer_raw_text": "X" * 250,
            "discount_price": idx,
            "regular_price": idx + 100,
            "promo_offer_items": [{"item_name": f"Service {idx}", "unit_type": "unit"}],
        }
        for idx in range(101)
    ]
    client = FakeDbClient(rows=rows)

    candidates = fetch_candidate_offers(client, "https://example.com/specials")

    assert len(candidates) == 100
    assert candidates[0]["id"] == "id-0"
    assert candidates[0]["candidate_index"] == 1
    assert candidates[0]["offer_raw_text"].endswith("...")
    assert len(candidates[0]["offer_raw_text"]) == 200

    fetch_call = client.fetch_calls[0]
    assert fetch_call["filters"] == {
        "promotion_id": f"eq.{PROMOTION_ID}",
        "is_active": "eq.true",
    }
    assert fetch_call["limit"] == 101


def test_fetch_candidate_offers_falls_back_when_columns_are_missing():
    class FallbackDbClient(FakeDbClient):
        def fetch_rows(self, table, select, **kwargs):
            self.fetch_calls.append({"table": table, "select": select, **kwargs})
            if "promo_offer_items" in select:
                raise RuntimeError("column promo_offer_master.promo_offer_items does not exist")
            return [
                {
                    "id": "offer-1",
                    "promotion_id": PROMOTION_ID,
                    "is_active": True,
                    "offer_raw_text": "Botox $11/unit",
                    "discount_price": 11,
                    "regular_price": 10,
                }
            ]

    client = FallbackDbClient()

    candidates = fetch_candidate_offers(client, "https://example.com/specials")

    assert len(candidates) == 1
    assert candidates[0]["id"] == "offer-1"
    assert candidates[0]["service_name"] == ""
    assert candidates[0]["candidate_index"] == 1
    assert candidates[0]["regular_price"] == 10
    assert candidates[0]["original_price"] == 10
    assert len(client.fetch_calls) == 2
    assert "promo_offer_items" in client.fetch_calls[0]["select"]
    assert "promo_offer_items" not in client.fetch_calls[1]["select"]


def test_enrich_update_actions_with_diff_prices_backfills_regular_and_discount_price():
    offers = [
        {
            "action": "update",
            "matched_id": "offer-1",
            "service_name": "Validation Botox Update",
            "offer_raw_text": "Validation Botox Update $11/unit limited time",
            "regular_price": "",
            "discount_price": "",
        }
    ]
    payload = {
        "json_diff": {
            "offers[0]": {
                "previous": {
                    "service_name": "Validation Botox Update",
                    "offer_raw_text": "Validation Botox Update $12/unit validation seed",
                    "discount_price": 12,
                },
                "current": {
                    "service_name": "Validation Botox Update",
                    "offer_raw_text": "Validation Botox Update $11/unit limited time",
                    "discount_price": 11,
                },
            }
        }
    }
    candidate_offers = [
        {
            "id": "offer-1",
            "service_name": "Validation Botox Update",
            "offer_raw_text": "Validation Botox Update $12/unit validation seed",
            "regular_price": None,
            "discount_price": 12,
            "original_price": None,
        }
    ]

    enriched = enrich_update_actions_with_diff_prices(offers, payload, candidate_offers)

    assert enriched[0]["regular_price"] == "12"
    assert enriched[0]["discount_price"] == "11"


def test_standardize_offer_service_names_uses_dictionary_values():
    offers = [
        {
            "action": "insert",
            "matched_id": "",
            "service_name": "Glow Membership",
            "raw_service_name": "Glow Membership",
            "membership_name": "Glow Membership",
            "offer_raw_text": "Glow Membership $199/month",
            "offer_content": "Glow Membership $199/month",
        },
        {
            "action": "update",
            "matched_id": "offer-1",
            "service_name": "Validation Botox Update",
            "raw_service_name": "Validation Botox Update",
            "membership_name": "",
            "offer_raw_text": "Validation Botox Update $11/unit limited time",
            "offer_content": "Validation Botox Update $11/unit limited time",
        },
        {
            "action": "insert",
            "matched_id": "",
            "service_name": "Laser Peel",
            "raw_service_name": "Laser Peel",
            "membership_name": "",
            "offer_raw_text": "Laser Peel $150 through June 30, 2026",
            "offer_content": "Laser Peel $150 through June 30, 2026",
        },
    ]
    candidate_offers = [
        {
            "id": "offer-1",
            "service_name": "Botox",
            "offer_raw_text": "Botox $12/unit member special",
        }
    ]

    standardized = standardize_offer_service_names(offers, candidate_offers)

    assert standardized[0]["service_name"] == "Others"
    assert standardized[0]["raw_service_name"] == "Glow Membership"
    assert standardized[1]["service_name"] == "Botox"
    assert standardized[1]["raw_service_name"] == "Validation Botox Update"
    assert standardized[2]["service_name"] == "Chemical Peel"
    assert standardized[2]["raw_service_name"] == "Laser Peel"


def test_build_change_extraction_messages_includes_candidates_and_empty_state():
    payload = {
        "url": "https://example.com/specials",
        "judgment_reason": "Promo changed",
        "meaningful_changes": ["Botox price changed"],
        "json_diff": {"pricing": {"before": ["A"], "after": ["B"]}},
        "text_diff": "- A\n+ B",
    }

    populated = build_change_extraction_messages(
        payload,
        "example.com",
        [{"id": "offer-1", "candidate_index": 1, "service_name": "Botox", "offer_raw_text": "$11/unit", "discount_price": 11, "original_price": ""}],
    )
    empty = build_change_extraction_messages(payload, "example.com", [])

    assert "Existing offers in database for this page" in populated[1]["content"]
    assert '"candidate_index": 1' in populated[1]["content"]
    assert '"id": "offer-1"' not in populated[1]["content"]
    assert "(no existing offers)" in empty[1]["content"]
    assert "Use empty string for missing scalar fields" not in populated[1]["content"]
    assert "Populate as many structured fields as the diff and candidate context support" in populated[1]["content"]
    assert "fill the structured fields instead of leaving them blank" in populated[0]["content"]
    assert "Allowed canonical service_name values" in populated[1]["content"]
    assert "raw_service_name should capture the source wording before normalization" in populated[1]["content"]
    assert "never output raw database ids" in populated[1]["content"]


def test_validate_offer_actions_handles_downgrades_and_skips():
    payload = {
        "offers": [
            {
                "action": "update",
                "matched_id": "missing-id",
                "service_name": "Botox",
                "offer_raw_text": "Botox $11/unit limited time",
            },
            {
                "action": "mark_ended",
                "matched_id": "",
            },
            {
                "action": "unexpected",
                "matched_id": "candidate-1",
                "service_name": "Filler",
                "offer_raw_text": "Filler special",
            },
            {
                "action": "insert",
                "matched_id": "",
                "service_name": "",
                "offer_raw_text": "",
            },
        ]
    }

    validated = validate_offer_actions(
        payload,
        [{"id": "candidate-1"}],
        source_url="https://example.com/specials",
    )

    assert validated["downgraded"] == 3
    assert validated["skipped"] == 2
    assert len(validated["offers"]) == 2
    assert [offer["action"] for offer in validated["offers"]] == ["insert", "insert"]
    assert all(not offer["matched_id"] for offer in validated["offers"])


def test_validate_offer_actions_maps_candidate_index_to_database_id():
    payload = {
        "offers": [
            {
                "action": "update",
                "matched_candidate_index": "2",
                "service_name": "Botox",
                "offer_raw_text": "Botox $11/unit limited time",
            },
            {
                "action": "mark_ended",
                "matched_candidate_index": "1",
            },
        ]
    }

    validated = validate_offer_actions(
        payload,
        [
            {"id": "candidate-1", "candidate_index": 1},
            {"id": "candidate-2", "candidate_index": 2},
        ],
        source_url="https://example.com/specials",
    )

    assert validated["downgraded"] == 0
    assert validated["skipped"] == 0
    assert validated["offers"][0]["matched_id"] == "candidate-2"
    assert validated["offers"][0]["matched_candidate_index"] == "2"
    assert validated["offers"][1]["matched_id"] == "candidate-1"
    assert validated["offers"][1]["matched_candidate_index"] == "1"


def test_validate_offer_actions_forces_insert_when_candidates_unavailable():
    payload = {
        "offers": [
            {
                "action": "update",
                "matched_id": "candidate-1",
                "service_name": "Botox",
                "offer_raw_text": "Botox $11/unit limited time",
            }
        ]
    }

    validated = validate_offer_actions(
        payload,
        [{"id": "candidate-1"}],
        source_url="https://example.com/specials",
        candidates_unavailable=True,
    )

    assert validated["downgraded"] == 1
    assert validated["skipped"] == 0
    assert validated["offers"][0]["action"] == "insert"
    assert validated["offers"][0]["matched_id"] == ""


def test_apply_offer_actions_handles_all_actions_and_continues_after_failures():
    client = FakeDbClient(
        fail_update_ids={"offer-update-fail"},
        fail_insert_offer_texts={"Should fail"},
    )
    offers = [
        {
            "action": "update",
            "matched_id": "offer-update-ok",
            "service_name": "Botox",
            "offer_raw_text": "Botox $11/unit",
            "regular_price": "12",
            "discount_price": "11",
        },
        {
            "action": "update",
            "matched_id": "offer-update-fail",
            "service_name": "Dysport",
            "offer_raw_text": "Dysport $4/unit",
            "discount_price": "4",
        },
        {
            "action": "mark_ended",
            "matched_id": "offer-ended",
        },
        {
            "action": "insert",
            "service_name": "New Offer",
            "offer_raw_text": "Laser special",
            "discount_price": "99",
        },
        {
            "action": "insert",
            "service_name": "Insert Fail",
            "offer_raw_text": "Should fail",
            "discount_price": "88",
        },
    ]

    result = apply_offer_actions(
        client,
        offers,
        source_url="https://example.com/specials",
        source_name="example.com",
        business_id=1,
    )

    assert {k: result[k] for k in ("updated", "inserted", "ended", "skipped")} == {
        "updated": 1,
        "inserted": 1,
        "ended": 1,
        "skipped": 0,
    }
    assert isinstance(result.get("sql_statements"), list)
    assert len(client.update_calls) == 3
    assert len(client.insert_calls) == 2
    assert client.update_calls[0]["payload"]["regular_price"] == 12.0
    assert client.update_calls[0]["payload"]["discount_price"] == 11.0
    assert client.insert_calls[0]["rows"][0]["is_active"] is True
    assert client.insert_calls[0]["rows"][0]["promotion_id"] == PROMOTION_ID
    assert "offer_fingerprint" in client.insert_calls[0]["rows"][0]


def test_apply_offer_actions_updates_on_fingerprint_match_instead_of_insert():
    from utils.offer_fingerprint import compute_offer_fingerprint

    source_url = "https://example.com/specials"
    fingerprint = compute_offer_fingerprint(
        source_url=source_url,
        service_name="Laser Hair Removal",
        unit_type="unit",
    )
    client = FakeDbClient(
        rows=[
            {
                "id": "existing-laser",
                "business_id": 1,
                "is_active": True,
                "offer_fingerprint": fingerprint,
            }
        ]
    )
    result = apply_offer_actions(
        client,
        [
            {
                "action": "insert",
                "service_name": "Laser Hair Removal",
                "offer_raw_text": "Laser Hair Removal $199",
                "discount_price": "199",
                "unit_type": "units",
            }
        ],
        source_url=source_url,
        source_name="example.com",
        business_id=1,
    )

    assert result["updated"] == 1
    assert result["inserted"] == 0
    assert len(client.insert_calls) == 0
    assert client.update_calls[-1]["filters"] == {"id": "eq.existing-laser"}
    assert client.update_calls[-1]["payload"]["discount_price"] == 199.0


def test_apply_offer_actions_retries_without_updated_at_when_column_missing():
    class MissingUpdatedAtDbClient(FakeDbClient):
        def update_row(self, table, filters, payload):
            self.update_calls.append({"table": table, "filters": filters, "payload": payload})
            if "updated_at" in payload:
                raise RuntimeError("column promo_offer_master.updated_at does not exist")
            return [{"id": filters.get("id", "").removeprefix("eq."), **payload}]

    client = MissingUpdatedAtDbClient()

    result = apply_offer_actions(
        client,
        [
            {
                "action": "update",
                "matched_id": "offer-1",
                "service_name": "Botox",
                "offer_raw_text": "Botox $11/unit",
                "discount_price": "11",
            },
            {
                "action": "mark_ended",
                "matched_id": "offer-2",
            },
        ],
        source_url="https://example.com/specials",
        source_name="example.com",
        business_id=1,
    )

    assert {k: result[k] for k in ("updated", "inserted", "ended", "skipped")} == {
        "updated": 1,
        "inserted": 0,
        "ended": 1,
        "skipped": 0,
    }
    assert isinstance(result.get("sql_statements"), list)
    assert len(client.update_calls) == 4
    assert "updated_at" in client.update_calls[0]["payload"]
    assert "updated_at" not in client.update_calls[1]["payload"]
    assert client.update_calls[3]["payload"] == {"is_active": False}


def test_apply_offer_actions_retries_when_http_error_hides_updated_at_in_response_text():
    class FakeResponse:
        text = "Could not find the 'updated_at' column of 'promo_offer_master' in the schema cache"

    class FakeHttpError(Exception):
        def __init__(self):
            super().__init__("400 Client Error: Bad Request for url")
            self.response = FakeResponse()

    class HttpErrorDbClient(FakeDbClient):
        def update_row(self, table, filters, payload):
            self.update_calls.append({"table": table, "filters": filters, "payload": payload})
            if "updated_at" in payload:
                raise FakeHttpError()
            return [{"id": filters.get("id", "").removeprefix("eq."), **payload}]

    client = HttpErrorDbClient()

    result = apply_offer_actions(
        client,
        [
            {
                "action": "update",
                "matched_id": "offer-1",
                "service_name": "Botox",
                "offer_raw_text": "Botox $11/unit",
            }
        ],
        source_url="https://example.com/specials",
        source_name="example.com",
        business_id=1,
    )

    assert {k: result[k] for k in ("updated", "inserted", "ended", "skipped")} == {
        "updated": 1,
        "inserted": 0,
        "ended": 0,
        "skipped": 0,
    }
    assert isinstance(result.get("sql_statements"), list)
    assert len(client.update_calls) == 2
    assert "updated_at" in client.update_calls[0]["payload"]
    assert "updated_at" not in client.update_calls[1]["payload"]


def test_build_change_event_payloads_maps_actions_to_audit_events():
    diff_payload = {
        "url": "https://example.com/specials?utm_source=x",
        "status": "changed",
        "text_diff": "- Botox $12/unit\n+ Botox $11/unit",
        "judgment_reason": "Botox price changed",
        "confidence": "high",
    }
    offers = [
        {
            "action": "update",
            "matched_id": "offer-botox",
            "matched_candidate_index": "1",
            "service_name": "Botox",
            "offer_raw_text": "Botox $11/unit",
            "regular_price": "12",
            "discount_price": "11",
        },
        {
            "action": "insert",
            "service_name": "Membership",
            "offer_raw_text": "Join now for $199/month",
            "discount_price": "199",
        },
        {"action": "mark_ended", "matched_id": "offer-old", "matched_candidate_index": "2"},
    ]
    candidates = [
        {"id": "offer-botox", "candidate_index": 1, "service_name": "Botox"},
        {"id": "offer-old", "candidate_index": 2, "service_name": "Old Offer"},
    ]

    result = build_change_event_payloads(
        offers,
        diff_payload,
        candidates,
        source_url="https://example.com/specials?utm_source=x",
        source_name="example.com",
    )

    events = result["change_events"]
    assert [event["proposed_action"] for event in events] == [
        "update_offer",
        "insert_offer",
        "mark_missing",
    ]
    assert events[0]["business_change_type"] == "price_changed"
    assert events[0]["target_offer_id"] == "offer-botox"
    assert events[0]["proposed_field_updates"] == {
        "offer_raw_text": "Botox $11/unit",
        "regular_price": 12.0,
        "discount_price": 11.0,
        "discount_amount": 1.0,
        "discount_percent": 8.33,
        "service_category": "Neurotoxins",
    }
    assert events[0]["source_url_normalized"] == "https://example.com/specials"
    assert events[0]["confidence"] == 0.9
    assert events[1]["business_change_type"] == "offer_added"
    assert events[1]["proposed_new_offer"]["is_active"] is True
    assert events[1]["proposed_new_offer"]["discount_price"] == 199.0
    assert events[2]["business_change_type"] == "offer_missing"
    assert events[2]["proposed_field_updates"]["is_active"] is False
    assert [item["candidate_offer_id"] for item in result["match_candidates"]] == [
        "offer-botox",
        "offer-old",
    ]
    assert all(item["is_selected"] for item in result["match_candidates"])


def test_build_change_event_decision_plan_splits_auto_apply_and_review():
    payloads = build_change_event_payloads(
        [
            {
                "action": "update",
                "matched_id": "offer-botox",
                "matched_candidate_index": "1",
                "service_name": "Botox",
                "offer_raw_text": "Botox $11/unit",
                "regular_price": "12",
                "discount_price": "11",
            },
            {
                "action": "insert",
                "service_name": "Membership",
                "offer_raw_text": "Join now for $199/month",
                "discount_price": "199",
            },
            {"action": "mark_ended", "matched_id": "offer-old", "matched_candidate_index": "2"},
        ],
        {
            "url": "https://example.com/specials",
            "status": "changed",
            "text_diff": "- Botox $12/unit\n+ Botox $11/unit",
            "judgment_reason": "Botox price changed",
            "confidence": "high",
        },
        [
            {"id": "offer-botox", "candidate_index": 1, "service_name": "Botox"},
            {"id": "offer-old", "candidate_index": 2, "service_name": "Old Offer"},
        ],
        source_url="https://example.com/specials",
        source_name="example.com",
    )

    plan = build_change_event_decision_plan(payloads)

    assert plan["decision_summary"] == {
        "events": 3,
        "auto_apply": 1,
        "needs_review": 2,
        "min_auto_apply_confidence": 0.9,
    }
    assert plan["auto_apply_events"][0]["target_offer_id"] == "offer-botox"
    assert plan["auto_apply_events"][0]["validator_status"] == "auto_apply"
    assert [event["proposed_action"] for event in plan["review_events"]] == [
        "insert_offer",
        "mark_missing",
    ]
    assert "action_requires_review:insert_offer" in plan["review_events"][0]["validator_errors"]
    assert "action_requires_review:mark_missing" in plan["review_events"][1]["validator_errors"]


def test_build_change_event_decision_plan_blocks_low_confidence_update():
    payloads = {
        "change_events": [
            {
                "proposed_action": "update_offer",
                "business_change_type": "price_changed",
                "target_offer_id": "offer-1",
                "proposed_field_updates": {"discount_price": 11.0},
                "confidence": 0.65,
                "validator_errors": [],
            }
        ],
        "match_candidates": [],
    }

    plan = build_change_event_decision_plan(payloads)

    assert plan["decision_summary"]["auto_apply"] == 0
    assert plan["decision_summary"]["needs_review"] == 1
    assert plan["review_events"][0]["validator_status"] == "needs_review"
    assert "confidence_below_auto_apply_threshold" in plan["review_events"][0]["validator_errors"]


def test_prepare_change_event_insert_rows_links_candidate_rows_without_report_fields():
    payloads = {
        "change_events": [
            {
                "event_index": 7,
                "source_name": "example.com",
                "source_url": "https://example.com/specials",
                "source_url_normalized": "https://example.com/specials",
                "business_change_type": "price_changed",
                "proposed_action": "update_offer",
                "target_offer_id": "offer-1",
                "proposed_field_updates": {"discount_price": 11.0},
                "proposed_new_offer": {},
                "validator_status": "pending",
                "validator_errors": [],
            }
        ],
        "match_candidates": [
            {
                "event_index": 7,
                "candidate_offer_id": "offer-1",
                "match_score": 1.0,
                "match_method": "llm_selected_candidate",
                "score_breakdown": {"llm_selected": 1.0},
                "rank": 1,
                "is_selected": True,
            }
        ],
    }

    prepared = prepare_change_event_insert_rows(payloads)

    event_row = prepared["change_event_rows"][0]
    match_row = prepared["match_candidate_rows"][0]
    assert "change_event_id" in event_row
    assert event_row["change_event_id"] == match_row["change_event_id"]
    assert "event_index" not in event_row
    assert "source_name" not in event_row
    assert "event_index" not in match_row
    assert match_row["candidate_offer_id"] == "offer-1"


def test_persist_change_event_payloads_dry_run_and_write_modes():
    payloads = build_change_event_payloads(
        [
            {
                "action": "update",
                "matched_id": "offer-1",
                "matched_candidate_index": "1",
                "service_name": "Botox",
                "offer_raw_text": "Botox $11/unit",
                "discount_price": "11",
            }
        ],
        {"status": "changed", "confidence": "medium"},
        [{"id": "offer-1", "candidate_index": 1}],
        source_url="https://example.com/specials",
        source_name="example.com",
    )
    dry_client = FakeDbClient()

    dry_result = persist_change_event_payloads(dry_client, payloads, dry_run=True)

    assert dry_result["dry_run"] is True
    assert dry_result["change_events_inserted"] == 0
    assert dry_result["match_candidates_inserted"] == 0
    assert len(dry_result["change_event_rows"]) == 1
    assert len(dry_result["match_candidate_rows"]) == 1
    assert dry_client.insert_calls == []

    write_client = FakeDbClient()
    write_result = persist_change_event_payloads(write_client, payloads, dry_run=False)

    assert write_result["change_events_inserted"] == 1
    assert write_result["match_candidates_inserted"] == 1
    assert [call["table"] for call in write_client.insert_calls] == [
        "promo_offer_change_events",
        "promo_offer_match_candidates",
    ]
    event_id = write_client.insert_calls[0]["rows"][0]["change_event_id"]
    assert write_client.insert_calls[1]["rows"][0]["change_event_id"] == event_id


def test_extract_and_upsert_check_pages_end_to_end_with_fixture():
    page = json.loads(
        (FIXTURES_DIR / "change_driven_monitor_check_diff.json").read_text(encoding="utf-8")
    )
    db = FakeDbClient(
        rows=[
            {
                "id": "offer-botox",
                "promotion_id": PROMOTION_ID,
                "is_active": True,
                "offer_raw_text": "Botox 20% off this month",
                "discount_price": None,
                "promo_offer_items": [{"item_name": "Botox"}],
            },
            {
                "id": "offer-filler",
                "promotion_id": PROMOTION_ID,
                "is_active": True,
                "offer_raw_text": "Juvederm summer special $100 off",
                "discount_price": None,
                "promo_offer_items": [{"item_name": "Juvederm"}],
            },
        ]
    )
    llm = FakeLlmClient(
        {
            "offers": [
                {
                    "action": "update",
                    "matched_candidate_index": "1",
                    "service_name": "Botox",
                    "offer_raw_text": "Botox $11/unit limited time",
                    "discount_price": "11",
                    "unit_type": "unit",
                },
                {
                    "action": "mark_ended",
                    "matched_candidate_index": "2",
                },
                {
                    "action": "insert",
                    "matched_candidate_index": "",
                    "service_name": "Laser Peel",
                    "offer_raw_text": "Laser Peel $299 limited time",
                    "discount_price": "299",
                },
            ]
        }
    )

    result = extract_and_upsert_check_pages(
        [page],
        llm,
        db,
        "example.com",
        dry_run=True,
        include_change_events=True,
    )

    assert result["pages_with_diff"] == 1
    assert result["pages_without_diff"] == 0
    assert result["total_offers_extracted"] == 3
    assert result["total_updated"] == 1
    assert result["total_inserted"] == 0
    assert result["total_ended"] == 0
    assert result["page_results"][0]["withheld_for_review"] == 2
    assert result["candidates_unavailable"] is False
    assert result["page_results"][0]["downgraded"] == 0
    assert len(result["page_results"][0]["offer_actions"]) == 3
    assert len(result["page_results"][0]["change_events"]) == 3
    assert result["total_auto_apply_events"] == 1
    assert result["total_review_events"] == 2
    assert result["page_results"][0]["decision_summary"]["auto_apply"] == 1
    assert result["page_results"][0]["change_events"][1]["proposed_action"] == "mark_missing"
    assert result["page_results"][0]["change_events"][1]["validator_status"] == "needs_review"
    assert result["page_results"][0]["offer_actions"][1]["action"] == "mark_ended"
    assert result["page_results"][0]["offer_actions"][0]["matched_id"] == "offer-botox"
    assert db.update_calls == []
    assert db.insert_calls == []
    assert len(llm.calls) == 1
    assert '"candidate_index": 1' in llm.calls[0][1]["content"]
    assert '"candidate_index": 2' in llm.calls[0][1]["content"]
    assert "offer-botox" not in llm.calls[0][1]["content"]
    assert "offer-filler" not in llm.calls[0][1]["content"]


def test_extract_and_upsert_check_pages_downgrades_to_insert_when_candidates_unavailable():
    page = json.loads(
        (FIXTURES_DIR / "change_driven_monitor_check_diff.json").read_text(encoding="utf-8")
    )

    class FetchFailDbClient(FakeDbClient):
        def fetch_rows(self, table, select, **kwargs):
            self.fetch_calls.append({"table": table, "select": select, **kwargs})
            raise RuntimeError("db read unavailable")

    db = FetchFailDbClient()
    llm = FakeLlmClient(
        {
            "offers": [
                {
                    "action": "update",
                    "matched_id": "offer-botox",
                    "service_name": "Botox",
                    "offer_raw_text": "Botox $11/unit limited time",
                    "discount_price": "11",
                }
            ]
        }
    )

    result = extract_and_upsert_check_pages(
        [page],
        llm,
        db,
        "example.com",
        dry_run=True,
    )

    assert result["candidates_unavailable"] is True
    assert result["total_inserted"] == 0
    assert result["total_updated"] == 0
    assert result["page_results"][0]["withheld_for_review"] == 1
    assert result["page_results"][0]["candidates_unavailable"] is True
    assert result["page_results"][0]["downgraded"] == 1
    assert result["page_results"][0]["offer_actions"][0]["action"] == "insert"
    assert result["page_results"][0]["offer_actions"][0]["matched_id"] == ""


def test_extract_and_upsert_check_pages_invalid_llm_payload_requests_fallback():
    page = json.loads(
        (FIXTURES_DIR / "change_driven_monitor_check_diff.json").read_text(encoding="utf-8")
    )
    db = FakeDbClient(rows=[])
    llm = FakeLlmClient({})

    result = extract_and_upsert_check_pages(
        [page],
        llm,
        db,
        "example.com",
        dry_run=True,
    )

    assert result["pages_with_diff"] == 1
    assert result["pages_without_diff"] == 1
    assert result["needs_apify_fallback"] is True
    assert result["total_offers_extracted"] == 0
    assert result["page_results"][0]["action"] == "invalid_llm_payload"


def test_extract_offer_price_fields_reads_new_schema_number_fields():
    from utils.change_driven_extractor import _extract_offer_price_fields

    node = {
        "regular_price": 200,
        "discount_price": 150,
        "name": "Botox",
        "unit_type": "unit",
    }
    result = _extract_offer_price_fields(node)
    assert result["regular_price"] == 200.0
    assert result["discount_price"] == 150.0


def test_extract_offer_price_fields_falls_back_when_new_fields_missing():
    from utils.change_driven_extractor import _extract_offer_price_fields

    node = {"name": "Botox", "price": "$10", "offer_raw_text": "Botox $11/unit"}
    result = _extract_offer_price_fields(node)
    assert result["regular_price"] is None
    assert result["discount_price"] == 11.0


def test_filter_candidates_by_diff_relevance_ranks_by_overlap_and_reindexes():
    from utils.change_driven_extractor import filter_candidates_by_diff_relevance

    candidates = [
        {"id": "1", "candidate_index": 1, "service_name": "Botox", "offer_raw_text": "$10/unit"},
        {"id": "2", "candidate_index": 2, "service_name": "Filler", "offer_raw_text": "$650/syringe"},
        {"id": "3", "candidate_index": 3, "service_name": "Facial", "offer_raw_text": "$150"},
    ]
    meaningful = [
        {"type": "changed", "before": "Botox $10/unit", "after": "Botox $11/unit", "reason": "price"}
    ]

    kept = filter_candidates_by_diff_relevance(candidates, meaningful, max_keep=2)
    assert len(kept) == 2
    assert kept[0]["id"] == "1"
    assert kept[0]["candidate_index"] == 1
    assert kept[1]["candidate_index"] == 2


def test_filter_candidates_returns_fallback_when_no_overlap():
    from utils.change_driven_extractor import filter_candidates_by_diff_relevance

    candidates = [
        {"id": "1", "candidate_index": 1, "service_name": "Botox", "offer_raw_text": "$10"},
        {"id": "2", "candidate_index": 2, "service_name": "Filler", "offer_raw_text": "$650"},
    ]
    meaningful = [
        {"type": "changed", "before": "Laser $300", "after": "Laser $250", "reason": "price"}
    ]

    kept = filter_candidates_by_diff_relevance(candidates, meaningful, max_keep=2)
    assert len(kept) == 2
    assert kept[0]["candidate_index"] == 1


def test_filter_candidates_empty_meaningful_keeps_first_n():
    from utils.change_driven_extractor import filter_candidates_by_diff_relevance

    candidates = [
        {"id": str(i), "candidate_index": i, "service_name": f"S{i}", "offer_raw_text": ""}
        for i in range(1, 6)
    ]
    kept = filter_candidates_by_diff_relevance(candidates, [], max_keep=3)
    assert len(kept) == 3
    assert [item["candidate_index"] for item in kept] == [1, 2, 3]


def test_head_tail_keeps_head_and_tail_with_marker():
    from utils.change_driven_extractor import _head_tail

    text = "A" * 2000 + "MIDDLE" + "B" * 2000
    out = _head_tail(text, 3000)
    assert len(out) < len(text)
    assert out.startswith("A")
    assert out.endswith("B")
    assert "truncated" in out.lower()
    assert out.count("A") >= 1400
    assert out.count("B") >= 1400
    assert "MIDDLE" not in out


def test_head_tail_short_text_unchanged():
    from utils.change_driven_extractor import _head_tail

    text = "short diff"
    assert _head_tail(text, 3000) == text


def test_extract_diff_payload_includes_confidence():
    from utils.change_driven_extractor import extract_diff_payload

    page = {
        "url": "https://x.com/s",
        "status": "changed",
        "diff": {"text": "x"},
        "judgment": {"meaningful": True, "confidence": "low", "reason": "r"},
    }
    payload = extract_diff_payload(page)
    assert payload["confidence"] == "low"


def test_extract_and_upsert_filters_candidates_and_records_pool_size():
    page = {
        "url": "https://example.com/specials",
        "status": "changed",
        "diff": {"text": "- Botox $10/unit\n+ Botox $11/unit"},
        "judgment": {
            "meaningful": True,
            "confidence": "high",
            "reason": "Botox price changed",
            "meaningfulChanges": [
                {
                    "type": "changed",
                    "before": "Botox $10/unit",
                    "after": "Botox $11/unit",
                    "reason": "price",
                }
            ],
        },
    }
    rows = [
        {
            "id": f"id-{idx}",
            "promotion_id": PROMOTION_ID,
            "is_active": True,
            "offer_raw_text": "Botox $10/unit" if idx == 0 else f"Other {idx}",
            "discount_price": 10 + idx,
            "promo_offer_items": [
                {"item_name": "Botox" if idx == 0 else f"Service {idx}"}
            ],
        }
        for idx in range(15)
    ]
    db = FakeDbClient(rows=rows)
    llm = FakeLlmClient({"offers": []})

    result = extract_and_upsert_check_pages(
        [page],
        llm,
        db,
        "example.com",
        dry_run=True,
    )

    assert result["page_results"][0]["candidate_pool_size"] == 15
    assert result["page_results"][0]["candidate_kept"] <= 10
    assert len(llm.calls) == 1


def test_extract_and_upsert_skips_low_confidence_and_triggers_fallback():
    page = {
        "url": "https://example.com/specials",
        "status": "changed",
        "diff": {"text": "price changed"},
        "judgment": {
            "meaningful": True,
            "confidence": "low",
            "reason": "unclear change",
            "meaningfulChanges": [{"type": "changed", "before": "a", "after": "b", "reason": "x"}],
        },
    }
    db = FakeDbClient(rows=[])
    llm = FakeLlmClient({"offers": []})

    result = extract_and_upsert_check_pages(
        [page],
        llm,
        db,
        "example.com",
        dry_run=True,
        min_confidence="medium",
    )

    assert result["page_results"][0]["action"] == "low_confidence_skipped"
    assert result["needs_apify_fallback"] is True
    assert len(llm.calls) == 0


@pytest.mark.parametrize(
    "payload,candidates,expected",
    [
        (
            {
                "offers": [
                    {
                        "action": "update",
                        "matched_candidate_index": "2",
                        "service_name": "Botox",
                        "offer_raw_text": "Botox $11/unit",
                    }
                ]
            },
            [{"id": "db-2", "candidate_index": 2}],
            {"matched_id": "db-2", "action": "update", "downgraded": 0},
        ),
        (
            {
                "offers": [
                    {
                        "action": "update",
                        "matched_id": "ghost",
                        "service_name": "Botox",
                        "offer_raw_text": "Botox $11/unit",
                    }
                ]
            },
            [{"id": "db-1", "candidate_index": 1}],
            {"matched_id": "", "action": "insert", "downgraded": 1},
        ),
        (
            {"offers": [{"action": "mark_ended", "matched_candidate_index": "1"}]},
            [{"id": "db-1", "candidate_index": 1}],
            {"matched_id": "db-1", "action": "mark_ended", "downgraded": 0},
        ),
    ],
)
def test_validate_offer_actions_table_driven(payload, candidates, expected):
    validated = validate_offer_actions(payload, candidates, source_url="https://example.com/specials")
    assert validated["downgraded"] == expected["downgraded"]
    assert validated["offers"][0]["action"] == expected["action"]
    assert validated["offers"][0]["matched_id"] == expected["matched_id"]


@pytest.mark.parametrize(
    "offer,json_diff,expected_regular,expected_discount",
    [
        (
            {
                "action": "update",
                "matched_id": "offer-1",
                "service_name": "Validation Botox Update",
                "offer_raw_text": "Validation Botox Update $11/unit limited time",
                "regular_price": "",
                "discount_price": "",
            },
            {
                "offers[0]": {
                    "previous": {
                        "service_name": "Validation Botox Update",
                        "offer_raw_text": "Validation Botox Update $12/unit validation seed",
                        "discount_price": 12,
                    },
                    "current": {
                        "service_name": "Validation Botox Update",
                        "offer_raw_text": "Validation Botox Update $11/unit limited time",
                        "discount_price": 11,
                    },
                }
            },
            "12",
            "11",
        ),
        (
            {
                "action": "update",
                "matched_id": "offer-1",
                "service_name": "Botox",
                "offer_raw_text": "Botox $11/unit",
                "regular_price": "12",
                "discount_price": "11",
            },
            {"offers[0]": {"previous": {"discount_price": 99}, "current": {"discount_price": 1}}},
            "12",
            "11",
        ),
    ],
)
def test_enrich_update_actions_price_backfill_table_driven(
    offer, json_diff, expected_regular, expected_discount
):
    candidate_offers = [
        {
            "id": "offer-1",
            "service_name": offer["service_name"],
            "offer_raw_text": "seed text",
            "regular_price": None,
            "discount_price": None,
            "original_price": None,
        }
    ]
    enriched = enrich_update_actions_with_diff_prices(
        [offer], {"json_diff": json_diff}, candidate_offers
    )
    assert enriched[0]["regular_price"] == expected_regular
    assert enriched[0]["discount_price"] == expected_discount


def test_build_change_event_payloads_maps_mark_ended_to_mark_missing():
    payloads = build_change_event_payloads(
        [{"action": "mark_ended", "matched_id": "offer-old", "matched_candidate_index": "1"}],
        {"status": "changed", "confidence": "high"},
        [{"id": "offer-old", "candidate_index": 1}],
        source_url="https://example.com/specials",
        source_name="example.com",
    )
    assert payloads["change_events"][0]["proposed_action"] == "mark_missing"
    assert payloads["change_events"][0]["business_change_type"] == "offer_missing"


def test_extract_diff_payload_on_harvested_cases():
    cases_dir = FIXTURES_DIR / "monitor_cases"
    if not cases_dir.exists():
        return

    checked = 0
    for path in sorted(cases_dir.glob("*.json")):
        if path.name == "manifest.json" or path.name.endswith(".dry_run.json"):
            continue
        page = json.loads(path.read_text(encoding="utf-8"))
        payload = extract_diff_payload(page)
        assert payload is not None, path.name
        assert payload.get("meaningful_changes"), path.name
        checked += 1

    if checked == 0 and (cases_dir / "manifest.json").exists():
        manifest = json.loads((cases_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest.get("count", 0) == 0
