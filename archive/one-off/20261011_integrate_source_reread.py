"""Integrate the user-released Ego evidence without altering original dry-run data."""

import csv
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/monitor_price_sync/visual_review_20261010"
NEW = ROOT / "output/monitor_price_sync/reread_20261011"
BASE = ROOT / "output/monitor_price_sync/dryrun_20261010"
RESTORED = {"VR022", "VR169", "VR007", "VR021"}
CONFIRMED_404 = {"VR164", "VR016", "VR105", "VR132"}


def save(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temp.replace(path)


def backup(path):
    destination = path.with_name(path.stem + "_before_user_reread" + path.suffix)
    if not destination.exists():
        shutil.copyfile(path, destination)


def main():
    manifest = json.loads((OUT / "manifest.json").read_text())
    source = json.loads((NEW / "reread_evidence.json").read_text())
    luna = json.loads((NEW / "luna_restored_row_findings.json").read_text())
    with (BASE / "dryrun_changes.csv").open(encoding="utf-8-sig") as handle:
        original = list(csv.DictReader(handle))
    original_by_id = {row["row_id"]: row for row in original}
    queues = {}
    for assignment in manifest["assignments"]:
        queue = json.loads(Path(assignment["queue"]).read_text())
        for job in queue["jobs"]:
            queues[job["job_id"]] = (assignment, job)
    expected_luna = {row["row_id"] for jid in RESTORED for row in queues[jid][1]["rows"]}
    findings = {row["row_id"]: row for row in luna["findings"]}
    assert len(findings) == len(luna["findings"]) == len(expected_luna) == 37
    assert set(findings) == expected_luna
    luna_viewed = set(luna["actual_viewed_screenshots"])
    host_viewed = set(source["summary"]["actual_viewed_screenshots"])
    for finding in findings.values():
        assert finding["job_id"] in RESTORED
        refs = set(finding["screenshot_refs"])
        assert refs and refs.issubset(luna_viewed)
        assert all(Path(ref).is_file() for ref in refs)
    now = datetime.now(timezone.utc).isoformat()
    backup(OUT / "manifest.json")
    backup(OUT / "progress.json")
    backup(OUT / "verification.json")
    backup(OUT / "unresolved_sources.csv")
    backup(OUT / "host_confirmed_field_changes.csv")
    backup(OUT / "host_adjudications.json")
    grants = manifest.setdefault("supplemental_evidence_by_job", {})
    adjudications = json.loads((OUT / "host_adjudications.json").read_text())

    def visible(quote, facts, conditions, dates, refs):
        assert set(refs).issubset(host_viewed | luna_viewed)
        return {"quote": quote, "price_facts": facts, "conditions": conditions, "date_evidence": dates, "screenshot_refs": refs}

    def path(job, name):
        return str(NEW / job / name)

    catalog = {job: [] for job in RESTORED}
    bquote = ["80 Units of Tox + 1 Syringe of Filler + 1 BBL Forever Young Photofacial ($325 in savings)", "15% off all skincare", "3 SkinPen + 3 VAMP + 1 FREE SkinBetter Peel ($325 in savings)", "Purchase 8 Venus Legacy treatments for one area, receive 3 FREE Emsculpt arm treatments ($750 in savings)"]
    bfacts = [{"savings":325,"fixed_tox_units":80,"filler_syringes":1,"bbl_treatments":1}, {"discount_percent":15,"scope":"skincare"}, {"savings":325,"skinpen":3,"vamp":3,"free_peel":1}, {"savings":750,"venus_legacy":8,"free_emsculpt_arms":3}]
    for quote, facts in zip(bquote, bfacts):
        catalog["VR022"].append(visible(quote, facts, [], ["September; no year stated"], [path("VR022", "offers_complete.png")]))
    for row in source["changes"]:
        if row["job_id"] in {"VR021", "VR169"} and row["field"] == "candidate_offer":
            catalog[row["job_id"]].append(visible(row["evidence_quote"], json.loads(row["proposed_value"]), ["monthly and daily specials cannot be combined"] if row["job_id"] == "VR021" else ["first session"], ["November; no year stated"] if row["job_id"] == "VR021" else [], json.loads(row["evidence_path"])))
    laser = [("Upper Lip or Chin",750,525),("Upper Lip and Chin",900,630),("Face (Lip + Chin + Sideburns)",1250,875),("Face + Beard",1500,1050),("Arms (Full)",1500,1050),("Underarms",1000,700),("Bikini",1000,700),("Brazilian",1500,1050),("Legs (Full)",3500,2450),("Legs (Knees and Below)",2000,1400),("Back",2500,1750),("Chest or Abdomen",1500,1050),("Chest and Abdomen",2500,1750)]
    for label, regular, price in laser:
        catalog["VR007"].append(visible(f"Packages include 6 treatments (standard for hair removal): {label}: ${regular}, ${price}", {"regular_price":regular,"discount_price":price,"package_treatments":6,"price_model":"total"}, [], ["October Specials; no explicit year or start/end day"], [path("VR007", "laser_prices.png")]))
    catalog["VR007"].extend([
        visible("Jeuveau: Only $9/unit!* (reg. price: $10/unit)", {"regular_price":10,"discount_price":9,"price_model":"per_unit"}, ["minimum 50 units", "all units used during same appointment"], ["October Specials; no explicit year"], [path("VR007","injection_prices.png"),path("VR007","injection_conditions.png")]),
        visible("VI Chemical Peels: $300 (reg. price: $350/peel)", {"regular_price":350,"discount_price":300}, [], ["October Specials; no explicit year"], [path("VR007","injection_prices.png")]),
        visible("Add a VI Peel to any Toxin Appointment for ONLY $250! (reg. price: $350/peel)", {"regular_price":350,"discount_price":250}, ["same appointment by same provider", "with any Toxin appointment"], ["October Specials; no explicit year"], [path("VR007","injection_prices.png"),path("VR007","injection_conditions.png")]),
        visible("Radiofrequency Microneedling: $650 (reg. price: $750/treatment)", {"regular_price":750,"discount_price":650}, [], ["October Specials; no explicit year"], [path("VR007","rf_price.png")]),
        visible("Radiesse: $75 OFF!* (reg. price: $699/syringe)", {"regular_price":699,"discount_amount":75,"scope":"syringe"}, ["register with Xperience+"], ["October Specials; no explicit year"], [path("VR007","radiesse_price.png"),path("VR007","radiesse_condition.png")]),
        visible("Radiesse: $75 OFF!* (reg. price: $849/hyperdiluted treatment)", {"regular_price":849,"discount_amount":75,"scope":"hyperdiluted treatment"}, ["register with Xperience+"], ["October Specials; no explicit year"], [path("VR007","radiesse_price.png"),path("VR007","radiesse_condition.png")]),
        visible("CO2 Face Now $1,100 (Reg. $1,250), SAVE $150", {"regular_price":1250,"discount_price":1100,"savings":150}, [], [], [path("VR007","co2_popup_direct.png")]),
        visible("CO2 Face + Neck Now $1,400 (Reg. $1,750), SAVE $250", {"regular_price":1750,"discount_price":1400,"advertised_savings":250,"arithmetic_savings":350,"source_conflict":True}, [], [], [path("VR007","co2_popup_direct.png")]),
    ])
    for c in source["coverage"]:
        jid = c["job_id"]
        assignment, job = queues[jid]
        review_path = OUT / assignment["owner"] / jid / "review.json"
        backup(review_path)
        capture = json.loads((NEW / jid / "capture.json").read_text())
        previous_page = assignment["page_label"]
        for h in assignment.get("page_handoffs", []):
            if jid in h["job_ids"]:
                previous_page = h["to_page"]
        receipt = {"job_ids":[jid],"from_page":previous_page,"to_page":capture["page_label"],"owner":"Host user-authorized browser reread", "evidence":str(NEW / jid / "capture.json"),"reason":"Previous owners terminal; user explicitly handed browser back, Host reused the same TaskSpace, then handed it back."}
        if not any(h.get("evidence") == receipt["evidence"] for h in assignment.get("page_handoffs", [])):
            assignment.setdefault("page_handoffs", []).append(receipt)
        evidence_paths = [str(p.resolve()) for p in (NEW / jid).iterdir() if p.is_file()]
        evidence_paths.append(str(NEW / "database_snapshot.json"))
        grants[jid] = sorted(evidence_paths)
        viewed = sorted(p for p in host_viewed | luna_viewed if Path(p).parent == NEW / jid)
        assert viewed
        if jid in RESTORED:
            row_findings = [{k:v for k,v in findings[row["row_id"]].items() if k != "job_id"} for row in job["rows"]]
            limitations = ["网页事实与数据库执行匹配分开；保留服务、资格、人工保护、年份及触发器阻塞。"]
            if jid == "VR169":
                limitations.append("图片报价已读清，但未找到唯一数据库商家。")
            elif jid == "VR007":
                limitations.append("CO2 Face + Neck 海报 savings 250 与明确标价差额350冲突，不自动覆盖金额。")
            status, complete = "accessible", True
        else:
            status, complete = ("not_found" if jid in CONFIRMED_404 or jid == "VR092" else "accessible"), False
            text = json.loads((NEW / jid / "dom.json").read_text())["text"]
            row_findings = []
            for row in job["rows"]:
                row_findings.append({
                    "row_id":row["row_id"],"visual_status":"confirmed" if jid in CONFIRMED_404 else "backend_only" if row["field"] == "business_match" else "not_found_incomplete",
                    "current_quote":text[:500],"reason":c["decision"] + "；用户404规则是状态处理依据，不假称读到已失效页面的优惠正文。",
                    "screenshot_refs":[path(jid,"full.png")],"recommendation":"evidence_confirmed" if jid in CONFIRMED_404 else "retain_review",
                })
            visibility_limits = {
                "VR092": "页面显示 Invalid Blog Post Identifier，未明确显示404；无法读取原优惠正文，商家关联仍不唯一。",
                "VR128": "当前页面可打开，但 SPECIALS 下没有优惠正文，也没有可恢复的优惠图片引用；不能证明优惠移除。",
                "VR131": "当前 specials 页面优惠正文空白，引用图片未加载且原图片资源404；图片资源404不等于来源页面404，不能证明优惠移除。",
            }
            limitations = [visibility_limits.get(jid, c["decision"])]
        review = {
            "job_id":jid,"url":job["url"],"final_url":capture["url"],"observed_at":capture["observed_at"],
            "browser":{"space_id":6,"page_label":capture["page_label"]},"page_status":status,"complete":complete,
            "limitations":limitations,"evidence_files":grants[jid],"screenshots_viewed":viewed,
            "visible_offers":catalog.get(jid,[]),"row_findings":row_findings,
            "owner":"Host integration; restored-source pixel review by GPT-6 Luna" if jid in RESTORED else "Host user-authorized reread",
            "capture_owner":"Host Ego Lite TaskSpace 6", "historical_review":str(review_path.with_name("review_before_user_reread.json")),
            "supplemental_database_read_at":source["summary"]["database_read_at"],
        }
        if jid in CONFIRMED_404:
            review["source_disposition"] = "confirmed_404_inactive"
            review["user_policy"] = "页面显示404的可以标为inactive；其余重新读取"
        save(review_path, review)
        for finding in row_findings:
            rid = finding["row_id"]
            old = original_by_id[rid]
            if jid in CONFIRMED_404:
                action, proposed = c["decision"], {"source_status":"inactive", "database_action":"none_needed", "matched_promotions":json.loads(c["promotion_ids"])}
            else:
                action, proposed = "待核验（来源事实已核验）" if complete else "待核验（来源证据不完整）", None
            adjudications[rid] = {"action":action,"reason":finding["reason"],"execution_blockers":old["execution_blockers"],"proposed_value":proposed,"supplemental_reread":str(NEW / "reread_evidence.json")}
    manifest["goal_status"] = "active"
    manifest["browser_control_state"] = "user"
    manifest["browser_control_reason"] = "Host called TaskSpace6.handOff after the eleven-source reread; no subsequent browser takeover. Luna supplement reads saved pixels only."
    manifest["blocked_audit"] = {"condition":"remaining_source_visibility","consecutive_goal_turns_observed":0,"resumed_at":now,"reset_reason":"Four previously blocked sources became readable with real browser evidence; current user-triggered run starts a fresh audit. Prior blocked receipt retained in manifest_before_user_reread.json."}
    manifest["user_confirmed_404_jobs"] = sorted(CONFIRMED_404)
    manifest["supplemental_reread_evidence"] = str(NEW / "reread_evidence.json")
    manifest["luna_restored_batch"] = {"task_name":"/root/luna_restored_sources_review","requested_role":"prove-efficient-worker","authoritative_model_mapping":"gpt-6-luna/max", "pixel_evidence":str(NEW / "luna_restored_row_findings.json"),"browser_operations":0,"scope_rows":37,"new_role_launch_record":"collaboration.spawn_agent in this resumed goal turn"}
    save(OUT / "manifest.json", manifest)
    save(OUT / "host_adjudications.json", adjudications)
    with (OUT / "host_confirmed_field_changes.csv").open(encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns, confirmed = reader.fieldnames, list(reader)
    confirmed = [row for row in confirmed if not row["row_id"].startswith("REREAD_")]
    for row in source["changes"]:
        if row["action"] != "需更新":
            continue
        added = {column:row.get(column,"") for column in columns}
        added.update({"row_id":"REREAD_"+row["row_id"],"action":"需更新（执行受阻）","certainty":"source_and_snapshot_difference_confirmed", "support_path":str(NEW / "reread_evidence.json"),"comparison_scope":"field","evidence_kind":"ego_lite_pixels_and_supplemental_read_only_snapshot"})
        confirmed.append(added)
    with (OUT / "host_confirmed_field_changes.csv").open("w",encoding="utf-8-sig",newline="") as handle:
        writer=csv.DictWriter(handle,fieldnames=columns);writer.writeheader();writer.writerows(confirmed)
    print(json.dumps({"integrated_sources":11,"new_luna_rows":37,"host_additional_rows":8,"confirmed_field_rows":len(confirmed),"original_scope":972,"original_rows":1072,"browser_control":"user","blocked_count":0},ensure_ascii=False))


if __name__ == "__main__":
    main()
