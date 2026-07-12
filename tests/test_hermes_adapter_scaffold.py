"""Hermes adapter and worker-interface tests."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.hermes_adapter import HermesAdapter, HermesOutboxWorker, MockHermesTransport
from utils.notification_outbox import NotificationOutboxRecord, NotificationSeverity


def _sample_record() -> NotificationOutboxRecord:
    return NotificationOutboxRecord(
        notification_id="n3",
        run_id="r3",
        notification_type="schema_migration",
        severity=NotificationSeverity.ERROR,
        target="#costfinder-ops",
        payload={"summary": "fix applied", "api_key": "super-secret"},
    )


def test_adapter_defaults_to_mock_transport_and_text_fallback():
    adapter = HermesAdapter()
    record = _sample_record()

    result = adapter.send(record)

    assert isinstance(adapter.transport, MockHermesTransport)
    assert len(adapter.transport.sent) == 1
    envelope = adapter.transport.sent[0]
    assert envelope.channel == "#costfinder-ops"
    assert envelope.blocks is None
    assert "api_key" in envelope.text
    assert "[REDACTED]" in envelope.text
    assert result.used_text_fallback is True
    assert result.provider_message_id.startswith("mock-")
    assert result.provider_request_id.startswith("mock-")


def test_worker_claims_sends_and_marks_sent():
    calls = []

    @dataclass
    class FakeRepository:
        claimed: bool = False
        sent: List[tuple] = field(default_factory=list)

        def claim_next(self, now):
            calls.append(("claim_next", now))
            if self.claimed:
                return None
            self.claimed = True
            return _sample_record()

        def mark_sent(self, notification_id, provider_message_id, provider_request_id, delivered_at):
            calls.append(("mark_sent", notification_id, provider_message_id, provider_request_id, delivered_at))
            self.sent.append((notification_id, provider_message_id, provider_request_id, delivered_at))

        def mark_retry(self, notification_id, error, next_attempt_at, delivered_at):
            calls.append(("mark_retry", notification_id, error, next_attempt_at, delivered_at))

        def mark_dead_letter(self, notification_id, error, delivered_at):
            calls.append(("mark_dead_letter", notification_id, error, delivered_at))

    repo = FakeRepository()
    worker = HermesOutboxWorker(repository=repo)
    now = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)

    result = worker.process_once(now=now)

    assert result is not None
    assert calls[0][0] == "claim_next"
    assert calls[1][0] == "mark_sent"
    assert repo.sent[0][0] == "n3"
    assert repo.sent[0][3] == now


def test_subprocess_transport_invokes_hermes_without_real_network(monkeypatch):
    from utils.hermes_adapter import SubprocessHermesTransport

    calls = []

    class Completed:
        returncode = 0
        stdout = "sent"
        stderr = ""

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return Completed()

    monkeypatch.setattr("utils.hermes_adapter.subprocess.run", fake_run)
    result = SubprocessHermesTransport(executable="hermes-test").send(
        adapter := __import__("utils.hermes_adapter", fromlist=["HermesDeliveryEnvelope"]).HermesDeliveryEnvelope(
            channel="C123", text="test", metadata={"notification_id": "n1"}
        )
    )
    assert result.used_text_fallback is True
    assert calls[0][0][:4] == ["hermes-test", "send", "--to", "slack:C123"]
