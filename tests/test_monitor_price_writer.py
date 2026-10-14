"""Execute the writer against a transactional fake; no database/schema writes."""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import re
import sys

import pytest
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils import monitor_price_writer as writer


PK = {table: spec[1] for table, spec in writer.TABLES.items()} | {"clinic_services": "service_id"}


def initial_state():
    return {
        "clinic_promotions": [{"promotion_id": 10, "business_id": 7,
            "source_url": "https://www.clinic.example/specials/", "is_active": True,
            "campaign_end_date": None, "updated_at": "2026-10-01T00:00:00+00:00"}],
        "promo_offer_master": [{"id": 20, "business_id": 7, "promotion_id": 10,
            "membership_plan_id": None, "discount_price": 100, "is_package": False,
            "offer_raw_text": "Facial $100", "is_active": True, "updated_at": "v1"}],
        "promo_offer_items": [{"offer_item_id": 30, "offer_id": 20, "service_id": 40,
            "item_name": "Facial", "delivered_units": 1, "unit_price": 100, "updated_at": "v1"}],
        "clinic_services": [{"service_id": 40, "business_id": 7, "service_name": "Facial", "updated_at": "v1"}],
    }


class FakeDatabase:
    def __init__(self):
        self.state = initial_state()
        self.connections = []
        self.queries = []
        self.next_id = 100
        self.commit_mode = "normal"
        self.trigger = None
        self.blanket_rows = []

    def connect(self):
        conn = FakeConnection(self)
        self.connections.append(conn)
        return conn


class FakeConnection:
    def __init__(self, db):
        self.db = db
        self.state = None
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self, **kwargs):
        return FakeCursor(self)

    def execute(self, query, params=()):
        return self.cursor().execute(query, params)

    def commit(self):
        self.commits += 1
        if self.db.commit_mode == "crash":
            raise KeyboardInterrupt("process interrupted before commit")
        if self.db.commit_mode == "rejected":
            raise ConnectionError("secret_dsn_never_print")
        self.db.state = deepcopy(self.state)
        self.state = None
        if self.db.commit_mode == "lost_ack":
            raise ConnectionError("secret_dsn_never_print")
        if self.db.commit_mode == "mixed":
            self.db.state["promo_offer_items"][0]["updated_at"] = "concurrent"
            raise ConnectionError("secret_dsn_never_print")

    def rollback(self):
        self.rollbacks += 1
        self.state = None

    def close(self):
        self.closed = True
        self.state = None


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def fetchall(self):
        return deepcopy(self.result)

    def execute(self, query, params=()):
        text = query if isinstance(query, str) else query.as_string(None)
        self.conn.db.queries.append((text, params))
        params = tuple(value.obj if isinstance(value, Jsonb) else value for value in params)
        plain = text.replace('"', '')
        self.result = []
        if plain.startswith("BEGIN"):
            self.conn.state = deepcopy(self.conn.db.state)
            return self
        state = self.conn.state if self.conn.state is not None else self.conn.db.state
        if plain.startswith("LOCK TABLE"):
            return self
        if plain.startswith("SET CONSTRAINTS"):
            if self.conn.db.trigger:
                self.conn.db.trigger(state)
            return self
        if "m.offer_raw_text ~*" in plain:
            self.result = self.conn.db.blanket_rows
            return self
        if "current_date AS today" in plain:
            self.result = [{"today": date(2026, 10, 9)}]
            return self
        if "pg_get_serial_sequence" in plain:
            self.result = [{"sequence": params[0] + "_id_seq"}]
            return self
        if "nextval" in plain:
            self.result = [{"next_id": self.conn.db.next_id}]
            self.conn.db.next_id += 1
            return self
        if "coalesce(max" in plain:
            self.result = [{"next_id": max((r["promotion_id"] for r in state["clinic_promotions"]), default=0) + 1}]
            return self
        if plain.startswith("SELECT"):
            table = re.search(r"FROM (?:public\.)?(\w+)", plain)[1]
            rows = state[table]
            if table == "promo_offer_master" and "business_id = %s OR" in plain:
                rows = [r for r in rows if r.get("business_id") == params[0] or r.get("promotion_id") in params[1]]
            elif "business_id = %s" in plain:
                rows = [r for r in rows if r.get("business_id") == params[0]]
            else:
                field = re.search(r"WHERE (\w+) =", plain)[1]
                rows = [r for r in rows if r.get(field) in params[0]] if "ANY" in plain else [r for r in rows if r.get(field) == params[0]]
            self.result = deepcopy(rows)
            return self
        if plain.startswith("INSERT"):
            table = re.search(r"INTO public\.(\w+)", plain)[1]
            fields = re.search(r"\(([^)]+)\) VALUES", plain)[1].split(", ")
            row = dict(zip(fields, params))
            state[table].append(row)
            self.result = [{PK[table]: row[PK[table]]}]
            return self
        if plain.startswith("UPDATE"):
            table = re.search(r"UPDATE public\.(\w+)", plain)[1]
            fields = re.findall(r"(\w+) = %s", plain)[0:-1]
            rows = [r for r in state[table] if r[PK[table]] == params[-1]]
            for row in rows:
                row.update(zip(fields, params[:-1]))
            self.result = [{PK[table]: row[PK[table]]} for row in rows]
            return self
        raise AssertionError(f"Unimplemented fake query: {plain}")


