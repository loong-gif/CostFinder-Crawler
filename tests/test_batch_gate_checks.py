from utils.batch_gate_checks import CHECKS, run_batch_gate


def _query(rows_by_check):
    def query(sql):
        name = next(name for name, text in CHECKS.items() if text == sql)
        return rows_by_check.get(name, [])
    return query


def test_gate_passes_only_when_every_check_is_empty():
    assert run_batch_gate(None, query=_query({}))["passed"] is True
    gate = run_batch_gate(None, query=_query({"B_visible_estimates": [{"id": 7, "offer_raw_text": "Botox $10 (est.)"}]}))
    assert gate["passed"] is False
    assert gate["checks"]["B_visible_estimates"]["failing"][0]["id"] == 7


def test_only_rows_with_a_documented_reason_are_retained():
    rows = {"C_blanket_service_link": [{"id": 1, "offer_item_id": 11}, {"id": 2, "offer_item_id": 22}]}
    retained = {"C_blanket_service_link": {"11": "single named service, reviewed 2026-10-14", "22": " "}}
    gate = run_batch_gate(None, retained, query=_query(rows))
    check = gate["checks"]["C_blanket_service_link"]
    assert [r["offer_item_id"] for r in check["retained"]] == [11]
    assert [r["offer_item_id"] for r in check["failing"]] == [22]
    assert gate["passed"] is False
