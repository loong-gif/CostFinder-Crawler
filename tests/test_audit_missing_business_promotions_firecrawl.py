from __future__ import annotations

import json
from datetime import date

import pytest

from scripts.audit_missing_business_promotions_firecrawl import (
    RawSpoolDb,
    SelfHostedFirecrawl,
    activity_from_dates,
    assign_businesses,
    audit_businesses,
    build_audit,
    has_explicit_promotion_signal,
    is_promotion_page_url,
    reconcile_audit_raw_ids,
    select_missing_businesses,
    write_reports,
    write_raw_spool,
)


def test_promotion_url_requires_bounded_special_or_promotion_token() -> None:
    assert is_promotion_page_url("https://clinic.example/specials")
    assert is_promotion_page_url("https://clinic.example/summer-specials-2026")
    assert is_promotion_page_url("https://clinic.example/promotions/current")
    assert not is_promotion_page_url("https://clinic.example/vein-specialist")
    assert not is_promotion_page_url("https://clinic.example/promo")
    assert not is_promotion_page_url("https://clinic.example/offers")


@pytest.mark.parametrize(
    "text",
    [
        "Botox is $10 per unit this month.",
        "Save $50 on your first treatment.",
        "Take 20% off all fillers.",
        "Buy two syringes and get one free.",
        "Free peel with any facial purchase.",
    ],
)
def test_explicit_promotion_signal_accepts_concrete_offer_text(text: str) -> None:
    assert has_explicit_promotion_signal(text)


@pytest.mark.parametrize(
    "text",
    [
        "View our current specials.",
        "Ask about a member discount.",
        "Our specialist can help.",
        "Members receive exclusive benefits.",
    ],
)
def test_explicit_promotion_signal_rejects_generic_words(text: str) -> None:
    assert not has_explicit_promotion_signal(text)


def test_activity_uses_an_inclusive_current_date_range() -> None:
    assert activity_from_dates(
        "2026-09-01", "2026-09-30", "September special", today=date(2026, 9, 5)
    ) == {"is_active": True, "date_review_required": False}
    assert activity_from_dates(
        "2026-10-01", "2026-10-31", "October special", today=date(2026, 9, 5)
    ) == {"is_active": False, "date_review_required": False}
    assert activity_from_dates(
        "2026-08-01", "2026-08-31", "August special", today=date(2026, 9, 5)
    ) == {"is_active": False, "date_review_required": False}


def test_activity_marks_incomplete_seasonal_dates_for_review() -> None:
    assert activity_from_dates(
        None, None, "Celebrate our Labor Day special", today=date(2026, 9, 5)
    ) == {"is_active": None, "date_review_required": True}
    assert activity_from_dates(
        None, "2026-09-30", "Valid through September 30, 2026", today=date(2026, 9, 5)
    ) == {"is_active": None, "date_review_required": True}
    assert activity_from_dates(
        None, None, "Ongoing Botox $10 per unit", today=date(2026, 9, 5)
    ) == {"is_active": True, "date_review_required": False}


def test_activity_marks_year_only_campaign_for_review() -> None:
    assert activity_from_dates(
        None,
        None,
        "Exclusive 2026 promotion: save $200 per month",
        today=date(2026, 9, 5),
    ) == {"is_active": None, "date_review_required": True}


def test_assign_businesses_requires_unique_identity_on_shared_host() -> None:
    businesses = [
        {
            "business_id": 1,
            "name": "Example Med Spa Irvine",
            "city": "Irvine",
            "address": "1 Main Street",
            "website": "https://example.com/locations/irvine",
        },
        {
            "business_id": 2,
            "name": "Example Med Spa Tustin",
            "city": "Tustin",
            "address": "2 Main Street",
            "website": "https://example.com/locations/tustin",
        },
    ]

    assert assign_businesses(
        "https://example.com/locations/irvine/specials",
        "Irvine patients save 20%.",
        businesses,
    ) == ([1], "location_path")
    assert assign_businesses(
        "https://example.com/specials",
        "All locations save 20%.",
        businesses,
    ) == ([], "shared_host_unresolved")


def test_assign_businesses_accepts_the_only_business_on_a_host() -> None:
    businesses = [{"business_id": 7, "name": "Solo Spa", "website": "https://solo.example"}]
    assert assign_businesses(
        "https://solo.example/specials", "Save 20%.", businesses
    ) == ([7], "single_host_business")


def test_assign_businesses_uses_unique_address_name_or_city_evidence() -> None:
    businesses = [
        {"business_id": 1, "name": "Alpha Spa", "city": "Irvine", "address": "1 Main Street", "website": "https://shared.example"},
        {"business_id": 2, "name": "Beta Spa", "city": "Tustin", "address": "2 Main Street", "website": "https://shared.example"},
    ]
    assert assign_businesses("https://shared.example/specials", "Visit us at 2 Main Street.", businesses) == ([2], "address")
    assert assign_businesses("https://shared.example/specials", "Alpha Spa saves 20%.", businesses) == ([1], "business_name")
    assert assign_businesses("https://shared.example/specials", "Available in Tustin.", businesses) == ([2], "city")