def plan_for(db):
    before = writer.source_graph(writer.read_business_graph(db.connect(), 7), {"url": "https://clinic.example/specials", "business_id": 7})
    return {"schema_version": 1, "status": "ready", "source": {"business_id": 7,
            "url": "https://clinic.example/specials", "promotion_id": 10}, "before": before,
            "operations": [
                {"table": "promo_offer_master", "action": "update", "id": 20, "values": {"discount_price": 80}},
                {"table": "promo_offer_items", "action": "update", "id": 30, "values": {"unit_price": 80}},
            ], "review": [], "evidence_summary": {"source": "frozen"}}


def journal_path(directory):
    return next(Path(directory).glob("*.json"))


def test_historical_inactive_missing_items_does_not_block_current_update(tmp_path):
    db=FakeDatabase()
    old=deepcopy(db.state["promo_offer_master"][0])
    old.update(id=99,is_active=False)
    db.state["promo_offer_master"].append(old)
    receipt=writer.apply_plan(plan_for(db),db.connect,tmp_path,{})
    assert receipt["status"] == "completed",receipt
    assert db.state["promo_offer_master"][1] == old
    assert db.state["promo_offer_master"][0]["discount_price"] == 80


def test_retry_ignores_protection_file_timestamp_only(tmp_path):
    db=FakeDatabase(); plan=plan_for(db); db.commit_mode="rejected"
    receipt=writer.apply_plan(plan,db.connect,tmp_path,{"updated_at":"one"})
    assert receipt["status"] == "retryable"
    db.commit_mode="normal"
    receipt=writer.apply_plan(plan,db.connect,tmp_path,{"updated_at":"two"})
    assert receipt["status"] == "completed",receipt


def test_success_uses_serializable_parameterized_sql_and_full_durable_journal(tmp_path):
    db = FakeDatabase()
    plan = plan_for(db)
    plan["review"] = [{"category": "nonblocking_image"}]
    receipt = writer.apply_plan(plan, db.connect, tmp_path, {})
    assert receipt["status"] == "completed"
    assert db.state["promo_offer_master"][0]["discount_price"] == 80
    journal = json.loads(journal_path(tmp_path).read_text())
    assert journal["stage"] == "completed"
    assert journal["after"] == receipt["after"]
    assert journal["before"]["offers"][0]["discount_price"] == 100
    assert any(q == "BEGIN ISOLATION LEVEL SERIALIZABLE" for q, _ in db.queries)
    assert any(q == "SET CONSTRAINTS ALL IMMEDIATE" for q, _ in db.queries)
    updates = [(q, p) for q, p in db.queries if q.startswith("UPDATE")]
    assert updates[0][1] == (80, 20) and '"discount_price" = %s' in updates[0][0]
    assert any(q == writer.BLANKET_CHECK_SQL for q, _ in db.queries)


def test_same_plan_completed_receipt_does_not_write_again(tmp_path):
    db = FakeDatabase()
    plan = plan_for(db)
    first = writer.apply_plan(plan, db.connect, tmp_path, {})
    count = sum(q.startswith("UPDATE") for q, _ in db.queries)
    assert writer.apply_plan(plan, db.connect, tmp_path, {}) == first
    assert sum(q.startswith("UPDATE") for q, _ in db.queries) == count


@pytest.mark.parametrize("mode,status", [("lost_ack", "completed"), ("rejected", "retryable"), ("mixed", "review")])
def test_unknown_commit_reads_new_connection_before_classifying(tmp_path, mode, status):
    db = FakeDatabase()
    plan = plan_for(db)
    db.commit_mode = mode
    receipt = writer.apply_plan(plan, db.connect, tmp_path, {})
    assert receipt["status"] == status
    assert len(db.connections) == 3
    assert db.connections[-2].closed and db.connections[-1].closed
    assert "secret_dsn" not in json.dumps(receipt)


