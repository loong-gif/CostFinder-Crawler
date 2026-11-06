"""File policy checked at the pending-SQL rendering boundary (the control file lives next to pending_sql/)."""
from pathlib import Path
from datetime import datetime, timedelta, timezone

from utils.monitor_price_store import read_json
from utils.monitor_rendered_browser import BREAKPOINT_NAMES
from utils.monitor_image_evidence import compare_reads, critical_fields, source_tokens


def capture_is_current(browser):
    try:
        captured = datetime.fromisoformat(browser["captured_at"].replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - captured
        return timedelta(0) <= age <= timedelta(hours=24)
    except (KeyError, TypeError, ValueError):
        return False


def image_write_block(plan, journal_dir):
    policy = plan.get("image_policy")
    control = read_json(Path(journal_dir).parent / "price_sync_control.json", {})
    if control.get("write_enabled") is False:
        return "writes_disabled"
    evidence_path = plan.get("evidence_path")
    evidence = read_json(Path(evidence_path), {}) if evidence_path else {}
    browser = read_json(Path(evidence_path).parent / "browser.json", {}) if evidence_path else {}
    if evidence.get("prior_visual_receipt"):
        if evidence.get("verified_capture") is not True:
            return "legacy_image_evidence_requires_refresh"
        receipt_path = browser.get("visual_receipt_path")
        artifact_path = browser.get("verified_artifact_path")
        receipts = read_json(Path(receipt_path), []) if receipt_path else []
        artifact = read_json(Path(artifact_path), {}) if artifact_path else {}
        valid = [r for r in receipts if r.get("url") == artifact.get("url")
                 and r.get("artifact_captured_at") == artifact.get("captured_at")
                 and r.get("artifact_markdown") == artifact.get("markdown")
                 and r.get("observed_at") == evidence["prior_visual_receipt"].get("observed_at")
                 and r.get("page_status") == "accessible" and r.get("complete") is True
                 and r.get("screenshots_viewed") and all(Path(f).is_file() for f in r["screenshots_viewed"])]
        if len(valid) != 1:
            return "human_capture_proof_mismatch"
        if not capture_is_current(browser):
            return "image_capture_expired_requires_refresh"
    requires_complete = any(op["table"] == "clinic_promotions" and op.get("action") != "insert" and set(op["values"]) - {"is_offer_extracted"}
                            or op["table"] == "promo_offer_master" and op["values"].get("is_active") is False
                            for op in plan.get("operations", []))
    coverage = browser.get("breakpoint_coverage")
    if evidence_path and requires_complete and (not coverage or set(coverage) != BREAKPOINT_NAMES
            or not all(row.get("complete") is True for row in coverage.values())):
        return "breakpoint_coverage_incomplete"
    for offer in evidence.get("offers", []):
        capture = offer.get("variant_evidence")
        if capture and (not Path(capture.get("screenshot_path", "__missing__")).is_file()
                        or not capture_is_current(capture) or capture.get("offer_raw_text") != offer.get("offer_raw_text")):
            return "variant_capture_proof_mismatch"
    if policy is not None:
        if not policy.get("depends_on_images"):
            return None
        if control.get("image_write_enabled") is not True:
            return "image_writes_disabled"
        if policy.get("dual_read_verified") is not True:
            return "image_dual_read_required"
        proof = read_json(Path(policy.get("dual_read_path", "__missing_image_proof__")), {})
        if {c.get("round") for c in proof.get("calls", [])} != {"a", "b"}:
            return "image_dual_read_required"
        reads = proof.get("reads", {})
        compared = compare_reads(reads.get("a", {}), reads.get("b", {}))
        browser = read_json(Path(evidence_path).parent / "browser.json", {}) if evidence_path else {}
        if (proof.get("url") != browser.get("url") or not proof.get("url")
                or proof.get("visual_digest") != browser.get("visual_digest")
                or proof.get("context_digest") != browser.get("context_digest")):
            return "image_capture_proof_mismatch"
        if not capture_is_current(browser):
            return "image_capture_expired_requires_refresh"
        if "image_obscured" in browser.get("failures", []) and browser.get("image_capture_mode") != "viewport_scrolled":
            return "image_obscured"
        requires_complete = any(op["table"] == "clinic_promotions" and op.get("action") != "insert" and set(op["values"]) - {"is_offer_extracted"}
                    or op["table"] == "promo_offer_master" and op["values"].get("is_active") is False
                    for op in plan.get("operations", []))
        dom_text = " ".join(source_tokens(browser.get("text")))
        grounded_text = any(o.get("visibility") == "visible_match" and bool(o.get("offer_raw_text")) and
                           ((" ".join(source_tokens(o.get("source_offer_raw_text", o.get("offer_raw_text")))) or "__missing_quote__") in dom_text
                            or (o.get("variant_evidence") or {}).get("offer_raw_text") == o.get("offer_raw_text"))
                            for o in evidence.get("offers", []))
        if any(op["table"] == "clinic_promotions" and op.get("action") == "insert"
               for op in plan.get("operations", [])) and not policy.get("candidate_facts") and not grounded_text:
            return "image_parent_facts_missing"
        if requires_complete and not (compared["complete"] and proof.get("complete") is True
                                     and proof.get("coverage_complete") is True and browser.get("complete") is True):
            return "image_coverage_incomplete"
        if any(not call.get("regions") or any(not Path(region["path"]).is_file()
                for region in call["regions"]) for call in proof.get("calls", [])):
            return "image_region_missing"
        facts = [critical_fields(o) for o in compared["accepted"]]
        if any(f not in facts for f in policy.get("candidate_facts", [])):
            return "image_fact_proof_mismatch"
        for candidate in compared["accepted"]:
            peers = [o for o in reads.get("b", {}).get("offers", []) if critical_fields(o) == critical_fields(candidate)]
            if not peers:
                return "image_dual_read_required"
            if any(not c.get("region_files") or any(not Path(region["path"]).is_file()
                    for region in c["region_files"]) for c in (candidate, peers[0])):
                return "image_region_missing"
        return None
    # Old OCR/ready plans retain their identity and IDs, but are not new proof.
    if evidence_path:
        directory = Path(evidence_path).parent
        browser = read_json(directory / "browser.json", {})
        evidence = read_json(Path(evidence_path), {})
        if (not browser or browser.get("image_review_required")
                or evidence.get("prior_visual_receipt")
                or any(o.get("visibility") == "image_only" for o in evidence.get("offers", []))):
            return "legacy_image_evidence_requires_refresh"
    return None
