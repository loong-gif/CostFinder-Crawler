from __future__ import annotations

import importlib.util
import base64
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.monitor_price_store import atomic_json, now

OUT = ROOT / "output/monitor_price_sync/verified_sync_20261015"
spec = importlib.util.spec_from_file_location("previous_verified_batch", ROOT / "one-off/20261011_apply_verified_monitor.py")
previous = importlib.util.module_from_spec(spec)
spec.loader.exec_module(previous)


def bootstrap():
    truth = json.loads((ROOT / "output/monitor_price_sync/image_accuracy_20261012/truth_draft.json").read_text())
    assert len(truth["samples"]) == 369, "approved sample set changed"
    target = OUT / "approved_samples.json"
    if target.exists():
        assert json.loads(target.read_text())["samples"] == truth["samples"], "frozen sample set changed"
    else:
        atomic_json(target, {"frozen_at": now(), "samples": truth["samples"]})
    pages = {}
    business_ids = set()
    for sid, sample in truth["samples"].items():
        row = pages.setdefault(sample["url"], {"url": sample["url"], "sample_ids": [], "statuses": []})
        row["sample_ids"].append(sid)
        row["statuses"].append(sample["status"])
        if sample["merchant"].startswith("business_"):
            business_ids.add(int(sample["merchant"].split("_")[1]))
    # 同域多个地点一并读取，地点归属单独核验。
    business_ids.update([3065, 3066, 3077, 2986, 3168, 246, 2233, 3175])
    atomic_json(OUT / "pages.json", list(pages.values()))
    packets = []
    ordered = sorted(business_ids)
    for index in range(0, len(ordered), 4):
        name = f"read_{index // 4:02d}"
        sql = previous.snapshot_sql(ordered[index:index + 4], auxiliary=True)
        (OUT / f"{name}.sql").write_text(sql)
        packets.append({"name": name, "business_ids": ordered[index:index + 4]})
    atomic_json(OUT / "read_manifest.json", {"packets": packets, "started_at": now()})
    print(json.dumps({"samples": len(truth["samples"]), "pages": len(pages), "businesses": len(ordered),
                      "statuses": dict(Counter(s["status"] for s in truth["samples"].values()))}))


def combine():
    manifest = json.loads((OUT / "read_manifest.json").read_text())
    groups = ["businesses", "promotions", "offers", "items", "terms", "services", "confirmations", "audit", "history", "queue"]
    result = {k: [] for k in groups}
    result.update(project_id="kdlpkjzcnbkjcvwsvlwn", read_at=now())
    for packet in manifest["packets"]:
        snapshot = json.loads((OUT / (packet["name"] + ".json")).read_text())
        for key in groups:
            result[key].extend(snapshot[key])
    for key in groups:
        pk = previous.TABLES.get(key, (None, "business_id" if key == "businesses" else "id"))[1]
        unique = {}
        for row in result[key]:
            assert row[pk] not in unique or unique[row[pk]] == row, f"snapshot conflict:{key}"
            unique[row[pk]] = row
        result[key] = list(unique.values())
    atomic_json(OUT / "database_before.json", result)
    print(json.dumps({k: len(result[k]) for k in groups}))


def guard():
    code = '''import fcntl,json,select,sys
from pathlib import Path
p=Path("/home/loong/costfinder_monitor/state")
with (p/".monitor.lock").open("a+") as handle:
 fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
 journals=[]
 for f in (p/"journals").glob("*.json"):
  d=json.loads(f.read_text())
  journals.append({"file":f.name,"stage":d.get("stage"),"requests_sent":d.get("requests_sent"),"source":d.get("source",d.get("plan",{}).get("source")),"plan":d.get("plan") if d.get("stage")!="completed" else None})
 print(json.dumps({"lock_held":True,"control":json.loads((p/"price_sync_control.json").read_text()),"protections":json.loads((p/"manual_protections.json").read_text()),"journals":journals}),flush=True)
 if select.select([sys.stdin],[],[],3600)[0]: sys.stdin.readline()
 print("LOCK_RELEASED",flush=True)
'''
    encoded = base64.b64encode(code.encode()).decode()
    command = 'wsl -e python3 -u -c "import base64;exec(base64.b64decode(\'' + encoded + '\'))"'
    proc = subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "firecrawl", command],
                            stdin=sys.stdin, stdout=subprocess.PIPE, text=True)
    first = proc.stdout.readline()
    receipt = json.loads(first)
    atomic_json(OUT / "server_guard.json", dict(receipt, acquired_at=now()))
    print(json.dumps({"lock_held": receipt["lock_held"], "control": receipt["control"],
                      "unfinished": [j for j in receipt["journals"] if j["stage"] != "completed"]}), flush=True)
    for line in proc.stdout:
        print(line, end="", flush=True)
    raise SystemExit(proc.wait())


