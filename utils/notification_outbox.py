"""Notification outbox domain modeling.

Assumptions documented here:
- The outbox snapshot is immutable once constructed; delivery state changes
  happen in the repository/worker layer, not by mutating the domain object.
- Redaction is conservative. Obvious secret-bearing keys are always removed,
  while token-like string values are only redacted when they are clearly
  secret-shaped. Callers should label sensitive fields explicitly.
- Text fallback is intentionally deterministic so tests and retries produce
  stable output even when richer provider formats are unavailable.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Optional, Sequence, Tuple

REDACTED_VALUE = "[REDACTED]"

_SENSITIVE_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "auth",
    "authorization",
    "bearer",
    "cookie",
    "session",
    "private_key",
    "api_key",
    "apikey",
)

_TOKEN_LIKE_VALUE = re.compile(r"^(?:Bearer\s+)?[A-Za-z0-9_\-+/=]{20,}$")


class NotificationSeverity(str, Enum):
    """Severity levels for notification payloads."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class NotificationOutboxStatus(str, Enum):
    """Delivery lifecycle state for a notification outbox row."""

    PENDING = "pending"
    CLAIMED = "claimed"
    SENT = "sent"
    RETRY = "retry"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True)
class NotificationOutboxRecord:
    """Immutable snapshot of one notification intent.

    The worker only reads from this object. Any attempt counts or status
    transitions should be persisted separately by the outbox repository.
    """

    notification_id: str
    run_id: str
    notification_type: str
    severity: NotificationSeverity
    target: str
    payload: Mapping[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: NotificationOutboxStatus = NotificationOutboxStatus.PENDING
    attempt_count: int = 0
    last_error: Optional[str] = None
    payload_hash: Optional[str] = None
    provider_message_id: Optional[str] = None
    provider_request_id: Optional[str] = None

    def redacted_payload(self) -> Dict[str, Any]:
        return redact_secrets(self.payload)

    def canonical_payload_json(self) -> str:
        return canonical_json(self.redacted_payload())

    def compute_payload_hash(self) -> str:
        return sha256_hex(self.canonical_payload_json())

    def with_payload_hash(self) -> "NotificationOutboxRecord":
        return replace(self, payload_hash=self.compute_payload_hash())

    def text_fallback(self, include_metadata: bool = False) -> str:
        return render_text_fallback(self, include_metadata=include_metadata)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.lower().replace("-", "_")
    return any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS)


def _looks_like_secret_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if value.startswith("Bearer "):
        return True
    return bool(_TOKEN_LIKE_VALUE.match(value))


def redact_secrets(value: Any) -> Any:
    """Recursively redact obvious secret-bearing fields.

    This is intentionally conservative and deterministic. The implementation
    favors removing values under sensitive keys over trying to guess every
    possible secret format.
    """

    if isinstance(value, Mapping):
        redacted: Dict[Any, Any] = {}
        for key in sorted(value.keys(), key=lambda item: repr(item)):
            item = value[key]
            if _is_sensitive_key(key):
                redacted[key] = REDACTED_VALUE
            else:
                redacted[key] = redact_secrets(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item) for item in value]
    if isinstance(value, set):
        return [redact_secrets(item) for item in sorted(value, key=repr)]
    if _looks_like_secret_value(value):
        return REDACTED_VALUE
    return value


def _render_scalar(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    return str(value)


def _render_mapping(mapping: Mapping[str, Any], indent: int) -> Iterable[str]:
    pad = " " * indent
    for key in sorted(mapping.keys(), key=lambda item: repr(item)):
        yield f"{pad}{key}:"
        yield from _render_value_lines(mapping[key], indent + 2)


def _render_sequence(sequence: Sequence[Any], indent: int) -> Iterable[str]:
    pad = " " * indent
    for index, item in enumerate(sequence):
        yield f"{pad}- [{index}]"
        yield from _render_value_lines(item, indent + 2)


def _render_value_lines(value: Any, indent: int) -> Iterable[str]:
    pad = " " * indent
    if isinstance(value, Mapping):
        yield from _render_mapping(value, indent)
        return
    if isinstance(value, (list, tuple)):
        yield from _render_sequence(value, indent)
        return
    if isinstance(value, set):
        yield from _render_sequence(sorted(value, key=repr), indent)
        return
    yield f"{pad}{_render_scalar(value)}"


def render_text_fallback(record: NotificationOutboxRecord, include_metadata: bool = False) -> str:
    """Render a deterministic, provider-agnostic text body for Hermes."""

    payload = record.redacted_payload()
    lines = [
        f"notification_type: {record.notification_type}",
        f"severity: {record.severity.value}",
        f"target: {record.target}",
        f"notification_id: {record.notification_id}",
        f"run_id: {record.run_id}",
    ]
    if include_metadata:
        lines.append(f"created_at: {record.created_at.astimezone(timezone.utc).isoformat()}")
        lines.append(f"status: {record.status.value}")
        lines.append(f"attempt_count: {record.attempt_count}")
    if payload:
        lines.append("payload:")
        lines.extend(_render_value_lines(payload, 2))
    return "\n".join(lines)
