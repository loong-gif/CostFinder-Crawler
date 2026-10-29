"""Merge literal browser findings into a read-only, traceable dry-run addendum."""

from collections import Counter
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "output/monitor_price_sync/dryrun_20261010"
OUT = ROOT / "output/monitor_price_sync/visual_review_20261010"
STATES = {
    "confirmed", "contradicted", "not_found_complete", "not_found_incomplete",
    "backend_only", "access_blocked",
}
RECOMMENDATIONS = {
    "evidence_confirmed", "retain_review", "consider_update", "consider_deactivate", "consider_new",
}


def save(path, content):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def main():
    manifest = json.loads((OUT / "manifest.json").read_text())
    original_evidence = json.loads((BASE / "dryrun_evidence.json").read_text())
    original_page_coverage = original_evidence["page_coverage"]
    assert len(original_page_coverage) == 189
    assert len({page["canonical_url"] for page in original_page_coverage}) == 179
    with (BASE / "dryrun_changes.csv").open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        original_columns = reader.fieldnames
        rows = list(reader)
    by_id = {row["row_id"]: row for row in rows}
    assert len(by_id) == len(rows) == 1072
    expected = {}
    findings = {}
    reviews = []
    jobs = []
    errors = []
    for assignment in manifest["assignments"]:
        owner_root = (OUT / assignment["owner"]).resolve()
        queue = json.loads(Path(assignment["queue"]).read_text())
        for job in queue["jobs"]:
            row_ids = {row["row_id"] for row in job["rows"]}
            assert not row_ids.intersection(expected), "Duplicate ownership"
            expected.update({row_id: job["job_id"] for row_id in row_ids})
            job_result = {
                "job_id": job["job_id"], "owner": assignment["owner"],
                "url": job["url"], "canonical_url": job["canonical_url"],
                "business_name": job["business_name"],
                "row_ids": sorted(row_ids), "review_state": "not_reviewed",
            }
            path = owner_root / job["job_id"] / "review.json"
            jobs.append(job_result)
            if not path.exists():
                continue
            review = json.loads(path.read_text())
            local_errors = []
            required_review = {
                "job_id", "url", "final_url", "observed_at", "browser", "page_status",
                "complete", "limitations", "evidence_files", "screenshots_viewed",
                "visible_offers", "row_findings",
            }
            missing_review = required_review.difference(review)
            if missing_review:
                local_errors.append(f"Missing review fields: {sorted(missing_review)}")
            if review.get("page_status") not in {"accessible", "access_blocked", "not_found", "failed"}:
                local_errors.append("Unknown page status")
            if not isinstance(review.get("complete"), bool):
                local_errors.append("Page completeness must be boolean")
            if review.get("job_id") != job["job_id"] or review.get("url") != job["url"]:
                local_errors.append("Job identity mismatch")
            expected_page = assignment["page_label"]
            for handoff in assignment.get("page_handoffs", []):
                if job["job_id"] in handoff["job_ids"]:
                    expected_page = handoff["to_page"]
            if review.get("browser") != {
                "space_id": 6, "page_label": expected_page,
            }:
                local_errors.append("Browser ownership mismatch")
            if not review.get("observed_at"):
                local_errors.append("Missing observation time")
            actual = [finding.get("row_id") for finding in review.get("row_findings", [])]
            if set(actual) != row_ids or len(actual) != len(row_ids):
                local_errors.append("Findings do not exactly cover assigned rows")
            viewed = set(review.get("screenshots_viewed", []))
            supplemental_paths = set(manifest.get("supplemental_evidence_by_job", {}).get(job["job_id"], []))
            for filename in review.get("evidence_files", []) + list(viewed):
                evidence = Path(filename).resolve()
                owned = evidence.is_relative_to(owner_root) or str(evidence) in supplemental_paths
                if not owned or not evidence.is_file():
                    local_errors.append(f"Missing or unowned evidence: {filename}")
            for offer_index, visible_offer in enumerate(review.get("visible_offers", [])):
                quote = visible_offer.get("quote", visible_offer.get("literal_quote"))
                refs = visible_offer.get("screenshot_refs", [])
                if not quote or not refs or not set(refs).issubset(viewed):
                    local_errors.append(f"Visible offer lacks quote or viewed evidence: {offer_index}")
                missing = {"price_facts", "conditions", "date_evidence"}.difference(visible_offer)
                if missing:
                    local_errors.append(f"Incomplete visible offer {offer_index}: {sorted(missing)}")
            for finding in review.get("row_findings", []):
                required = {"row_id", "visual_status", "current_quote", "reason", "screenshot_refs", "recommendation"}
                missing = required.difference(finding)
                if missing:
                    local_errors.append(f"Incomplete finding {finding.get('row_id')}: {sorted(missing)}")
                if finding.get("recommendation") not in RECOMMENDATIONS:
                    local_errors.append(f"Unknown recommendation: {finding.get('row_id')}")
                state = finding.get("visual_status")
                refs = finding.get("screenshot_refs", [])
                if refs and not set(refs).issubset(viewed):
                    local_errors.append(f"Unviewed screenshot reference: {finding['row_id']}")
                if state not in STATES:
                    local_errors.append(f"Unknown visual state: {state}")
                if state in {"confirmed", "contradicted", "not_found_complete"}:
                    if not refs or not set(refs).issubset(viewed):
                        local_errors.append(f"No actually viewed screenshot reference: {finding['row_id']}")
                if state == "not_found_complete" and not review.get("complete"):
                    local_errors.append(f"Removal suggestion from incomplete page: {finding['row_id']}")
            if local_errors:
                errors.extend(f"{job['job_id']}: {error}" for error in local_errors)
                job_result["review_state"] = "invalid_review"
                continue
            job_result.update({
                "review_state": "review_recorded", "page_status": review["page_status"],
                "complete": review["complete"], "observed_at": review["observed_at"],
                "screenshots_viewed": list(viewed), "limitations": review["limitations"],
                "review_path": str(path),
                "source_disposition": review.get("source_disposition"),
            })
            reviews.append(review)
            for finding in review["row_findings"]:
                findings[finding["row_id"]] = {
                    **finding, "job_id": job["job_id"],
                    "observed_at": review["observed_at"], "page_complete": review["complete"],
                    "review_path": str(path),
                }
    assert len(expected) == manifest["total_pending_rows"] == 972
    assert len(jobs) == manifest["total_jobs"] == 178
    assert set(expected) == {row["row_id"] for row in rows if row["action"] == "待核验"}
    by_canonical = {job["canonical_url"]: job for job in jobs}
    assert len(by_canonical) == 178
    assert set(by_canonical) == {
        page["canonical_url"] for page in original_page_coverage
        if "待核验" in page["actions"]
    }
    page_coverage = []
    for page in original_page_coverage:
        job = by_canonical.get(page["canonical_url"])
        page_coverage.append({
            **page,
            "visual_job_id": job["job_id"] if job else None,
            "visual_review_state": job["review_state"] if job else "prior_evidence_only",
            "visual_page_status": job.get("page_status") if job else None,
            "visual_complete": job.get("complete") if job else None,
            "visual_observed_at": job.get("observed_at") if job else None,
            "visual_review_path": job.get("review_path") if job else None,
            "visual_scope_note": "待核验行的规范化来源共用视觉记录" if job else "该来源原表无待核验行，保留已有证据",
        })
    adjudications_path = OUT / "host_adjudications.json"
    adjudications = json.loads(adjudications_path.read_text()) if adjudications_path.exists() else {}
    visual_columns = [
        "visual_status", "visual_current_quote", "visual_reason", "visual_recommendation",
        "visual_observed_at", "visual_page_complete", "visual_screenshot_paths",
        "visual_review_path", "host_action", "host_reason", "host_execution_blockers",
        "host_proposed_value",
    ]
    merged = []
    for original in rows:
        row = {**original, **{column: "" for column in visual_columns}}
        finding = findings.get(row["row_id"])
        adjudication = adjudications.get(row["row_id"], {})
        if not finding:
            finding = adjudication.get("visual_finding")
        row["visual_status"] = "not_reviewed" if row["row_id"] in expected else "prior_evidence_only"
        if finding:
            row.update({
                "visual_status": finding["visual_status"],
                "visual_current_quote": finding["current_quote"],
                "visual_reason": finding["reason"],
                "visual_recommendation": finding["recommendation"],
                "visual_observed_at": finding["observed_at"],
                "visual_page_complete": str(finding["page_complete"]).lower(),
                "visual_screenshot_paths": json.dumps(finding["screenshot_refs"], ensure_ascii=False),
                "visual_review_path": finding["review_path"],
            })
        row["host_action"] = adjudication.get("action", "待核验" if finding else "")
        row["host_reason"] = adjudication.get("reason", "来源事实与数据库可执行匹配分开判定；视觉确认不自动消除原表中的执行阻塞。" if finding else "")
        row["host_execution_blockers"] = adjudication.get("execution_blockers", row["execution_blockers"])
        if "proposed_value" in adjudication:
            row["host_proposed_value"] = json.dumps(adjudication["proposed_value"], ensure_ascii=False)
        merged.append(row)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=original_columns + visual_columns)
    writer.writeheader()
    writer.writerows(merged)
    save(OUT / "dryrun_changes_visual_review.csv", "\ufeff" + stream.getvalue())
    pending_counts = Counter(findings[row_id]["visual_status"] if row_id in findings else "not_reviewed" for row_id in expected)
    page_counts = Counter(job.get("page_status", job["review_state"]) for job in jobs)
    generated = datetime.now(timezone.utc).isoformat()
    missing_visual_capture = [
        review["job_id"] for review in reviews
        if review["page_status"] == "accessible" and not review["screenshots_viewed"]
    ]
    incomplete_accessible = [
        review["job_id"] for review in reviews
        if review["page_status"] == "accessible" and not review["complete"]
    ]
    policy_resolved_404 = [
        review["job_id"] for review in reviews
        if review["page_status"] == "not_found"
        and review.get("source_disposition") == "confirmed_404_inactive"
        and review["job_id"] in manifest.get("user_confirmed_404_jobs", [])
        and review["screenshots_viewed"]
    ]
    absence_confirmation = manifest.get("user_confirmed_absence", {})
    absence_snapshot = {}
    if absence_confirmation:
        absence_path = Path(absence_confirmation["database_snapshot"])
        if absence_path.is_file():
            absence_snapshot = json.loads(absence_path.read_text())
        else:
            errors.append("User absence confirmation has no database snapshot")
    absence_matches = {row["job_id"]: row for row in absence_snapshot.get("target_matches", [])}
    policy_resolved_absence = [
        review["job_id"] for review in reviews
        if review.get("source_disposition") == "user_confirmed_no_offers_no_matching_records"
        and review["job_id"] in absence_confirmation.get("job_ids", [])
        and absence_confirmation.get("user_message")
        and absence_snapshot.get("project_id") == "kdlpkjzcnbkjcvwsvlwn"
        and absence_matches.get(review["job_id"], {}).get("url") == review["url"]
        and not absence_matches[review["job_id"]]["promotion_ids"]
        and not absence_matches[review["job_id"]]["offer_ids"]
        and review["screenshots_viewed"]
    ]
    unresolved_visibility = [
        review["job_id"] for review in reviews
        if review["job_id"] not in policy_resolved_404 + policy_resolved_absence and (
        review["page_status"] in {"access_blocked", "not_found", "failed"}
        or not review["complete"]
        or not review["screenshots_viewed"]
        )
    ]
    unresolved_jobs = [job for job in jobs if job["job_id"] in unresolved_visibility]
    unresolved_columns = [
        "job_id", "business_name", "url", "page_status", "complete",
        "affected_row_count", "row_ids", "observed_at", "limitations", "review_path",
    ]
    unresolved_stream = io.StringIO(newline="")
    unresolved_writer = csv.DictWriter(unresolved_stream, fieldnames=unresolved_columns)
    unresolved_writer.writeheader()
    for job in unresolved_jobs:
        unresolved_writer.writerow({
            "job_id": job["job_id"], "business_name": job["business_name"],
            "url": job["url"], "page_status": job.get("page_status", job["review_state"]),
            "complete": job.get("complete", False),
            "affected_row_count": len(job["row_ids"]),
            "row_ids": json.dumps(job["row_ids"], ensure_ascii=False),
            "observed_at": job.get("observed_at", ""),
            "limitations": json.dumps(job.get("limitations", []), ensure_ascii=False),
            "review_path": job.get("review_path", ""),
        })
    save(OUT / "unresolved_sources.csv", "\ufeff" + unresolved_stream.getvalue())
    blocked = (
        (manifest.get("goal_status") == "blocked"
         or manifest.get("browser_control_state") == "awaiting_user_confirmation")
        and manifest.get("blocked_audit", {}).get("consecutive_goal_turns_observed", 0) >= 3
        and (len(findings) < 972 or unresolved_visibility)
    )
    summary = {
        "generated_at": generated, "verdict": "BLOCKED" if blocked else ("FIX" if len(findings) < 972 or errors or unresolved_visibility else "PASS"),
        "configured_pages": 189, "configured_canonical_sources": 179,
        "configured_pages_with_coverage_result": len(page_coverage),
        "total_sources": 178, "sources_with_review": len(reviews),
        "sources_with_viewed_screenshot": sum(bool(review["screenshots_viewed"]) for review in reviews),
        "complete_accessible_sources": sum(review["page_status"] == "accessible" and review["complete"] for review in reviews),
        "pending_rows": 972, "rows_with_findings": len(findings),
        "accessible_sources_missing_visual_capture": missing_visual_capture,
        "accessible_sources_needing_complete_review": [job for job in incomplete_accessible if job in unresolved_visibility],
        "accessible_sources_with_incomplete_raw_capture": incomplete_accessible,
        "sources_with_unresolved_visibility": unresolved_visibility,
        "sources_resolved_by_user_404_policy": policy_resolved_404,
        "sources_resolved_by_user_absence_and_database_check": policy_resolved_absence,
        "pending_row_visual_status": dict(pending_counts), "page_status": dict(page_counts),
        "validation_errors": errors, "database_writes": 0, "ddl_calls": 0,
        "baseline_or_scheduler_mutations": 0,
        "scope_note": "PASS here concerns documented investigation coverage, not approval to write database records.",
    }
    save(OUT / "visual_review_summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
    save(OUT / "visual_review_evidence.json", json.dumps({
        "generated_at": generated, "manifest": manifest, "summary": summary,
        "original_database_evidence": str(BASE / "dryrun_evidence.json"),
        "database_read_time_note": "Original columns retain the 2026-10-10 snapshot. Supplemental reread for eleven sources uses a separately saved 2026-10-11 snapshot; each added field records its own read time.",
        "supplemental_reread_evidence": manifest.get("supplemental_reread_evidence"),
        "page_coverage": page_coverage,
        "jobs": jobs, "reviews": reviews, "host_adjudications": adjudications,
    }, ensure_ascii=False, indent=2))
    lines = [
        "# 待核验项视觉核验", "", f"状态：**{summary['verdict']}**。生成时间：{generated}。", "",
        f"范围为原 dry-run 的 972 条待核验项、178 个规范化来源。已取得 {len(reviews)} 个来源的 review、{len(findings)} 条逐行结论；其中 {summary['sources_with_viewed_screenshot']} 个来源实际查看过截图，{summary['complete_accessible_sources']} 个来源完整可访问。其余范围继续保留，不按部分核验声明整体完成。", "",
        "原监控的 189 条页面配置对应 179 个规范化来源，全部覆盖结果保存在 evidence 的 page_coverage。178 个含待核验行的来源进入本次视觉队列；Glendora 来源原表没有待核验行，保留已有证据。重复配置共用来源视觉记录，不重复计算来源数量。", "",
        "原 dry-run 字段保留 2026-10-10T06:36:02.413Z 的只读快照。用户处理验证页面后，11 个来源在同一 Ego TaskSpace 重新读取；补充数据库快照为 2026-10-11T02:22:24.090292+00:00，与原始列分开保存。原 GPT-6 Luna 批次及 Host 补充证据均保留；新 Luna 代理实际查看恢复页面截图，Host 负责整合。", "",
        "## 行级进度", "", "| 视觉状态 | 待核验行数 |", "|---|---:|",
    ]
    for state, count in sorted(pending_counts.items()):
        lines.append(f"| {state} | {count} |")
    with (OUT / "host_confirmed_field_changes.csv").open(encoding="utf-8-sig") as handle:
        confirmed_field_changes = list(csv.DictReader(handle))
    offer_count = len({row["offer_id"] for row in confirmed_field_changes if row["offer_id"]})
    lines.extend([
        "", "confirmed 表示网页展示事实得到画面支持，不表示已完成服务、活动、资格或保护名单的数据库执行匹配。backend_only 表示画面不能解决后端关联问题。not_found_complete 仅为页面未再展示旧条款的事实，停用建议仍需审查活动归属和人工保护。没有画面的 DOM 读取不计为视觉确认。", "",
        "## 已审查的重要发现", "",
        f"[Host 确认的字段差异表](host_confirmed_field_changes.csv)列出 {offer_count} 个已有 Offer 及关联活动/Item 的 {len(confirmed_field_changes)} 项字段差异；仍含明确的执行阻塞。完整附表保留原 1,072 行和原始值，追加视觉证据及 Host 判断列，避免覆盖旧证据。", "",
        "- New Look 当前页面显示女士 Brazilian + Underarms $57、Full Legs $134；数据库活动 2 的 Offer 8664/8674 为 $58/$135。价格差异已得到实际画面支持，日期归属、服务/数量口径及父活动保护仍阻止直接写入。相同 URL 另一份旧抓取中的‘未发现差异’结论已在附表标为受到新画面反证。", 
        "- 同页女士 Lower Legs（含膝盖）为 $66，Offer 8679 仍为 $68；Lip filler 为 $50 off，Offer 8676 为 $60 off。应列金额和原文差异，不能直接按旧报价缺失停用。", 
        "- New Look 的旧 Underarms 买 5 送 2/$125、Full Back 买 5 送 1/$799，与新画面买 3 送 1/$75 和 $479 的固定交付数量不同。应审查旧套餐替换，不能把它们作为相同套餐的简单改价，也不分摊套餐价。", 
        "- Faces Plus 的九折 Tox 订阅优惠和新客 Botox $10/unit 仍可见；不能以抓取缺失为由停用 Offer 10434。同类 10434/10720 的重复记录仍需后端审查。", 
        "- Beauty Fix 图片确实显示 September、$50/$100 off any service、Sylfirm X $1,000（normally $1,399）、PDO 10% off；没有可读年份或确切有效期，不自动把 September 转成 2026-09-30。Blanket 优惠的 service_id 要求保持独立检查。", "",
        "- The New Laser Light 的当前画面明确显示 Dysport $12/unit 和有效期至 October 31, 2026。Offer 8687、Item 2565 为 $10.99/unit，活动 168 的截止日为 2026-09-30。金额和结束日期差异已确认；不推断月初开始日期，也不覆盖同 URL 的 inactive 历史活动 3589。", "",
        "- Zen 的画面明确分为新客 $10/unit、现客 $11/unit（30 UNIT MINIMUM，NO BANKING）。旧 OCR 的 10-unit minimum 已被画面反证。Offer 1382 是 inactive 新客记录，不应按现客 $11 覆盖；active 新客 1375 仍与 $10 展示一致。最低30单位是购买门槛，不能写成固定交付数量。", "",
        "- BioRevive 的旧 Cool Peel 399 链接已跳转到 499 页面，画面明确为 $499、原价 $1,000。监控 URL 路径中的数字不能当作现价；该来源没有已匹配活动/Offer，新增前仍需跳转归属和去重审查。一年无限次激光脱毛仍为 $649、原价 $1,800，与 Offer 9822 金额一致；页面展示预约日历不自动批准重新激活 inactive 活动 815/Offer 9822。", "",
        "- Advanced Aesthetics 的 Chemical Peel 网页报价为 $150，inactive Offer 8530 保留 $200；金额和原文差异已确认。网页写明 Fall Event November 2–5，但没有年份，不能把未来活动报价直接设为当前有效价或覆盖旧活动；Item 1610 和关联触发器计划也须审查。", "",
        "- Bella Luce 的页面明确称每月优惠为 members-only；五个新增候选的会员要求 false 与画面不符，附表的 Host 建议已改为 true，会员方案关联仍需审查。", "",
        "- Greenspring 的该来源明确是 October Specials – 2018，活动 626 已 inactive。IPL MaxG/M22 的 $189/$299 与另一项 IPL Depigmenting 的 $89/$199 分开列出，Latisse $139/5mL 也不是每支 $20 奖励；附表已拒绝七项跨服务覆盖建议，并把该来源全部 33 行标为历史内容。", "",
        "- dermani Fort Myers 页面仍写新客首次到访 Botox $9/unit、最低消费 $180，SkinPen $200 新客优惠排除 PRP；不能删除新客资格。激光脱毛写 as Low as $50，只是起价，附表已拒绝固定总价 $50 的候选。数据库商家名称为 Johns Creek，门店关联仍需审查。", "",
        "- Richland 的 New Patient Specials 仍展示 Jeuveau $10/unit 与第二支 filler 半价，不能以页面移除为由停用。相同 URL 对应两组 business/promotion，来源展示不能决定覆盖哪组记录。", "",
        "- Bespoke Beauty 的活动画面写 September 14th–19th，没有年份；附表已拒绝把现有日期改成 September 1–30。", "",
        "- LA Queen 的 Russian Doll + Lip Flip 新客优惠仍为 $499（原价 $850，省 $351）。Xeomin 页面同时明确列 50 units、$325 总价、$6.50/unit 和最低 50 units；这些来源事实分别保留，数据库计价及触发器处理仍需统一审查。", "",
        "- South Coast MedSpa 的两项优惠是最高 50% off，页面没有起始金额，附表已拒绝 price_model=from 的覆盖候选；也不能把最高折扣当作所有服务固定五折。", "",
        "- Beauty With Bubbly 的新客 Botox 页面明确为 $10/unit、标准 $13/unit、每单位省 $3，首 40 units 最高省 $120。active Offer 8116 / Item 2279 已保留这些金额和资格；inactive Offer 8015 的候选覆盖已单独拒绝。40 是优惠适用上限，不是固定交付数量，原条件的纯措辞差异也不需要更新。", "",
        "- Iconique 的 21 条报价已通过滚动截图恢复。Skintyte 1 次 $500、5 次 $2,000 与 inactive Offer 10715/10716 同文同价，应审查激活，不能重复新增。2 Vials Kybella 和 BBL + MOXI combo 的报价不支持把 is_package 从 true 改成 false；一次疗程仍可包含多个服务。脸/颈/胸三次组合价 $3,600 与已有 Offer 10718 一致，不分摊单位价。", "",
        "- New Look 颈部单次 $24 与旧 Offer 8681 六次套餐 $149 不是同一报价；当前套餐是买三送一、总价 $89，附表已拒绝用 $24 覆盖旧套餐。男士 Full Back / Full Chest and Abdomen 当前各 $149，旧价 $159，但旧优惠另含免费颈部，附赠和保护要求需一并审查。", "",
        "- The Skin Center 的单次 Chemical Peel 减 $50 与 Offer 8216 对应的配合 Botox 五折优惠同时可见，附表已拒绝跨优惠覆盖或叠加两种折扣；减 $50 作为独立新增候选审查。New-To-You 要求首次尝试该治疗，不能直接等同首次到店的新客，会员/资格与 blanket 关联仍单独保留。", "",
        "- LIV 的低对比月度条款已通过正常选择文字的截图读清：优惠不可叠加，须在列明的促销月份内购买和使用，之后不能兑换。页面未给具体月份、年份或日历日期；这些通用条款也不证明顶部公告适用相同期限，或能映射到 20 条历史服务记录。", "",
        "- Avere 的正文曾放大低分辨率模糊预览图。Host 已通过页面明确引用的两张公开较清晰图片，实际读清感受因素图和首次咨询事项图，并查看当前整篇正文；该来源的图片缺口已解除。页面没有具体优惠报价，DR00077 的后台比较/关联限制仍保留，不据此停用优惠。", "",
        "- JTN Specials 的公开 Shopify 图片之前显示 404；原截图与限制保留。用户随后确认当前页面实际没有优惠正文，新只读回读也没有该 URL 对应的活动或 Offer，因此这页无需数据库停用。" if "VR131" in policy_resolved_absence else "- JTN Specials 引用的公开 Shopify 图片地址也显示 404，Host 已实际查看该画面并返回原页面。图片当前缺失，仍无法核验优惠内容；这不证明业务优惠已经移除。", "",
        "## 控制权与限制", "",
        f"浏览器控制状态：{manifest.get('browser_control_state', '未记录')}。{manifest.get('browser_control_reason', '')} 补充批次复用保存的实际截图。原浏览器批次和 per-job 交接记录保留；不以 DOM、截图文件存在或成功退出替代实际像素审查。", "",
        "Avere 的额外 Luna 恢复任务在执行前因工作区额度不足终止。Host 确认旧代理终态、无遗留浏览器进程并完成明确交接后补充恢复；该阶段的实际操作与像素审查归属 Host，原 Luna 核验记录和错误均保留。", "",
        "全程仅浏览器、本地文件及数据库只读查询：未写数据库、未执行 DDL，未调用定时任务或推进监控基线。", "",
    ])
    if manifest.get("supplemental_reread_evidence"):
        lines.extend([
            "## 用户处理验证页后的补充", "",
            "[11 来源重新读取报告](../reread_20261011/reread_report.md)及[补充差异表](../reread_20261011/dryrun_changes.csv)保留新的 58 行建议/问题，并以完整附表中的新列关联原 57 行。四个恢复页面共 37 条原待核验项由新 Luna 像素复核，后端及服务匹配阻塞继续保留。", "",
            f"用户明确允许当前页面显示 404 的来源按 inactive 处理，实际确认 {len(policy_resolved_404)} 个：Symmetry、Skin Perfect Lancaster、Amoderm、JTN July Xeomin。Amoderm 活动147/Offer4455已 inactive，其他三个没有对应活动，无须写入；不借此停用同商家不同 URL 活动。", "",
            "Allure CO2 的两项价格仍在弹窗展示，不能因为正文没写而停用。Face + Neck 海报的 SAVE $250 与 $1750−$1400=$350 存在冲突，保留审查。Jeuveau $9/unit、最低50units 与 Letybo 不同；脱毛明确为6次套餐，VI Peel独立$300与搭配Toxin$250分别保留。", "",
        ])
    if policy_resolved_absence:
        lines.extend([
            "## 用户确认无优惠正文后的数据库检查", "",
            f"用户明确确认三个页面实际没有优惠正文。{absence_snapshot['read_at']} 的 Supabase 只读回读按 URL 及商家读取活动、全部 active/inactive Offers 和 Items：Bel Viso、Heavenly、JTN pages/specials 均无对应活动或 Offer，三个来源无需同步 inactive。原画面完整性标签保留，不把人工确认伪称为新的页面内容抓取。", "",
            "[状态检查报告](../inactive_check_20261011/inactive_check_report.md)另列 JTN 其他来源的过期活动3595及Offer10397：购买截止2026-09-30、治疗截止2026-12-31，仍 active，建议单独停用。该建议未执行。", "",
        ])
    if unresolved_jobs:
        lines.extend([
            "## 尚不能完整核验的来源", "",
            f"[限制清单](unresolved_sources.csv)记录剩余 {len(unresolved_jobs)} 个来源的原 URL、受影响行和实际限制。原生页面404及用户明确确认无优惠且数据库无匹配记录的来源按各自证据单列结案；服务及后端关联的执行限制仍保留。", "",
            "| Job | 商家 | 页面状态 | 待核验行 | 限制 |", "|---|---|---|---:|---|",
        ])
        for job in unresolved_jobs:
            name = job["business_name"].replace("|", "\\|")
            limitations = "；".join(job.get("limitations", [])).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {job['job_id']} | {name} | {job.get('page_status', job['review_state'])} | {len(job['row_ids'])} | {limitations} |")
        lines.append("")
    lines.extend([
        "## 来源覆盖索引", "", "| Job | 商家 | 行数 | 页面状态 | 完整 | Review |", "|---|---|---:|---|---|---|",
    ])
    for job in jobs:
        name = job["business_name"].replace("|", "\\|")
        review_link = f"[证据]({job['review_path']})" if job.get("review_path") else "—"
        lines.append(f"| {job['job_id']} | {name} | {len(job['row_ids'])} | {job.get('page_status',job['review_state'])} | {job.get('complete','—')} | {review_link} |")
    if errors:
        lines.extend(["", "## 待修复的证据记录", ""] + [f"- {error}" for error in errors])
    save(OUT / "visual_review_report.md", "\n".join(lines) + "\n")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
