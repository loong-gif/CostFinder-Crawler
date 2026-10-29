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


def manual_protections(previous: dict | None = None) -> dict:
    """Protections are only the entries and IDs added to the file by hand."""
    result = dict(previous or {})
    result.update(schema_version=1, updated_at=now(), entries=list(result.get("entries", [])))
    for key in ("offer_ids", "item_ids", "promotion_ids", "membership_ids", "service_ids"):
        result.setdefault(key, [])
    return result
