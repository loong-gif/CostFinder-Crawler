import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.deploy_monitor_price_sync import remote_python
from utils.discovery_logic import website_host
from utils.supabase_access import fetch_all_rows, load_supabase_client
from utils.batch_gate_checks import linked_query

OUT = ROOT / "one-off/outputs/candidates_20261020"
OUT.mkdir(parents=True, exist_ok=True)


def norm(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def main():
    status_path = OUT / "business_status.json"
    if status_path.exists():
        assert not any(r.get("master_started_at") for r in json.loads(status_path.read_text())), "preflight_already_seeded_use_existing_checkpoint"
    with (ROOT / "one-off/inputs/costfinder_candidates_medspa_plastic.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 760, len(rows)
    assert len({r["google place_id"] for r in rows}) == 760
    assert set(r["谷歌品类"] for r in rows) <= {"Medical spa", "Plastic surgeon", "Plastic surgery clinic"}
    schema = json.loads((ROOT / "schema/live_schema_snapshot.json").read_text())
    client = load_supabase_client(ROOT)
    client.assert_write_project()
    masters = fetch_all_rows(client, "master_business_info", ",".join(schema["master_business_info"]), order="business_id.asc")
    phones = linked_query("select business_id,phone from master_business_info order by business_id", root=ROOT)
    assert {m["business_id"] for m in masters} == {m["business_id"] for m in phones}
    by_id = {m["business_id"]: m for m in masters}
    for record in phones:
        by_id[record["business_id"]]["phone"] = record["phone"]
    (OUT / "master_before.json").write_text(json.dumps(masters, ensure_ascii=False, indent=2))
    results = []
    for index, row in enumerate(rows, 1):
        entry = {"input_row": index, "source": row, "status": "pending", "matches": []}
        if not row["官网"].strip():
            entry.update(status="skipped", reason="官网为空")
        else:
            place = [m for m in masters if m.get("google_place_id") == row["google place_id"]]
            host = website_host(row["官网"])
            street = row["地址"].split(",")[0].split()[0]
            domain_street = [m for m in masters if website_host(m.get("website")) == host and str(m.get("address") or "").split()[:1] == [street]]
            name_address = [m for m in masters if norm(m.get("name")) == norm(row["名称"]) and norm(m.get("address")) == norm(row["地址"])]
            number = re.sub(r"\D", "", row["电话"])[-10:]
            phone = [m for m in masters if number and re.sub(r"\D", "", str(m.get("phone") or ""))[-10:] == number]
            matches = place or domain_street or phone or name_address
            entry["matches"] = [m["business_id"] for m in matches]
            entry["match_method"] = "place_id" if place else "domain_street_number" if domain_street else "phone" if phone else "name_address" if name_address else None
            entry["master_action"] = "match" if len(matches) == 1 else "review" if matches else "insert"
            if len(matches) > 1:
                entry.update(status="review", reason="多个现有商家匹配")
        results.append(entry)
    (OUT / "business_status.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    summary = {"input": len(rows), "existing_masters": len(masters), "statuses": dict(Counter(r["status"] for r in results)), "master_actions": dict(Counter(r.get("master_action") for r in results)), "phone_matching": "按任务要求读取已存在的 phone 列用于去重；商家写入字段仍限定为 snapshot"}
    (OUT / "preflight_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False))
    code = '''import json,subprocess
from pathlib import Path
from datetime import datetime
base=Path('/home/loong/costfinder_monitor')
print('server_time',datetime.now().astimezone().isoformat())
print(subprocess.check_output(['ps','-eo','pid,comm,args'],text=True))
locked=subprocess.run(['flock','-n',str(base/'state/.monitor.lock'),'true'],capture_output=True)
print('monitor_lock_available',locked.returncode == 0)
print('remote_files',json.dumps([str(p.relative_to(base)) for p in base.glob('scripts/*retry*')]))
'''
    remote = remote_python(code)
    (OUT / "server_preflight.txt").write_text(remote)
    print("server preflight saved")


if __name__ == "__main__":
    main()
