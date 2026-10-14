#!/usr/bin/env python3
"""Fill clinic_promotions.screenshot via Firecrawl + Supabase Storage.

Firecrawl screenshot URLs expire (~24h). This script downloads the PNG and
uploads to the public `deal-images` bucket under promotion-screenshots/,
then optionally PATCHes clinic_promotions (same contract as fill_promotion_screenshots.py).

Usage:
  python3 scripts/fill_promotion_screenshots_firecrawl.py \\
    --input output/clinic_promotions_empty_screenshots.json \\
    --log output/promotion_screenshots_firecrawl.jsonl \\
    --sql output/promotion_screenshots_firecrawl.sql \\
    --apply --limit 3

Env (loaded from costfinder/.env then BrandRap/costfinder/.env):
  FIRECRAWL_API_KEY or fc- key in BrandRap .env
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_SECRET_KEY)
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

BRANDRAP_ENV = Path("/Users/wyl/BrandRap/costfinder/.env")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"
STORAGE_BUCKET = "deal-images"
STORAGE_PREFIX = "promotion-screenshots"
HTTP_TIMEOUT = 180


def _load_sibling_redbin_helpers():
    spec = importlib.util.spec_from_file_location(
        "fill_promotion_screenshots",
        PROJECT_ROOT / "scripts" / "fill_promotion_screenshots.py",
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_fps = _load_sibling_redbin_helpers()
load_rows = _fps.load_rows
load_done_urls = _fps.load_done_urls
append_jsonl = _fps.append_jsonl
write_sql = _fps.write_sql
apply_screenshot = _fps.apply_screenshot


def load_env_files() -> None:
    for path in (PROJECT_ROOT / ".env", BRANDRAP_ENV):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


def firecrawl_api_key() -> str:
    keys: list[str] = []
    if BRANDRAP_ENV.exists():
        for line in BRANDRAP_ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith("FIRECRAWL_API_KEY="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val.startswith("fc-"):
                    keys.append(val)
    env_key = (os.environ.get("FIRECRAWL_API_KEY") or "").strip()
    if env_key.startswith("fc-"):
        keys.append(env_key)
    if not keys:
        raise SystemExit("FIRECRAWL_API_KEY (fc-...) required in env or BrandRap .env")
    return keys[-1]


LINKED_PROJECT_REF = "kdlpkjzcnbkjcvwsvlwn"


def _jwt_ref(token: str) -> str:
    import base64

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        body = json.loads(base64.urlsafe_b64decode(payload))
        return str(body.get("ref") or "")
    except Exception:
        return ""


def _cli_service_role_key() -> str:
    import subprocess

    out = subprocess.check_output(
        ["supabase", "projects", "api-keys", "--project-ref", LINKED_PROJECT_REF],
        cwd=PROJECT_ROOT,
        text=True,
        stderr=subprocess.STDOUT,
    )
    for line in out.splitlines():
        if "service_role" in line and "eyJ" in line:
            candidate = line.split("|")[-1].strip()
            if candidate.startswith("eyJ") and _jwt_ref(candidate) == LINKED_PROJECT_REF:
                return candidate
    return ""


def _jwt_service_role_key() -> str:
    # BrandRap .env may point at a different Supabase project; prefer linked CLI key.
    try:
        cli_key = _cli_service_role_key()
        if cli_key:
            return cli_key
    except Exception:
        pass
    for env_name in ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY"):
        val = (os.environ.get(env_name) or "").strip()
        if val.startswith("eyJ") and _jwt_ref(val) == LINKED_PROJECT_REF:
            return val
    return ""


def supabase_creds() -> tuple[str, str]:
    url = (
        os.environ.get("SUPABASE_URL")
        or f"https://{LINKED_PROJECT_REF}.supabase.co"
    ).strip()
    key = _jwt_service_role_key()
    if not key:
        raise SystemExit("JWT SUPABASE_SERVICE_ROLE_KEY required (sb_secret_* cannot upload to Storage)")
    return url, key


def storage_object_path(source_url: str) -> str:
    digest = hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:16]
    return f"{STORAGE_PREFIX}/{digest}.png"


def public_storage_url(supabase_url: str, object_path: str) -> str:
    base = supabase_url.rstrip("/")
    encoded = urllib.parse.quote(object_path, safe="/")
    return f"{base}/storage/v1/object/public/{STORAGE_BUCKET}/{encoded}"


def download_bytes(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "costfinder-screenshot/1.0"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = resp.read()
        ctype = resp.headers.get("Content-Type", "image/png").split(";")[0].strip() or "image/png"
    return data, ctype


def upload_storage(
    supabase_url: str,
    service_key: str,
    object_path: str,
    data: bytes,
    content_type: str = "image/png",
) -> str:
    encoded = urllib.parse.quote(object_path, safe="/")
    endpoint = f"{supabase_url.rstrip('/')}/storage/v1/object/{STORAGE_BUCKET}/{encoded}"
    req = urllib.request.Request(
        endpoint,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {service_key}",
            "apikey": service_key,
            "Content-Type": content_type,
            "x-upsert": "true",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"storage upload HTTP {e.code}: {detail}") from e
    return public_storage_url(supabase_url, object_path)


def firecrawl_capture(api_key: str, page_url: str) -> dict[str, Any]:
    body = {
        "url": page_url,
        "formats": [
            {
                "type": "screenshot",
                "fullPage": True,
                "quality": 80,
                "viewport": {"width": 1440, "height": 1200},
            }
        ],
        "onlyMainContent": False,
        "blockAds": True,
        "waitFor": 8000,
        "timeout": 120000,
        "proxy": "auto",
    }
    req = urllib.request.Request(
        FIRECRAWL_SCRAPE_URL,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:500]
        return {
            "ok": False,
            "source_url": page_url,
            "error": f"HTTP {e.code}: {err}",
            "duration_s": round(time.time() - t0, 2),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "source_url": page_url,
            "error": f"{type(e).__name__}: {e}",
            "duration_s": round(time.time() - t0, 2),
        }

    if not payload.get("success"):
        return {
            "ok": False,
            "source_url": page_url,
            "error": f"firecrawl error: {payload.get('error') or payload}",
            "duration_s": round(time.time() - t0, 2),
        }

    data = payload.get("data") or {}
    temp_url = str(data.get("screenshot") or "").strip()
    if not temp_url.startswith("http"):
        return {
            "ok": False,
            "source_url": page_url,
            "error": f"screenshot_missing: {str(data)[:200]}",
            "duration_s": round(time.time() - t0, 2),
        }

    return {
        "ok": True,
        "source_url": page_url,
        "firecrawl_screenshot": temp_url,
        "duration_s": round(time.time() - t0, 2),
    }


def capture_and_persist(
    fc_key: str,
    supabase_url: str,
    service_key: str,
    page_url: str,
) -> dict[str, Any]:
    result = firecrawl_capture(fc_key, page_url)
    if not result.get("ok"):
        return result
    try:
        img, ctype = download_bytes(result["firecrawl_screenshot"])
        obj = storage_object_path(page_url)
        permanent = upload_storage(supabase_url, service_key, obj, img, ctype)
    except Exception as e:  # noqa: BLE001
        result["ok"] = False
        result["error"] = f"storage: {type(e).__name__}: {e}"
        return result
    result["screenshot"] = permanent
    result["storage_bytes"] = len(img)
    return result


def self_check() -> None:
    assert storage_object_path("https://x.com/a") == storage_object_path("https://x.com/a")
    assert STORAGE_PREFIX in storage_object_path("https://example.com")
    print("self_check ok", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=False)
    ap.add_argument("--log", type=Path, default=Path("output/promotion_screenshots_firecrawl.jsonl"))
    ap.add_argument("--sql", type=Path, default=Path("output/promotion_screenshots_firecrawl.sql"))
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()

    if args.self_check:
        self_check()
        return 0

    load_env_files()
    if not args.input:
        print("--input required", file=sys.stderr)
        return 2

    fc_key = firecrawl_api_key()
    supabase_url, service_key = supabase_creds()

    rows = load_rows(args.input)
    by_url: dict[str, list[int]] = {}
    for r in rows:
        by_url.setdefault(r["source_url"], []).append(r["promotion_id"])

    urls = sorted(by_url.keys())
    done = load_done_urls(args.log) if args.resume else set()
    pending = [u for u in urls if u not in done]
    if args.limit and args.limit > 0:
        pending = pending[: args.limit]

    print(
        f"engine=firecrawl rows={len(rows)} unique_urls={len(urls)} "
        f"done={len(done)} pending={len(pending)} bucket={STORAGE_BUCKET}/{STORAGE_PREFIX}",
        flush=True,
    )

    successes: dict[str, str] = {}
    if args.resume and args.log.exists():
        with args.log.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("ok") and obj.get("source_url") and obj.get("screenshot"):
                    successes[obj["source_url"]] = obj["screenshot"]

    ok_n = fail_n = 0
    for i, url in enumerate(pending, 1):
        result = capture_and_persist(fc_key, supabase_url, service_key, url)
        result["promotion_ids"] = by_url.get(url, [])
        append_jsonl(args.log, result)
        if result.get("ok"):
            ok_n += 1
            successes[url] = result["screenshot"]
            if args.apply:
                try:
                    apply_screenshot(
                        supabase_url,
                        service_key,
                        result["screenshot"],
                        promotion_ids=result["promotion_ids"],
                    )
                    result["applied"] = True
                except Exception as e:  # noqa: BLE001
                    result["applied"] = False
                    result["apply_error"] = f"{type(e).__name__}: {e}"
                    append_jsonl(args.log, {**result, "phase": "apply_error"})
                    print(f"  apply FAIL: {result['apply_error']}", flush=True)
            print(
                f"[{i}/{len(pending)}] OK {url[:80]} -> {result['screenshot'][:70]}...",
                flush=True,
            )
        else:
            fail_n += 1
            print(f"[{i}/{len(pending)}] FAIL {url[:80]} :: {result.get('error')}", flush=True)

    write_sql(args.sql, successes)
    print(
        f"done ok={ok_n} fail={fail_n} sql_updates={len(successes)} log={args.log} sql={args.sql}",
        flush=True,
    )
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