def test_audit_persists_only_scrape_raw_and_classifies_targets(monkeypatch) -> None:
    businesses = [
        {"business_id": 1, "name": "Found Spa", "website": "https://found.example"},
        {"business_id": 2, "name": "No Site", "website": None},
    ]
    raw_calls: list[dict] = []

    class Firecrawl:
        def map(self, _website, *, limit):
            assert limit == 500
            return ["https://found.example/specials"]

        def scrape(self, url):
            return {
                "success": True,
                "data": {
                    "markdown": "# September Special\nBotox $10 per unit from September 1, 2026 through September 30, 2026.",
                    "metadata": {"title": "September Special"},
                },
            }

    class Gemini:
        def create_json_response(self, *_args, **_kwargs):
            return {
                "explanation": "explicit offer",
                "promotions": [
                    {
                        "promotion_title": "September Special",
                        "promotion_content": [
                            "Botox $10 per unit from September 1, 2026 through September 30, 2026."
                        ],
                        "campaign_start_date": "2026-09-01",
                        "campaign_end_date": "2026-09-30",
                    }
                ],
            }

    monkeypatch.setattr(
        "scripts.audit_missing_business_promotions_firecrawl.save_scrape_response",
        lambda _db, _fingerprint, source_url, _response, **kwargs: raw_calls.append(
            {"table": "firecrawl_scrape_raw", "source_url": source_url, **kwargs}
        )
        or [{"id": 88}],
    )

    results = audit_businesses(
        db=object(),
        firecrawl=Firecrawl(),
        gemini=Gemini(),
        targets=businesses,
        all_businesses=businesses,
        map_limit=500,
        write_raw=True,
        today=date(2026, 9, 5),
    )

    assert [row["status"] for row in results] == ["promotion_found", "missing_website"]
    assert results[0]["raw_ids"] == [88]
    assert raw_calls == [
        {
            "table": "firecrawl_scrape_raw",
            "source_url": "https://found.example/specials",
            "success": True,
            "error_message": None,
        }
    ]
    assert all(call["table"] != "clinic_promotions" for call in raw_calls)


def test_audit_does_not_turn_map_failure_into_no_promotion() -> None:
    class Firecrawl:
        def map(self, _website, *, limit):
            raise RuntimeError("map unavailable")

    results = audit_businesses(
        db=object(),
        firecrawl=Firecrawl(),
        gemini=object(),
        targets=[{"business_id": 3, "name": "Map Fail", "website": "https://map.example"}],
        all_businesses=[{"business_id": 3, "name": "Map Fail", "website": "https://map.example"}],
        write_raw=False,
    )

    assert results[0]["status"] == "map_failed"


def test_audit_classifies_scrape_and_gemini_failures_separately() -> None:
    targets = [{"business_id": 4, "name": "Error Spa", "website": "https://error.example"}]

    class ScrapeFailure:
        def map(self, _website, *, limit):
            return ["https://error.example/specials"]

        def scrape(self, _url):
            raise RuntimeError("scrape failed")

    scrape_results = audit_businesses(
        db=object(),
        firecrawl=ScrapeFailure(),
        gemini=object(),
        targets=targets,
        all_businesses=targets,
        write_raw=False,
    )
    assert scrape_results[0]["status"] == "scrape_failed"

    class GeminiFailure(ScrapeFailure):
        def scrape(self, _url):
            return {"success": True, "data": {"markdown": "# Special\nSave $20 today."}}

    class BrokenGemini:
        def create_json_response(self, *_args, **_kwargs):
            raise RuntimeError("gemini failed")

    gemini_results = audit_businesses(
        db=object(),
        firecrawl=GeminiFailure(),
        gemini=BrokenGemini(),
        targets=targets,
        all_businesses=targets,
        write_raw=False,
    )
    assert gemini_results[0]["status"] == "gemini_failed"


def test_audit_marks_shared_and_incomplete_date_results_for_review() -> None:
    shared = [
        {"business_id": 11, "name": "North Spa", "city": "North", "website": "https://shared.example"},
        {"business_id": 12, "name": "South Spa", "city": "South", "website": "https://shared.example"},
    ]

    class Firecrawl:
        def map(self, _website, *, limit):
            return ["https://shared.example/specials"]

        def scrape(self, _url):
            return {"success": True, "data": {"markdown": "# Holiday Special\nSave $20 this holiday."}}

    class Gemini:
        def create_json_response(self, *_args, **_kwargs):
            return {
                "explanation": "found",
                "promotions": [{
                    "promotion_title": "Holiday Special",
                    "promotion_content": ["Save $20 this holiday."],
                    "campaign_start_date": None,
                    "campaign_end_date": None,
                }],
            }

    unresolved = audit_businesses(
        db=object(), firecrawl=Firecrawl(), gemini=Gemini(), targets=shared,
        all_businesses=shared, write_raw=False,
    )
    assert {row["status"] for row in unresolved} == {"shared_host_unresolved"}

    dated = audit_businesses(
        db=object(), firecrawl=Firecrawl(), gemini=Gemini(), targets=[shared[0]],
        all_businesses=[shared[0]], write_raw=False,
    )
    assert dated[0]["status"] == "date_review_required"
    assert dated[0]["promotions"][0]["is_active"] is None


