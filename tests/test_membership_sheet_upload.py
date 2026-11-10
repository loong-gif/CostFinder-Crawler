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
    # 人工打的 qa_status 不参与判定：可见性由渲染核验决定
    assert screen_row(row(qa_status="WARN"), BUSINESSES)[1] == ""
    cases = {
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
    rows = [row(sheet_row=2), row(sheet_row=3, membership_name="Silver"), row(sheet_row=4, membership_name="silver")]
    plan = build_plan(rows, snapshot, SHEET_NAMES)
    assert [e["membership_name"] for e in plan["entries"]] == ["Silver"]
    assert {r["sheet_row"]: r["reason"] for r in plan["rejected"]} == {4: "duplicate_in_sheet"}


def _visible(*claims, hidden=()):
    return {"https://a.com/membership": {"status": "page_accessible", "claimResults": [
        {"recordType": "membership", "claim": claim, "status": "not_visible" if claim in hidden else "visible_match"}
        for claim in claims]}}


def test_render_visibility_decides_which_rows_are_written():
    from utils.visibility_manifest import build_shard

    snapshot = {"businesses": [{"business_id": 1, "name": "A Spa"}], "memberships": []}
    rows = [row(sheet_row=2, membership_name="Silver"), row(sheet_row=3, membership_name="Bronze")]
    first = build_plan(rows, snapshot, SHEET_NAMES)
    claims = [c["line"] for item in build_shard(first, "run")["items"] for c in item["claims"]]
    assert {"Silver", "Silver $150", "$150 monthly account credit"} <= set(claims)
    benefits = ("$150 monthly account credit", "10% off products")
    manifest = _visible("Silver", "Silver $150", "Bronze", *benefits, "Bronze $150", hidden=("Bronze $150", "10% off products"))
    plan = build_plan(rows, snapshot, SHEET_NAMES, manifest)
    # Silver 名称与费用可见；一条权益看不到，权益未完整核验即为 NULL
    assert [(e["membership_name"], e["benefits"]) for e in plan["entries"]] == [("Silver", None)]
    assert {r["sheet_row"]: r["reason"] for r in plan["rejected"]} == {3: "membership_name_or_fee_not_visible"}


def test_business_id_taken_over_by_another_business_is_rejected():
    snapshot = {"businesses": [{"business_id": 1, "name": "LaserAway Morningside Heights"}], "memberships": []}
    plan = build_plan([row(sheet_row=2, membership_name="Silver")], snapshot, {1: "Skin Spa New York - Upper West Side"})
    assert plan["entries"] == []
    assert plan["rejected"][0]["reason"] == "business_missing_or_name_differs_in_database"


def test_apply_sql_is_one_checked_block_that_never_overwrites():
    entry, _ = screen_row(row(membership_name="Gold's Plan"), BUSINESSES)
    text = render_apply({"entries": [entry], "visibility_manifest_sha256": "x"})
    assert text.count("do $pipeline$") == 1 and "do nothing" in text and "Gold''s Plan" in text
    assert "stale_plan" in text and "v_inserted <> 1" in text
