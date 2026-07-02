import json
from pathlib import Path

from utils.change_driven_extractor import (
    apply_offer_actions,
    build_change_extraction_messages,
    enrich_update_actions_with_diff_prices,
    extract_and_upsert_check_pages,
    fetch_candidate_offers,
    standardize_offer_service_names,
    validate_offer_actions,
)


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class FakeDbClient:
    def __init__(self, rows=None, *, fail_update_ids=None, fail_insert_service_names=None):
        self.rows = rows or []
        self.fail_update_ids = set(fail_update_ids or [])
        self.fail_insert_service_names = set(fail_insert_service_names or [])
        self.fetch_calls = []
        self.update_calls = []
        self.insert_calls = []

    def fetch_rows(self, table, select, **kwargs):
        self.fetch_calls.append({"table": table, "select": select, **kwargs})
        return list(self.rows)

    def update_row(self, table, filters, payload):
        self.update_calls.append({"table": table, "filters": filters, "payload": payload})
        row_id = filters.get("id", "").removeprefix("eq.")
        if row_id in self.fail_update_ids:
            raise RuntimeError(f"boom-update-{row_id}")
        return [{"id": row_id, **payload}]

    def insert_rows(self, table, rows):
        self.insert_calls.append({"table": table, "rows": rows})
        for row in rows:
            if row.get("service_name") in self.fail_insert_service_names:
                raise RuntimeError(f"boom-insert-{row['service_name']}")
        return rows


class FakeLlmClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create_json_response(self, messages):
        self.calls.append(messages)
        return self.response


def test_fetch_candidate_offers_truncates_and_filters_active_query():
    rows = [
        {
            "id": f"id-{idx}",
            "service_name": f"Service {idx}",
            "offer_raw_text": "X" * 250,
            "discount_price": idx,
            "original_price": idx + 100,
            "status": "active",
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
        "source_url": "eq.https://example.com/specials",
        "status": "eq.active",
    }
    assert fetch_call["limit"] == 101


def test_fetch_candidate_offers_falls_back_when_columns_are_missing():
    class FallbackDbClient(FakeDbClient):
        def fetch_rows(self, table, select, **kwargs):
            self.fetch_calls.append({"table": table, "select": select, **kwargs})
            if "original_price" in select:
                raise RuntimeError("column promo_offer_master.original_price does not exist")
            return [
                {
                    "id": "offer-1",
                    "service_name": "Botox",
                    "offer_raw_text": "Botox $11/unit",
                    "discount_price": 11,
                    "status": "active",
                }
            ]

    client = FallbackDbClient()

    candidates = fetch_candidate_offers(client, "https://example.com/specials")

    assert len(candidates) == 1
    assert candidates[0]["id"] == "offer-1"
    assert candidates[0]["candidate_index"] == 1
    assert candidates[0]["regular_price"] is None
    assert candidates[0]["original_price"] is None
    assert len(client.fetch_calls) == 2
    assert "original_price" in client.fetch_calls[0]["select"]
    assert client.fetch_calls[1]["select"] == "id,service_name,offer_raw_text,regular_price,discount_price,status"


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

    assert standardized[0]["service_name"] == "Membership"
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
        fail_insert_service_names={"Insert Fail"},
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
    )

    assert result == {"updated": 1, "inserted": 1, "ended": 1, "skipped": 0}
    assert len(client.update_calls) == 3
    assert len(client.insert_calls) == 2
    assert client.update_calls[0]["payload"]["regular_price"] == 12.0
    assert client.update_calls[0]["payload"]["discount_price"] == 11.0
    assert client.insert_calls[0]["rows"][0]["status"] == "active"


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
    )

    assert result == {"updated": 1, "inserted": 0, "ended": 1, "skipped": 0}
    assert len(client.update_calls) == 4
    assert "updated_at" in client.update_calls[0]["payload"]
    assert "updated_at" not in client.update_calls[1]["payload"]
    assert client.update_calls[3]["payload"] == {"status": "ended"}


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
    )

    assert result == {"updated": 1, "inserted": 0, "ended": 0, "skipped": 0}
    assert len(client.update_calls) == 2
    assert "updated_at" in client.update_calls[0]["payload"]
    assert "updated_at" not in client.update_calls[1]["payload"]


def test_extract_and_upsert_check_pages_end_to_end_with_fixture():
    page = json.loads(
        (FIXTURES_DIR / "change_driven_monitor_check_diff.json").read_text(encoding="utf-8")
    )
    db = FakeDbClient(
        rows=[
            {
                "id": "offer-botox",
                "service_name": "Botox",
                "offer_raw_text": "Botox 20% off this month",
                "discount_price": None,
                "original_price": None,
                "status": "active",
            },
            {
                "id": "offer-filler",
                "service_name": "Juvederm",
                "offer_raw_text": "Juvederm summer special $100 off",
                "discount_price": None,
                "original_price": None,
                "status": "active",
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
                    "service_name": "Membership",
                    "offer_raw_text": "Join now for $199/month",
                    "membership_price": "199",
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
    )

    assert result["pages_with_diff"] == 1
    assert result["pages_without_diff"] == 0
    assert result["total_offers_extracted"] == 3
    assert result["total_updated"] == 1
    assert result["total_inserted"] == 1
    assert result["total_ended"] == 1
    assert result["candidates_unavailable"] is False
    assert result["page_results"][0]["downgraded"] == 0
    assert len(result["page_results"][0]["offer_actions"]) == 3
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
    assert result["total_inserted"] == 1
    assert result["total_updated"] == 0
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
