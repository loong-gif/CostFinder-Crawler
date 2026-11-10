#!/usr/bin/env python3
"""Copy deal-images bucket objects from production to replica."""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = PROJECT_ROOT / "output" / "sync_replica_20260930"
OBJECTS_FILE = OUT_DIR / "storage_objects.json"

PROD_REF = "kdlpkjzcnbkjcvwsvlwn"
REPLICA_REF = "ebamlycrvgtivuluipvn"
BUCKET = "deal-images"
HTTP_TIMEOUT = 120


def _jwt_ref(token: str) -> str:
    import base64

    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    body = json.loads(base64.urlsafe_b64decode(payload))
    return str(body.get("ref") or "")


def service_role_key(project_ref: str) -> str:
    out = subprocess.check_output(
        ["supabase", "projects", "api-keys", "--project-ref", project_ref, "-o", "json"],
        cwd=PROJECT_ROOT,
        text=True,
    )
    for row in json.loads(out):
        if row.get("name") == "service_role":
            key = (row.get("api_key") or "").strip()
            if key.startswith("eyJ") and _jwt_ref(key) == project_ref:
                return key
    raise SystemExit(f"JWT service_role not found for {project_ref}")


def download_object(base: str, key: str, object_path: str) -> tuple[bytes, str]:
    encoded = urllib.parse.quote(object_path, safe="/")
    url = f"{base.rstrip('/')}/storage/v1/object/{BUCKET}/{encoded}"
    req = urllib.request.Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = resp.read()
        ctype = resp.headers.get("Content-Type", "image/png")
    return data, ctype.split(";")[0].strip() or "image/png"


def upload_object(base: str, key: str, object_path: str, data: bytes, content_type: str) -> None:
    encoded = urllib.parse.quote(object_path, safe="/")
    url = f"{base.rstrip('/')}/storage/v1/object/{BUCKET}/{encoded}"
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": content_type,
            "x-upsert": "true",
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT):
        pass


def copy_bucket() -> dict[str, Any]:
    objects: list[str] = json.loads(OBJECTS_FILE.read_text(encoding="utf-8"))
    prod_base = f"https://{PROD_REF}.supabase.co"
    replica_base = f"https://{REPLICA_REF}.supabase.co"
    prod_key = service_role_key(PROD_REF)
    replica_key = service_role_key(REPLICA_REF)

    copied = 0
    failed: list[dict[str, str]] = []
    for obj in objects:
        try:
            data, ctype = download_object(prod_base, prod_key, obj)
            upload_object(replica_base, replica_key, obj, data, ctype)
            copied += 1
            print(f"copied {obj} ({len(data)} bytes)")
        except Exception as e:
            failed.append({"object": obj, "error": str(e)[:300]})
            print(f"FAILED {obj}: {e}")

    result = {"copied": copied, "total": len(objects), "failed": failed}
    (OUT_DIR / "storage_sync_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main() -> None:
    copy_bucket()


if __name__ == "__main__":
    main()
