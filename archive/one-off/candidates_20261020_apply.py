import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

from requests import HTTPError
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.batch_gate_checks import linked_query
from utils.clinic_service_extraction import attach_service_ids_to_items
from utils.extraction_persist import build_master_from_offer
from utils.firecrawl_scrape_raw_db import canonical_scrape_url
from utils.membership_plan_lookup import normalize_plan_name
from utils.monitor_price_store import atomic_json, now, run_lock
from utils.monitor_price_writer import BLANKET_CHECK_SQL
from utils.offer_evidence_validator import validate_offer_evidence
from utils.offer_purchase_semantics import service_supported_by_source
from utils.promo_offer_items_db import is_blanket_offer_text, upsert_offer_items
from utils.rendered_visibility_gate import normalize_claim
from utils.supabase_access import load_supabase_client

OUT = ROOT / "one-off/outputs/candidates_20261020"
KEYS = {"clinic_services": "service_id", "clinic_memberships": "plan_id", "clinic_promotions": "promotion_id"}


def read(path):
    return json.loads(path.read_text())


def insert_promotion(client, payload):
    columns = list(payload)
    statement = sql.SQL("""
begin;
lock table public.clinic_promotions in share row exclusive mode;
insert into public.clinic_promotions (promotion_id, {columns})
select (select coalesce(max(promotion_id), 0) + 1 from public.clinic_promotions), {values}
from jsonb_populate_record(null::public.clinic_promotions, {payload}::jsonb) r
where not exists (select 1 from public.clinic_promotions p
 where p.business_id = r.business_id and p.source_url = r.source_url);
commit;
""").format(columns=sql.SQL(',').join(map(sql.Identifier, columns)),
             values=sql.SQL(',').join(sql.Identifier('r', column) for column in columns),
             payload=sql.Literal(json.dumps(payload, ensure_ascii=False))).as_string()
    linked = read(ROOT / "supabase/.temp/linked-project.json")
    assert linked["ref"] == "kdlpkjzcnbkjcvwsvlwn", "database_project_mismatch"
    result = subprocess.run(["supabase", "db", "query", "--linked", "-o", "json", statement],
                            cwd=ROOT, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError("promotion_transaction_failed:" + result.stderr[-500:])
    return client.fetch_rows("clinic_promotions", "*", filters={
        "business_id": f"eq.{payload['business_id']}", "source_url": f"eq.{payload['source_url']}"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    plan = read(OUT / "full_plan.json")
    schema = read(ROOT / "schema/live_schema_snapshot.json")
    client = load_supabase_client(ROOT)
    client.assert_write_project()
    started = now()
    directory = OUT / "apply" / started.replace(":", "_").replace(".", "_")
    journal = {"started_at": started, "plan_generated_at": plan["generated_at"], "apply": args.apply, "entries": []}
    priority = {"clinic_services": 0, "clinic_memberships": 1, "clinic_promotions": 2, "promo_offer_master": 3}
    with run_lock(OUT / "writer"):
        before = linked_query(BLANKET_CHECK_SQL, root=ROOT)
        assert not before, "blanket_gate_failed_before_apply"
        for record in sorted((item for item in plan["records"] if item["status"] == "ready"), key=lambda item: priority[item["table"]]):
            entry = {"plan_name": record["plan_name"], "business_id": record["business_id"], "table": record["table"], "source_url": record["source_url"], "status": "checking"}
            journal["entries"].append(entry)
            extraction = read(ROOT / record["extraction_path"])
            visibility = read(ROOT / record["visibility_path"])
            if extraction.get("updated_at") != record.get("extraction_updated_at") or visibility.get("captured_at") != record.get("visibility_captured_at"):
                entry.update(status="review", reason="evidence_changed_since_plan")
                atomic_json(directory / "journal.json", journal)
                continue
            assert visibility["source_captured_at"] == record["captured_at"] and visibility["business_id"] == record["business_id"]
            visible = "\n".join(node["text"] for node in visibility["capture"]["nodes"] if node.get("visible"))
            payload = dict(record["insert_fields"])
            table = record["table"]
            assert set(payload) <= set(schema[table]), "snapshot_payload_mismatch"
            if table != "promo_offer_master":
                existing = client.fetch_rows(table, ",".join(schema[table]), filters={"business_id": f"eq.{record['business_id']}"}, limit=1000)
                if table == "clinic_services":
                    matches = [item for item in existing if item["service_name"] == payload["service_name"]]
                elif table == "clinic_memberships":
                    matches = [item for item in existing if normalize_plan_name(item["membership_name"]) == normalize_plan_name(payload["membership_name"]) and item.get("billing_period") == payload.get("billing_period")]
                else:
                    matches = [item for item in existing if canonical_scrape_url(item["source_url"]) == canonical_scrape_url(payload["source_url"])]
                if len(matches) > 1:
                    entry.update(status="review", reason="ambiguous_existing_identity")
                elif matches:
                    item = matches[0]
                    comparable = {key: value for key, value in payload.items() if key != "source_url"}
                    entry.update(status="reused" if all(item.get(key) == value for key, value in comparable.items()) else "review", row_id=item[KEYS[table]], reason="existing_record_preserved")
                elif not args.apply:
                    entry.update(status="would_insert", payload=payload)
                else:
                    entry.update(status="insert_intent", payload=payload)
                    atomic_json(directory / "journal.json", journal)
                    try:
                        created = insert_promotion(client, payload) if table == "clinic_promotions" else client.insert_rows(table, [payload])
                    except HTTPError as error:
                        entry.update(status="insert_failed", database_error=error.response.json())
                        atomic_json(directory / "journal.json", journal)
                        raise
                    assert len(created) == 1
                    row_id = created[0][KEYS[table]]
                    entry.update(status="inserted_awaiting_readback", row_id=row_id)
                    atomic_json(directory / "journal.json", journal)
                    actual = client.fetch_rows(table, ",".join(schema[table]), filters={KEYS[table]: f"eq.{row_id}"})
                    assert len(actual) == 1 and all(actual[0].get(key) == value for key, value in payload.items()), "insert_readback_mismatch"
                    entry.update(status="inserted_verified", verified_at=now())
            else:
                raw = record["original"]
                decision = validate_offer_evidence(raw, visible)
                if not decision.accepted or decision.issues:
                    entry.update(status="review", reason="offer_evidence_recheck_failed")
                    atomic_json(directory / "journal.json", journal)
                    continue
                if raw.get("is_membership_required"):
                    sources = client.fetch_rows("clinic_memberships", ",".join(schema["clinic_memberships"]), filters={"business_id": f"eq.{record['business_id']}"}, limit=1000)
                    sources = [item for item in sources if canonical_scrape_url(item["source_url"]) == canonical_scrape_url(record["source_url"])]
                    source_id, promotion_id = "plan_id", None
                else:
                    source_url = record["source_url"]
                    aliases = [item for item in plan["records"] if item["table"] == "clinic_promotions"
                               and item["business_id"] == record["business_id"] and item["source_url"] == source_url
                               and item["status"] == "duplicate_source"]
                    if len(aliases) == 1:
                        canonical = [item for item in plan["records"] if item["plan_name"] == aliases[0]["reuse_plan_name"]
                                     and item["status"] == "ready" and item["business_id"] == record["business_id"]]
                        if len(canonical) == 1:
                            source_url = canonical[0]["source_url"]
                            entry["canonical_source_url"] = source_url
                    sources = client.fetch_rows("clinic_promotions", ",".join(schema["clinic_promotions"]), filters={"business_id": f"eq.{record['business_id']}", "is_active": "eq.true"}, limit=1000)
                    sources = [item for item in sources if canonical_scrape_url(item["source_url"]) == canonical_scrape_url(source_url)]
                    source_id, promotion_id = "promotion_id", None
                if len(sources) != 1:
                    entry.update(status="review" if args.apply else "would_wait_for_source", reason="source_identity_unresolved")
                    atomic_json(directory / "journal.json", journal)
                    continue
                membership_id = sources[0][source_id] if source_id == "plan_id" else None
                promotion_id = sources[0][source_id] if source_id == "promotion_id" else None
                master, items = build_master_from_offer(decision.offer, business_id=record["business_id"], promotion_id=promotion_id, source_url=record["source_url"], membership_plan_id=membership_id)
                blanket = is_blanket_offer_text(master["offer_raw_text"])
                linked = items if blanket else attach_service_ids_to_items(client, business_id=record["business_id"], items=items, fallback_text=master["offer_raw_text"])
                for item in linked:
                    if blanket:
                        item["service_id"] = None
                    elif item.get("service_id"):
                        services = client.fetch_rows("clinic_services", "service_id,business_id,service_name", filters={"service_id": f"eq.{item['service_id']}"})
                        assert len(services) == 1 and services[0]["business_id"] == record["business_id"]
                        if not service_supported_by_source(services[0]["service_name"], master["offer_raw_text"]):
                            item["service_id"] = None
                repeated = {value for value, count in Counter(item.get("service_id") for item in linked if item.get("service_id")).items() if count > 1}
                for item in linked:
                    if item.get("service_id") in repeated:
                        item["service_id"] = None
                if raw.get("purchase_type") == "bundle" or master["price_model"] == "from":
                    linked = [dict(item, unit_price=None) for item in linked]
                elif master["price_model"] == "total" and len(linked) == 1 and linked[0].get("quantity", linked[0].get("delivered_units")) in (None, 1):
                    linked = [dict(linked[0], unit_price=linked[0].get("unit_price") or master.get("discount_price"))]
                assert set(master) <= set(schema[table]) and linked
                if master["price_model"] == "per_unit" and (not master.get("discount_price") or any(item.get("unit_price") is None for item in linked)):
                    entry.update(status="review", reason="per_unit_price_incomplete")
                    atomic_json(directory / "journal.json", journal)
                    continue
                filters = {"business_id": f"eq.{record['business_id']}", "promotion_id": f"eq.{promotion_id}" if promotion_id else "is.null", "membership_plan_id": f"eq.{membership_id}" if membership_id else "is.null"}
                existing = client.fetch_rows(table, ",".join(schema[table]), filters=filters, limit=1000)
                matches = [item for item in existing if normalize_claim(item["offer_raw_text"]) == normalize_claim(master["offer_raw_text"])]
                if matches:
                    stored = client.fetch_rows("promo_offer_items", "*", filters={"offer_id": f"eq.{matches[0]['id']}"})
                    expected = Counter((item.get("item_name") or item.get("service_name"), item.get("service_id"), item.get("quantity", item.get("delivered_units")), item.get("unit_price")) for item in linked)
                    actual = Counter((item["item_name"], item["service_id"], item["delivered_units"], item["unit_price"]) for item in stored)
                    same = len(matches) == 1 and matches[0].get("is_active") and expected == actual
                    same = same and all(matches[0].get(key) == value for key, value in master.items() if key not in {"offer_fingerprint", "is_active"})
                    entry.update(status="reused_verified" if same else "review", reason="existing_offer_preserved", row_id=matches[0]["id"])
                elif not args.apply:
                    entry.update(status="would_insert", payload=master, items=linked)
                else:
                    master["is_active"] = False
                    entry.update(status="offer_insert_intent", payload=master, items=linked)
                    atomic_json(directory / "journal.json", journal)
                    created = client.insert_rows(table, [master])
                    assert len(created) == 1
                    offer_id = created[0]["id"]
                    entry.update(status="inactive_offer_created", row_id=offer_id)
                    atomic_json(directory / "journal.json", journal)
                    written = upsert_offer_items(client, offer_id, linked)
                    assert len(written) == len(linked)
                    current = client.fetch_rows(table, "id,is_active,updated_at", filters={"id": f"eq.{offer_id}"})
                    assert len(current) == 1 and current[0]["is_active"] is False
                    activated = client.update_row(table, {"id": f"eq.{offer_id}", "is_active": "eq.false", "updated_at": f"eq.{current[0]['updated_at']}"}, {"is_active": True})
                    assert len(activated) == 1, "offer_activation_conflict"
                    actual = client.fetch_rows(table, ",".join(schema[table]), filters={"id": f"eq.{offer_id}"})
                    actual_items = client.fetch_rows("promo_offer_items", ",".join(schema["promo_offer_items"]), filters={"offer_id": f"eq.{offer_id}"})
                    assert len(actual) == 1 and actual[0]["is_active"] and actual[0]["business_id"] == record["business_id"]
                    assert len(actual_items) == len(linked)
                    for key, value in master.items():
                        if key not in {"offer_fingerprint", "is_active"} and value is not None:
                            assert actual[0].get(key) == value, "offer_field_readback_mismatch"
                    expected_items = Counter((item.get("item_name") or item.get("service_name"), item.get("service_id"), item.get("quantity", item.get("delivered_units")), item.get("unit_price")) for item in linked)
                    stored_items = Counter((item["item_name"], item["service_id"], item["delivered_units"], item["unit_price"]) for item in actual_items)
                    assert expected_items == stored_items, "offer_item_readback_mismatch"
                    if blanket:
                        assert all(item["service_id"] is None for item in actual_items)
                    entry.update(status="inserted_verified", verified_at=now(), is_offer_extracted=all(item.get("service_id") for item in linked) or blanket)
            atomic_json(directory / "journal.json", journal)
        after = linked_query(BLANKET_CHECK_SQL, root=ROOT)
        assert not after, "blanket_gate_failed_after_apply"
        journal.update(completed_at=now(), blanket_rows=after, status_counts=dict(Counter(entry["status"] for entry in journal["entries"])))
        atomic_json(directory / "journal.json", journal)
        atomic_json(OUT / "latest_apply.json", journal)
        print(json.dumps({"apply": args.apply, "status_counts": journal["status_counts"], "blanket_rows": len(after), "journal": str(directory / "journal.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
