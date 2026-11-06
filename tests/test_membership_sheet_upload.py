from __future__ import annotations

import json

from utils.membership_sheet_upload import build_plan, render_apply, screen_row

BUSINESSES = {1, 2}


def row(**fields):
    base = {"sheet_row": 2, "business_id": 1, "membership_name": "Gold Program", "membership_price": "150",
            "billing_period": "monthly", "minimum_commitment_months": None,
            "benefits": json.dumps(["$150 monthly account credit", "10% off products"]),
            "source_url": "https://a.com/membership", "qa_status": "PASS"}
    return {**base, **fields}


def test_valid_row_is_accepted_with_typed_fields():
    entry, reason = screen_row(row(minimum_commitment_months="12.0"), BUSINESSES)
    assert reason == "" and entry["membership_price"] == 150.0 and entry["minimum_commitment_months"] == 12
    assert screen_row(row(minimum_commitment_months="1.5"), BUSINESSES)[1] == "commitment_months_not_whole_number"
    assert entry["benefits"] == ["$150 monthly account credit", "10% off products"]


def test_rows_failing_the_screen_report_the_reason():
    cases = {
        "qa_status_WARN": row(qa_status="WARN"),
        "business_missing_or_name_differs_in_database": row(business_id=99),
        "empty_name": row(membership_name="  "),
        "price_missing_or_not_positive": row(membership_price=None),
        "benefits_empty": row(benefits="[]"),
        "benefit_link_residue": row(benefits=json.dumps(["[Join](https://a.com/join)"])),
        "benefit_line_empty_or_too_short": row(benefits=json.dumps(["ok benefit", " "])),
        "billing_period_unknown": row(billing_period="weekly"),
    }
    for expected, candidate in cases.items():
        assert screen_row(candidate, BUSINESSES)[1] == expected


SHEET_NAMES = {1: "A Spa", 2: "B Spa"}


def test_plan_skips_rows_already_in_database_and_duplicates_in_sheet():
    snapshot = {"businesses": [{"business_id": 1, "name": "A Spa"}, {"business_id": 2, "name": "B Spa"}],
                "memberships": [{"business_id": 1, "source_url": "https://www.a.com/membership/",
                                 "membership_name": " gold program "}]}
    rows = [row(sheet_row=2), row(sheet_row=3, membership_name="Silver"), row(sheet_row=4, membership_name="silver"),
            row(sheet_row=5, membership_name="Bronze", qa_status="FAIL")]
    plan = build_plan(rows, snapshot, SHEET_NAMES)
    assert [e["membership_name"] for e in plan["entries"]] == ["Silver"]
    assert {r["sheet_row"]: r["reason"] for r in plan["rejected"]} == {4: "duplicate_in_sheet", 5: "qa_status_FAIL"}


def test_business_id_taken_over_by_another_business_is_rejected():
    snapshot = {"businesses": [{"business_id": 1, "name": "LaserAway Morningside Heights"}], "memberships": []}
    plan = build_plan([row(sheet_row=2, membership_name="Silver")], snapshot, {1: "Skin Spa New York - Upper West Side"})
    assert plan["entries"] == []
    assert plan["rejected"][0]["reason"] == "business_missing_or_name_differs_in_database"


def test_apply_sql_is_one_checked_block_that_never_overwrites():
    entry, _ = screen_row(row(membership_name="Gold's Plan"), BUSINESSES)
    text = render_apply({"entries": [entry]})
    assert text.count("do $pipeline$") == 1 and "do nothing" in text and "Gold''s Plan" in text
    assert "stale_plan" in text and "v_inserted <> 1" in text
