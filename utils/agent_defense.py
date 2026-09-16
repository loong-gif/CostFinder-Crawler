"""Shared helpers for agent DB defense patterns (soft delete audit, change events)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, Mapping

TABLE_PROMO_OFFER_CHANGE_EVENTS = "promo_offer_change_events"
TABLE_PROMO_OFFER_MASTER = "promo_offer_master"


def script_agent_name() -> str:
    return os.path.basename(sys.argv[0]) or "cf-crawler"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_soft_delete_change_event(
    *,
    target_offer_id: int | None,
    applied_by: str,
    reason: str,
    proposed_action: str = "mark_ended",
    source_url: str = "",
) -> dict[str, Any]:
    now = _now_iso()
    return {
        "source_url": source_url,
        "source_url_normalized": source_url.rstrip("/"),
        "proposed_action": proposed_action,
        "target_offer_id": target_offer_id,
        "applied_by": applied_by,
        "applied_at": now,
        "reason": reason,
        "validator_status": "applied",
    }


def record_change_event(client: Any, event: Mapping[str, Any]) -> None:
    client.insert_rows(TABLE_PROMO_OFFER_CHANGE_EVENTS, [dict(event)])


def soft_delete_promo_offer(
    client: Any,
    *,
    offer_id: int,
    applied_by: str,
    reason: str,
    filters: dict[str, str] | None = None,
    source_url: str = "",
) -> list[dict[str, Any]]:
    """Soft-delete a promo_offer_master row and record the actor in change_events."""
    now = _now_iso()
    params = dict(filters or {"id": f"eq.{offer_id}"})
    changed = client.update_row(
        TABLE_PROMO_OFFER_MASTER,
        params,
        {"deleted_at": now, "is_active": False, "updated_at": now},
    )
    if changed:
        record_change_event(
            client,
            build_soft_delete_change_event(
                target_offer_id=offer_id,
                applied_by=applied_by,
                reason=reason,
                source_url=source_url,
            ),
        )
    return changed


def audit_then_delete_rows(
    client: Any,
    table: str,
    filters: dict[str, str],
    *,
    applied_by: str,
    reason: str,
    target_offer_id: int | None = None,
    source_url: str = "",
) -> list[dict[str, Any]]:
    """Hard-delete when the table has no deleted_at; still record change_events."""
    deleted = client.delete_rows(table, filters)
    if deleted:
        record_change_event(
            client,
            build_soft_delete_change_event(
                target_offer_id=target_offer_id,
                applied_by=applied_by,
                reason=reason,
                source_url=source_url,
            ),
        )
    return deleted


if __name__ == "__main__":
    event = build_soft_delete_change_event(
        target_offer_id=42,
        applied_by="agent:test",
        reason="soft_delete: self-check",
    )
    assert event["target_offer_id"] == 42
    assert event["applied_by"] == "agent:test"
    assert event["validator_status"] == "applied"
    print("agent_defense self-check ok")
