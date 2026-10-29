"""Build a read-only addendum from the eleven user-released browser sources."""

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/monitor_price_sync/reread_20261011"
OLD = ROOT / "output/monitor_price_sync/visual_review_20261010"
BASE = ROOT / "output/monitor_price_sync/dryrun_20261010"


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def canonical(url):
    parsed = urlsplit(url)
    return (parsed.hostname or "").lower().removeprefix("www.") + parsed.path.rstrip("/")


def main():
    db = json.loads((OUT / "database_snapshot.json").read_text())
    original = read_csv(BASE / "dryrun_changes.csv")
    old_evidence = json.loads((BASE / "dryrun_evidence.json").read_text())
    sources = read_csv(OUT / "initial_unresolved_sources.csv")
    offers = {m["id"]: m for m in db["offers"]}
    promos = {p["promotion_id"]: p for p in db["promotions"]}
    items = {i["offer_item_id"]: i for i in db["items"]}
    protected = {(entry["table"], entry["id"]) for entry in old_evidence["manual_protections"]["entries"]}
    statuses = {
        "VR022": "可读", "VR169": "可读", "VR007": "可读", "VR021": "可读",
        "VR092": "博客标识无效", "VR128": "优惠正文空白", "VR131": "优惠正文空白／图片缺失",
        "VR164": "404", "VR016": "404", "VR105": "404", "VR132": "404",
    }
    coverage = []
    for source in sources:
        job = source["job_id"]
        capture = json.loads((OUT / job / "capture.json").read_text())
        matches = [p for p in db["promotions"] if canonical(p["source_url"]) == canonical(source["url"])]
        linked = [m for m in db["offers"] if m["promotion_id"] in {p["promotion_id"] for p in matches}]
        prior_rows = [r["row_id"] for r in original if canonical(r["url"]) == canonical(source["url"])]
        known_business_ids = {r["business_id"] for r in original if r["row_id"] in prior_rows and r["business_id"]}
        business_id = int(next(iter(known_business_ids))) if len(known_business_ids) == 1 else ""
        if statuses[job] == "404":
            decision = "对应 Promotion/Offer 已 inactive；无须写入" if matches else "无对应 Promotion；无可停用记录，不停用同商家其他来源"
        elif job in {"VR128", "VR131", "VR092"}:
            decision = "保留待核验；该状态不是用户授权停用的明确 404"
        elif job == "VR169":
            decision = "已读到图片优惠；数据库商家／活动匹配仍未解决"
        else:
            decision = "已恢复可读；按最新正文及图片重新比较"
        coverage.append({
            "job_id": job, "business_id": business_id, "business_name": source["business_name"] or ("Veritas Medi Spa" if job == "VR169" else "Bel Viso"),
            "url": source["url"], "final_url": capture["url"], "page_status": statuses[job],
            "observed_at": capture["observed_at"], "database_read_at": db["read_at"],
            "promotion_ids": json.dumps([p["promotion_id"] for p in matches]),
            "offer_ids": json.dumps([m["id"] for m in linked]),
            "active_promotion_ids": json.dumps([p["promotion_id"] for p in matches if p["is_active"]]),
            "active_offer_ids": json.dumps([m["id"] for m in linked if m["is_active"]]),
            "prior_row_ids": json.dumps(prior_rows), "decision": decision,
            "evidence_path": str(OUT / job / "capture.json"),
        })
    by_job = {c["job_id"]: c for c in coverage}
    changes = []

    def add(job, table, field, old, new, action, reason, quote, *, promotion=None, offer=None, item=None, blockers="", proof=None):
        c = by_job[job]
        record = offers.get(offer, {}) if offer is not None else promos.get(promotion, {})
        promotion = promotion if promotion is not None else record.get("promotion_id")
        entity = item if table == "promo_offer_items" else offer if table == "promo_offer_master" else promotion
        is_protected = (table, entity) in protected
        refs = proof or [str(OUT / job / "full.png")]
        changes.append({
            "row_id": f"RR{len(changes) + 1:05d}", "job_id": job,
            "business_id": record.get("business_id", c["business_id"]), "business_name": c["business_name"], "url": c["url"],
            "promotion_id": promotion or "", "offer_id": offer or "", "item_id": item or "",
            "table": table, "field": field, "old_value": json.dumps(old, ensure_ascii=False),
            "proposed_value": json.dumps(new, ensure_ascii=False), "action": action,
            "reason": reason, "evidence_quote": quote,
            "captured_at": max(datetime.fromtimestamp(Path(p).stat().st_mtime, timezone.utc).isoformat() for p in refs),
            "database_read_at": db["read_at"], "protection_status": "protected" if is_protected else "none_in_prior_protection_snapshot",
            "execution_blockers": ";".join(filter(None, [blockers, "protection_must_be_rechecked_before_write"])),
            "evidence_path": json.dumps(refs, ensure_ascii=False), "database_evidence_path": str(OUT / "database_snapshot.json"),
        })

    for c in coverage:
        job = c["job_id"]
        text = json.loads((OUT / job / "dom.json").read_text())["text"]
        if c["page_status"] == "404":
            for pid in json.loads(c["promotion_ids"]):
                p = promos[pid]
                assert p["is_active"] is False
                add(job, "clinic_promotions", "is_active", False, False, "未发现差异", "按用户当前规则处理明确 404；记录已经停用。", text[:250], promotion=pid)
            for oid in json.loads(c["offer_ids"]):
                m = offers[oid]
                assert m["is_active"] is False
                add(job, "promo_offer_master", "is_active", False, False, "未发现差异", "对应 404 来源的下游 Offer 已停用，保留 ID。", text[:250], offer=oid)
            if not json.loads(c["promotion_ids"]):
                add(job, "source", "source_status", "无对应活动记录", "inactive_source", "无可更新记录", "已记录来源停用建议；不借用其他 URL 的 Promotion/Offer。", text[:250])
        elif job in {"VR128", "VR131", "VR092"}:
            add(job, "source", "page_evidence", c["page_status"], None, "待核验", c["decision"], text[:350], blockers="incomplete_source_evidence")

    # Literal facts from screenshots, kept separate from original WSL evidence.
    bproof = [str(OUT / "VR022/offers_complete.png")]
    bquotes = [
        "80 Units of Tox + 1 Syringe of Filler + 1 BBL Forever Young Photofacial ($325 in savings)",
        "15% off all skincare",
        "3 SkinPen + 3 VAMP + 1 FREE SkinBetter Peel ($325 in savings)",
        "Purchase 8 Venus Legacy treatments for one area, receive 3 FREE Emsculpt arm treatments ($750 in savings)",
    ]
    for oid, quote in [(9797, bquotes[0]), (9799, bquotes[2])]:
        add("VR022", "promo_offer_master", "is_active", offers[oid]["is_active"], None, "未发现移除", "现有优惠原文仍展示，撤销此前缺证据的停用疑点；月份无年份，未推断有效期。", quote, offer=oid, proof=bproof)
    add("VR022", "promo_offer_master", "is_active", False, None, "待核验", "15% skincare 优惠仍展示，但已有 9798 为 inactive；不自动重新激活人工／历史状态。Blanket 关联必须为 NULL。", bquotes[1], offer=9798, proof=bproof, blockers="inactive_offer_reactivation_review")
    add("VR022", "promo_offer_items", "package_item_scope", [i for i in db["items"] if i["offer_id"] == 9799], {"SkinPen": 3, "VAMP": 3, "SkinBetter Peel": 1}, "待核验", "9799 当前两个 Item 均命名为 SkinPen，未完整表达 VAMP 和赠送 Peel；保留已有 ID，不分摊套餐单价。", bquotes[2], offer=9799, proof=bproof, blockers="service_mapping_and_package_reconciliation")
    add("VR022", "promo_offer_master", "candidate_offer", None, {"offer_raw_text": bquotes[3], "discount_amount": 750, "is_package": True, "items": [{"name": "Venus Legacy", "delivered_units": 8}, {"name": "Emsculpt arms", "delivered_units": 3, "free": True}]}, "可新增", "当前同来源 Offer 集合中缺少此优惠；仅提出候选，不分配新 ID，不反推套餐总价。", bquotes[3], promotion=176, proof=bproof, blockers="service_mapping_and_campaign_validity")
    add("VR022", "clinic_promotions", "is_offer_extracted", promos[176]["is_offer_extracted"], False, "需更新", "当前页面有未完整表达的套餐及缺失优惠，已完成标志不符合业务闭环。", " | ".join(bquotes), promotion=176, proof=bproof, blockers="reconcile_downstream_before_marking_complete")
    nov_text = json.loads((OUT / "VR021/dom.json").read_text())["text"]
    nov_quotes = ["20% off all services and skincare products", "$1 off per unit of neuromodulators (Botox, Xeomin, Dysport)", "$100 off all dermal fillers", "Laser Monday–50% off all laser treatments", "Toxin Tuesday–$1 off all tox", "Hydrafacial Wednesday–30% off all hydrafacials", "Thread Thursday–20% off threadlift treatment", "Filler Friday–$100 off all syringes of fillers"]
    for quote in nov_quotes:
        assert quote in nov_text
        add("VR021", "promo_offer_master", "candidate_offer", None, {"literal_offer": quote, "non_stacking": True, "is_active": None}, "可新增", "优惠文字已恢复读取；November 未注明年份，新活动归属和时效未解决。泛服务优惠保持 service_id=NULL；Toxin Tuesday 不擅自补充 per-unit 口径。", quote, blockers="new_promotion_ownership_and_seasonal_validity", proof=[str(OUT / "VR021/offers.png"), str(OUT / "VR021/daily_specials.png")])
    add("VR169", "promo_offer_master", "candidate_offer", None, {"offer_raw_text": "Complimentary Consultation & $25 Off Your First Session", "discount_amount": 25, "scope": "Tattoo Remodeling & Tattoo Removal", "conditions": ["first session"]}, "可新增", "图片已实际读取；当前数据库未找到唯一商家，首个 session 不等于首次到店，不设置新客标志。", "Tattoo Remodeling & Tattoo Removal — Complimentary Consultation & $25 Off Your First Session", blockers="business_and_source_ownership_unresolved", proof=[str(OUT / "VR169/full.png")])

    aproof = [str(OUT / "VR007/laser_prices.png"), str(OUT / "VR007/rf_price.png")]
    laser = [(8129, 1250, 875, "Face (Lip + Chin + Sideburns): $1250, $875"), (8130, 1500, 1050, "Face + Beard: $1500, $1050"), (8136, 2000, 1400, "Legs (Knees and Below): $2000, $1400")]
    for oid, regular, discounted, quote in laser:
        expected = {"regular_price": regular, "discount_percent": float((Decimal(regular - discounted) / Decimal(regular) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)), "discount_amount": regular - discounted}
        for field, value in expected.items():
            add("VR007", "promo_offer_master", field, offers[oid][field], value, "需更新", "套餐现价未变，原价错误导致折扣字段错误；新原价和总价均由网页直接给出，折扣由同口径两值计算。", quote, offer=oid, proof=aproof, blockers="package_trigger_semantics_before_write")
    for oid in [8127, 8128, 8129, 8130, 8136, 8139]:
        add("VR007", "promo_offer_items", "package_quantity_scope", [i for i in db["items"] if i["offer_id"] == oid], {"package_treatments": 6, "price_model": "total", "do_not_allocate_multi_item_unit_prices": True}, "待核验", "来源明确为 6 次套餐；现有 Item 数量为空。按项目数量及触发器规则整体修复，不能将套餐总价当单次价格。", "Packages include 6 treatments (standard for hair removal):", offer=oid, proof=aproof, blockers="item_scope_mapping_and_trigger_reconciliation")
    add("VR007", "promo_offer_master", "is_package", offers[8127]["is_package"], True, "需更新", "Upper Lip 优惠属于明确的 6 次套餐，现有非套餐标志错误。", "Packages include 6 treatments (standard for hair removal): Upper Lip or Chin: $750, $525", offer=8127, proof=aproof, blockers="preserve_total_525_and_resolve_quantity_in_same_plan")
    add("VR007", "promo_offer_master", "is_active", offers[8025]["is_active"], False, "建议停用", "完整当前正文及优惠图片展示 Jeuveau，而非 Letybo；不能把不同药品视为同一优惠改价。", "Jeuveau: Only $9/unit!* (reg. price: $10/unit)", offer=8025, proof=[str(OUT / "VR007/injection_prices.png")], blockers="source_plan_review_before_write")
    add("VR007", "promo_offer_master", "candidate_offer", None, {"offer_raw_text": "Jeuveau: Only $9/unit!*", "regular_price": 10, "discount_price": 9, "price_model": "per_unit", "conditions": ["minimum 50 units", "all units used during same appointment"], "delivered_units": None}, "可新增", "同来源无 Jeuveau Offer；最低 50 units 是购买门槛，不是固定交付数量。未找到同商家 Jeuveau Service，保持待解决。", "Jeuveau: Only $9/unit!* (reg. price: $10/unit); must receive 50 or more units to qualify. All units must be used during same appointment.", promotion=217, proof=[str(OUT / "VR007/injection_prices.png"), str(OUT / "VR007/injection_conditions.png")], blockers="missing_jeuveau_service")
    peel_conditions = ["add a VI Peel to any Toxin appointment", "treatments completed during same appointment by same provider"]
    add("VR007", "promo_offer_master", "conditions", offers[8126]["conditions"], peel_conditions, "需更新", "$250 是搭配 Toxin 的有条件价，现有条件为空；保留现价，不混成无条件 $300。", "Add a VI Peel to any Toxin Appointment for ONLY $250!; treatments must be completed during same appointment by same provider.", offer=8126, proof=[str(OUT / "VR007/injection_prices.png"), str(OUT / "VR007/injection_conditions.png")])
    add("VR007", "promo_offer_master", "candidate_offer", None, {"regular_price": 350, "discount_price": 300, "offer_raw_text": "VI Chemical Peels: $300", "price_model": "total"}, "可新增", "单独 VI Peel $300 与搭配 Toxin $250 为不同资格，不覆盖原 8126。", "VI Chemical Peels: $300 OR Add a VI Peel to any Toxin Appointment for ONLY $250! (reg. price: $350/peel)", promotion=217, proof=[str(OUT / "VR007/injection_prices.png")], blockers="offer_and_item_validation")
    for label, regular, discounted in [("Arms (Full)",1500,1050),("Underarms",1000,700),("Bikini",1000,700),("Brazilian",1500,1050),("Legs (Full)",3500,2450),("Back",2500,1750),("Chest or Abdomen",1500,1050)]:
        add("VR007", "promo_offer_master", "candidate_offer", None, {"offer_raw_text": f"{label}: ${regular}, ${discounted}", "regular_price": regular, "discount_price": discounted, "is_package": True, "package_treatments": 6, "price_model": "total"}, "可新增", "当前同来源 Offer 集合缺少该范围的套餐；保留总价，服务/部位和 OR 语义须解决后执行。", f"Packages include 6 treatments (standard for hair removal): {label}: ${regular}, ${discounted}", promotion=217, proof=aproof, blockers="service_area_mapping_and_package_scope")
    for regular, scope in [(699, "syringe"), (849, "hyperdiluted treatment")]:
        add("VR007", "promo_offer_master", "candidate_offer", None, {"offer_raw_text": "Radiesse: $75 OFF!*", "regular_price": regular, "discount_amount": 75, "scope": scope, "conditions": ["register with Xperience+ to redeem discount"]}, "可新增", "不同交付口径分别列候选，不合成一个价格；新 Service/Item 关联仍须解决。", "Radiesse: $75 OFF!* (reg. price: $699/syringe or $849/hyperdiluted treatment); must register with Xperience+ to redeem discount", promotion=217, proof=[str(OUT / "VR007/radiesse_price.png"), str(OUT / "VR007/radiesse_condition.png")], blockers="radiesse_service_scope_mapping")
    add("VR007", "promo_offer_master", "discount_amount", offers[8124]["discount_amount"], None, "待核验", "原价 1750 与现价 1400 相差 350，但当前 CO2 海报写 SAVE $250；冲突未解决，保留原值，不停用仍展示的 CO2。", "Face + Neck Now $1,400 (Reg. $1,750), SAVE $250", offer=8124, proof=[str(OUT / "VR007/co2_popup_direct.png")], blockers="source_price_savings_conflict")
    for oid in [8123, 8124]:
        add("VR007", "promo_offer_master", "is_active", offers[oid]["is_active"], None, "未发现移除", "CO2 当前弹窗图片仍明确支持这条优惠；撤销此前只看正文而产生的下架疑点。", offers[oid]["offer_raw_text"], offer=oid, proof=[str(OUT / "VR007/co2_popup_direct.png")])
    add("VR007", "promo_offer_master", "item_service_scope", [i for i in db["items"] if i["offer_id"] == 8140], "Radiofrequency Microneedling", "待核验", "$650 / $750 数值一致，但 RF 与普通 Microneedling 的服务范围须核对，不能只凭价格相同直接接受映射。", "Radiofrequency Microneedling: $650 (reg. price: $750/treatment)", offer=8140, proof=[str(OUT / "VR007/rf_price.png")], blockers="radiofrequency_service_mapping")
    add("VR007", "clinic_promotions", "promotion_content", promos[217]["promotion_content"], {"verified_current_fragments": ["October Specials", "Jeuveau $9/unit, regular $10/unit, minimum 50 units used in the same appointment", "VI Peel $300, or $250 added to Toxin in same appointment by same provider", "Laser hair removal packages include 6 treatments; prices in laser_prices.png", "Radiofrequency Microneedling $650, regular $750/treatment", "Radiesse $75 off; registration with Xperience+ required", "CO2 Face $1100 / reg $1250; CO2 Face + Neck $1400 / reg $1750, savings label conflict"], "replace_whole_content": False}, "需更新", "现有正文保留旧 Letybo 且缺少当前价格和资格；只给出有证据的片段，不纳入会员费，不推断年份。", "October Specials; Jeuveau: Only $9/unit!*", promotion=217, proof=[str(OUT / "VR007/full.png"), str(OUT / "VR007/injection_prices.png"), str(OUT / "VR007/co2_popup_direct.png")], blockers="reconcile_full_offer_set_and_source_conflict")
    add("VR007", "clinic_promotions", "is_offer_extracted", promos[217]["is_offer_extracted"], False, "需更新", "新增优惠、套餐数量和资格条件尚未完整进入下游，当前完成标志不符合闭环。", "Packages include 6 treatments (standard for hair removal):", promotion=217, proof=aproof)

    # All original columns are retained; the fresh snapshot is an explicit later layer.
    full = read_csv(OLD / "dryrun_changes_visual_review.csv")
    extra = ["reread_job_id", "reread_page_status", "reread_observed_at", "reread_database_at", "reread_decision", "reread_change_ids", "reread_evidence_path"]
    by_url = {canonical(c["url"]): c for c in coverage}
    for row in full:
        row.update({key: "" for key in extra})
        c = by_url.get(canonical(row["url"]))
        if c:
            related = [r["row_id"] for r in changes if r["job_id"] == c["job_id"] and (not row["offer_id"] or not r["offer_id"] or str(r["offer_id"]) == row["offer_id"])]
            row.update(dict(zip(extra, [c["job_id"], c["page_status"], c["observed_at"], db["read_at"], c["decision"], json.dumps(related), c["evidence_path"]])))
    write_csv(OUT / "source_reread.csv", coverage, list(coverage[0]))
    write_csv(OUT / "dryrun_changes.csv", changes, list(changes[0]))
    write_csv(OUT / "dryrun_changes_with_reread.csv", full, list(full[0]))
    # This explicit list records Host image-view receipts; file existence alone is not vision evidence.
    viewed_relative = [f"{c['job_id']}/full.png" for c in coverage] + [
        "VR022/viewport.png", "VR022/offers.png", "VR022/offers_complete.png",
        "VR021/offers.png", "VR021/daily_specials.png",
        "VR007/popup.png", "VR007/offer_cards_1.png", "VR007/offer_cards_2.png",
        "VR007/offer_cards_3.png", "VR007/jeuveau_vi_terms.png",
        "VR007/co2_popup_direct.png", "VR007/laser_prices.png", "VR007/radiesse_price.png",
        "VR007/injection_prices.png", "VR007/injection_conditions.png", "VR007/rf_price.png",
        "VR007/radiesse_condition.png",
    ]
    viewed = sorted(str(OUT / name) for name in viewed_relative)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "pages": len(coverage),
        "page_status_counts": dict(Counter(c["page_status"] for c in coverage)),
        "action_counts": dict(Counter(r["action"] for r in changes)),
        "field_rows": len(changes), "affected_original_rows": sum(bool(r["reread_job_id"]) for r in full),
        "all_original_rows": len(full), "database_read_at": db["read_at"],
        "database_writes": 0, "ddl": 0, "baseline_or_scheduler_changes": 0,
        "reread_coverage_verdict": "PASS", "synchronization_readiness": "FIX",
        "owner": "Host browser reread and comparison; restored-page screenshots independently reviewed by GPT-6 Luna",
        "restored_luna_evidence": str(OUT / "luna_restored_row_findings.json"),
        "original_page_coverage": old_evidence["page_coverage"], "actual_viewed_screenshots": viewed,
        "no_id_allocation": True, "authorization_note": "User allows explicit page 404 to be classified inactive. Current matching database records already inactive; no DML needed. Other price/state differences remain dry-run.",
        "timestamp_note": "Source observed_at is the initial browser read; each change captured_at is the supporting screenshot file save time. Later browser evidence is kept separate from the original WSL/DB snapshot.",
    }
    (OUT / "reread_evidence.json").write_text(json.dumps({"summary": summary, "coverage": coverage, "changes": changes, "database_snapshot": db}, ensure_ascii=False, indent=2))
    lines = ["# 11 个来源重新读取与 dry-run 补充", "", f"本次数据库回读：{db['read_at']}。原始 189 页面覆盖与历史报告保留；新报告为独立时间层。", "", "11/11 页面重新读取，4 个可读、4 个明确 404、1 个无效博客、2 个优惠正文空白。", "", "明确 404 的 Amoderm Promotion 147 / Offer 4455 已 inactive；Symmetry、Skin Perfect Lancaster、JTN July Xeomin 无对应 Promotion，未停用其他 URL 活动。没有数据库写入、DDL、迁移或监控基线/任务变更。", "", f"本次 {len(changes)} 行建议/问题：" + "；".join(f"{k} {v}" for k,v in summary['action_counts'].items()) + "。", "", "| 页面 | 当前结果 | Promotion | Offer | 处理 |", "| --- | --- | --- | --- | --- |"]
    for c in coverage:
        lines.append(f"| [{c['business_name']}]({c['url']}) | {c['page_status']} | {c['promotion_ids']} | {c['offer_ids']} | {c['decision']} |")
    lines.extend(["", "## 主要差异", "", "- Allure Offer 8129 / 8130 / 8136 的原价应为 1250 / 1500 / 2000；折扣比率和金额随之调整，当前现价保持 875 / 1050 / 1400。", "- Allure 脱毛是 6 次套餐，8127 仍为非套餐；Item 数量和总价/单次价应在同一计划中核对，多个部位不无依据分摊价格。", "- 当前 Jeuveau 是 9/unit、最低 50 units、同次使用；Letybo 8025 为建议停用，不改造成不同药品。", "- CO2 Face 与 Face + Neck 仍在弹窗展示，不建议停用。Face + Neck 海报 SAVE 250 与 1750−1400=350 冲突，保留审查。", "- VI Peel 无条件 300 与搭配 Toxin 250 分开；8126 保留 250 并补齐同次/同提供者条件。", "- Belle Âme 两个现有套餐仍展示；SkinPen/VAMP/Peel 的 Item 表达不完整，Venus/Emsculpt 套餐缺失。", "- November 活动未提供年份，8 条可见候选均保留时效及活动归属阻塞；泛服务优惠保持 service_id=NULL。", "- Veritas 图片已读取到首个 tattoo session 减 25 及免费咨询，但未找到唯一数据库商家。", "", "## 验证", "", "重新读取覆盖 PASS：11 个来源均有实际浏览器证据；恢复页面的 37 条原待核验项另由 GPT-6 Luna 实际查看 17 张保存截图复核。57 个原表关联行通过补充列连接到新报告，1072 原行及原字段保持不变。同步就绪 FIX：空白/无效页面、服务映射、图片价格冲突和活动年份尚未解决。", "", "新增建议不分配 ID；人工保护使用原保护名单，实际写入前须重新读取当前人工审核状态。网页当前展示只证明对应可比较的字段，不代表所有活动均在有效期内。"])
    (OUT / "reread_report.md").write_text("\n".join(lines) + "\n")

    # Checks change delivery if source coverage, IDs, evidence or history retention fail.
    assert len(coverage) == 11 and len({c["job_id"] for c in coverage}) == 11
    assert len(full) == len(original) == 1072
    assert len(summary["original_page_coverage"]) == 189
    assert summary["affected_original_rows"] == 57
    assert len({r["row_id"] for r in changes}) == len(changes)
    for before, after in zip(original, full):
        assert all(after[key] == value for key, value in before.items())
    for row in changes:
        assert row["evidence_quote"] and row["captured_at"] and row["database_read_at"]
        assert all(Path(p).is_file() and p in viewed for p in json.loads(row["evidence_path"]))
        if row["offer_id"]:
            assert offers[row["offer_id"]]["promotion_id"] == row["promotion_id"]
        if row["field"] == "candidate_offer":
            assert not row["offer_id"] and not row["item_id"]
    assert not any(json.loads(c["active_offer_ids"]) or json.loads(c["active_promotion_ids"]) for c in coverage if c["page_status"] == "404")
    (OUT / "verification.json").write_text(json.dumps({"verdict": "PASS", "checks": ["11_source_coverage", "189_original_coverage_preserved", "1072_original_rows_and_fields_unchanged", "57_rows_linked", "all_references_existing_and_viewed", "all_offer_promotion_ids_traced", "404_has_no_active_matched_record", "new_candidates_have_no_allocated_ids"], "database_writes": 0, "ddl": 0}, indent=2))
    print(json.dumps({k:v for k,v in summary.items() if k not in {"original_page_coverage", "actual_viewed_screenshots"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