@pytest.mark.parametrize("kind", ["version", "extra_offer", "extra_item", "extra_alias_promotion", "service", "missing_item"])
def test_stale_full_graph_rejects_membership_and_versions_without_dml(tmp_path, kind):
    db = FakeDatabase()
    plan = plan_for(db)
    if kind == "version":
        db.state["promo_offer_master"][0]["updated_at"] = "v2"
    elif kind == "extra_offer":
        db.state["promo_offer_master"].append(dict(db.state["promo_offer_master"][0], id=21))
    elif kind == "extra_item":
        db.state["promo_offer_items"].append(dict(db.state["promo_offer_items"][0], offer_item_id=31))
    elif kind == "extra_alias_promotion":
        db.state["clinic_promotions"].append(dict(db.state["clinic_promotions"][0], promotion_id=11, source_url="http://clinic.example/specials"))
    elif kind == "service":
        db.state["clinic_services"][0]["updated_at"] = "v2"
    else:
        db.state["promo_offer_items"].clear()
    receipt = writer.apply_plan(plan, db.connect, tmp_path, {})
    assert receipt["category"] == "stale_before"
    assert not any(q.startswith(("INSERT", "UPDATE")) for q, _ in db.queries)
    assert db.connections[-1].rollbacks == 1


@pytest.mark.parametrize("category,trigger", [
    ("protected_row_changed", lambda state: state["clinic_promotions"][0].update(promotion_title="trigger changed")),
    ("unexpected_deactivation", lambda state: state["promo_offer_master"][0].update(is_active=False)),
    ("requested_fields_changed", lambda state: state["promo_offer_items"][0].update(unit_price=3)),
])
def test_trigger_side_effect_rolls_back_whole_batch(tmp_path, category, trigger):
    db = FakeDatabase()
    plan = plan_for(db)
    before = deepcopy(db.state)
    # Protect a separate row, allowing the planned source edits to reach triggers.
    db.state["clinic_promotions"].append({"promotion_id": 99, "business_id": 88, "source_url": "https://other.example/specials"})
    before = deepcopy(db.state)
    if category == "protected_row_changed":
        trigger = lambda state: state["clinic_promotions"][-1].update(promotion_title="trigger changed")
    db.trigger = trigger
    receipt = writer.apply_plan(plan, db.connect, tmp_path, {"promotion_ids": [99]})
    assert receipt["category"] == category
    assert db.state == before
    assert db.connections[-1].commits == 0 and db.connections[-1].rollbacks == 1


def test_direct_protected_operation_blocked(tmp_path):
    db = FakeDatabase()
    receipt = writer.apply_plan(plan_for(db), db.connect, tmp_path, {"entries": [{"table": "promo_offer_items", "id": 30}]})
    assert receipt["category"] == "protected_operation"
    assert not any(q.startswith("UPDATE") for q, _ in db.queries)


def test_blanket_global_check_rolls_back_even_unrelated_matching_row(tmp_path):
    db = FakeDatabase()
    before = deepcopy(db.state)
    db.blanket_rows = [{"id": 999, "offer_raw_text": "all treatments", "item_name": "Facial"}]
    receipt = writer.apply_plan(plan_for(db), db.connect, tmp_path, {})
    assert receipt["category"] == "blanket_service_mapping"
    assert db.state == before


def insert_plan(db):
    plan = plan_for(db)
    plan["operations"] = [
        {"table": "clinic_promotions", "action": "insert", "id": -1,
         "values": {"business_id": 7, "source_url": plan["source"]["url"], "is_active": True}},
        {"table": "promo_offer_master", "action": "insert", "id": -1,
         "values": {"business_id": 7, "promotion_id": -1, "membership_plan_id": None,
                    "discount_price": 50, "is_package": False, "is_active": True, "offer_raw_text": "Facial $50"}},
        {"table": "promo_offer_items", "action": "insert", "id": -1,
         "values": {"offer_id": -1, "service_id": 40, "item_name": "Facial", "delivered_units": 1, "unit_price": 50}},
    ]
    return plan


