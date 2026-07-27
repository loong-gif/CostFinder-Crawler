#!/usr/bin/env python3
"""Preview or apply service-level expansion for broad active promotion offers."""
from __future__ import annotations

import argparse, json, os, sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from utils.promo_offer_scope_expansion import build_expansion_plan
from utils.schema_contract import CLINIC_SERVICE_SELECT, OFFER_MASTER_WITH_ITEMS_SELECT, TABLE_CLINIC_SERVICES, TABLE_PROMO_OFFER_ITEMS, TABLE_PROMO_OFFER_MASTER
from utils.supabase_rest import SupabaseRestClient, get_supabase_secret_key, get_supabase_writer_key

def _client(apply: bool) -> SupabaseRestClient:
    load_dotenv(ROOT / ".env")
    key = get_supabase_writer_key() if apply else get_supabase_secret_key()
    if not os.getenv("SUPABASE_URL") or not key: raise RuntimeError("Missing SUPABASE_URL or required Supabase credential")
    return SupabaseRestClient(os.environ["SUPABASE_URL"], key)

def _all(client: SupabaseRestClient, table: str, select: str, **kwargs: Any) -> list[dict[str, Any]]:
    rows=[]; offset=0
    while True:
        page=client.fetch_rows(table, select, limit=1000, offset=offset, **kwargs); rows.extend(page)
        if len(page) < 1000: return rows
        offset += len(page)

def run(*, output: Path, apply: bool) -> dict[str, Any]:
    client=_client(apply)
    offers=_all(client, TABLE_PROMO_OFFER_MASTER, OFFER_MASTER_WITH_ITEMS_SELECT, filters={"is_active":"eq.true"})
    services=_all(client, TABLE_CLINIC_SERVICES, CLINIC_SERVICE_SELECT)
    plan=build_expansion_plan(offers, services)
    all_fingerprints=_all(client, TABLE_PROMO_OFFER_MASTER, "offer_fingerprint")
    existing={str(row.get("offer_fingerprint") or "") for row in all_fingerprints}
    pending=[row for row in plan.creates if row["master"]["offer_fingerprint"] not in existing]
    now=datetime.now(timezone.utc).isoformat(); created_services=[]; created_offers=[]; failures=[]
    ids={(int(s["business_id"]),str(s["service_name"])):int(s["service_id"]) for s in services if s.get("business_id") is not None and s.get("service_id") is not None}
    if apply:
        for payload in plan.service_creates:
            key=(int(payload["business_id"]),str(payload["service_name"]))
            try:
                row=client.insert_rows(TABLE_CLINIC_SERVICES,[{**payload,"created_at":now,"updated_at":now}])[0]
                ids[key]=int(row["service_id"]); created_services.append(row)
            except Exception as exc: failures.append({"stage":"create_service",**payload,"error":str(exc)})
        required_by_source=Counter(int(entry["source_offer_id"]) for entry in pending)
        successful_by_source=Counter()
        seen_fingerprints=set(existing)
        for entry in pending:
            source=int(entry["source_offer_id"]); key=(int(entry["master"]["business_id"]),str(entry["service_name"]))
            fingerprint=entry["master"]["offer_fingerprint"]
            if fingerprint in seen_fingerprints:
                successful_by_source[source] += 1
                continue
            service_id=entry["item"].get("service_id") or ids.get(key)
            if service_id is None: failures.append({"stage":"resolve_service", "source_offer_id":source, "service_name":key[1]}); continue
            try:
                master=client.insert_rows(TABLE_PROMO_OFFER_MASTER,[{**entry["master"],"created_at":now,"updated_at":now}])[0]
                client.insert_rows(TABLE_PROMO_OFFER_ITEMS,[{"offer_id":master["id"],"service_id":service_id,"quantity":entry["item"]["quantity"],"unit_price":entry["item"]["unit_price"],"created_at":now,"updated_at":now}])
                created_offers.append(int(master["id"])); seen_fingerprints.add(fingerprint); successful_by_source[source] += 1
            except Exception as exc: failures.append({"stage":"create_offer", "source_offer_id":source, "service_name":key[1], "error":str(exc)})
        for source in plan.deactivate_offer_ids:
            if successful_by_source[source] != required_by_source[source]: continue
            try: client.update_row(TABLE_PROMO_OFFER_MASTER,{"id":f"eq.{source}","is_active":"eq.true"},{"is_active":False,"updated_at":now})
            except Exception as exc: failures.append({"stage":"deactivate_source", "source_offer_id":source, "error":str(exc)})
    report={"mode":"apply" if apply else "dry_run","planned_service_creates":plan.service_creates,"planned_offer_creates":plan.creates,"planned_deactivate_offer_ids":plan.deactivate_offer_ids,"skipped":plan.skipped,"existing_fingerprint_count":len(plan.creates)-len(pending),"created_services":created_services,"created_offer_ids":created_offers,"failures":failures}
    output.parent.mkdir(parents=True,exist_ok=True); output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    return report

def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--apply",action="store_true"); parser.add_argument("--output",type=Path,default=ROOT / "outputs" / "promo_offer_scope_expansion_report.json")
    args=parser.parse_args(); report=run(output=args.output,apply=args.apply)
    print(json.dumps({"mode":report["mode"],"planned_offer_creates":len(report["planned_offer_creates"]),"planned_service_creates":len(report["planned_service_creates"]),"output":str(args.output)},ensure_ascii=False)); return 0
if __name__ == "__main__": raise SystemExit(main())