def test_audit_treats_nonexistent_fallback_pages_as_no_qualifying_promotion() -> None:
    target = {"business_id": 20, "name": "No Promo", "website": "https://none.example"}

    class Firecrawl:
        def map(self, _website, *, limit):
            return []

        def scrape(self, _url):
            return {"success": True, "data": {"markdown": "$404", "metadata": {"statusCode": 404}}}

    results = audit_businesses(
        db=object(), firecrawl=Firecrawl(), gemini=object(), targets=[target],
        all_businesses=[target], write_raw=False,
    )
    assert results[0]["status"] == "no_qualifying_promotion_found"
    assert len(results[0]["candidate_urls"]) == 4


def test_self_hosted_firecrawl_calls_v1_map_and_scrape(monkeypatch) -> None:
    calls: list[tuple[str, str, dict | None]] = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            if calls[-1][1].endswith("/v1/map"):
                return {"success": True, "links": ["https://clinic.example/specials"]}
            return {"success": True, "data": {"markdown": "Save $20."}}

    class Session:
        def __init__(self):
            self.headers = {}
            self.trust_env = True

        def get(self, url, timeout):
            calls.append(("GET", url, None))
            return Response()

        def post(self, url, json, timeout):
            calls.append(("POST", url, json))
            return Response()

    monkeypatch.setattr("scripts.audit_missing_business_promotions_firecrawl.requests.Session", Session)
    client = SelfHostedFirecrawl("http://192.168.1.68:3002/", "secret", timeout=30)
    client.healthcheck()
    assert client.map("https://clinic.example", limit=50) == ["https://clinic.example/specials"]
    assert client.scrape("https://clinic.example/specials")["success"] is True
    assert calls[1][2] == {"url": "https://clinic.example/", "limit": 50}
    assert calls[2][2]["formats"] == ["markdown", "links"]


def test_select_missing_businesses_and_write_reports(tmp_path) -> None:
    class Db:
        def fetch_rows(self, table, _select, **_kwargs):
            if table == "master_business_info":
                return [
                    {"business_id": 1, "name": "Has Promo", "website": "https://one.example"},
                    {"business_id": 2, "name": "Missing", "website": "https://two.example"},
                    {"business_id": 3, "name": "No Site", "website": None},
                ]
            return [{"business_id": 1}]

    targets, all_businesses = select_missing_businesses(Db())
    assert [row["business_id"] for row in targets] == [2, 3]
    assert len(all_businesses) == 3
    selected, _ = select_missing_businesses(Db(), business_ids=[3], limit=1)
    assert [row["business_id"] for row in selected] == [3]

    audit = build_audit(
        [
            {"business_id": 2, "name": "Missing", "status": "no_qualifying_promotion_found", "promotions": [], "errors": []},
            {"business_id": 3, "name": "No Site", "status": "missing_website", "promotions": [], "errors": []},
        ],
        write_raw=True,
    )
    json_path, csv_path = write_reports(audit, tmp_path)
    assert " " not in json_path.name
    assert " " not in csv_path.name
    assert json.loads(json_path.read_text())["balanced"] is True
    assert "business_id,name,website,host,status" in csv_path.read_text()


def test_raw_spool_db_reuses_fingerprint_and_writes_underscore_filename(tmp_path) -> None:
    db = RawSpoolDb()
    inserted = db.insert_rows(
        "firecrawl_scrape_raw",
        [{"request_fingerprint": "fp-1", "source_url": "https://example.com/specials"}],
    )
    assert inserted[0]["id"] == 1
    assert db.fetch_rows(
        "firecrawl_scrape_raw", "id", filters={"request_fingerprint": "eq.fp-1"}, limit=1
    ) == [{"id": 1}]
    updated = db.update_row(
        "firecrawl_scrape_raw", {"id": "eq.1"}, {"success": True, "markdown": "Save $20"}
    )
    assert updated[0]["success"] is True

    spool_path = write_raw_spool(db, tmp_path, "2026-09-05")
    assert spool_path.name == "missing_business_promotion_raw_2026_09_05.json"
    assert json.loads(spool_path.read_text())["records"][0]["request_fingerprint"] == "fp-1"


def test_reconcile_audit_raw_ids_returns_a_new_resolved_audit() -> None:
    audit = {
        "results": [{
            "business_id": 1,
            "raw_ids": [1, 2],
            "promotions": [{"raw_id": 2}],
            "unresolved_promotions": [{"raw_id": 1}],
        }]
    }
    resolved = reconcile_audit_raw_ids(audit, {1: 101, 2: 102})

    assert resolved["results"][0]["raw_ids"] == [101, 102]
    assert resolved["results"][0]["promotions"][0]["raw_id"] == 102
    assert resolved["results"][0]["unresolved_promotions"][0]["raw_id"] == 101
    assert audit["results"][0]["raw_ids"] == [1, 2]
