from __future__ import annotations

from datetime import datetime, timezone

from scripts.backfill_missing_scrape_links import (
    group_targets_by_url,
    is_usable_raw,
    choose_latest_usable_raw,
)


def test_group_targets_by_url_deduplicates_across_tables() -> None:
    grouped = group_targets_by_url(
        [
            {
                "table_name": "clinic_promotions",
                "row_id": 1,
                "source_url": "https://www.example.com/specials/",
            },
            {
                "table_name": "clinic_memberships",
                "row_id": 2,
                "source_url": "https://example.com/specials",
            },
        ]
    )

    assert list(grouped) == ["https://example.com/specials"]
    assert [(row["table_name"], row["row_id"]) for row in grouped[list(grouped)[0]]] == [
        ("clinic_promotions", 1),
        ("clinic_memberships", 2),
    ]


def test_is_usable_raw_requires_success_and_payload() -> None:
    assert is_usable_raw({"success": True, "markdown": "# Pricing"})
    assert is_usable_raw({"success": True, "response_json": {"data": {"html": "<p>"}}})
    assert not is_usable_raw({"success": False, "markdown": "# Pricing"})
    assert not is_usable_raw({"success": True, "markdown": ""})


def test_choose_latest_usable_raw_ignores_failed_rows() -> None:
    old = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    new = datetime(2026, 2, 1, tzinfo=timezone.utc).isoformat()
    rows = [
        {"id": 3, "success": False, "markdown": "", "updated_at": new},
        {"id": 2, "success": True, "markdown": "new", "updated_at": new},
        {"id": 1, "success": True, "markdown": "old", "updated_at": old},
    ]

    assert choose_latest_usable_raw(rows)["id"] == 2
