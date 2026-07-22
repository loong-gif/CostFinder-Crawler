"""Self-check for change_driven SQL audit rendering.

Run directly:  python tests/test_change_driven_sql.py
No test framework. All assertions raise on failure.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.change_driven_extractor import build_offer_sql_statements, sql_quote

NOW = "2026-07-02T14:00:00+00:00"
URL = "https://example.com/specials"
DOMAIN = "example.com"


def test_sql_quote_numbers_none_strings() -> None:
    assert sql_quote(None) == "NULL"
    assert sql_quote("") == "NULL"
    assert sql_quote("   ") == "NULL"
    assert sql_quote(99) == "99"
    assert sql_quote(12.5) == "12.5"
    assert sql_quote(True) == "TRUE"
    assert sql_quote(False) == "FALSE"
    assert sql_quote("botox") == "'botox'"
    assert sql_quote("O'Brien") == "'O''Brien'"
    assert sql_quote("a;b") == "'a;b'"


def test_insert_full_fields() -> None:
    offer = {
        "action": "insert",
        "service_name": "Botox",
        "service_category": "Injectables",
        "offer_raw_text": "$10/unit",
        "regular_price": 12,
        "discount_price": 10,
    }
    sqls = build_offer_sql_statements(
        [offer], source_url=URL, source_name=DOMAIN, now_iso=NOW
    )
    assert len(sqls) == 2, sqls
    master_sql, item_sql = sqls
    assert master_sql.startswith("INSERT INTO promo_offer_master ("), master_sql
    assert "is_active" in master_sql and "offer_fingerprint" in master_sql
    assert "channel" not in master_sql and "status" not in master_sql
    assert "10" in master_sql and "12" in master_sql
    assert "'$10/unit'" in master_sql
    assert master_sql.endswith(");"), master_sql
    assert item_sql.startswith("INSERT INTO promo_offer_items"), item_sql
    assert "'Botox'" in item_sql


def test_update_only_non_empty_fields() -> None:
    offer = {
        "action": "update",
        "matched_id": "abc-123",
        "service_name": "  ",
        "discount_price": "8.5",
    }
    sqls = build_offer_sql_statements(
        [offer], source_url=URL, source_name=DOMAIN, now_iso=NOW
    )
    assert len(sqls) == 1, sqls
    sql = sqls[0]
    assert sql.startswith("UPDATE promo_offer_master SET "), sql
    assert "service_name" not in sql, sql  # empty field excluded
    assert "discount_price=8.5" in sql, sql
    assert "updated_at" not in sql, sql
    assert "WHERE id='abc-123';" in sql, sql


def test_mark_ended_fixed_shape() -> None:
    offer = {"action": "mark_ended", "matched_id": "uuid-9"}
    sqls = build_offer_sql_statements(
        [offer], source_url=URL, source_name=DOMAIN, now_iso=NOW
    )
    assert len(sqls) == 1, sqls
    assert sqls[0] == (
        "UPDATE promo_offer_master SET is_active=FALSE WHERE id='uuid-9';"
    ), sqls[0]


def test_empty_offers_returns_empty_list() -> None:
    assert build_offer_sql_statements([], source_url=URL, source_name=DOMAIN, now_iso=NOW) == []


def test_quote_injection_does_not_break_sql() -> None:
    offer = {
        "action": "insert",
        "service_name": "Inject'); DROP TABLE x;--",
        "offer_raw_text": "O'Brien's \"deal\"",
    }
    sqls = build_offer_sql_statements(
        [offer], source_url=URL, source_name=DOMAIN, now_iso=NOW
    )
    assert len(sqls) == 2, sqls
    master_sql, item_sql = sqls
    assert "O''Brien" in master_sql, master_sql
    assert "Inject''); DROP TABLE x;--" in item_sql, item_sql
    assert master_sql.count("INSERT INTO") == 1
    assert item_sql.count("INSERT INTO") == 1


def test_none_and_empty_become_null() -> None:
    offer = {
        "action": "update",
        "matched_id": "id-1",
        "service_name": "",        # -> excluded
        "regular_price": None,     # -> excluded (build_offer_update_payload drops None)
        "discount_price": "8.5",
    }
    sqls = build_offer_sql_statements(
        [offer], source_url=URL, source_name=DOMAIN, now_iso=NOW
    )
    assert len(sqls) == 1, sqls
    assert "discount_price=8.5" in sqls[0], sqls[0]
    assert "regular_price" not in sqls[0], sqls[0]
    assert "membership_name" not in sqls[0], sqls[0]


def test_update_without_matched_id_skipped() -> None:
    offer = {"action": "update", "discount_price": "5"}
    assert build_offer_sql_statements(
        [offer], source_url=URL, source_name=DOMAIN, now_iso=NOW
    ) == []


def test_mark_ended_without_matched_id_skipped() -> None:
    offer = {"action": "mark_ended"}
    assert build_offer_sql_statements(
        [offer], source_url=URL, source_name=DOMAIN, now_iso=NOW
    ) == []


def main() -> None:
    tests = [
        test_sql_quote_numbers_none_strings,
        test_insert_full_fields,
        test_update_only_non_empty_fields,
        test_mark_ended_fixed_shape,
        test_empty_offers_returns_empty_list,
        test_quote_injection_does_not_break_sql,
        test_none_and_empty_become_null,
        test_update_without_matched_id_skipped,
        test_mark_ended_without_matched_id_skipped,
    ]
    for fn in tests:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\nAll {len(tests)} checks passed.")


if __name__ == "__main__":
    main()
