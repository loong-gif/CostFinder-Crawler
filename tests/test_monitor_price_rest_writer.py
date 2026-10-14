"""REST request boundaries with live trigger semantics, no network or DDL."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils import monitor_price_rest_writer as writer
from utils.monitor_price_plan import build_plan

PK = {table: spec[1] for table, spec in writer.TABLES.items()} | {"clinic_services": "service_id"}


def state():
    return {
        "clinic_promotions": [{"promotion_id": 10, "business_id": 7,
            "source_url": "https://clinic.example/specials", "is_active": True,
            "is_offer_extracted": True, "promotion_content": ["Facial $100"],
            "campaign_end_date": None, "updated_at": "2026-10-01T00:00:00+00:00"}],
        "promo_offer_master": [{"id": 20, "business_id": 7, "promotion_id": 10,
            "membership_plan_id": None, "discount_price": 100, "regular_price": None,
            "price_model": "total", "is_package": False, "conditions": [],
            "offer_raw_text": "Facial $100", "is_active": True, "updated_at": "v1"}],
        "promo_offer_items": [{"offer_item_id": 30, "offer_id": 20, "service_id": 40,
            "item_name": "Facial", "delivered_units": 1, "unit_price": 100, "updated_at": "v1"}],
        "promo_offer_terms": [],
        "clinic_services": [{"service_id": 40, "business_id": 7, "service_name": "Facial",
            "service_area": None, "source_url":"https://clinic.example/facial", "updated_at": "v1"}],
    }


def _matches(row, filters):
    for key, value in (filters or {}).items():
        if value.startswith("in.("):
            if row.get(key) not in [int(x) for x in value[4:-1].split(",")]:
                return False
        elif value == "not.is.null":
            if row.get(key) is None:
                return False
        elif value.startswith("is."):
            if row.get(key) != {"null": None, "true": True, "false": False}[value[3:]]:
                return False
        else:
            raw = value[3:]
            existing = row.get(key)
            if isinstance(existing, list):
                # Ensure actual field types in the query match the DB schema.
                if key in {"conditions", "raw_terms"}:
                    assert raw.startswith("{") and raw.endswith("}")
                    expected = [] if raw == "{}" else json.loads("[" + raw[1:-1] + "]")
                else:
                    expected = json.loads(raw)
                if existing != expected:
                    return False
            elif str(existing) != raw:
                return False
    return True


class FakeRest:
    def __init__(self):
        self.state = state()
        self.writes = []
        self.counter = 100
        self.timeout_at = None
        self.timeout_before = False
        self.crash_at = None
        self.concurrent = None
        self.read_failure = False

    def fetch_rows(self, table, select, *, filters=None, limit=None, offset=None, order=None):
        if self.read_failure:
            raise ConnectionError("secret_key_never_export")
        rows = [r for r in self.state[table] if _matches(r, filters)]
        if order:
            field, direction = order.split(".")
            rows = sorted(rows, key=lambda r: r[field], reverse=direction == "desc")
        rows = rows[offset or 0:]
        if limit:
            rows = rows[:limit]
        if select != "*":
            rows = [{k: r[k] for k in select.split(",")} for r in rows]
        return deepcopy(rows)

    def _write(self, table, filters, values, insert):
        self.writes.append((table, deepcopy(filters), deepcopy(values), insert))
        n = len(self.writes)
        if self.concurrent:
            hook, self.concurrent = self.concurrent, None
            hook(self.state)
        if self.timeout_at == n and self.timeout_before:
            raise TimeoutError("credential_never_export")
        old_state = deepcopy(self.state)
        if insert:
            row = dict.fromkeys(writer.TABLES[writer.TERMS][2]) if table == writer.TERMS else {}
            if table == writer.TERMS:
                row["raw_terms"] = []
            row.update(deepcopy(values))
            if PK[table] not in row:
                row[PK[table]] = self.counter
                self.counter += 1
            if any(r[PK[table]] == row[PK[table]] for r in self.state[table]):
                raise ValueError("duplicate primary key")
            row.setdefault("updated_at", f"v{n+1}")
            row.setdefault("created_at", "2026-10-09T00:00:00+00:00")
            self.state[table].append(row)
        else:
            matches = [r for r in self.state[table] if _matches(r, filters)]
            if not matches:
                return []
            assert len(matches) == 1
            row = matches[0]
            row.update(deepcopy(values))
            row["updated_at"] = f"v{n+1}"
        if table == "promo_offer_master":
            self._derived(row, next((r for r in old_state[table] if r["id"] == row["id"]), {}))
            if not row.get("is_package") and any(k in values and old_state[table] and values[k] != next((r.get(k) for r in old_state[table] if r["id"] == row["id"]), None) for k in ("discount_price", "price_model")) and not insert:
                self._sync(row)
            row["offer_fingerprint"] = f"f{n}"
            if row.get("is_active"):
                parent = next(r for r in self.state["clinic_promotions"] if r["promotion_id"] == row["promotion_id"])
                children = [i for i in self.state["promo_offer_items"] if i["offer_id"] == row["id"]]
                if not parent["is_active"] or not children:
                    row["is_active"] = False
            if insert or set(values) & {"conditions", "is_new_customer_required", "is_membership_required", "membership_plan_id"}:
                self._legacy_terms(row, n)
        elif table == "promo_offer_items":
            parent = next(r for r in self.state["promo_offer_master"] if r["id"] == row["offer_id"])
            if insert or set(values) & {"offer_id", "service_id", "delivered_units", "unit_price"}:
                self._normalize(parent, row)
            if insert or set(values) & {"offer_id", "service_id"}:
                if parent.get("is_package"):
                    for child in self.state["promo_offer_items"]:
                        if child["offer_id"] == parent["id"]:
                            child["unit_price"] = None
                            child["updated_at"] = f"v{n+1}"
            if insert or set(values) & {"offer_id", "service_id", "delivered_units"}:
                self._sync(parent)
            if insert or set(values) & {"offer_id", "service_id", "delivered_units", "item_name"}:
                parent["offer_fingerprint"] = f"f{n}"
                parent["updated_at"] = f"v{n+1}"
        elif table == "clinic_promotions" and values.get("is_active") is False:
            for child in self.state["promo_offer_master"]:
                if child["promotion_id"] == row["promotion_id"] and child["is_active"]:
                    child["is_active"] = False
                    child["updated_at"] = f"v{n+1}"
                    child["offer_fingerprint"] = f"f{n}"
        # Deferred constraint runs at the end of each REST request, even when inactive.
        for master in self.state["promo_offer_master"]:
            if not master.get("is_package") and master.get("discount_price") is not None:
                for child in self.state["promo_offer_items"]:
                    if child["offer_id"] == master["id"]:
                        if child["unit_price"] is None or round(child["unit_price"] * (child.get("delivered_units") or 1), 2) != round(master["discount_price"], 2):
                            self.state = old_state
                            raise ValueError("per-request price constraint")
        if self.crash_at == n:
            raise KeyboardInterrupt()
        if self.timeout_at == n:
            raise TimeoutError("credential_never_export")
        return [deepcopy(row)]

    def _normalize(self, master, item):
        if master.get("is_package") or (master.get("discount_price") is None and master.get("price_model") != "per_unit"):
            item["unit_price"] = None
        elif master.get("discount_price") is not None:
            item["unit_price"] = round(master["discount_price"] / (item.get("delivered_units") or 1), 4)

    def _sync(self, master):
        if not master.get("is_package"):
            for item in self.state["promo_offer_items"]:
                if item["offer_id"] == master["id"]:
                    old = item["unit_price"]
                    self._normalize(master, item)
                    if item["unit_price"] != old:
                        item["updated_at"] = f"v{len(self.writes)+1}"

    def _derived(self, master, old):
        regular, discount = master.get("regular_price"), master.get("discount_price")
        if regular is not None and discount is not None and 0 <= discount < regular:
            master["discount_amount"] = round(regular - discount, 2)
            master["discount_percent"] = round((regular-discount) * 100 / regular, 2)
        elif old.get("regular_price") is not None and old.get("discount_price") is not None and 0 <= old["discount_price"] < old["regular_price"]:
            prior = {"discount_amount": round(old["regular_price"]-old["discount_price"], 2),
                     "discount_percent": round((old["regular_price"]-old["discount_price"])*100/old["regular_price"], 2)}
            for key, value in prior.items():
                if master.get(key) == old.get(key) == value:
                    master[key] = None

    def _legacy_terms(self, master, n):
        terms = next((r for r in self.state[writer.TERMS] if r["offer_id"] == master["id"]), None)
        if terms is None:
            terms = dict.fromkeys(writer.TABLES[writer.TERMS][2])
            terms.update(created_at=f"v{n+1}", updated_at=f"v{n+1}")
            self.state[writer.TERMS].append(terms)
        raw = master.get("conditions") or []
        returning = any("returning customer required" in c.lower() for c in raw)
        new = master.get("is_new_customer_required")
        terms.update(offer_id=master["id"], raw_terms=deepcopy(raw),
                     membership_plan_id=master.get("membership_plan_id"),
                     is_new_customer_required=True if new is True else False if returning else new,
                     is_returning_customer_required=False if new is True else True if returning else None,
                     is_membership_required=True if master.get("membership_plan_id") is not None else master.get("is_membership_required"))


    def update_row(self, table, filters, values):
        return self._write(table, filters, values, False)

    def insert_rows(self, table, rows):
        assert len(rows) == 1
        return self._write(table, None, rows[0], True)


def plan_for(client, operations=None):
    source = {"business_id": 7, "url": "https://clinic.example/specials", "promotion_id": 10}
    return {"schema_version": 1, "status": "ready", "source": source,
            "before": writer.source_graph(writer.read_business_graph(client, 7), source),
            "operations": operations or [
                {"table": "promo_offer_master", "action": "update", "id": 20, "values": {"discount_price": 80}},
                {"table": "promo_offer_items", "action": "update", "id": 30, "values": {"unit_price": 80}}],
            "evidence_summary": {"complete": True}, "review": []}


def terms_plan(client, *, existing=True, change_conditions=False, new_offer=False):
    """Freeze a real planner candidate, including Terms-only repair cases."""
    raw = "Facial 1 session Regular $125 Special $100. One per patient"
    conditions = ["One per patient"]
    master = client.state["promo_offer_master"][0]
    master.update(offer_raw_text=raw, regular_price=125, discount_amount=None,
                  discount_percent=None, conditions=conditions,
                  is_membership_required=False, is_new_customer_required=False)
    client.state["clinic_promotions"][0]["promotion_content"] = [raw]
    if existing:
        terms = dict.fromkeys(writer.TABLES[writer.TERMS][2])
        terms.update(offer_id=20, raw_terms=['old, "quote" \\ slash'],
                     is_one_per_patient=False, is_new_customer_required=False,
                     is_membership_required=False, created_at="v0", updated_at="v1")
        client.state[writer.TERMS] = [terms]
    if change_conditions:
        master["conditions"] = ["Cannot be combined"]
    offer = {k: deepcopy(master[k]) for k in (
        "offer_raw_text", "regular_price", "discount_price", "discount_percent", "discount_amount",
        "price_model", "is_package", "is_membership_required", "is_new_customer_required")}
    offer.update(conditions=conditions, visibility="visible_match", quantity_kind="fixed",
                 items=[{"item_name": "Facial", "service_id": 40, "delivered_units": 1, "unit_price": 100}])
    source = plan_for(client)["source"]
    evidence = {"url": source["url"], "accessible": True, "complete": True,
                "markdown": raw, "offers": [offer]}
    if new_offer:
        client.state["promo_offer_master"] = []
        client.state["promo_offer_items"] = []
        client.state[writer.TERMS] = []
    graph = writer.source_graph(writer.read_business_graph(client, 7), source)
    plan = build_plan(source, graph, evidence, {})
    assert plan["status"] == "ready", plan
    return plan


@pytest.mark.parametrize("existing", [True, False])
def test_planner_terms_only_update_or_missing_insert_preserves_activation(tmp_path, existing):
    client = FakeRest(); plan = terms_plan(client, existing=existing)
    assert len(plan["operations"]) == 1, plan["operations"]
    op = plan["operations"][0]
    assert op["table"] == writer.TERMS and op["id"] == 20
    assert op["action"] == ("update" if existing else "insert")
    assert ("offer_id" in op["values"]) is not existing
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["status"] == "completed", receipt
    assert client.state[writer.TERMS][0]["raw_terms"] == ["One per patient"]
    assert client.state[writer.TERMS][0]["is_one_per_patient"] is True
    assert client.state["promo_offer_master"][0]["is_active"] is True
    assert client.state["clinic_promotions"][0]["is_offer_extracted"] is True
    assert [table for table, _, _, _ in client.writes] == [
        "promo_offer_master", "clinic_promotions", writer.TERMS,
        "promo_offer_master", "clinic_promotions"]
    terms_write = client.writes[2]
    if existing:
        assert terms_write[1]["offer_id"] == "eq.20"
        assert terms_write[1]["updated_at"] == "eq.v1"
        assert terms_write[1]["raw_terms"] == writer._eq(['old, "quote" \\ slash'], "raw_terms")
        assert terms_write[1]["raw_terms"].startswith("eq.{")
    writes = len(client.writes)
    assert writer.apply_plan(plan, client, tmp_path, {}) == receipt
    assert len(client.writes) == writes


@pytest.mark.parametrize("existing,new_offer", [(True, False), (False, False), (False, True)])
def test_planner_terms_with_master_trigger_uses_verified_cas_row(tmp_path, existing, new_offer):
    client = FakeRest(); plan = terms_plan(client, existing=existing, change_conditions=True, new_offer=new_offer)
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["status"] == "completed", receipt
    offer = client.state["promo_offer_master"][0]
    terms = client.state[writer.TERMS][0]
    assert terms["offer_id"] == offer["id"]
    assert terms["raw_terms"] == ["One per patient"]
    assert terms["is_one_per_patient"] is True
    terms_write = next(w for w in client.writes if w[0] == writer.TERMS)
    assert terms_write[3] is False  # Master trigger inserted/upserted this row first.
    assert terms_write[1]["offer_id"] == f"eq.{offer['id']}"
    assert offer["is_active"] is True


@pytest.mark.parametrize("protected", [{"offer_ids": [20]}, {"item_ids": [30]}, {"promotion_ids": [10]}])
def test_terms_only_change_respects_parent_and_child_protection(tmp_path, protected):
    client = FakeRest(); plan = terms_plan(client)
    receipt = writer.apply_plan(plan, client, tmp_path, protected)
    assert receipt["category"] == "protected_row_or_cascade"
    assert not client.writes


def test_terms_changed_since_plan_is_stale_before_any_write(tmp_path):
    client = FakeRest(); plan = terms_plan(client)
    client.state[writer.TERMS][0]["min_quantity"] = 10
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["category"] == "stale_before"
    assert not client.writes


@pytest.mark.parametrize("change_timestamp", [False, True])
def test_terms_cas_conflict_keeps_offer_inactive_and_preserves_manual_terms(tmp_path, change_timestamp):
    class ConcurrentTerms(FakeRest):
        def update_row(self, table, filters, values):
            if table == writer.TERMS:
                self.state[table][0]["raw_terms"] = ["manual terms"]
                if change_timestamp:
                    self.state[table][0]["updated_at"] = "manual"
            return super().update_row(table, filters, values)
    client = ConcurrentTerms(); plan = terms_plan(client)
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["category"] == "cas_conflict"
    assert client.state[writer.TERMS][0]["raw_terms"] == ["manual terms"]
    assert client.state["promo_offer_master"][0]["is_active"] is False
    assert client.state["clinic_promotions"][0]["is_offer_extracted"] is False


@pytest.mark.parametrize("change_parent", [False, True])
def test_terms_request_cannot_modify_parent_or_unrequested_terms(tmp_path, change_parent):
    class BadTerms(FakeRest):
        def update_row(self, table, filters, values):
            returned = super().update_row(table, filters, values)
            if table == writer.TERMS:
                if change_parent:
                    self.state["promo_offer_master"][0]["offer_raw_text"] = "unexpected"
                else:
                    self.state[table][0]["promo_code"] = "unexpected"
            return returned
    client = BadTerms(); plan = terms_plan(client)
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["category"] == "unexpected_terms_or_trigger_change"
    assert client.state["promo_offer_master"][0]["is_active"] is False


@pytest.mark.parametrize("before", [False, True])
def test_terms_patch_timeout_reconciles_then_resumes_once(tmp_path, before):
    client = FakeRest(); plan = terms_plan(client)
    client.timeout_at = 3; client.timeout_before = before
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["status"] == "review"
    assert client.state["promo_offer_master"][0]["is_active"] is False
    client.timeout_at = None
    journal_path = next(tmp_path.glob("*.json"))
    assert writer.recover_journal(journal_path, client)["status"] == "retryable"
    assert writer.apply_plan(plan, client, tmp_path, {})["status"] == "completed"
    assert len([w for w in client.writes if w[0] == writer.TERMS]) == (2 if before else 1)


@pytest.mark.parametrize("before", [False, True])
def test_unknown_terms_insert_never_reposts_or_activates(tmp_path, before):
    client = FakeRest(); plan = terms_plan(client, existing=False)
    client.timeout_at = 3; client.timeout_before = before
    assert writer.apply_plan(plan, client, tmp_path, {})["status"] == "review"
    client.timeout_at = None
    journal_path = next(tmp_path.glob("*.json"))
    assert writer.recover_journal(journal_path, client)["category"] == "insert_outcome_unknown"
    assert writer.apply_plan(plan, client, tmp_path, {})["category"] == "insert_outcome_unknown"
    assert len([w for w in client.writes if w[0] == writer.TERMS]) == 1
    assert client.state["promo_offer_master"][0]["is_active"] is False


def test_base_request_cannot_hide_unexpected_terms_change(tmp_path):
    client = FakeRest(); terms_plan(client)
    plan = plan_for(client)
    client.concurrent = lambda s: s[writer.TERMS][0].update(promo_code="manual")
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["category"] == "unexpected_terms_or_trigger_change"
    assert len(client.writes) == 1


@pytest.mark.parametrize("new,expected_new,expected_returning", [
    (None, False, True), (False, False, True), (True, True, False)])
def test_master_qualification_trigger_verifies_nullable_returning_terms(tmp_path, new, expected_new, expected_returning):
    client = FakeRest()
    plan = plan_for(client, [{"table": "promo_offer_master", "action": "update", "id": 20,
                             "values": {"conditions": ["Returning CUSTOMER required"],
                                        "is_new_customer_required": new}}])
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["status"] == "completed", receipt
    terms = client.state[writer.TERMS][0]
    assert terms["is_new_customer_required"] is expected_new
    assert terms["is_returning_customer_required"] is expected_returning
    assert terms["min_quantity"] is None


@pytest.mark.parametrize("changes,category", [
    ({"id": 99}, "invalid_terms_reference"),
    ({"values": {"offer_id": 99, "raw_terms": []}}, "invalid_terms_operation"),
    ({"values": {"updated_at": "manual"}}, "invalid_terms_operation"),
    ({"values": {"membership_plan_id": 123}}, "ownership_changed"),
])
def test_invalid_terms_operation_fails_without_requests(tmp_path, changes, category):
    client = FakeRest(); plan = terms_plan(client)
    plan["operations"][0].update(changes)
    with pytest.raises(writer.WriterError, match=category):
        writer.apply_plan(plan, client, tmp_path, {})
    assert not client.writes


def test_price_update_cas_trigger_readbacks_and_replay(tmp_path):
    client = FakeRest(); plan = plan_for(client)
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["status"] == "completed", receipt
    assert receipt["atomic"] is False
    assert client.state["promo_offer_master"][0]["id"] == 20
    assert client.state["promo_offer_items"][0]["unit_price"] == 80
    assert client.writes[0][2] == {"is_active": False}
    assert all("updated_at" in filters for _, filters, _, insert in client.writes if not insert)
    writes = len(client.writes)
    assert writer.apply_plan(plan, client, tmp_path, {}) == receipt
    assert len(client.writes) == writes


@pytest.mark.parametrize("reverse_terms", [False, True])
def test_runner_snapshot_and_writer_read_service_projection_are_identical(tmp_path, reverse_terms):
    from scripts.run_monitor_price_sync import snapshot_business_graph
    client=FakeRest();source={"business_id":7,"url":"https://clinic.example/specials"}
    client.state["promo_offer_master"].append({**client.state["promo_offer_master"][0],"id":21})
    client.state["promo_offer_items"].append({**client.state["promo_offer_items"][0],"offer_id":21,"offer_item_id":31})
    client.state[writer.TERMS] = [{"offer_id":rid,"raw_terms":[],"updated_at":"v1"} for rid in (20,21)]
    if reverse_terms:
        client.state[writer.TERMS].reverse()
    snapshot={"promotions":deepcopy(client.state["clinic_promotions"]),
              "offers":deepcopy(client.state["promo_offer_master"]),
              "items":deepcopy(client.state["promo_offer_items"]),
              "terms":deepcopy(client.state["promo_offer_terms"]),
              "services":client.fetch_rows("clinic_services",writer.SERVICE_COLUMNS)}
    before=writer.source_graph(snapshot_business_graph(snapshot,7),source)
    assert before == writer.source_graph(writer.read_business_graph(client,7),source)
    plan=plan_for(client);plan["before"]=before
    receipt=writer.apply_plan(plan,client,tmp_path,{})
    assert receipt["status"] == "completed",receipt


@pytest.mark.parametrize("before", [False, True])
def test_patch_timeout_reconciles_exact_state_and_resumes(tmp_path, before):
    client = FakeRest(); plan = plan_for(client)
    client.timeout_at = 3; client.timeout_before = before
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["status"] == "review"
    assert client.state["promo_offer_master"][0]["is_active"] is False
    client.timeout_at = None
    journal = next(tmp_path.glob("*.json"))
    assert writer.recover_journal(journal, client)["status"] == "retryable"
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["status"] == "completed", receipt
    price_writes = [v for _, _, v, _ in client.writes if "discount_price" in v]
    assert len(price_writes) == (2 if before else 1)
    assert "credential_never_export" not in journal.read_text()


def test_crash_after_patch_before_journal_readback_recovers(tmp_path):
    client = FakeRest(); plan = plan_for(client); client.crash_at = 3
    with pytest.raises(KeyboardInterrupt):
        writer.apply_plan(plan, client, tmp_path, {})
    client.crash_at = None
    assert writer.apply_plan(plan, client, tmp_path, {})["status"] == "completed"


@pytest.mark.parametrize("protected", [{"offer_ids": [20]}, {"item_ids": [30]}, {"promotion_ids": [10]}])
def test_indirect_protection_blocks_before_any_request(tmp_path, protected):
    client = FakeRest()
    plan = plan_for(client, [{"table": "promo_offer_items", "action": "update", "id": 30, "values": {"unit_price": 80}}])
    receipt = writer.apply_plan(plan, client, tmp_path, protected)
    assert receipt["category"] == "protected_row_or_cascade"
    assert not client.writes


def test_protected_item_blocks_parent_content_change(tmp_path):
    client = FakeRest()
    plan = plan_for(client, [{"table": "clinic_promotions", "action": "update", "id": 10, "values": {"promotion_content": ["Facial $100 now"]}}])
    assert writer.apply_plan(plan, client, tmp_path, {"item_ids": [30]})["category"] == "protected_row_or_cascade"
    assert not client.writes


def test_version_change_before_request_pauses_without_overwrite(tmp_path):
    client = FakeRest(); plan = plan_for(client)
    client.concurrent = lambda s: s["promo_offer_master"][0].update(updated_at="manual")
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["category"] == "cas_conflict"
    assert client.state["promo_offer_master"][0]["discount_price"] == 100
    repeated = writer.apply_plan(plan, client, tmp_path, {})
    assert {k:v for k,v in repeated.items() if k != "readback"} == receipt
    assert repeated["readback"]["matches_last_ack"] is False
    assert client.state["promo_offer_master"][0]["updated_at"] == "manual"


def test_source_membership_change_during_patch_stops_source(tmp_path):
    client = FakeRest(); plan = plan_for(client)
    client.concurrent = lambda s: s["promo_offer_master"].append({**s["promo_offer_master"][0], "id": 88, "is_active": False})
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["category"] == "unexpected_row_or_trigger_change"
    assert len(client.writes) == 1


def additions(client, new_promotion=False):
    values = deepcopy(client.state["promo_offer_master"][0])
    for key in ("id", "updated_at"):
        values.pop(key)
    values.update(offer_raw_text="Facial $50", discount_price=50)
    ops = []
    if new_promotion:
        ops.append({"table": "clinic_promotions", "action": "insert", "id": -1,
                    "values": {"business_id": 7, "source_url": "https://clinic.example/specials", "promotion_content": ["Facial $50"], "is_active": True, "is_offer_extracted": True}})
        values["promotion_id"] = -1
    ops += [{"table": "promo_offer_master", "action": "insert", "id": -1, "values": values},
            {"table": "promo_offer_items", "action": "insert", "id": -1, "values": {"offer_id": -1, "service_id": 40, "item_name": "Facial", "delivered_units": 1, "unit_price": 50}}]
    return plan_for(client, ops)


@pytest.mark.parametrize("new_promotion", [False, True])
def test_insert_defaults_ids_and_activation_only_after_children(tmp_path, new_promotion):
    client = FakeRest(); plan = additions(client, new_promotion)
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["status"] == "completed", receipt
    insert_calls = [(t, v) for t, _, v, ins in client.writes if ins]
    for table, values in insert_calls:
        if table != "clinic_promotions":
            assert PK[table] not in values
        if table in {"clinic_promotions", "promo_offer_master"}:
            assert values["is_active"] is False
    assert client.state["promo_offer_master"][-1]["is_active"] is True
    assert receipt["allocated_ids"]["promo_offer_master"]["-1"] == 100
    if new_promotion:
        assert receipt["allocated_ids"]["clinic_promotions"]["-1"] == 11
        assert client.state["clinic_promotions"][-1]["is_offer_extracted"] is True


@pytest.mark.parametrize("before", [False, True])
def test_unknown_post_never_reposts_even_when_absent(tmp_path, before):
    client = FakeRest(); plan = additions(client)
    client.timeout_at = 1; client.timeout_before = before
    assert writer.apply_plan(plan, client, tmp_path, {})["status"] == "review"
    client.timeout_at = None
    journal = next(tmp_path.glob("*.json"))
    assert writer.recover_journal(journal, client)["category"] == "insert_outcome_unknown"
    assert writer.apply_plan(plan, client, tmp_path, {})["category"] == "insert_outcome_unknown"
    assert len(client.writes) == 1
    assert len(client.state["promo_offer_master"]) == (1 if before else 2)


def test_promotion_content_json_cas_and_text_array_filters(tmp_path):
    client = FakeRest()
    plan = plan_for(client, [{"table": "clinic_promotions", "action": "update", "id": 10,
                            "values": {"promotion_content": ['Facial $100; (new), "quote"'], "is_offer_extracted": True}}])
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["status"] == "completed", receipt
    assert writer._eq(['a,b', 'x"y'], "conditions") == 'eq.{"a,b","x\\"y"}'
    assert json.loads(writer._eq(["offer"], "promotion_content")[3:]) == ["offer"]
    assert writer._eq(2989) == "eq.2989"
    assert writer._eq("2026-10-09T12:00:00+00:00") == "eq.2026-10-09T12:00:00+00:00"


def test_fixed_quantity_preserves_master_total_not_double_division(tmp_path):
    client = FakeRest(); client.state["promo_offer_items"][0].update(delivered_units=10, unit_price=10)
    plan = plan_for(client, [{"table": "promo_offer_master", "action": "update", "id": 20, "values": {"discount_price": 80}},
                            {"table": "promo_offer_items", "action": "update", "id": 30, "values": {"unit_price": 8}}])
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["status"] == "completed", receipt
    assert client.state["promo_offer_master"][0]["discount_price"] == 80
    assert client.state["promo_offer_items"][0]["unit_price"] == 8


def test_package_switch_clears_on_item_request_not_master(tmp_path):
    client = FakeRest()
    plan = plan_for(client, [{"table": "promo_offer_master", "action": "update", "id": 20, "values": {"is_package": True}},
                            {"table": "promo_offer_items", "action": "update", "id": 30, "values": {"unit_price": None}}])
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["status"] == "completed", receipt
    assert client.state["promo_offer_items"][0]["unit_price"] is None


def test_package_to_nonpackage_without_sync_trigger_is_preflight_review(tmp_path):
    client = FakeRest(); client.state["promo_offer_master"][0]["is_package"] = True
    client.state["promo_offer_items"][0]["unit_price"] = None
    plan = plan_for(client, [{"table": "promo_offer_master", "action": "update", "id": 20, "values": {"is_package": False}},
                            {"table": "promo_offer_items", "action": "update", "id": 30, "values": {"unit_price": 100}}])
    assert writer.apply_plan(plan, client, tmp_path, {})["category"] == "rest_intermediate_price_state"
    assert not client.writes


def test_percentage_only_per_unit_explicit_rate_survives(tmp_path):
    client = FakeRest()
    client.state["promo_offer_master"][0].update(discount_price=None, discount_percent=10, price_model="per_unit")
    plan = plan_for(client, [{"table": "promo_offer_items", "action": "update", "id": 30, "values": {"unit_price": 90}}])
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["status"] == "completed", receipt
    assert client.state["promo_offer_items"][0]["unit_price"] == 90


def test_deactivation_preserves_ids_and_historical_rows(tmp_path):
    client = FakeRest()
    plan = plan_for(client, [{"table": "promo_offer_master", "action": "update", "id": 20, "values": {"is_active": False}},
                            {"table": "clinic_promotions", "action": "update", "id": 10, "values": {"is_active": False, "is_offer_extracted": False}}])
    assert writer.apply_plan(plan, client, tmp_path, {})["status"] == "completed"
    assert len(client.state["promo_offer_items"]) == 1
    assert not client.state["promo_offer_master"][0]["is_active"]
    assert not client.state["clinic_promotions"][0]["is_active"]


def test_stale_evidence_and_unsupported_final_graph_do_not_write(tmp_path):
    client = FakeRest(); plan = plan_for(client)
    plan["prepared_at"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    assert writer.apply_plan(plan, client, tmp_path / "a", {})["category"] == "expired_unstarted_refresh_evidence"
    assert not client.writes
    plan = plan_for(client, [{"table": "promo_offer_items", "action": "update", "id": 30, "values": {"service_id": None}}])
    assert writer.apply_plan(plan, client, tmp_path / "b", {})["status"] == "completed"
    assert client.state["promo_offer_items"][0]["service_id"] is None
    assert client.state["promo_offer_master"][0]["is_active"] is True


def test_null_service_requires_evidenced_nonempty_item_name(tmp_path):
    client = FakeRest()
    plan = plan_for(client, [{"table": "promo_offer_items", "action": "update", "id": 30,
                             "values": {"service_id": None, "item_name": ""}}])
    assert writer.apply_plan(plan, client, tmp_path, {})["category"] == "missing_item_name"
    assert not client.writes


def test_blanket_global_gate_equivalent_to_agents_sql(tmp_path):
    client = FakeRest()
    client.state["promo_offer_master"].append({**client.state["promo_offer_master"][0], "id": 99, "business_id": 9, "promotion_id": None, "offer_raw_text": "20% off all services"})
    client.state["promo_offer_items"].append({**client.state["promo_offer_items"][0], "offer_item_id": 98, "offer_id": 99})
    receipt = writer.apply_plan(plan_for(client), client, tmp_path, {})
    assert receipt["status"] == "review" and receipt["category"] == "blanket_service_mapping"


def test_legacy_sql_journal_never_uses_rest_replay(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"stage": "committing"}))
    client = FakeRest()
    assert writer.recover_journal(path, client)["category"] == "legacy_sql_journal_requires_review"
    assert not client.writes


def test_acknowledged_insert_id_survives_subsequent_get_failure(tmp_path):
    class FailingReadback(FakeRest):
        def insert_rows(self, table, rows):
            result = super().insert_rows(table, rows)
            self.read_failure = True
            return result
    client = FailingReadback(); plan = additions(client)
    assert writer.apply_plan(plan, client, tmp_path, {})["status"] == "review"
    journal_path = next(tmp_path.glob("*.json"))
    journal = json.loads(journal_path.read_text())
    assert journal["allocated_ids"]["promo_offer_master"]["-1"] == 100
    assert journal["pending"]["returned"][0]["id"] == 100
    client.read_failure = False
    assert writer.recover_journal(journal_path, client)["status"] == "retryable"
    # Don't fail the subsequent item readback again.
    client.insert_rows = lambda table, rows: FakeRest.insert_rows(client, table, rows)
    assert writer.apply_plan(plan, client, tmp_path, {})["status"] == "completed"
    assert len([t for t, _, _, ins in client.writes if ins and t == "promo_offer_master"]) == 1


def test_failed_readback_does_not_claim_completion_or_leak_credentials(tmp_path):
    client = FakeRest(); plan = plan_for(client)
    client.read_failure = True
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["status"] == "review"
    assert not client.writes
    assert "secret_key_never_export" not in next(tmp_path.glob("*.json")).read_text()


def test_runner_old_journal_membership_guard_precedes_recovery(tmp_path, monkeypatch):
    from scripts import run_monitor_price_sync as runner
    (tmp_path / "journals").mkdir(); (tmp_path / "events").mkdir()
    journal = {"transport": "rest", "stage": "review", "plan": {"source": {"url": "https://clinic.example/specials", "business_id": 7}}}
    (tmp_path / "journals" / "old.json").write_text(json.dumps(journal))
    event = {"event_id": "new", "url": "https://clinic.example/specials", "status": "planned", "created_at": "now"}
    (tmp_path / "events" / "new.json").write_text(json.dumps(event))
    snapshot = {"confirmations": [], "audit": [], "memberships": [{"source_url": event["url"], "business_id": 7}]}
    monkeypatch.setattr(runner, "target_client", lambda: object())
    monkeypatch.setattr(runner, "recover_journal", lambda *a: pytest.fail("scope rejected before recovery"))
    monkeypatch.setattr(runner, "apply_plan", lambda *a: pytest.fail("no membership writes"))
    monkeypatch.setattr(runner, "prepare_event", lambda *a: pytest.fail("blocked source doesn't reextract"))
    report = runner.process_events(tmp_path, snapshot, write_enabled=True)
    assert report["sources"][0]["category"] == "membership_source_requires_review"


def test_runner_review_blocks_only_its_source(tmp_path, monkeypatch):
    from scripts import run_monitor_price_sync as runner
    (tmp_path / "journals").mkdir(); (tmp_path / "events").mkdir()
    journal = {"transport": "rest", "stage": "review", "plan": {"source": {"url": "https://clinic.example/specials", "business_id": 7}}}
    (tmp_path / "journals" / "old.json").write_text(json.dumps(journal))
    for name, url in (("old", "https://clinic.example/specials"), ("other", "https://other.example/specials")):
        (tmp_path / "events" / f"{name}.json").write_text(json.dumps({"event_id": name, "url": url, "status": "planned", "created_at": name}))
    monkeypatch.setattr(runner, "target_client", lambda: object())
    monkeypatch.setattr(runner, "bind_source", lambda *a: {"business_id": 7})
    monkeypatch.setattr(runner, "recover_journal", lambda *a: {"status": "review", "category": "insert_outcome_unknown"})
    monkeypatch.setattr(runner, "prepare_event", lambda *a: {"status": "ready", "operations": []})
    applied = []
    monkeypatch.setattr(runner, "apply_plan", lambda *a: applied.append(a[0]) or {"status": "completed"})
    report = runner.process_events(tmp_path, {"confirmations": [], "audit": []}, write_enabled=True)
    assert len(applied) == 1
    assert {r["status"] for r in report["sources"]} == {"completed", "review"}


def test_service_key_backend_never_requires_db_password(monkeypatch):
    from scripts import run_monitor_price_sync as runner
    monkeypatch.setenv("SUPABASE_URL", "https://kdlpkjzcnbkjcvwsvlwn.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-only-key")
    monkeypatch.delenv("DB_URL", raising=False)
    client = runner.target_client()
    assert client.base_url.endswith("/rest/v1")
    monkeypatch.setenv("SUPABASE_URL", "https://other.supabase.co")
    with pytest.raises(RuntimeError, match="database_project_mismatch"):
        runner.target_client()


@pytest.mark.parametrize("field,value", [("discount_percent", 20), ("discount_amount", 25)])
def test_stale_derived_price_restored_as_explicit_source_discount(tmp_path, field, value):
    client = FakeRest()
    client.state["promo_offer_master"][0].update(regular_price=125, discount_price=100,
                                                discount_amount=25, discount_percent=20)
    values = {"regular_price": None, "discount_price": None, "offer_raw_text": f"Facial {value}% off"}
    values["discount_amount" if field == "discount_percent" else "discount_percent"] = None
    plan = plan_for(client, [{"table": "promo_offer_master", "action": "update", "id": 20, "values": values},
                            {"table": "promo_offer_items", "action": "update", "id": 30, "values": {"unit_price": None}}])
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["status"] == "completed", receipt
    assert client.state["promo_offer_master"][0][field] == value
    assert any(v == {field: value} for table, _, v, _ in client.writes if table == "promo_offer_master")
    assert client.state["promo_offer_master"][0]["is_active"] is True


def test_recovery_read_timeout_can_be_retried_without_rewriting_patch(tmp_path):
    client = FakeRest(); plan = plan_for(client); client.timeout_at = 3
    assert writer.apply_plan(plan, client, tmp_path, {})["status"] == "review"
    journal_path = next(tmp_path.glob("*.json"))
    client.read_failure = True
    assert writer.recover_journal(journal_path, client)["category"] == "readback_unavailable"
    assert "category" not in json.loads(journal_path.read_text())
    client.read_failure = False; client.timeout_at = None
    assert writer.recover_journal(journal_path, client)["status"] == "retryable"
    assert writer.apply_plan(plan, client, tmp_path, {})["status"] == "completed"
    assert len([v for _, _, v, _ in client.writes if "discount_price" in v]) == 1


def test_runner_recovered_source_defers_newer_event_to_fresh_snapshot(tmp_path, monkeypatch):
    from scripts import run_monitor_price_sync as runner
    (tmp_path / "journals").mkdir(); (tmp_path / "events").mkdir()
    old = {"transport": "rest", "stage": "review", "plan": {"event_id": "old", "source": {"url": "https://clinic.example/specials", "business_id": 7}}}
    (tmp_path / "journals" / "old.json").write_text(json.dumps(old))
    for name in ("old", "new"):
        (tmp_path / "events" / f"{name}.json").write_text(json.dumps({"event_id": name, "url": "https://clinic.example/specials", "status": "planned", "created_at": "a" if name == "old" else "b"}))
    monkeypatch.setattr(runner, "target_client", lambda: object())
    monkeypatch.setattr(runner, "bind_source", lambda *a: {"business_id": 7})
    monkeypatch.setattr(runner, "recover_journal", lambda *a: {"status": "completed"})
    monkeypatch.setattr(runner, "prepare_event", lambda *a: pytest.fail("pre-recovery snapshot must not freeze plan"))
    monkeypatch.setattr(runner, "apply_plan", lambda *a: pytest.fail("no stale journal is created"))
    report = runner.process_events(tmp_path, {"confirmations": [], "audit": []}, write_enabled=True)
    assert report["sources"][0]["status"] == "retryable"
    assert report["sources"][0]["review"][0]["reason"] == "source_recovered_refresh_evidence"
    assert json.loads((tmp_path / "events" / "new.json").read_text())["status"] == "retryable"
    assert json.loads((tmp_path / "events" / "old.json").read_text())["status"] == "completed"
    assert len(list((tmp_path / "journals").glob("*.json"))) == 1


def test_initial_get_failure_recovery_allows_first_write_once_reads_return(tmp_path):
    client = FakeRest(); plan = plan_for(client)
    client.read_failure = True
    assert writer.apply_plan(plan, client, tmp_path, {})["status"] == "review"
    journal_path = next(tmp_path.glob("*.json"))
    assert "current" not in json.loads(journal_path.read_text())
    client.read_failure = False
    assert writer.recover_journal(journal_path, client)["status"] == "retryable"
    assert not client.writes
    assert writer.apply_plan(plan, client, tmp_path, {})["status"] == "completed"


def test_weekly_recovery_retires_expired_initial_get_failure_without_replaying(tmp_path):
    client=FakeRest();plan=plan_for(client)
    plan["prepared_at"]=datetime.now(timezone.utc).isoformat()
    client.read_failure=True
    assert writer.apply_plan(plan,client,tmp_path,{})["status"] == "review"
    path=next(tmp_path.glob("*.json"));journal=json.loads(path.read_text())
    journal["plan"]["prepared_at"]=(datetime.now(timezone.utc)-timedelta(days=7)).isoformat()
    path.write_text(json.dumps(journal));client.read_failure=False
    retired=writer.recover_journal(path,client)
    assert retired["status"] == "superseded" and not retired.get("resume_required")
    assert not client.writes
    fresh=plan_for(client);fresh["prepared_at"]=datetime.now(timezone.utc).isoformat()
    assert writer.apply_plan(fresh,client,tmp_path,{})["status"] == "completed"
    assert json.loads(path.read_text())["stage"] == "superseded"


def test_old_state_after_unknown_patch_does_not_prove_no_request_was_sent(tmp_path):
    client=FakeRest();plan=plan_for(client);client.timeout_at=1;client.timeout_before=True
    assert writer.apply_plan(plan,client,tmp_path,{})["status"] == "review"
    path=next(tmp_path.glob("*.json"));client.timeout_at=None
    assert writer.recover_journal(path,client)["status"] == "retryable"
    journal=json.loads(path.read_text())
    assert journal["requests_sent"] == 1 and journal["next_step"] == 0 and not journal.get("pending")
    journal["plan"]["prepared_at"]=(datetime.now(timezone.utc)-timedelta(days=7)).isoformat()
    assert not writer._expired_without_requests(journal)
    journal.pop("requests_sent")  # Legacy journals get the same conservative treatment.
    assert not writer._expired_without_requests(journal)


@pytest.mark.parametrize("protection,allowed",[({"offer_ids":[999999]},True),
    ({"offer_ids":[20]},False),({"item_ids":[30]},False),({"promotion_ids":[10]},False)])
def test_resume_checks_only_protection_of_affected_source(tmp_path,protection,allowed):
    client=FakeRest();plan=plan_for(client);client.timeout_at=3
    assert writer.apply_plan(plan,client,tmp_path,{})["status"] == "review"
    path=next(tmp_path.glob("*.json"));client.timeout_at=None
    assert writer.recover_journal(path,client)["status"] == "retryable"
    count=len(client.writes)
    receipt=writer.apply_plan(plan,client,tmp_path,protection)
    assert receipt["status"] == ("completed" if allowed else "review"),receipt
    if not allowed:
        assert receipt["category"] == "protected_row_or_cascade"
        assert len(client.writes) == count


@pytest.mark.parametrize("protected_group,table",[("promotion_ids","clinic_promotions"),
    ("offer_ids","promo_offer_master"),("item_ids","promo_offer_items")])
def test_newly_allocated_real_ids_remain_protected_during_resume(tmp_path,protected_group,table):
    client=FakeRest();plan=additions(client,new_promotion=True)
    client.timeout_at=4;client.timeout_before=True
    assert writer.apply_plan(plan,client,tmp_path,{})["status"] == "review"
    path=next(tmp_path.glob("*.json"));client.timeout_at=None
    assert writer.recover_journal(path,client)["status"] == "retryable"
    journal=json.loads(path.read_text());real_id=journal["allocated_ids"][table]["-1"]
    count=len(client.writes)
    receipt=writer.apply_plan(plan,client,tmp_path,{protected_group:[real_id]})
    assert receipt["status"] == "review" and receipt["category"] == "protected_row_or_cascade"
    assert len(client.writes) == count
    offer_id=journal["allocated_ids"]["promo_offer_master"]["-1"]
    assert next(r for r in client.state["promo_offer_master"] if r["id"] == offer_id)["is_active"] is False


def test_insert_with_unexpected_discount_never_reaches_activation(tmp_path):
    class BadTrigger(FakeRest):
        def insert_rows(self, table, rows):
            result = super().insert_rows(table, rows)
            if table == "promo_offer_master":
                self.state[table][-1]["discount_percent"] = 99
                result[0]["discount_percent"] = 99
            return result
    client = BadTrigger(); plan = additions(client)
    plan["operations"][0]["values"]["discount_percent"] = None
    receipt = writer.apply_plan(plan, client, tmp_path, {})
    assert receipt["category"] == "requested_fields_changed"
    assert client.state["promo_offer_master"][-1]["is_active"] is False
    assert len(client.writes) == 1


def test_runner_orphan_journal_review_is_reported_without_pending_event(tmp_path, monkeypatch):
    from scripts import run_monitor_price_sync as runner
    (tmp_path / "journals").mkdir()
    journal = {"transport": "rest", "stage": "review", "plan": {"source": {"url": "https://clinic.example/specials", "business_id": 7}}}
    (tmp_path / "journals" / "old.json").write_text(json.dumps(journal))
    monkeypatch.setattr(runner, "target_client", lambda: object())
    monkeypatch.setattr(runner, "bind_source", lambda *a: {"business_id": 7})
    monkeypatch.setattr(runner, "recover_journal", lambda *a: {"status": "review", "category": "insert_outcome_unknown"})
    report = runner.process_events(tmp_path, {"confirmations": [], "audit": []}, write_enabled=True)
    assert report["sources"] == [{"url": "https://clinic.example/specials", "status": "review", "category": "insert_outcome_unknown"}]


def test_runner_caps_actual_unknown_insert_readbacks_without_reposting(tmp_path, monkeypatch):
    from scripts import run_monitor_price_sync as runner
    from utils.monitor_price_retry import RetryLimits
    class CountReads(FakeRest):
        reads = 0
        def fetch_rows(self, *args, **kwargs):
            self.reads += 1
            return super().fetch_rows(*args, **kwargs)
    client = CountReads(); plan = additions(client); client.timeout_at = 1
    journal_dir = tmp_path / "journals"
    assert writer.apply_plan(plan, client, journal_dir, {})["category"] == "request_or_readback_unconfirmed"
    journal_path = next(journal_dir.glob("*.json"))
    pending = json.loads(journal_path.read_text())["pending"]
    writes = len(client.writes)
    client.timeout_at = None
    monkeypatch.setattr(runner, "target_client", lambda: client)
    monkeypatch.setattr(runner, "bind_source", lambda *a: plan["source"])
    for _ in range(3):
        client.reads = 0
        runner.process_events(tmp_path, {"confirmations": [], "audit": []}, write_enabled=True, retryable_only=True)
        assert client.reads > 0
    assert RetryLimits(tmp_path).paused("recovery", plan["source"]["url"])
    client.reads = 0
    runner.process_events(tmp_path, {"confirmations": [], "audit": []}, write_enabled=True, retryable_only=True)
    assert client.reads == 0 and len(client.writes) == writes
    assert json.loads(journal_path.read_text())["pending"] == pending
    runner.process_events(tmp_path, {"confirmations": [], "audit": []}, write_enabled=True, recovery_probe=True)
    assert client.reads > 0 and len(client.writes) == writes
    assert json.loads(journal_path.read_text())["receipt"]["category"] == "insert_outcome_unknown"
