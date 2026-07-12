"""Supabase RPC-backed notification outbox repository."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from utils.supabase_rest import SupabaseRestClient


class SupabaseNotificationRepository:
    def __init__(self, client: SupabaseRestClient):
        self.client = client

    def claim_next(self, now: datetime) -> dict[str, Any] | None:
        rows = self.client.rpc("claim_notification_outbox", {"p_now": now.isoformat(), "p_limit": 1})
        if isinstance(rows, list) and rows:
            return rows[0]
        if isinstance(rows, dict) and rows:
            return rows
        return None

    def mark_sent(self, notification_id: str, provider_message_id: str, provider_request_id: str, delivered_at: datetime) -> None:
        self.client.rpc("mark_notification_sent", {
            "p_notification_id": notification_id,
            "p_provider_message_id": provider_message_id,
            "p_provider_request_id": provider_request_id,
            "p_delivered_at": delivered_at.isoformat(),
        })

    def mark_retry(self, notification_id: str, error: str, next_attempt_at: datetime, delivered_at: datetime) -> None:
        self.client.rpc("mark_notification_retry", {
            "p_notification_id": notification_id,
            "p_error": error[:1000],
            "p_next_attempt_at": next_attempt_at.isoformat(),
            "p_delivered_at": delivered_at.isoformat(),
        })

    def mark_dead_letter(self, notification_id: str, error: str, delivered_at: datetime) -> None:
        self.client.rpc("mark_notification_dead_letter", {
            "p_notification_id": notification_id,
            "p_error": error[:1000],
            "p_delivered_at": delivered_at.isoformat(),
        })