def test_allocate_negative_ids_and_activate_after_items(tmp_path):
    db = FakeDatabase()
    plan = insert_plan(db)
    receipt = writer.apply_plan(plan, db.connect, tmp_path, {})
    assert receipt["status"] == "completed"
    assert receipt["allocated_ids"] == {"clinic_promotions": {"-1": 11}, "promo_offer_master": {"-1": 100}, "promo_offer_items": {"-1": 101}}
    assert db.state["promo_offer_master"][-1]["promotion_id"] == 11
    assert db.state["promo_offer_items"][-1]["offer_id"] == 100
    dml = [(q, p) for q, p in db.queries if q.startswith(("INSERT", "UPDATE"))]
    assert dml[1][1][-3] is False  # master initially inactive
    assert dml[-1][0].startswith('UPDATE "public"."promo_offer_master"')
    assert dml[-1][1] == (True, 100)
    assert any(q.startswith("LOCK TABLE public.clinic_promotions") for q, _ in db.queries)
    assert not any(re.search(r"\b(CREATE|ALTER|DROP)\b", q) for q, _ in db.queries)


def test_crash_before_full_after_saved_recovers_retryable(tmp_path, monkeypatch):
    db = FakeDatabase()
    plan = insert_plan(db)
    original = writer.write_json
    def interrupted(path, value):
        if value.get("stage") == "committing":
            raise KeyboardInterrupt("crash before final journal")
        original(path, value)
    monkeypatch.setattr(writer, "write_json", interrupted)
    with pytest.raises(KeyboardInterrupt):
        writer.apply_plan(plan, db.connect, tmp_path, {})
    monkeypatch.setattr(writer, "write_json", original)
    journal = json.loads(journal_path(tmp_path).read_text())
    assert journal["stage"] == "allocated" and "after" not in journal
    receipt = writer.recover_journal(journal_path(tmp_path), db.connect)
    assert receipt["status"] == "retryable"
    assert writer.apply_plan(plan, db.connect, tmp_path, {})["status"] == "completed"


def test_crash_after_full_after_saved_before_commit_recovers_retryable(tmp_path):
    db = FakeDatabase()
    db.commit_mode = "crash"
    with pytest.raises(KeyboardInterrupt):
        writer.apply_plan(insert_plan(db), db.connect, tmp_path, {})
    assert json.loads(journal_path(tmp_path).read_text())["stage"] == "committing"
    assert writer.recover_journal(journal_path(tmp_path), db.connect)["status"] == "retryable"


def test_recovery_before_missing_after_with_allocated_id_present_requires_review(tmp_path, monkeypatch):
    db = FakeDatabase()
    plan = insert_plan(db)
    original = writer.write_json
    def crash(path, value):
        if value.get("stage") == "committing":
            raise KeyboardInterrupt
        original(path, value)
    monkeypatch.setattr(writer, "write_json", crash)
    with pytest.raises(KeyboardInterrupt):
        writer.apply_plan(plan, db.connect, tmp_path, {})
    monkeypatch.setattr(writer, "write_json", original)
    # Same allocated ID appearing under another business is not safe to retry.
    db.state["promo_offer_master"].append({"id": 100, "business_id": 88, "promotion_id": None})
    assert writer.recover_journal(journal_path(tmp_path), db.connect)["category"] == "ambiguous_state"


@pytest.mark.parametrize("values", [{"unexpected": 1}, {"updated_at": "v3"}, {"id": 20}])
def test_allowlist_rejects_fields_before_connect(tmp_path, values):
    db = FakeDatabase()
    plan = plan_for(db)
    plan["operations"][0]["values"] = values
    with pytest.raises(writer.WriterError, match="invalid_fields"):
        writer.apply_plan(plan, lambda: pytest.fail("must not connect"), tmp_path, {})


def test_url_normalization_preserves_path_case_and_query():
    assert writer.normalize_source_url(" HTTP://www.CLINIC.example/Specials/?offer=1#banner ") == "clinic.example/Specials?offer=1"
    assert writer.normalize_source_url("https://clinic.example/specials?offer=1") != writer.normalize_source_url("https://clinic.example/Specials?offer=1")
    with pytest.raises(writer.WriterError):
        writer.normalize_source_url("https://user:password@clinic.example/specials")


@pytest.mark.parametrize("protections", [{"promotion_ids": [10]}, {"offer_ids": [20]}])
def test_parent_protection_blocks_existing_child_operations(tmp_path, protections):
    db = FakeDatabase()
    receipt = writer.apply_plan(plan_for(db), db.connect, tmp_path, protections)
    assert receipt["category"] == "protected_operation"
    assert not any(q.startswith(("INSERT", "UPDATE")) for q, _ in db.queries)


