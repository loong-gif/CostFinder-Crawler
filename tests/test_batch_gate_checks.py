import json

from utils import pipeline_db
from utils.batch_gate_checks import CHECKS, run_batch_gate, snapshot_tables


def _read(rows_by_check):
    def read(tables):
        assert set(tables) == set(CHECKS)
        return {name: rows_by_check.get(name, []) for name in tables}
    return read


def test_gate_passes_only_when_every_check_is_empty():
    assert run_batch_gate({}, read=_read({}))["passed"] is True
    gate = run_batch_gate({}, read=_read({"B_visible_estimates": [{"id": 7, "offer_raw_text": "Botox $10 (est.)"}]}))
    assert gate["passed"] is False
    assert gate["checks"]["B_visible_estimates"]["failing"][0]["id"] == 7


def test_only_rows_with_a_documented_reason_are_retained():
    rows = {"E_price_not_in_text": [{"id": 1}, {"id": 2}]}
    retained = {"E_price_not_in_text": {"1": "render still shows both prices, reviewed 2026-10-14", "2": " "}}
    check = run_batch_gate(retained, read=_read(rows))["checks"]["E_price_not_in_text"]
    assert [r["id"] for r in check["retained"]] == [1] and [r["id"] for r in check["failing"]] == [2]


def test_every_check_is_read_through_one_read_only_snapshot(tmp_path, monkeypatch):
    seen = []

    def fake_cli(read_sql):
        seen.append(read_sql.read_text())
        return pipeline_db.write_text(read_sql.parent / "snapshot.json",
                                      json.dumps({name: {"count": 0, "rows": []} for name in CHECKS}))

    monkeypatch.setattr(pipeline_db, "read_snapshot", fake_cli)
    gate = run_batch_gate({}, run_dir=tmp_path)
    assert gate["passed"] is True and len(seen) == 1
    for name in CHECKS:
        assert f"'{name}'" in seen[0]
    pipeline_db.snapshot_query(snapshot_tables())


def test_category_scoped_discount_linked_to_its_category_is_not_a_blanket_violation():
    sql = CHECKS["C_blanket_service_link"]
    assert "substring(lower(m.offer_raw_text) from 'all ([a-z-]+) services')" in sql
    assert "s.service_name::text ~*" in sql


def test_same_benefit_in_different_membership_plans_is_not_a_duplicate():
    assert "group by business_id, promotion_id, membership_plan_id, offer_raw_text" in CHECKS["D_blanket_duplicates"]


def test_buy_get_offers_share_their_quantity_and_are_not_undelivered_packs():
    assert r"\mbuy\s+\d+" in CHECKS["G_units_in_text_not_delivered"]
