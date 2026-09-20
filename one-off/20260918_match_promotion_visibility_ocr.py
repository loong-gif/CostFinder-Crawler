#!/usr/bin/env python3
"""Match current not-found Promotion claims against OCR from ego-visible images."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
QA_PATH = ROOT / "outputs/visibility_qa_live_20260918.json"
MEDIA_PATH = ROOT / "outputs/visibility_review_20260918/visible_image_manifest.json"
OCR_PATH = ROOT / "outputs/visibility_review_20260918/visible_image_ocr_manifest.json"
OUTPUT_PATH = ROOT / "outputs/visibility_review_20260918/promotion_claim_ocr_review.json"


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return re.sub(r"\s+", " ", re.sub(r"[*_`#>\\]", "", text)).strip().casefold()


qa = json.loads(QA_PATH.read_text(encoding="utf-8"))
media = json.loads(MEDIA_PATH.read_text(encoding="utf-8"))
ocr = json.loads(OCR_PATH.read_text(encoding="utf-8"))

# Read current lifecycle state so historical inactive records do not become
# candidates for current-content changes.
env_values = {}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    match = re.match(r"^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$", line)
    if match:
        env_values[match.group(1)] = match.group(2).strip("'\"")
base_url = str(env_values.get("SUPABASE_URL") or "").rstrip("/")
service_key = env_values.get("SUPABASE_SERVICE_ROLE_KEY") or env_values.get("SUPABASE_SECRET_KEY")
request = urllib.request.Request(
    f"{base_url}/rest/v1/clinic_promotions?select=promotion_id,is_active&limit=1000",
    headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
)
with urllib.request.urlopen(request, timeout=30) as response:
    active_ids = {int(row["promotion_id"]) for row in json.load(response) if row.get("is_active") is True}

transcript_by_page: dict[str, list[str]] = {}
for image_url, record in ocr.items():
    lines = [str(line).strip() for line in record.get("transcript") or [] if str(line).strip()]
    if not lines:
        continue
    for page_url in record.get("pages") or []:
        transcript_by_page.setdefault(page_url, []).extend(lines)

results: list[dict[str, Any]] = []
for page_url, page_result in qa.get("results", {}).items():
    claims = [claim for claim in page_result.get("claimResults") or [] if claim.get("recordType") == "promotion" and claim.get("status") == "not_found"]
    if not claims:
        continue
    media_result = media.get(page_url) or {}
    transcript = transcript_by_page.get(page_url, [])
    transcript_text = normalize("\n".join(transcript))
    for claim in claims:
        if int(claim["recordId"]) not in active_ids:
            continue
        claim_text = normalize(claim.get("claim"))
        exact_line = any(claim_text and claim_text == normalize(line) for line in transcript)
        exact = bool(exact_line or (len(claim_text) >= 14 and len(claim_text.split()) >= 2 and claim_text in transcript_text))
        if exact:
            status = "image_only_verified"
        elif page_result.get("status") != "page_accessible" or media_result.get("status") != "accessible":
            status = "page_unavailable"
        elif not media_result.get("images") and not media_result.get("iframes") and not media_result.get("canvases"):
            status = "no_visible_media_review"
        elif transcript:
            status = "visual_unmatched_review"
        else:
            status = "visual_evidence_missing_review"
        results.append({
            "promotion_id": int(claim["recordId"]),
            "source_url": page_url,
            "claim": claim["claim"],
            "status": status,
            "matching_transcript": [line for line in transcript if claim_text and claim_text in normalize(line)],
            "transcript_lines": transcript,
            "visible_image_count": len(media_result.get("images") or []),
            "visible_iframe_count": len(media_result.get("iframes") or []),
            "visible_canvas_count": len(media_result.get("canvases") or []),
        })

summary = {}
for item in results:
    summary[item["status"]] = summary.get(item["status"], 0) + 1
output = {"generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(), "summary": summary, "results": results}
OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"summary": summary, "output": str(OUTPUT_PATH)}, ensure_ascii=False, indent=2))
