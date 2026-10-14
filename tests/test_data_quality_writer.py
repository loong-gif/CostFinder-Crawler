from copy import deepcopy

import pytest

from utils.data_quality_writer import apply_repair, recover_repairs, scope
from utils.monitor_price_rest_writer import _eq


def snapshot():
    return {"promotions": [{"promotion_id": 1, "business_id": 7, "source_url": "https://clinic.example/specials", "is_active": True}],
            "memberships": [], "services": [], "terms": [],
            "offers": [{"id": 1, "business_id": 7, "promotion_id": 1, "membership_plan_id": None,
                        "is_active": True, "discount_price": None, "regular_price": None}],
            "items": [{"offer_item_id": 101, "offer_id": 1, "item_name": "Botox", "service_id": None}],
            "confirmations": [], "audit": []}


class MemoryClient:
    tables = {"clinic_promotions": "promotions", "clinic_memberships": "memberships",
              "clinic_services": "services", "promo_offer_master": "offers", "promo_offer_items": "items",
              "promo_offer_terms": "terms", "offer_confirmations": "confirmations", "admin_audit_log": "audit"}

    def __init__(self, data):
        self.data = deepcopy(data)
        self.requests = 0
        self.timeout = None
        self.read_failure = False
        self.cas_miss = False

    def assert_write_project(self):
        pass

    def fetch_rows(self, table, select, *, filters=None, limit=500, offset=0, order=None):
        if self.read_failure:
            self.read_failure = False
            raise TimeoutError("read unavailable")
        rows = self.data[self.tables[table]]
        for key, value in (filters or {}).items():
            if value.startswith("in.("):
                ids = {int(x) for x in value[4:-1].split(",")}
                rows = [r for r in rows if r.get(key) in ids]
            else:
                rows = [r for r in rows if _eq(r.get(key), key) == value]
        return deepcopy(rows[offset:offset+limit])

    def update_row(self, table, filters, values):
        self.requests += 1
        if self.timeout == "before":
            self.timeout = None
            raise TimeoutError("request did not land")
        rows = self.data[self.tables[table]]
        matched = [r for r in rows if all(_eq(r.get(k), k) == v for k, v in filters.items())]
        if self.cas_miss:
            return []
        assert len(matched) == 1
        matched[0].update(values)
        if self.timeout == "after":
            self.timeout = None
            raise TimeoutError("response lost")
        return deepcopy(matched)


def repair(data):
    return {"table": "promo_offer_master", "id": 1, "business_id": 7, "source_url": "https://clinic.example/specials",
            "before": deepcopy(data["offers"][0]), "values": {"is_active": False}}


def test_patch_readback_and_unrelated_rows_are_verified(tmp_path):
    data = snapshot()
    client = MemoryClient(data)
    receipt = apply_repair(repair(data), scope(data, 7), client, tmp_path, {})
    assert receipt["status"] == "completed"
    assert client.data["offers"][0]["is_active"] is False
    assert client.data["items"] == data["items"]


def test_scope_or_target_change_skips_without_a_request(tmp_path):
    data = snapshot()
    client = MemoryClient(data)
    client.data["items"][0]["item_name"] = "manually revised"
    receipt = apply_repair(repair(data), scope(data, 7), client, tmp_path, {})
    assert receipt["status"] == "conflict"
    assert client.requests == 0


def test_parent_protection_blocks_child_deactivation(tmp_path):
    data = snapshot()
    client = MemoryClient(data)
    receipt = apply_repair(repair(data), scope(data, 7), client, tmp_path, {"promotion_ids": [1]})
    assert receipt["reason"] == "manual_protection"
    assert client.requests == 0


def test_late_admin_protection_is_reloaded_at_write_boundary(tmp_path):
    data = snapshot()
    client = MemoryClient(data)
    client.data["audit"] = [{"id": 10, "target_type": "deal", "target_id": "1", "action": "update", "created_at": "now"}]
    receipt = apply_repair(repair(data), scope(data, 7), client, tmp_path, {})
    assert receipt["reason"] == "manual_protection"
    assert client.requests == 0


@pytest.mark.parametrize("when,expected_requests", [("before", 2), ("after", 1)])
def test_patch_timeout_recovery_distinguishes_old_and_expected_state(tmp_path, when, expected_requests):
    data = snapshot()
    client = MemoryClient(data)
    client.timeout = when
    receipt = apply_repair(repair(data), scope(data, 7), client, tmp_path, {})
    assert receipt["resume_required"] is True
    recovered = recover_repairs(client, tmp_path, {})
    assert recovered[0]["status"] == "completed"
    assert client.requests == expected_requests
    assert client.data["offers"][0]["is_active"] is False


def test_known_cas_miss_is_not_success(tmp_path):
    data = snapshot()
    client = MemoryClient(data)
    client.cas_miss = True
    receipt = apply_repair(repair(data), scope(data, 7), client, tmp_path, {})
    assert receipt["status"] == "conflict"
    assert client.data["offers"][0]["is_active"] is True


def test_catalog_scalar_patch_keeps_other_fields_and_plan_ids(tmp_path):
    data = snapshot()
    data["memberships"] = [{"plan_id": 461, "business_id": 7, "membership_price": 120,
                            "benefits": None, "is_offer_extracted": False}]
    client = MemoryClient(data)
    patch = {"table": "clinic_memberships", "id": 461, "business_id": 7,
             "before": deepcopy(data["memberships"][0]), "values": {"benefits": ["10% off products"]}}
    receipt = apply_repair(patch, scope(data, 7), client, tmp_path, {})
    assert receipt["status"] == "completed"
    assert client.data["memberships"][0]["membership_price"] == 120
    assert client.data["memberships"][0]["plan_id"] == 461


def test_verify_transition_does_not_mutate_the_snapshots_it_compares():
    from utils.data_quality_writer import verify_transition
    before = {"memberships": [{"plan_id": 1, "business_id": 7, "is_offer_extracted": True, "updated_at": "t0"},
                              {"plan_id": 2, "business_id": 7, "is_offer_extracted": True, "updated_at": "t0"}]}
    after = deepcopy(before)
    after["memberships"][0].update(is_offer_extracted=False, updated_at="t1")
    original_before, original_after = deepcopy(before), deepcopy(after)
    verify_transition(before, after, {"table": "clinic_memberships", "id": 1, "values": {"is_offer_extracted": False}})
    # The after scope becomes the next sibling repair's frozen state; it must stay exact.
    assert before == original_before
    assert after == original_after
