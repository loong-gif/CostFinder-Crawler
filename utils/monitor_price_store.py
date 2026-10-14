"""Durable server files for the price monitor; no database schema required."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def atomic_json(path: Path, value) -> None:
    """A successful return means both contents and rename have reached disk."""
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2, default=json_default) + "\n").encode())


def atomic_bytes(path: Path, value: bytes) -> None:
    missing = []
    current = path.parent
    while not current.exists():
        missing.append(current)
        current = current.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    for directory_path in reversed(missing):
        parent_fd = os.open(directory_path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    fd, temporary = tempfile.mkstemp(prefix=".pending_", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=json_default).encode()).hexdigest()


@contextmanager
def run_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".monitor.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("monitor_already_running") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def manual_protections(snapshot: dict, previous: dict | None = None) -> dict:
    """Conservatively retain traceable business corrections and admin decisions."""
    result = dict(previous or {})
    entries = list(result.get("entries", []))
    known = {(row.get("table"), str(row.get("id")), row.get("evidence_id")) for row in entries}
    for row in snapshot.get("confirmations", []):
        # A dismissed confirmation is not a confirmed business value.
        if row.get("status") == "dismissed":
            continue
        entry = {"table": "promo_offer_master", "id": row["offer_id"],
                 "reason": "business_confirmation_" + str(row.get("kind")),
                 "evidence_id": row["id"], "recorded_at": row.get("updated_at")}
        key = (entry["table"], str(entry["id"]), entry["evidence_id"])
        if key not in known:
            entries.append(entry); known.add(key)
    for row in snapshot.get("audit", []):
        target = row.get("target_type")
        table = {"deal": "promo_offer_master", "offer": "promo_offer_master",
                 "promotion": "clinic_promotions", "item": "promo_offer_items",
                 "service": "clinic_services", "membership": "clinic_memberships"}.get(target)
        if not table or not str(row.get("target_id", "")).isdigit():
            continue
        entry = {"table": table, "id": int(row["target_id"]),
                 "reason": "admin_" + str(row.get("action")),
                 "evidence_id": row["id"], "recorded_at": row.get("created_at")}
        key = (entry["table"], str(entry["id"]), entry["evidence_id"])
        if key not in known:
            entries.append(entry); known.add(key)
    result.update(schema_version=1, updated_at=now(), entries=entries)
    for key in ("offer_ids", "item_ids", "promotion_ids", "membership_ids", "service_ids"):
        result.setdefault(key, [])
    return result
