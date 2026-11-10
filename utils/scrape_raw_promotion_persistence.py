from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Mapping

from utils.firecrawl_scrape_raw_db import canonical_scrape_url
from utils.recent_raw_extraction import validate_promotion
from utils.rendered_visibility_gate import filter_visible_promotion_content

PROMOTION_SELECT = (
    "promotion_id,business_id,source_url,promotion_title,promotion_content,"
    "campaign_start_date,campaign_end_date,is_active"
)


def _complete_promotion(row: dict[str, Any]) -> bool:
    return bool(
        str(row.get("promotion_title") or "").strip()
        and [value for value in row.get("promotion_content") or [] if str(value).strip()]
    )


def persist_page_promotion(
    client: Any,
    *,
    business_id: int,
    source_url: str,
    item: dict[str, Any],
    apply: bool = False,
    rendered_visibility: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Reuse, fill, or insert the one page-anchored promotion record."""
    candidate = copy.deepcopy(item)
    if apply:
        visibility = filter_visible_promotion_content(
            candidate.get("promotion_content") or [], rendered_visibility
        )
        if not visibility.accepted:
            return {"accepted": False, "action": "rejected", "reason": visibility.reason}
        candidate["promotion_content"] = list(visibility.content)
    validation = validate_promotion(candidate, "")
    if not validation.accepted:
        return {"accepted": False, "action": "rejected", "reason": validation.reason}

    canonical_url = canonical_scrape_url(source_url)
    if not canonical_url:
        return {"accepted": False, "action": "rejected", "reason": "missing_source_url"}
    existing_rows = client.fetch_rows(
        "clinic_promotions",
        PROMOTION_SELECT,
        filters={"business_id": f"eq.{int(business_id)}"},
        limit=1000,
    )
    anchors = [
        row
        for row in existing_rows
        if canonical_scrape_url(str(row.get("source_url") or "")) == canonical_url
    ]
    complete = next((row for row in anchors if _complete_promotion(row)), None)
    if complete is not None:
        candidate_start = candidate.get("campaign_start_date")
        candidate_end = candidate.get("campaign_end_date")
        missing_existing_dates = (
            complete.get("campaign_start_date") is None
            and complete.get("campaign_end_date") is None
        )
        if missing_existing_dates and candidate_start and candidate_end:
            update_payload: dict[str, Any] = {"updated_at": datetime.now(timezone.utc).isoformat()}
            update_payload.update({
                "campaign_start_date": candidate_start,
                "campaign_end_date": candidate_end,
            })
            if not apply:
                return {
                    "accepted": True,
                    "action": "would_update_dates",
                    "promotion_id": complete.get("promotion_id"),
                }
            promotion_id = complete.get("promotion_id")
            updated = client.update_row(
                "clinic_promotions",
                {"promotion_id": f"eq.{promotion_id}"},
                update_payload,
            )
            if len(updated) != 1:
                raise RuntimeError(
                    f"clinic_promotions date update returned {len(updated)} rows; expected exactly one"
                )
            return {"accepted": True, "action": "updated_dates", "promotion_id": promotion_id}
        action = "reused" if apply else "would_reuse"
        return {
            "accepted": True,
            "action": action,
            "promotion_id": complete.get("promotion_id"),
        }

    payload = {
        "business_id": int(business_id),
        "source_url": canonical_url,
        "promotion_title": str(candidate.get("promotion_title") or "").strip(),
        "promotion_content": list(candidate.get("promotion_content") or []),
        "campaign_start_date": candidate.get("campaign_start_date"),
        "campaign_end_date": candidate.get("campaign_end_date"),
        # A valid page extraction is not lifecycle evidence.  Explicit
        # historical/removed-page decisions belong to the evidence review
        # workflow, not to a date comparison here.
        "is_active": True,
    }
    skeleton = anchors[0] if anchors else None
    if not apply:
        return {
            "accepted": True,
            "action": "would_update" if skeleton is not None else "would_insert",
            "promotion_id": skeleton.get("promotion_id") if skeleton is not None else None,
        }

    if skeleton is not None:
        promotion_id = skeleton.get("promotion_id")
        update_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"business_id", "source_url"}
        }
        update_payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        updated = client.update_row(
            "clinic_promotions",
            {"promotion_id": f"eq.{promotion_id}"},
            update_payload,
        )
        if len(updated) != 1:
            raise RuntimeError(
                f"clinic_promotions update returned {len(updated)} rows; expected exactly one"
            )
        resolved_id = updated[0].get("promotion_id", promotion_id)
        return {"accepted": True, "action": "updated", "promotion_id": resolved_id}

    inserted = client.insert_rows("clinic_promotions", [payload])
    if len(inserted) != 1 or inserted[0].get("promotion_id") is None:
        raise RuntimeError(
            f"clinic_promotions insert returned {len(inserted)} rows; expected one identified row"
        )
    promotion_id = inserted[0]["promotion_id"]
    return {"accepted": True, "action": "inserted", "promotion_id": promotion_id}
