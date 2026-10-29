"""Tests for concrete failure modes of the frozen MCP apply batch."""
import importlib.util
from copy import deepcopy
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verified_apply", ROOT / "one-off/20261011_apply_verified_monitor.py")
apply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply)


class FrozenApplyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (apply.OUT / "frozen_plan.json").exists():
            raise unittest.SkipTest("one-off frozen batch artifacts are not present")

    def test_package_switch_explicitly_clears_units_and_preserves_id_total(self):
        plan = apply.load(apply.OUT / "plan_217.json")
        master = next(o for o in plan["expected"]["offers"] if o["id"] == 8127)
        item = next(i for i in plan["expected"]["items"] if i["offer_id"] == 8127)
        self.assertEqual((master["discount_price"], master["is_package"], item["offer_item_id"], item["delivered_units"], item["unit_price"]), (525, True, 2164, 6, None))
        self.assertIn("package unit price remaining", apply.transaction_sql(plan))

    def test_conditional_peel_does_not_become_unconditional(self):
        plan = apply.load(apply.OUT / "plan_217.json")
        master = next(o for o in plan["expected"]["offers"] if o["id"] == 8126)
        term = next(t for t in plan["expected"]["terms"] if t["offer_id"] == 8126)
        self.assertEqual(master["discount_price"], 250)
        self.assertEqual(term["raw_terms"], master["conditions"])
        self.assertTrue(any("same appointment" in c for c in master["conditions"]))
        self.assertEqual(plan["operations"][-1]["table"], "offers")
        self.assertIn("stale fingerprint", apply.transaction_sql(plan))

    def test_minimum_quantity_is_not_delivered(self):
        from utils.monitor_price_plan import _resolve_items
        items = _resolve_items({"offer_raw_text": "Jeuveau $9/unit minimum 50 units", "quantity_kind": "minimum", "items": [{"service_name": "Jeuveau", "quantity": 50}]}, [{"business_id": 1, "service_id": 10, "service_name": "Jeuveau"}], 1)
        self.assertIsNone(items[0]["delivered_units"])

    def test_deactivation_preserves_inactive_history_and_items(self):
        plan = apply.load(apply.OUT / "plan_3595.json")
        self.assertFalse(next(o for o in plan["expected"]["offers"] if o["id"] == 10397)["is_active"])
        self.assertEqual(plan["changed"]["offers"], [10397])
        self.assertEqual(plan["expected"]["items"], plan["before"]["items"])
        self.assertEqual(plan["expected"]["terms"], plan["before"]["terms"])

    def test_price_history_and_derived_discounts_are_checked(self):
        plan = apply.load(apply.OUT / "plan_217.json")
        self.assertEqual(len(plan["expected_history"]), 3)
        for row in plan["expected_history"]:
            self.assertEqual(row["new_discount_percent"], 30)
        self.assertIn("unexpected price history", apply.transaction_sql(plan))

    def test_before_snapshot_includes_protection_and_versions(self):
        plan = apply.load(apply.OUT / "plan_168.json")
        self.assertIn("confirmations", plan["before"])
        self.assertIn("audit", plan["before"])
        self.assertTrue(all("updated_at" in o for o in plan["before"]["offers"]))
        sql = apply.transaction_sql(plan)
        self.assertLess(sql.index("frozen snapshot/version/protection conflict"), sql.index("UPDATE public."))

    def test_typed_payload_rejects_schema_fields_and_escapes_delimiter(self):
        plan = deepcopy(apply.load(apply.OUT / "plan_3595.json"))
        plan["url"] = "https://example.test/$apply$/'\\"
        sql = apply.transaction_sql(plan)
        self.assertIn("DO $apply_$", sql)
        self.assertNotIn("CREATE TABLE", sql)
        plan["operations"][0]["values"]["injected_schema_field"] = 1
        with self.assertRaises(ValueError):
            apply.transaction_sql(plan)
        with self.assertRaises(ValueError):
            apply.ids([True])
        with self.assertRaises(ValueError):
            apply.literal("bad\x00text")

    def test_unknown_commit_keeps_ids_and_requires_protection_versions(self):
        plan = apply.load(apply.OUT / "plan_3595.json")
        old = deepcopy(plan["before"])
        self.assertEqual(apply.reconcile(plan, old), "retry_same_plan_under_lock")
        old["audit"].append({"id": "new", "action": "approve", "target_type": "deal", "target_id": "10397"})
        self.assertEqual(apply.reconcile(plan, old), "review_conflict_or_partial_state")
        old = deepcopy(plan["before"])
        old["offers"][0]["updated_at"] = "later"
        self.assertEqual(apply.reconcile(plan, old), "review_conflict_or_partial_state")
        current = deepcopy(plan["expected"])
        self.assertEqual(apply.reconcile(plan, current), "confirmed_current_state")

    def test_new_offer_ids_and_relationships_must_all_match(self):
        plan = apply.load(apply.OUT / "plan_217.json")
        if not plan["new_ids"]:
            self.skipTest("ID reservation not yet performed")
        current = deepcopy(plan["expected"])
        current["history"] += [{"id": 900000 + n, "changed_at": "now", **h} for n, h in enumerate(plan["expected_history"])]
        self.assertEqual(apply.reconcile(plan, current), "confirmed_current_state")
        current["items"] = [i for i in current["items"] if i["offer_item_id"] != plan["new_ids"][1]]
        self.assertEqual(apply.reconcile(plan, current), "review_conflict_or_partial_state")


if __name__ == "__main__":
    unittest.main()
