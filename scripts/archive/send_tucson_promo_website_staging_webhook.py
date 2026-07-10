#!/usr/bin/env python3
"""Send promo_website_staging rows to an offer extraction webhook with retries."""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlsplit

import requests

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL.*")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
DEFAULT_WEBHOOK_URL = "https://flows.brandrap.co/webhook/274496d1-5bf1-4d27-a558-2050e9c0e837"
DEFAULT_ERROR_PATTERNS = [
    "too many request",
    "too many requests",
    "rate limit",
    "resource_exhausted",
    "quota",
]


def load_env(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
        os.environ.setdefault(key, value)
    return values


class SupabaseRestClient:
    def __init__(self, base_url: str, service_role_key: str):
        self.base_url = base_url.rstrip("/") + "/rest/v1"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def fetch_all(
        self,
        table: str,
        select: str,
        *,
        filters: Optional[Dict[str, str]] = None,
        order: Optional[str] = None,
        page_size: int = 1000,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        offset = 0
        while True:
            params: Dict[str, str] = {"select": select, "limit": str(page_size), "offset": str(offset)}
            if filters:
                params.update(filters)
            if order:
                params["order"] = order
            response = self.session.get(f"{self.base_url}/{table}", params=params, timeout=60)
            response.raise_for_status()
            batch = response.json()
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
        return rows


def normalize_domain(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        parsed = urlsplit(text)
        text = parsed.netloc or parsed.path.split("/")[0]
    else:
        text = text.split("/")[0]
    text = text.split("?")[0].split("#")[0]
    text = re.sub(r"^www\.", "", text)
    return text.strip(".")


def domain_from_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    return normalize_domain(parsed.netloc)


def master_domains(master: Dict[str, Any]) -> List[str]:
    domains = {
        normalize_domain(master.get("website_clean")),
        domain_from_url(master.get("website")),
    }
    return sorted(domain for domain in domains if domain)


def build_targets(
    masters: Iterable[Dict[str, Any]],
    staging_rows: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    master_domain_rows: List[Dict[str, Any]] = []
    for master in masters:
        compact_master = {
            "id": master.get("id"),
            "business_id": master.get("business_id"),
            "name": master.get("name"),
            "city": master.get("city"),
            "website": master.get("website"),
            "website_clean": master.get("website_clean"),
            "membership": master.get("membership"),
        }
        for domain in master_domains(master):
            master_domain_rows.append({"domain": domain, "master": compact_master})

    targets: List[Dict[str, Any]] = []
    seen_ids = set()
    for row in staging_rows:
        row_domain = normalize_domain(row.get("domain_name")) or domain_from_url(row.get("subpage_url"))
        matched_masters: List[Dict[str, Any]] = []
        for item in master_domain_rows:
            master_domain = item["domain"]
            if row_domain == master_domain or master_domain in row_domain:
                matched_masters.append(item["master"])
        if not matched_masters:
            continue
        matched_masters = sorted(
            {str(item.get("business_id")): item for item in matched_masters}.values(),
            key=lambda item: int(item.get("business_id") or 0),
        )
        row_id = row.get("promo_website_id")
        if row_id in seen_ids:
            continue
        seen_ids.add(row_id)
        primary_master = matched_masters[0]
        targets.append(
            {
                **row,
                "business_id": primary_master.get("business_id"),
                "master_business_info_id": primary_master.get("id"),
                "master_business_name": primary_master.get("name"),
                "matched_city": "Tucson",
                "matched_domain": row_domain,
                "matched_master_business_info": matched_masters,
                "join_method": "promo_website_staging.domain_name_like_master_business_info.website_clean",
                "promo_website_staging": row,
            }
        )
    targets.sort(key=lambda item: int(item.get("promo_website_id") or 0))
    return targets


def _extract_run_metadata(response_json: Any) -> Dict[str, Any]:
    run_status: Optional[str] = None
    run_id: Optional[str] = None
    queue: List[Any] = [response_json]
    while queue:
        current = queue.pop(0)
        if isinstance(current, dict):
            if run_status is None:
                for key in ("run_status", "runStatus", "status", "state"):
                    value = current.get(key)
                    if isinstance(value, str) and value.strip():
                        run_status = value.strip().lower()
                        break
            if run_id is None:
                for key in ("run_id", "runId", "id", "executionId", "execution_id"):
                    value = current.get(key)
                    if value is None:
                        continue
                    run_id_text = str(value).strip()
                    if run_id_text:
                        run_id = run_id_text
                        break
            queue.extend(current.values())
            continue
        if isinstance(current, list):
            queue.extend(current)

    return {"run_status": run_status, "run_id": run_id}


def post_payload(webhook_url: str, payload: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    response = requests.post(webhook_url, json=payload, timeout=timeout)
    result: Dict[str, Any] = {
        "status_code": response.status_code,
        "ok": response.ok,
        "response_text": response.text[:4000],
    }
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type.lower():
        try:
            result["response_json"] = response.json()
            result.update(_extract_run_metadata(result["response_json"]))
        except ValueError:
            pass
    if not response.ok:
        result["error"] = f"HTTP {response.status_code}"
    return result


def parse_csv_to_int_set(value: str) -> Set[int]:
    output: Set[int] = set()
    for chunk in (value or "").split(","):
        text = chunk.strip()
        if not text:
            continue
        output.add(int(text))
    return output


def parse_csv_to_text_set(value: str) -> Set[str]:
    output: Set[str] = set()
    for chunk in (value or "").split(","):
        text = chunk.strip().lower()
        if text:
            output.add(text)
    return output


def has_error_payload(result: Dict[str, Any], error_patterns: Set[str]) -> Optional[str]:
    response_json = result.get("response_json")
    response_text = str(result.get("response_text") or "")

    json_text = ""
    if response_json is not None:
        try:
            json_text = json.dumps(response_json, ensure_ascii=False)
        except TypeError:
            json_text = str(response_json)

    full_text = f"{response_text}\n{json_text}".lower()
    for pattern in error_patterns:
        if pattern and pattern in full_text:
            return f"response_pattern_{pattern}"

    if isinstance(response_json, list):
        for item in response_json:
            if isinstance(item, dict) and item.get("error"):
                return "response_error_field"
    if isinstance(response_json, dict) and response_json.get("error"):
        return "response_error_field"
    return None


def should_retry_result(
    result: Dict[str, Any],
    *,
    retry_http_codes: Set[int],
    retry_run_statuses: Set[str],
    error_patterns: Set[str],
) -> Tuple[bool, Optional[str]]:
    error_payload_reason = has_error_payload(result, error_patterns)
    if error_payload_reason:
        return True, error_payload_reason

    if result.get("ok") is False:
        status_code = result.get("status_code")
        if isinstance(status_code, int):
            if status_code in retry_http_codes:
                return True, f"http_{status_code}"
            return False, f"http_{status_code}"
        return True, "request_exception"

    run_status = str(result.get("run_status") or "").strip().lower()
    if run_status and run_status in retry_run_statuses:
        return True, f"run_status_{run_status}"
    return False, None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send Tucson promo website staging rows to webhook")
    parser.add_argument("--city", default="Tucson", help="master_business_info.city value to match")
    parser.add_argument("--all-cities", action="store_true", help="Do not filter master_business_info by city")
    parser.add_argument("--webhook-url", default=DEFAULT_WEBHOOK_URL, help="Workflow webhook URL")
    parser.add_argument("--send", action="store_true", help="Actually POST rows to the webhook")
    parser.add_argument("--limit", type=int, default=None, help="Only send/check first N matched rows")
    parser.add_argument("--offset", type=int, default=0, help="Skip the first N matched rows after filters")
    parser.add_argument("--workers", type=int, default=6, help="Parallel webhook requests")
    parser.add_argument("--batch-size", type=int, default=1, help="Number of staging rows per webhook request")
    parser.add_argument(
        "--exclude-domain",
        action="append",
        default=[],
        help="Domain to exclude; may be passed multiple times",
    )
    parser.add_argument("--timeout", type=int, default=240, help="Webhook request timeout in seconds")
    parser.add_argument("--sleep", type=float, default=0.2, help="Delay between webhook calls")
    parser.add_argument(
        "--min-request-interval",
        type=float,
        default=0.0,
        help="Global minimum interval in seconds between webhook requests across all workers",
    )
    parser.add_argument("--only-unprocessed", action="store_true", help="Only send rows where processed_status = false")
    parser.add_argument("--max-attempts", type=int, default=3, help="Max attempts per row when webhook/run fails")
    parser.add_argument(
        "--retry-http-codes",
        default="408,409,425,429,500,502,503,504",
        help="Comma-separated HTTP status codes that should trigger retry",
    )
    parser.add_argument(
        "--retry-run-statuses",
        default="failed,error,aborted,cancelled,timed_out,timeout",
        help="Comma-separated run statuses from webhook response that should trigger retry",
    )
    parser.add_argument(
        "--error-patterns",
        default=",".join(DEFAULT_ERROR_PATTERNS),
        help="Comma-separated response body patterns treated as failed run and retried",
    )
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=2.0,
        help="Base seconds for exponential backoff between retries (attempt_n delay = base * 2^(n-1))",
    )
    parser.add_argument("--quiet", action="store_true", help="Only print final summary; full results are written to report JSON")
    parser.add_argument("--only-ids", default="", help="Comma-separated promo_website_id whitelist")
    parser.add_argument("--progress", action="store_true", help="Show live progress bar while sending")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    env = load_env(PROJECT_ROOT / ".env")
    base_url = env.get("SUPABASE_URL")
    service_role_key = env.get("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")

    client = SupabaseRestClient(base_url, service_role_key)
    masters = client.fetch_all(
        "master_business_info",
        "id,business_id,name,city,website,website_clean,membership",
        filters=None if args.all_cities else {"city": f"ilike.{args.city}"},
        order="id.asc",
    )
    staging_rows = client.fetch_all(
        "promo_website_staging",
        "promo_website_id,crawl_timestamp,subpage_url,page_content,domain_name,processed_status,name,needs_ocr",
        filters={"processed_status": "is.false"} if args.only_unprocessed else None,
        order="promo_website_id.asc",
    )
    targets = build_targets(masters, staging_rows)
    excluded_domains = {normalize_domain(domain) for domain in args.exclude_domain if normalize_domain(domain)}
    if excluded_domains:
        targets = [row for row in targets if normalize_domain(row.get("matched_domain")) not in excluded_domains]
    if args.offset:
        targets = targets[args.offset :]
    if args.limit is not None:
        targets = targets[: args.limit]
    only_ids = parse_csv_to_int_set(args.only_ids) if args.only_ids else set()
    if only_ids:
        targets = [row for row in targets if int(row.get("promo_website_id") or 0) in only_ids]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = OUTPUT_DIR / f"tucson_promo_website_webhook_report_{timestamp}.json"

    report: Dict[str, Any] = {
        "generated_at": timestamp,
        "city": args.city,
        "all_cities": args.all_cities,
        "send": args.send,
        "webhook_url": args.webhook_url,
        "master_rows": len(masters),
        "staging_rows_scanned": len(staging_rows),
        "matched_target_rows": len(targets),
        "offset": args.offset,
        "limit": args.limit,
        "workers": args.workers,
        "batch_size": max(1, args.batch_size),
        "only_unprocessed": args.only_unprocessed,
        "max_attempts": args.max_attempts,
        "retry_http_codes": sorted(parse_csv_to_int_set(args.retry_http_codes)),
        "retry_run_statuses": sorted(parse_csv_to_text_set(args.retry_run_statuses)),
        "error_patterns": sorted(parse_csv_to_text_set(args.error_patterns)),
        "min_request_interval": args.min_request_interval,
        "only_ids_count": len(only_ids),
        "progress": args.progress,
        "retry_backoff": args.retry_backoff,
        "excluded_domains": sorted(excluded_domains),
        "target_ids": [row.get("promo_website_id") for row in targets],
        "results": [],
    }

    retry_http_codes = parse_csv_to_int_set(args.retry_http_codes)
    retry_run_statuses = parse_csv_to_text_set(args.retry_run_statuses)
    error_patterns = parse_csv_to_text_set(args.error_patterns)
    max_attempts = max(1, args.max_attempts)
    throttle_lock = threading.Lock()
    throttle_state = {"next_allowed_at": 0.0}

    def wait_for_global_rate_limit() -> None:
        if args.min_request_interval <= 0:
            return
        while True:
            sleep_seconds = 0.0
            with throttle_lock:
                now = time.monotonic()
                if now >= throttle_state["next_allowed_at"]:
                    throttle_state["next_allowed_at"] = now + args.min_request_interval
                    return
                sleep_seconds = throttle_state["next_allowed_at"] - now
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

    def write_report() -> None:
        with report_path.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)

    def print_progress(done: int, total: int) -> None:
        if not args.progress:
            return
        total_safe = max(1, total)
        width = 28
        filled = int(width * done / total_safe)
        bar = "#" * filled + "-" * (width - filled)
        percent = (done * 100.0) / total_safe
        sys.stderr.write(f"\rprogress [{bar}] {done}/{total} ({percent:5.1f}%)")
        if done >= total:
            sys.stderr.write("\n")
        sys.stderr.flush()

    batch_size = max(1, args.batch_size)

    def chunk_targets(rows: List[Dict[str, Any]], size: int) -> List[List[Dict[str, Any]]]:
        return [rows[i : i + size] for i in range(0, len(rows), size)]

    target_batches = chunk_targets(targets, batch_size)

    def build_result(index: int, payload_batch: List[Dict[str, Any]]) -> Dict[str, Any]:
        first_row = payload_batch[0]
        batch_ids = [row.get("promo_website_id") for row in payload_batch]
        result = {
            "index": index,
            "batch_size": len(payload_batch),
            "promo_website_ids": batch_ids,
            "promo_website_id": first_row.get("promo_website_id"),
            "domain_name": first_row.get("domain_name"),
            "subpage_url": first_row.get("subpage_url"),
            "matched_master_business_ids": sorted(
                {
                    item.get("business_id")
                    for row in payload_batch
                    for item in row.get("matched_master_business_info", [])
                    if item.get("business_id") is not None
                }
            ),
        }
        if args.send:
            request_payload: Any
            if len(payload_batch) == 1:
                request_payload = payload_batch[0]
            else:
                request_payload = payload_batch
            attempts: List[Dict[str, Any]] = []
            last_retry_reason: Optional[str] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    wait_for_global_rate_limit()
                    attempt_result = post_payload(args.webhook_url, request_payload, args.timeout)
                except Exception as exc:  # noqa: BLE001 - report integration failures per row.
                    attempt_result = {"ok": False, "error": repr(exc), "exception_type": type(exc).__name__}

                attempt_result["attempt"] = attempt
                attempts.append(
                    {
                        "attempt": attempt,
                        "ok": attempt_result.get("ok"),
                        "status_code": attempt_result.get("status_code"),
                        "run_status": attempt_result.get("run_status"),
                        "run_id": attempt_result.get("run_id"),
                        "error": attempt_result.get("error"),
                    }
                )
                result.update(attempt_result)

                should_retry, retry_reason = should_retry_result(
                    attempt_result,
                    retry_http_codes=retry_http_codes,
                    retry_run_statuses=retry_run_statuses,
                    error_patterns=error_patterns,
                )
                last_retry_reason = retry_reason
                if not should_retry or attempt >= max_attempts:
                    break

                if args.sleep > 0:
                    time.sleep(args.sleep)
                if args.retry_backoff > 0:
                    time.sleep(args.retry_backoff * (2 ** (attempt - 1)))

            result["attempts"] = attempts
            result["attempt_count"] = len(attempts)
            result["last_retry_reason"] = last_retry_reason
            result["retried"] = len(attempts) > 1
        return result

    indexed_targets = list(enumerate(target_batches, start=1))
    if args.send and args.workers > 1:
        total_batches = len(indexed_targets)
        completed_batches = 0
        print_progress(completed_batches, total_batches)
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_map = {
                executor.submit(build_result, index, payload): index
                for index, payload in indexed_targets
            }
            for future in as_completed(future_map):
                result = future.result()
                report["results"].append(result)
                completed_batches += 1
                print_progress(completed_batches, total_batches)
                write_report()
                if not args.quiet:
                    print(json.dumps(result, ensure_ascii=False))
                    sys.stdout.flush()
        report["results"].sort(key=lambda item: int(item.get("index") or 0))
        write_report()
    else:
        total_batches = len(indexed_targets)
        completed_batches = 0
        print_progress(completed_batches, total_batches)
        for index, payload in indexed_targets:
            result = build_result(index, payload)
            report["results"].append(result)
            completed_batches += 1
            print_progress(completed_batches, total_batches)
            write_report()
            if not args.quiet:
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(
                    json.dumps(
                        {
                            "index": result.get("index"),
                            "promo_website_id": result.get("promo_website_id"),
                            "status_code": result.get("status_code"),
                            "ok": result.get("ok"),
                            "error": result.get("error"),
                        },
                        ensure_ascii=False,
                    )
                )
            sys.stdout.flush()

    write_report()

    ok_batch_count = sum(1 for item in report["results"] if item.get("ok"))
    error_batch_count = sum(1 for item in report["results"] if item.get("ok") is False)
    ok_row_count = sum(
        len(item.get("promo_website_ids") or [item.get("promo_website_id")])
        for item in report["results"]
        if item.get("ok")
    )
    error_row_count = sum(
        len(item.get("promo_website_ids") or [item.get("promo_website_id")])
        for item in report["results"]
        if item.get("ok") is False
    )
    retried_count = sum(1 for item in report["results"] if item.get("retried"))
    print(
        json.dumps(
            {
                "report_path": str(report_path),
                "matched_target_rows": len(targets),
                "ok_count": ok_batch_count,
                "error_count": error_batch_count,
                "ok_row_count": ok_row_count,
                "error_row_count": error_row_count,
                "retried_count": retried_count,
                "sent": args.send,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
