#!/usr/bin/env python3
"""Deliver durable notification_outbox rows through the local Hermes CLI."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from crawler.staging_recrawl import load_supabase_client
from utils.hermes_adapter import HermesAdapter, HermesOutboxWorker, SubprocessHermesTransport
from utils.notification_outbox import NotificationOutboxRecord, NotificationSeverity
from utils.notification_repository import SupabaseNotificationRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _record(row: dict) -> NotificationOutboxRecord:
    return NotificationOutboxRecord(
        notification_id=str(row["notification_id"]),
        run_id=str(row.get("run_id") or ""),
        notification_type=str(row["notification_type"]),
        severity=NotificationSeverity(str(row["severity"])),
        target=str(row["target"]),
        payload=dict(row.get("payload") or {}),
        attempt_count=int(row.get("attempt_count") or 0),
        payload_hash=row.get("payload_hash"),
        provider_message_id=row.get("provider_message_id"),
        provider_request_id=row.get("provider_request_id"),
    )


class OneRowRepository(SupabaseNotificationRepository):
    def claim_next(self, now):
        row = super().claim_next(now)
        return _record(row) if row else None


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    interval = max(5, int(os.getenv("NOTIFICATION_WORKER_INTERVAL_SECONDS", "30")))
    client = load_supabase_client(PROJECT_ROOT)
    repository = OneRowRepository(client)
    worker = HermesOutboxWorker(
        repository=repository,
        adapter=HermesAdapter(transport=SubprocessHermesTransport()),
    )
    while True:
        worker.process_once(now=datetime.now(timezone.utc))
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