def inspect():
    approved = json.loads((OUT / "approved_samples.json").read_text())["samples"]
    database = json.loads((OUT / "database_before.json").read_text())
    for prefix in sys.argv[2:]:
        samples = [v for k, v in approved.items() if k.split("_")[0] == prefix]
        if not samples:
            raise ValueError("unknown sample group:" + prefix)
        url = samples[0]["url"]
        business = samples[0]["merchant"]
        bids = [int(business.split("_")[1])] if business.startswith("business_") else []
        promotions = [p for p in database["promotions"] if previous.normalize_source_url(p["source_url"]) == previous.normalize_source_url(url)]
        print("SOURCE", prefix, business, url)
        for p in promotions:
            print("PROMOTION", json.dumps({k:p[k] for k in ("promotion_id", "business_id", "is_active", "promotion_title", "campaign_start_date", "campaign_end_date", "is_offer_extracted")}, ensure_ascii=False))
        for m in database["offers"]:
            if m["promotion_id"] in {p["promotion_id"] for p in promotions}:
                if not m["is_active"]:
                    print("INACTIVE", m["id"], m["offer_raw_text"][:120])
                    continue
                fields = ("id", "promotion_id", "is_active", "offer_raw_text", "regular_price", "discount_price", "discount_amount", "discount_percent", "price_model", "is_package", "conditions", "is_new_customer_required", "is_membership_required")
                item_fields = ("offer_item_id", "item_name", "service_id", "delivered_units", "unit_price")
                print("DATABASE_OFFER", json.dumps(dict({k:m[k] for k in fields}, items=[{k:i[k] for k in item_fields} for i in database["items"] if i["offer_id"] == m["id"]]), ensure_ascii=False))
        for sample in samples:
            print("VERIFIED", sample["sample_id"], sample["status"], json.dumps(sample["facts"], ensure_ascii=False))
        path = OUT / "current_evidence" / prefix / "capture.json"
        if path.exists():
            capture = json.loads(path.read_text())
            print("CURRENT", capture.get("capture_status"), capture.get("final_url"), capture.get("page", {}).get("text", "")[:24000])
        else:
            print("CURRENT", "pending")


def crops():
    for prefix in sys.argv[2:]:
        directory = OUT / "current_evidence" / prefix
        capture = json.loads((directory / "capture.json").read_text())
        image = Image.open(directory / "full_page.png")
        paths = []
        for index, region in enumerate(capture["page"]["images"]):
            if region["width"] < 450 or region["height"] < 200 or region["y"] > 10000:
                continue
            x, y = max(0, int(region["x"])), max(0, int(region["y"]))
            right = min(image.width, int(region["x"] + region["width"]))
            bottom = min(image.height, int(region["y"] + region["height"]))
            if right <= x or bottom <= y:
                continue
            path = directory / f"image_{index:02d}.png"
            image.crop((x, y, right, bottom)).save(path)
            paths.append(str(path))
        print(json.dumps({"source": prefix, "paths": paths, "size": image.size,
                          "navigation_error": capture["navigation_error"]}))


def overview():
    samples = json.loads((OUT / "approved_samples.json").read_text())["samples"]
    database = json.loads((OUT / "database_before.json").read_text())
    for page in json.loads((OUT / "pages.json").read_text()):
        prefix = page["sample_ids"][0].split("_")[0]
        promotions = [p for p in database["promotions"] if previous.normalize_source_url(p["source_url"]) == previous.normalize_source_url(page["url"])]
        path = OUT / "current_evidence" / prefix / "capture.json"
        capture = json.loads(path.read_text()) if path.exists() else {}
        print(prefix, len(page["sample_ids"]), ",".join(sorted(set(page["statuses"]))),
              "P=" + ",".join(str(p["promotion_id"]) + ("A" if p["is_active"] else "I") for p in promotions),
              capture.get("capture_status", "pending"), len(capture.get("page", {}).get("text", "")))


def catalog():
    database = json.loads((OUT / "database_before.json").read_text())
    wanted = {int(x) for x in sys.argv[2:]}
    print(json.dumps({key: [r for r in database[key] if r["business_id"] in wanted]
                      for key in ("businesses", "services", "promotions")}, ensure_ascii=False))


def records():
    database = json.loads((OUT / "database_before.json").read_text())
    wanted = {int(x) for x in sys.argv[2:]}
    for row in database["offers"]:
        if row["promotion_id"] in wanted:
            fields = ("id", "promotion_id", "is_active", "offer_raw_text", "regular_price", "discount_price", "discount_amount", "discount_percent", "is_package", "conditions")
            print(json.dumps(dict({k:row[k] for k in fields}, items=[{k:i[k] for k in ("offer_item_id", "item_name", "service_id", "delivered_units", "unit_price")} for i in database["items"] if i["offer_id"] == row["id"]]), ensure_ascii=False))


def draft():
    module_spec = importlib.util.spec_from_file_location("verified_decisions", ROOT / "one-off/20261015_verified_decisions.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    database = json.loads((OUT / "database_before.json").read_text())
    samples = json.loads((OUT / "approved_samples.json").read_text())["samples"]
    decisions, operations, additions = module.build(database, samples)
    atomic_json(OUT / "decisions_draft.json", {"decisions": decisions, "operations": operations, "additions": additions})
    print(json.dumps({"classified": len(decisions), "pending": len(samples)-len(decisions),
                      "statuses": dict(Counter(x["status"] for x in decisions.values())),
                      "new_offers": sum(len(x) for x in additions.values())}))
    for sid, sample in samples.items():
        if sid not in decisions:
            print(sid, sample["status"], json.dumps(sample["facts"], ensure_ascii=False))


if __name__ == "__main__":
    {"bootstrap": bootstrap, "combine": combine, "guard": guard, "inspect": inspect, "crops": crops, "overview": overview, "catalog": catalog, "records": records, "draft": draft}[sys.argv[1]]()