def test_parent_protection_blocks_new_child(tmp_path):
    db = FakeDatabase()
    plan = insert_plan(db)
    plan["operations"] = plan["operations"][1:]
    plan["operations"][0]["values"]["promotion_id"] = 10
    receipt = writer.apply_plan(plan, db.connect, tmp_path, {"promotion_ids": [10]})
    assert receipt["category"] == "protected_operation"


@pytest.mark.parametrize("case,category", [("package", "package_unit_price"),
    ("arithmetic", "unit_total_mismatch"), ("quantity", "invalid_quantity"),
    ("missing_item", "offer_without_items"), ("membership", "invalid_offer_source"),
    ("inactive_promotion", "inactive_promotion")])
def test_final_invariants_rollback(tmp_path, case, category):
    db = FakeDatabase()
    if case == "membership":
        db.state["promo_offer_master"][0]["membership_plan_id"] = 50
    if case == "inactive_promotion":
        db.state["clinic_promotions"][0]["is_active"] = False
    plan = plan_for(db)
    if case == "package":
        plan["operations"][0]["values"]["is_package"] = True
    elif case == "arithmetic":
        plan["operations"][1]["values"]["unit_price"] = 79
    elif case == "quantity":
        plan["operations"][1]["values"]["delivered_units"] = 0
    elif case == "missing_item":
        db.state["promo_offer_items"] = []
        plan = plan_for(db)
        plan["operations"] = plan["operations"][:1]
    before = deepcopy(db.state)
    receipt = writer.apply_plan(plan, db.connect, tmp_path, {})
    assert receipt["category"] == category
    assert db.state == before


def test_is_offer_extracted_live_field_is_writable(tmp_path):
    db = FakeDatabase()
    plan = plan_for(db)
    plan["operations"].append({"table": "clinic_promotions", "id": 10, "action": "update", "values": {"is_offer_extracted": True}})
    assert writer.apply_plan(plan, db.connect, tmp_path, {})["status"] == "completed"


def test_protected_parent_detects_trigger_inserted_child(tmp_path):
    db = FakeDatabase()
    plan = plan_for(db)
    db.state["clinic_promotions"].append({"promotion_id": 99, "business_id": 88, "source_url": "https://other.example/specials"})
    db.trigger = lambda state: state["promo_offer_master"].append({"id": 999, "business_id": 88, "promotion_id": 99})
    receipt = writer.apply_plan(plan, db.connect, tmp_path, {"promotion_ids": [99]})
    assert receipt["category"] == "protected_row_changed"
    assert not any(row["id"] == 999 for row in db.state["promo_offer_master"])


def test_promotion_content_is_adapted_as_jsonb(tmp_path):
    db = FakeDatabase()
    plan = plan_for(db)
    quotes = [{"offer_raw_text": "Facial $80", "source_quote": "Facial $80"}]
    plan["operations"].append({"table": "clinic_promotions", "id": 10, "action": "update", "values": {"promotion_content": quotes}})
    assert writer.apply_plan(plan, db.connect, tmp_path, {})["status"] == "completed"
    bound = [params for query, params in db.queries if query.startswith("UPDATE") and '"promotion_content"' in query][0]
    assert isinstance(bound[0], Jsonb) and bound[0].obj == quotes
    assert db.state["clinic_promotions"][0]["promotion_content"] == quotes


@pytest.mark.parametrize("wrong,status", [(False, "completed"), (True, "review")])
def test_live_derived_discounts_checked_against_formula(tmp_path, wrong, status):
    db = FakeDatabase()
    plan = plan_for(db)
    plan["operations"][0]["values"].update(regular_price=120, discount_amount=None, discount_percent=None)
    def derived(state):
        state["promo_offer_master"][0].update(discount_amount=40, discount_percent=33.33 if not wrong else 30)
    db.trigger = derived
    receipt = writer.apply_plan(plan, db.connect, tmp_path, {})
    assert receipt["status"] == status
    if wrong:
        assert receipt["category"] == "requested_fields_changed"
        assert db.state["promo_offer_master"][0]["discount_price"] == 100


@pytest.mark.parametrize("text,status", [("20% off all services", "completed"), ("gift card $80", "completed"), ("20% off entire menu", "completed"), ("Facial $80", "completed")])
def test_evidenced_blanket_and_concrete_null_service_can_be_active(tmp_path, text, status):
    db = FakeDatabase()
    plan = plan_for(db)
    plan["operations"][0]["values"]["offer_raw_text"] = text
    plan["operations"][1]["values"].update(service_id=None)
    receipt = writer.apply_plan(plan, db.connect, tmp_path, {})
    assert receipt["status"] == status
    if status == "review":
        assert receipt["category"] == "active_service_unresolved"
