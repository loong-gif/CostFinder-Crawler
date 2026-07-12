"""Hermes adapter and worker interface scaffolding.

The default transport is a mock so unit tests and local runs are safe by
default. A future real Hermes integration can plug into the transport
protocol without changing the outbox model or worker interface.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import os
import subprocess
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from utils.notification_outbox import (
    NotificationOutboxRecord,
    canonical_json,
    redact_secrets,
    render_text_fallback,
    sha256_hex,
)


@dataclass(frozen=True)
class HermesDeliveryEnvelope:
    channel: str
    text: str
    metadata: Mapping[str, Any]
    blocks: Optional[Sequence[Mapping[str, Any]]] = None


@dataclass(frozen=True)
class HermesSendResult:
    provider_message_id: str
    provider_request_id: str
    used_text_fallback: bool


class HermesTransport(Protocol):
    def send(self, envelope: HermesDeliveryEnvelope) -> HermesSendResult:
        """Send one Hermes message.

        Implementations may talk to a real provider, but the default transport
        in this repo is a mock so tests never depend on external sends.
        """


@dataclass
class SubprocessHermesTransport:
    """Production transport using the local Hermes CLI text fallback."""

    executable: str = "hermes"
    timeout_seconds: int = 60

    def send(self, envelope: HermesDeliveryEnvelope) -> HermesSendResult:
        completed = subprocess.run(
            [self.executable, "send", "--to", f"slack:{envelope.channel}", envelope.text],
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            env=os.environ.copy(),
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "Hermes send failed").strip()
            raise RuntimeError(detail[:1000])
        request_id = sha256_hex(
            canonical_json({"channel": envelope.channel, "metadata": dict(envelope.metadata)})
        )
        return HermesSendResult(
            provider_message_id="hermes-" + request_id[:16],
            provider_request_id=request_id[16:32],
            used_text_fallback=True,
        )


@dataclass
class MockHermesTransport:
    sent: List[HermesDeliveryEnvelope] = field(default_factory=list)

    def send(self, envelope: HermesDeliveryEnvelope) -> HermesSendResult:
        self.sent.append(envelope)
        request_fingerprint = sha256_hex(
            canonical_json(
                {
                    "channel": envelope.channel,
                    "text": envelope.text,
                    "metadata": redact_secrets(dict(envelope.metadata)),
                    "blocks": list(envelope.blocks) if envelope.blocks is not None else None,
                }
            )
        )
        return HermesSendResult(
            provider_message_id="mock-" + request_fingerprint[:16],
            provider_request_id="mock-" + request_fingerprint[16:32],
            used_text_fallback=True,
        )


@dataclass
class HermesAdapter:
    """Hermes-facing adapter.

    Assumption: block payloads are opt-in. Text fallback is always emitted so
    delivery remains deterministic even when the transport only understands a
    plain message body.
    """

    transport: HermesTransport = field(default_factory=MockHermesTransport)
    allow_blocks: bool = False

    def build_envelope(
        self,
        record: NotificationOutboxRecord,
        blocks: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> HermesDeliveryEnvelope:
        text = render_text_fallback(record)
        chosen_blocks = blocks if self.allow_blocks else None
        metadata = {
            "notification_id": record.notification_id,
            "run_id": record.run_id,
            "notification_type": record.notification_type,
            "severity": record.severity.value,
            "payload_hash": record.payload_hash or record.compute_payload_hash(),
        }
        return HermesDeliveryEnvelope(
            channel=record.target,
            text=text,
            metadata=redact_secrets(metadata),
            blocks=chosen_blocks,
        )

    def send(
        self,
        record: NotificationOutboxRecord,
        blocks: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> HermesSendResult:
        envelope = self.build_envelope(record, blocks=blocks)
        return self.transport.send(envelope)


class OutboxRepository(Protocol):
    def claim_next(self, now: datetime) -> Optional[NotificationOutboxRecord]:
        """Return the next claimable outbox record or None."""

    def mark_sent(
        self,
        notification_id: str,
        provider_message_id: str,
        provider_request_id: str,
        delivered_at: datetime,
    ) -> None:
        """Persist a successful delivery."""

    def mark_retry(
        self,
        notification_id: str,
        error: str,
        next_attempt_at: datetime,
        delivered_at: datetime,
    ) -> None:
        """Persist a retryable failure."""

    def mark_dead_letter(
        self,
        notification_id: str,
        error: str,
        delivered_at: datetime,
    ) -> None:
        """Persist a terminal failure."""


@dataclass
class HermesOutboxWorker:
    """Single-record worker interface for the Hermes outbox.

    This is intentionally small: claim one record, render a deterministic
    fallback, send through the adapter, and persist the outcome through the
    repository interface.
    """

    repository: OutboxRepository
    adapter: HermesAdapter = field(default_factory=HermesAdapter)

    def process_once(self, now: Optional[datetime] = None) -> Optional[HermesSendResult]:
        current_time = now or datetime.now(timezone.utc)
        record = self.repository.claim_next(current_time)
        if record is None:
            return None
        try:
            result = self.adapter.send(record)
        except Exception as exc:  # pragma: no cover - exercised via fakes if needed
            if record.attempt_count >= 5:
                self.repository.mark_dead_letter(record.notification_id, str(exc), current_time)
            else:
                delays = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15), timedelta(hours=1), timedelta(hours=6))
                delay = delays[min(record.attempt_count, len(delays) - 1)]
                self.repository.mark_retry(
                    record.notification_id,
                    str(exc),
                    current_time + delay,
                    current_time,
                )
            return None
        self.repository.mark_sent(
            record.notification_id,
            result.provider_message_id,
            result.provider_request_id,
            current_time,
        )
        return result
