"""File policy checked at the pending-SQL rendering boundary (the image control file lives next to pending_sql/)."""
from pathlib import Path
from datetime import datetime, timedelta, timezone

from utils.monitor_price_store import read_json
from utils.monitor_rendered_browser import BREAKPOINT_NAMES
from utils.monitor_image_evidence import compare_reads, critical_fields, source_tokens
from utils.monitor_visibility_evidence import coverage_complete


def capture_is_current(browser):
    """24 小时内、且按现行宽度集合渲染的结果才可复用；宽度集合不同的旧渲染（例如三宽度时期的）需要重新渲染。"""
    coverage = browser.get("breakpoint_coverage")
    if coverage is not None and set(coverage) != BREAKPOINT_NAMES:
        return False
    try:
        captured = datetime.fromisoformat(browser["captured_at"].replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - captured
        return timedelta(0) <= age <= timedelta(hours=24)
    except (KeyError, TypeError, ValueError):
        return False


def image_writes_enabled(state_dir) -> bool:
    """图片 offer 经 OCR 双读核验后照常写入；只有 price_sync_control.json 显式写 image_write_enabled=false 才停用。"""
    return read_json(Path(state_dir) / "price_sync_control.json", {}).get("image_write_enabled", True) is not False


def _requires_complete(plan) -> bool:
    """计划改动现有促销的内容或停用 offer：需要完整的页面证据。"""
    return any(op["table"] == "clinic_promotions" and op.get("action") != "insert" and set(op["values"]) - {"is_offer_extracted"}
               or op["table"] == "promo_offer_master" and op["values"].get("is_active") is False
               for op in plan.get("operations", []))


def _files_exist(paths) -> bool:
    return all(Path(path).is_file() for path in paths)


def _receipt_matches(receipt, artifact, observed_at) -> bool:
    return bool(receipt.get("url") == artifact.get("url")
                and receipt.get("artifact_captured_at") == artifact.get("captured_at")
                and receipt.get("artifact_markdown") == artifact.get("markdown")
                and receipt.get("observed_at") == observed_at
                and receipt.get("page_status") == "accessible" and receipt.get("complete") is True
                and receipt.get("screenshots_viewed") and _files_exist(receipt["screenshots_viewed"]))


def _human_receipt_block(evidence, browser):
    """人工核验的证据只对应它核验过的那一次抓取，且截图仍在、未超过 24 小时。"""
    if not evidence.get("prior_visual_receipt"):
        return None
    if evidence.get("verified_capture") is not True:
        return "legacy_image_evidence_requires_refresh"
    receipt_path = browser.get("visual_receipt_path")
    artifact_path = browser.get("verified_artifact_path")
    receipts = read_json(Path(receipt_path), []) if receipt_path else []
    artifact = read_json(Path(artifact_path), {}) if artifact_path else {}
    observed_at = evidence["prior_visual_receipt"].get("observed_at")
    if sum(_receipt_matches(r, artifact, observed_at) for r in receipts) != 1:
        return "human_capture_proof_mismatch"
    return None if capture_is_current(browser) else "image_capture_expired_requires_refresh"


def _variant_block(evidence):
    for offer in evidence.get("offers", []):
        capture = offer.get("variant_evidence")
        if capture and (not Path(capture.get("screenshot_path", "__missing__")).is_file()
                        or not capture_is_current(capture) or capture.get("offer_raw_text") != offer.get("offer_raw_text")):
            return "variant_capture_proof_mismatch"
    return None


def _proof_capture_block(proof, browser):
    """双读证明必须来自当前这次渲染的截图。"""
    if (proof.get("url") != browser.get("url") or not proof.get("url")
            or proof.get("visual_digest") != browser.get("visual_digest")
            or proof.get("context_digest") != browser.get("context_digest")):
        return "image_capture_proof_mismatch"
    if not capture_is_current(browser):
        return "image_capture_expired_requires_refresh"
    if "image_obscured" in browser.get("failures", []) and browser.get("image_capture_mode") != "full_page":
        return "image_obscured"
    return None


def _grounded_in_text(offer, dom_text) -> bool:
    quote = " ".join(source_tokens(offer.get("source_offer_raw_text", offer.get("offer_raw_text")))) or "__missing_quote__"
    return (offer.get("visibility") == "visible_match" and bool(offer.get("offer_raw_text"))
            and (quote in dom_text or (offer.get("variant_evidence") or {}).get("offer_raw_text") == offer.get("offer_raw_text")))


def _parent_facts_block(plan, policy, evidence, browser):
    """新建的促销要么有图片事实，要么有页面文字里的 offer 作为依据。"""
    dom_text = " ".join(source_tokens(browser.get("text")))
    inserts_parent = any(op["table"] == "clinic_promotions" and op.get("action") == "insert"
                         for op in plan.get("operations", []))
    grounded = any(_grounded_in_text(offer, dom_text) for offer in evidence.get("offers", []))
    return "image_parent_facts_missing" if inserts_parent and not policy.get("candidate_facts") and not grounded else None


def _regions_missing(regions) -> bool:
    return not regions or not _files_exist(region["path"] for region in regions)


def _read_proof_block(policy, proof, compared):
    """两轮读取的图块都还在，候选事实都被两轮读出。"""
    if any(_regions_missing(call.get("regions")) for call in proof.get("calls", [])):
        return "image_region_missing"
    facts = [critical_fields(o) for o in compared["accepted"]]
    if any(f not in facts for f in policy.get("candidate_facts", [])):
        return "image_fact_proof_mismatch"
    second = proof.get("reads", {}).get("b", {}).get("offers", [])
    for candidate in compared["accepted"]:
        peers = [o for o in second if critical_fields(o) == critical_fields(candidate)]
        if not peers:
            return "image_dual_read_required"
        if any(_regions_missing(c.get("region_files")) for c in (candidate, peers[0])):
            return "image_region_missing"
    return None


def _image_policy_block(plan, policy, evidence, browser, journal_dir):
    if not policy.get("depends_on_images"):
        return None
    if not image_writes_enabled(Path(journal_dir).parent):
        return "image_writes_disabled"
    proof = read_json(Path(policy.get("dual_read_path", "__missing_image_proof__")), {})
    if policy.get("dual_read_verified") is not True or {c.get("round") for c in proof.get("calls", [])} != {"a", "b"}:
        return "image_dual_read_required"
    reads = proof.get("reads", {})
    compared = compare_reads(reads.get("a", {}), reads.get("b", {}))
    block = _proof_capture_block(proof, browser) or _parent_facts_block(plan, policy, evidence, browser)
    if block:
        return block
    if _requires_complete(plan) and not _image_coverage_complete(compared, proof, browser):
        return "image_coverage_incomplete"
    return _read_proof_block(policy, proof, compared)


def _image_coverage_complete(compared, proof, browser) -> bool:
    return bool(compared["complete"] and proof.get("complete") is True and proof.get("coverage_complete") is True
                and browser.get("complete") is True)


def _legacy_block(evidence, browser):
    # Old OCR/ready plans retain their identity and IDs, but are not new proof.
    if (not browser or browser.get("image_review_required") or evidence.get("prior_visual_receipt")
            or any(o.get("visibility") == "image_only" for o in evidence.get("offers", []))):
        return "legacy_image_evidence_requires_refresh"
    return None


def _coverage_block(plan, browser):
    return "breakpoint_coverage_incomplete" if _requires_complete(plan) and not coverage_complete(browser) else None


def image_write_block(plan, journal_dir):
    policy = plan.get("image_policy")
    evidence_path = plan.get("evidence_path")
    if not evidence_path:
        return None if policy is None else _image_policy_block(plan, policy, {}, {}, journal_dir)
    evidence = read_json(Path(evidence_path), {})
    browser = read_json(Path(evidence_path).parent / "browser.json", {})
    block = _human_receipt_block(evidence, browser) or _coverage_block(plan, browser) or _variant_block(evidence)
    if block or policy is not None:
        return block or _image_policy_block(plan, policy, evidence, browser, journal_dir)
    return _legacy_block(evidence, browser)
