"""Domain-model tests for notification outbox scaffolding."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.notification_outbox import (
    NotificationOutboxRecord,
    NotificationOutboxStatus,
    NotificationSeverity,
    REDACTED_VALUE,
    redact_secrets,
    render_text_fallback,
)


def test_redact_secrets_recurses_and_is_conservative():
    payload = {
        "customer": "Alice",
        "access_token": "Bearer abcdefghijklmnopqrstuvwxyz123456",
        "nested": {
            "secret": "keep-me-hidden",
            "notes": ["plain", {"password": "super-secret"}],
        },
        "token_like": "abc123xyz4567890TOKEN",
    }

    redacted = redact_secrets(payload)

    assert redacted["customer"] == "Alice"
    assert redacted["access_token"] == REDACTED_VALUE
    assert redacted["nested"]["secret"] == REDACTED_VALUE
    assert redacted["nested"]["notes"][1]["password"] == REDACTED_VALUE
    assert redacted["token_like"] == REDACTED_VALUE


def test_record_hash_is_stable_after_redaction_and_key_sorting():
    payload_a = {"b": 2, "a": {"secret": "x", "keep": "y"}}
    payload_b = {"a": {"keep": "y", "secret": "z"}, "b": 2}

    record_a = NotificationOutboxRecord(
        notification_id="n1",
        run_id="r1",
        notification_type="daily_digest",
        severity=NotificationSeverity.INFO,
        target="#costfinder-ops",
        payload=payload_a,
        status=NotificationOutboxStatus.PENDING,
    )
    record_b = NotificationOutboxRecord(
        notification_id="n1",
        run_id="r1",
        notification_type="daily_digest",
        severity=NotificationSeverity.INFO,
        target="#costfinder-ops",
        payload=payload_b,
        status=NotificationOutboxStatus.PENDING,
    )

    assert record_a.redacted_payload() == record_b.redacted_payload()
    assert record_a.canonical_payload_json() == record_b.canonical_payload_json()
    assert record_a.compute_payload_hash() == record_b.compute_payload_hash()


def test_text_fallback_is_deterministic_and_redacted():
    record = NotificationOutboxRecord(
        notification_id="n2",
        run_id="r2",
        notification_type="needs_review",
        severity=NotificationSeverity.WARNING,
        target="#costfinder-ops",
        payload={
            "zeta": 3,
            "alpha": {"password": "abc", "items": [2, 1]},
            "beta": True,
        },
        status=NotificationOutboxStatus.CLAIMED,
        attempt_count=2,
    )

    text_one = render_text_fallback(record)
    text_two = render_text_fallback(record)

    assert text_one == text_two
    assert "password" in text_one
    assert REDACTED_VALUE in text_one
    assert "alpha:" in text_one
    assert "beta:" in text_one
