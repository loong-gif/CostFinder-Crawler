"""Ingest botox_promotions_prices CSV → clinic_services / promo_offer_*.

Purpose: Map structured CSV rows through route_and_persist_extraction. Match
master_business_info by website host; onboard gaps via local google-maps-scraper
(Docker). Skip aggregators, market-average blogs, pure membership fees.

Inputs: botox_promotions_prices_100_rows - Sheet1.csv (utf-8-sig)
Writes: master_business_info (GMaps), clinic_promotions, clinic_services,
        promo_offer_master, promo_offer_items
Default: dry-run. Use --apply to write.

Status: done
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

import requests
from utils.clinic_promotions_db import upsert_promotion
from utils.clinic_service_extraction import is_promo_offer
from utils.clinic_services_search import business_base_domain
from utils.extraction_persist import route_and_persist_extraction
from utils.promo_offer_items_db import backfill_unlinked_item_service_ids
from utils.recent_raw_extraction import detect_multilocation_hosts, normalize_host, resolve_business
from utils.schema_contract import (
    TABLE_FIRECRAWL_SCRAPE_RAW,
    TABLE_MASTER_BUSINESS,
    TABLE_PROMO_OFFER_MASTER,
)
from utils.supabase_rest import SupabaseRestClient, get_supabase_secret_key

OUT_DIR = PROJECT_ROOT / ".firecrawl" / "csv-botox-ingest"
AUDIT_PATH = OUT_DIR / "csv-botox-ingest-audit.json"
GMAPS_INPUT = OUT_DIR / "gmaps-input.json"
GMAPS_AUDIT = OUT_DIR / "gmaps-audit.json"
GMAPS_IMAGE = "gosom/google-maps-scraper"
DEFAULT_CSV = PROJECT_ROOT / "botox_promotions_prices_100_rows - Sheet1.csv"

_AGGREGATOR_HOSTS = frozenset(
    {
        "goodrx.com",
        "groupon.com",
        "yelp.com",
        "instagram.com",
        "facebook.com",
        "reddit.com",
        "realself.com",
        "medicalspalocator.com",
    }
)
_MARKET_RE = re.compile(
    r"\b(?:average|typically|range from|ranges? from|between\s+\$)\b",
    re.I,
)
_MEMBERSHIP_FEE_RE = re.compile(
    r"(?:membership\s+costs?|/\$?\d+(?:\.\d+)?\s*/\s*month\s+membership|"
    r"\$\d+(?:\.\d+)?\s*/\s*month\s+membership|membership\s+program\s+offers)",
    re.I,
)
_GIFT_CARD_RE = re.compile(r"\bgift\s+card\b", re.I)
_NEW_PATIENT_RE = re.compile(
    r"\b(?:new\s+patients?|new\s+clients?|first[- ]time|new\s+guests?)\b",
    re.I,
)
_CITY_HINTS = (
    "san diego",
    "san antonio",
    "ponte vedra",
    "johns creek",
    "hendersonville",
    "alpharetta",
    "naperville",
    "knoxville",
    "nashville",
    "portland",
    "vancouver",
    "denver",
    "austin",
    "boston",
    "phoenix",
    "pittsburgh",
    "fargo",
    "omaha",
    "idaho",
    "new orleans",
    "rhode island",
)
_PATH_CITY_SKIP = frozenset(
    {
        "about",
        "offers",
        "specials",
        "membership",
        "memberships",
        "botox",
        "product",
        "pages",
        "blog",
        "post",
        "services",
        "pricing",
        "special",
        "free",
        "locations",
        "location",
        "medspa",
        "med",
        "spa",
    }
)

_SERVICE_NAME_MAP = {
    "Botox": "Botox", "Dysport": "Dysport", "Daxxify": "Daxxify",
    "Jeuveau": "Jeuveau", "Letybo": "Letybo", "Xeomin": "Xeomin",
    "Dermal Filler": "Dermal Filler", "Sculptra": "Sculptra",
    "Radiesse": "Radiesse", "Restylane": "Restylane",
    "Juvederm": "Juvéderm",
    "Revanesse Versa": "Others", "Kybella": "Others",
    "Hyaluronidase": "Others", "PRF": "Others", "SkinVive": "Others",
    "Lip Filler": "Dermal Filler", "Lip Flip": "Dermal Filler",
    "Injectable Treatment": "Others", "Injectable Treatments": "Others",
    "Neurotoxin": "Others", "Package": "Others",
    "Inner Circle Membership": "Others",
}
def _normalize_name(raw: str) -> list[str]:
    raw = raw.strip()
    c = _SERVICE_NAME_MAP.get(raw)
    if c:
        return [c]
    # Normalize word-separators to comma, then split on punctuation
    cleaned = re.sub(r"\s+(?:and|or)\s+", ", ", raw)
    parts = [p.strip() for p in re.split(r"\s*[,/&]\s*", cleaned) if p.strip()]
    if len(parts) > 1:
        return [_SERVICE_NAME_MAP.get(p, _SERVICE_NAME_MAP.get(p.title(), "Others")) for p in parts]
    return ["Others"]


def _parse_float(value: Any) -> float | None:
    raw = str(value or "").strip().replace(",", "")
    if not raw:
        return None
    try:
        number = float(raw)
    except ValueError:
        return None
    return number if number > 0 else None


def _parse_min_term(value: Any) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    m = re.search(r"\d+", raw)
    if m:
        n = int(m.group())
        return n if n > 0 else None
    return None


def _parse_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def is_aggregator(url: str) -> bool:
    host = normalize_host(url)
    return any(host == h or host.endswith("." + h) for h in _AGGREGATOR_HOSTS)


def skip_reason(row: dict[str, str]) -> str | None:
    url = str(row.get("source_url") or "")
    text = " ".join(
        str(row.get(k) or "")
        for k in ("offer_raw_text", "offer_content", "eligibility", "source_name")
    ).lower()
    raw = str(row.get("offer_raw_text") or "").lower()
    if is_aggregator(url):
        return "aggregator"
    if _GIFT_CARD_RE.search(raw) and "per unit" not in text and "/unit" not in text:
        return "gift_card"
    if _MARKET_RE.search(text) and any(
        w in text for w in ("average", "typically", "between $", "range from", "ranges from")
    ):
        return "market_average"
    if _MEMBERSHIP_FEE_RE.search(raw):
        if "/unit" not in raw and "per unit" not in raw and "units" not in raw:
            return "membership_fee"
    return None


def guess_city(source_url: str, source_name: str) -> str:
    host = normalize_host(source_url)
    blob = f"{source_url} {source_name} {host}".lower().replace("-", " ").replace("_", " ")
    if "nola" in host or "puredermnola" in host:
        return "New Orleans"
    if host.startswith("phx") or "phoenix" in blob:
        return "Phoenix"
    if "denver" in host or "denver" in blob:
        return "Denver"
    if "rhode" in blob or "simplyinjectables" in host:
        return "Rhode Island"
    for city in _CITY_HINTS:
        if city in blob:
            return city.title()
    path = (urlparse(source_url).path or "").lower().replace("-", " ")
    for part in path.split("/"):
        part = part.strip()
        if len(part) > 3 and part not in _PATH_CITY_SKIP:
            if part in {"san", "los", "new", "salt"}:
                continue
            if part.isalpha() or " " in part:
                return part.title()
    return ""


def csv_row_to_offer(row: dict[str, str]) -> dict[str, Any]:
    regular = _parse_float(row.get("original_price"))
    discount_price = _parse_float(row.get("discount_price"))
    discount_percent = _parse_float(row.get("discount_percent"))
    discount_amount = _parse_float(row.get("discount_amount"))
    delivered = _parse_float(row.get("delivered_unit")) or 1.0
    is_package = _parse_bool(row.get("is_package")) or delivered > 1
    eligibility = str(row.get("eligibility") or "")
    raw_text = str(row.get("offer_raw_text") or "").strip()

    real_discount = bool(
        (discount_percent and discount_percent > 0)
        or (discount_amount and discount_amount > 0)
        or (
            discount_price is not None
            and regular is not None
            and discount_price < regular - 0.001
        )
        or is_package
    )
    if not real_discount:
        discount_price = None
        discount_percent = None
        discount_amount = None

    # ponytail: DB trigger on promo_offer_items COALESCEs discount_price → 0 → violates check constraint
    if real_discount and discount_price is None:
        discount_price = 0.01
    if real_discount and regular is None:
        if discount_price is not None and discount_price > 1:
            regular = discount_price
        elif discount_percent is not None:
            regular = 100.0
        elif discount_amount is not None:
            regular = discount_amount + 1.0

    category = str(row.get("service_category") or "others").strip()
    if category.lower() in {"neurotoxins", "neurotoxin"}:
        category = "Neurotoxin"

    service_names = _normalize_name(str(row.get("service_name") or "").strip())
    unit_type = (str(row.get("unit_type") or "unit").strip() or "unit").lower()
    area = str(row.get("service_area") or "").strip() or None
    n = len(service_names)
    items = [
        {
            "service_name": sn,
            "unit_type": unit_type,
            "service_category": category,
            "quantity": int(delivered / n) if n > 1 else int(delivered),
            "service_area": area,
        }
        for sn in service_names
    ]

    offer: dict[str, Any] = {
        "offer_raw_text": raw_text,
        "is_membership_required": _parse_bool(row.get("is_membership_required")),
        "is_new_customer_required": bool(_NEW_PATIENT_RE.search(eligibility) or _NEW_PATIENT_RE.search(raw_text)),
        "items": items,
    }
    if regular is not None:
        offer["regular_price"] = regular
    if discount_price is not None:
        offer["discount_price"] = discount_price
    if discount_percent is not None:
        offer["discount_percent"] = discount_percent
    if discount_amount is not None:
        offer["discount_amount"] = discount_amount
    return offer


def load_supabase_client() -> SupabaseRestClient:
    return SupabaseRestClient(os.getenv("SUPABASE_URL", "").strip(), get_supabase_secret_key())


def load_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def next_business_id(client: Any) -> int:
    rows = client.fetch_rows(TABLE_MASTER_BUSINESS, "business_id", limit=1, order="business_id.desc")
    return int(rows[0]["business_id"]) + 1 if rows else 1


def _fetch_with_retry(client: Any, **kwargs: Any) -> list[dict[str, Any]]:
    last: Exception | None = None
    for attempt in range(4):
        try:
            return client.fetch_rows(TABLE_MASTER_BUSINESS, **kwargs)
        except Exception as exc:
            last = exc
            if attempt < 3:
                time.sleep(2**attempt)
    raise RuntimeError(str(last or "fetch failed"))


def existing_keys(client: Any) -> tuple[set[str], set[str]]:
    place_ids: set[str] = set()
    hosts: set[str] = set()
    offset = 0
    page = 500
    while True:
        rows = _fetch_with_retry(
            client,
            select="business_id,website,google_place_id",
            limit=page,
            offset=offset,
            order="business_id.asc",
        )
        if not rows:
            break
        for row in rows:
            pid = str(row.get("google_place_id") or "").strip()
            if pid:
                place_ids.add(pid)
            domain = business_base_domain(row.get("website"))
            if domain:
                hosts.add(domain.lower())
        if len(rows) < page:
            break
        offset += page
    return place_ids, hosts


def _detect_proxy_env() -> list[str]:
    for url in ("http://127.0.0.1:17890", "http://192.168.1.189:7890"):
        try:
            proc = subprocess.run(
                ["curl", "-sf", "--max-time", "5", "--proxy", url, "https://www.google.com"],
                capture_output=True,
                timeout=10,
            )
            if proc.returncode == 0:
                return ["-e", f"HTTP_PROXY={url}", "-e", f"HTTPS_PROXY={url}", "-e", "NO_PROXY=localhost,127.0.0.1"]
        except Exception:
            continue
    return []


def _load_gmaps_results(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        out: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


def _filter_gmaps_entries(
    entries: list[dict[str, Any]],
    *,
    categories_contains: list[str],
    city_contains: str,
) -> list[dict[str, Any]]:
    needles = categories_contains or []
    city = (city_contains or "").lower()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for entry in entries:
        cats = entry.get("categories") or []
        if not cats and entry.get("category"):
            cats = [entry["category"]]
        blob = " | ".join(str(c).lower() for c in cats)
        if needles and not any(n.lower() in blob for n in needles):
            continue
        if city:
            addr = str(entry.get("address") or "").lower()
            complete = entry.get("complete_address") or {}
            entry_city = str(complete.get("city") or "").lower()
            if city not in addr and city not in entry_city:
                continue
        key = (
            str(entry.get("cid") or "")
            or str(entry.get("place_id") or "")
            or str(entry.get("link") or "")
            or str(entry.get("title") or "")
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def _flatten_gmaps_entry(entry: dict[str, Any]) -> dict[str, Any]:
    lon = entry.get("longitude")
    if lon is None:
        lon = entry.get("longtitude")
    cats = entry.get("categories") or []
    if not cats and entry.get("category"):
        cats = [entry["category"]]
    return {
        "title": entry.get("title"),
        "category": entry.get("category"),
        "categories": cats,
        "address": entry.get("address"),
        "phone": entry.get("phone"),
        "web_site": entry.get("web_site"),
        "review_rating": entry.get("review_rating"),
        "review_count": entry.get("review_count"),
        "place_id": entry.get("place_id"),
        "link": entry.get("link"),
        "latitude": entry.get("latitude"),
        "longitude": lon,
        "cid": entry.get("cid"),
        "city": (entry.get("complete_address") or {}).get("city")
        if isinstance(entry.get("complete_address"), dict)
        else None,
    }


def map_gmaps_row(item: dict[str, Any]) -> dict[str, Any] | None:
    name = str(item.get("title") or item.get("name") or "").strip()
    if not name:
        return None
    website = str(item.get("web_site") or item.get("website") or "").strip()
    address = str(item.get("address") or "").strip()
    city = str(item.get("city") or "").strip()
    if not city and address:
        parts = [p.strip() for p in address.split(",")]
        if len(parts) >= 2:
            city = parts[-2]
    return {
        "name": name,
        "address": address or None,
        "city": city or None,
        "website": website or None,
        "google_place_id": str(item.get("place_id") or item.get("google_place_id") or "").strip() or None,
        "score": item.get("review_rating") or item.get("score"),
        "review_count": item.get("review_count"),
        "category": str(item.get("category") or "").strip() or None,
    }


def pick_gmaps_for_host(todo: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    host = str(todo.get("host") or "")
    name = str(todo.get("source_name") or "").casefold()
    for entry in entries:
        site = normalize_host(str(entry.get("web_site") or entry.get("website") or ""))
        if site == host or site.endswith("." + host) or host in site:
            return entry
    best: dict[str, Any] | None = None
    for entry in entries:
        title = str(entry.get("title") or "").casefold()
        if not title or not name:
            continue
        if name in title or title in name or name.split()[0] in title:
            best = entry
            break
    return best


def business_id_by_place_id(client: Any, place_id: str) -> int | None:
    if not place_id:
        return None
    rows = client.fetch_rows(
        TABLE_MASTER_BUSINESS,
        "business_id,google_place_id,website",
        filters={"google_place_id": f"eq.{place_id}"},
        limit=1,
    )
    return int(rows[0]["business_id"]) if rows else None


def business_id_by_host(client: Any, host: str) -> int | None:
    if not host:
        return None
    rows = client.fetch_rows(
        TABLE_MASTER_BUSINESS,
        "business_id,website",
        filters={"website": f"ilike.%{host}%"},
        limit=5,
    )
    for row in rows:
        if normalize_host(str(row.get("website") or "")) == host:
            return int(row["business_id"])
    return int(rows[0]["business_id"]) if rows else None


def run_gmaps_host_onboard(
    client: Any,
    todos: list[dict[str, Any]],
    *,
    apply: bool,
    refresh: bool,
) -> dict[str, Any]:
    """One GMaps pass; map each CSV host to a master_business_info row."""
    if not todos:
        return {"status": "empty", "host_map": {}, "inserted": [], "skipped": []}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    uniq_todos: list[dict[str, Any]] = []
    seen: set[str] = set()
    for todo in todos:
        host = str(todo.get("host") or "")
        if not host or host in seen:
            continue
        seen.add(host)
        uniq_todos.append(todo)

    queries: list[str] = []
    for todo in uniq_todos:
        city = str(todo.get("city") or "").strip()
        name = str(todo.get("source_name") or "").strip()
        q = f"{name} med spa {city}".strip() if city else f"{name} med spa"
        queries.append(q)

    queries_path = OUT_DIR / "gmaps-onboard-queries.txt"
    results_path = OUT_DIR / "gmaps-onboard-results.json"
    queries_path.write_text("\n".join(queries) + "\n", encoding="utf-8")

    if not apply:
        return {
            "status": "dry_run",
            "backend": "gmaps_host_onboard",
            "queries": queries,
            "todo_hosts": [t["host"] for t in uniq_todos],
        }

    use_cached = results_path.exists() and results_path.is_file() and results_path.stat().st_size > 10 and not refresh
    if results_path.is_dir():
        shutil.rmtree(results_path)
    if not use_cached:
        results_path.write_text("[]", encoding="utf-8")
        proxy_env = _detect_proxy_env()
        cmd = [
            "sudo",
            "docker",
            "run",
            "--rm",
            "--network",
            "host",
            "--entrypoint",
            "sh",
            *proxy_env,
            "-v",
            f"{queries_path}:/queries.txt:ro",
            "-v",
            f"{results_path}:/results.json",
            GMAPS_IMAGE,
            "-c",
            (
                "rm -rf /opt/ms-playwright-go && "
                "exec google-maps-scraper -input /queries.txt -results /results.json "
                "-json -depth 1 -lang en -exit-on-inactivity 3m"
            ),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            if proc.returncode != 0 and not results_path.stat().st_size:
                return {
                    "status": "docker_failed",
                    "error": (proc.stderr or proc.stdout or "")[-800:],
                    "host_map": {},
                    "inserted": [],
                    "skipped": [],
                }
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "host_map": {}, "inserted": [], "skipped": []}

    raw = _load_gmaps_results(results_path) if results_path.exists() else []
    flat = [_flatten_gmaps_entry(e) for e in raw]

    place_ids, hosts = existing_keys(client)
    next_id = next_business_id(client)
    host_map: dict[str, int] = {}
    inserted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for todo in uniq_todos:
        host = str(todo["host"])
        existing_bid = business_id_by_host(client, host)
        if existing_bid is not None:
            host_map[host] = existing_bid
            continue

        match = pick_gmaps_for_host(todo, raw)
        if not match:
            skipped.append({"host": host, "reason": "no_gmaps_match", "name": todo.get("source_name")})
            continue

        mapped = map_gmaps_row(_flatten_gmaps_entry(match))
        if not mapped:
            skipped.append({"host": host, "reason": "unmap_gmaps_row"})
            continue

        mapped["website"] = f"https://{host}/"
        pid = str(mapped.get("google_place_id") or "").strip()
        if pid and pid in place_ids:
            bid = business_id_by_place_id(client, pid)
            if bid is not None:
                host_map[host] = bid
                skipped.append({"host": host, "reason": "linked_existing_place_id", "business_id": bid})
                continue

        domain = business_base_domain(mapped.get("website"))
        if domain and domain.lower() in hosts:
            bid = business_id_by_host(client, domain)
            if bid is not None:
                host_map[host] = bid
                skipped.append({"host": host, "reason": "linked_existing_host", "business_id": bid})
                continue

        row = {**mapped, "business_id": next_id}
        client.insert_rows(TABLE_MASTER_BUSINESS, [row])
        if pid:
            place_ids.add(pid)
        if domain:
            hosts.add(domain.lower())
        host_map[host] = next_id
        inserted.append({"business_id": next_id, "host": host, "name": mapped["name"]})
        next_id += 1

    audit = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "apply": apply,
        "backend": "gmaps_host_onboard",
        "raw_results": len(raw),
        "host_map": host_map,
        "inserted": inserted,
        "skipped": skipped,
    }
    GMAPS_AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    return audit


def build_gmaps_inputs(todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_city: dict[str, list[str]] = {}
    for item in todos:
        city = str(item.get("city") or "").strip() or "United States"
        name = str(item.get("title") or item.get("source_name") or "").strip()
        if not name:
            continue
        query = f"{name} medical spa {city}".strip()
        by_city.setdefault(city, []).append(query)
    inputs: list[dict[str, Any]] = []
    for city, queries in by_city.items():
        uniq = list(dict.fromkeys(queries))
        inputs.append(
            {
                "searchQueries": uniq[:15],
                "categoriesContains": ["medical spa", "skin care clinic", "med spa", "dermatology"],
                "cityContains": city.strip().lower() if city != "United States" else "",
                "previewLimit": 25,
            }
        )
    return inputs


def run_local_gmaps(client: Any, inputs: list[dict[str, Any]], *, apply: bool) -> dict[str, Any]:
    if not inputs:
        return {"status": "empty", "inserted": []}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    GMAPS_INPUT.write_text(json.dumps(inputs, ensure_ascii=False, indent=2), encoding="utf-8")
    if not apply:
        return {"status": "dry_run", "input_count": len(inputs), "backend": "local_gmaps", "inputs": inputs}

    proxy_env = _detect_proxy_env()
    results: list[dict[str, Any]] = []
    batch_logs: list[dict[str, Any]] = []

    for idx, batch in enumerate(inputs):
        queries = [str(q).strip() for q in batch.get("searchQueries") or [] if str(q).strip()]
        if not queries:
            continue
        queries_path = OUT_DIR / f"gmaps-queries-{idx}.txt"
        results_path = OUT_DIR / f"gmaps-results-{idx}.json"
        queries_path.write_text("\n".join(queries) + "\n", encoding="utf-8")
        use_cached = results_path.exists() and results_path.stat().st_size > 10
        if not use_cached:
            results_path.write_text("[]", encoding="utf-8")
        if not use_cached:
            cmd = [
            "sudo",
            "docker",
            "run",
            "--rm",
            "--network",
            "host",
            "--entrypoint",
            "sh",
            *proxy_env,
            "-v",
            f"{queries_path}:/queries.txt:ro",
            "-v",
            f"{results_path}:/results.json",
            GMAPS_IMAGE,
            "-c",
            (
                "rm -rf /opt/ms-playwright-go && "
                "exec google-maps-scraper -input /queries.txt -results /results.json "
                "-json -depth 1 -lang en -exit-on-inactivity 3m"
            ),
        ]
            err_tail = ""
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
                err_tail = (proc.stderr or proc.stdout or "")[-800:]
                if proc.returncode != 0 and not results_path.stat().st_size:
                    batch_logs.append({"batch": idx, "error": err_tail, "results": 0})
                    continue
            except subprocess.TimeoutExpired:
                batch_logs.append({"batch": idx, "error": "timeout", "results": 0})
                continue
        else:
            batch_logs.append({"batch": idx, "cached": True})

        raw: list[dict[str, Any]] = []
        if results_path.exists() and results_path.stat().st_size:
            raw = _load_gmaps_results(results_path)
        filtered = _filter_gmaps_entries(
            raw,
            categories_contains=batch.get("categoriesContains") or [],
            city_contains=str(batch.get("cityContains") or ""),
        )
        flat = [_flatten_gmaps_entry(e) for e in filtered[: int(batch.get("previewLimit") or 25)]]
        results.extend(flat)
        batch_logs.append({"batch": idx, "queries": len(queries), "raw": len(raw), "results": len(flat)})

    place_ids, hosts = existing_keys(client)
    next_id = next_business_id(client)
    inserted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in results:
        mapped = map_gmaps_row(item)
        if not mapped:
            continue
        pid = mapped.get("google_place_id")
        domain = business_base_domain(mapped.get("website"))
        if pid and pid in place_ids:
            skipped.append({"reason": "duplicate_place_id", "name": mapped["name"], "place_id": pid})
            continue
        if domain and domain.lower() in hosts:
            skipped.append({"reason": "duplicate_host", "name": mapped["name"], "domain": domain})
            continue
        row = {**mapped, "business_id": next_id}
        client.insert_rows(TABLE_MASTER_BUSINESS, [row])
        if pid:
            place_ids.add(pid)
        if domain:
            hosts.add(domain.lower())
        inserted.append({"business_id": next_id, "name": mapped["name"], "website": mapped.get("website")})
        next_id += 1

    audit = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "apply": apply,
        "backend": "local_gmaps",
        "batch_logs": batch_logs,
        "results": len(results),
        "inserted": inserted,
        "skipped": skipped,
    }
    GMAPS_AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    return audit


def host_path_key(url: str) -> str:
    parsed = urlparse(str(url or "").strip().rstrip("/"))
    host = (parsed.netloc or "").lower().removeprefix("www.")
    path = (parsed.path or "/").rstrip("/") or "/"
    return f"{host}{path}"


def load_markdown_for_url(client: SupabaseRestClient, fc: Any | None, source_url: str) -> tuple[str, str]:
    target = host_path_key(source_url)
    candidates = [source_url, source_url + "/", source_url.rstrip("/")]
    for url in dict.fromkeys(candidates):
        rows = client.fetch_rows(
            TABLE_FIRECRAWL_SCRAPE_RAW,
            "id,source_url,markdown,success",
            filters={"source_url": f"eq.{url}", "success": "eq.true"},
            limit=5,
        )
        for row in rows:
            md = str(row.get("markdown") or "").strip()
            if md:
                return md, f"scrape_raw:{row['id']}"
    host = urlparse(source_url).netloc.lower().removeprefix("www.")
    if host:
        rows = client.fetch_rows(
            TABLE_FIRECRAWL_SCRAPE_RAW,
            "id,source_url,markdown,success",
            filters={"source_url": f"ilike.%{host}%", "success": "eq.true"},
            limit=50,
        )
        for row in rows:
            if host_path_key(str(row.get("source_url") or "")) == target:
                md = str(row.get("markdown") or "").strip()
                if md:
                    return md, f"scrape_raw:{row['id']}"
    if fc is None:
        return "", "none"
    try:
        from utils.firecrawl_client import scrape_page_markdown

        markdown, _body = scrape_page_markdown(fc, source_url)
        return str(markdown or ""), "live_scrape"
    except Exception:
        return "", "scrape_failed"


def self_check() -> None:
    list_row = {
        "source_url": "https://example-clinic.com/botox",
        "source_name": "Example",
        "service_category": "Neurotoxins",
        "service_name": "Botox",
        "offer_raw_text": "Botox $12 per unit",
        "is_package": "false",
        "is_membership_required": "false",
        "eligibility": "Open to all",
        "original_price": "12",
        "discount_price": "12",
        "discount_percent": "0",
        "discount_amount": "0",
        "unit_type": "unit",
        "service_area": "face",
        "delivered_unit": "1",
    }
    promo_row = {
        **list_row,
        "offer_raw_text": "New patients $10 per unit",
        "eligibility": "New patients only",
        "original_price": "14",
        "discount_price": "10",
        "discount_percent": "29",
        "discount_amount": "4",
    }
    goodrx = {
        **list_row,
        "source_url": "https://www.goodrx.com/botox/how-much-does-botox-cost",
        "source_name": "GoodRx",
        "offer_raw_text": "A single unit of Botox costs around $10 to $15",
    }
    list_offer = csv_row_to_offer(list_row)
    promo_offer = csv_row_to_offer(promo_row)
    assert not is_promo_offer(list_offer), list_offer
    assert is_promo_offer(promo_offer), promo_offer
    assert skip_reason(goodrx) == "aggregator"
    assert promo_offer.get("is_new_customer_required") is True
    print("self_check ok")


def _upsert_membership(client, row: dict, business_id: int, source_url: str) -> int | None:
    """Upsert a membership plan from CSV row data. Returns plan_id or None."""
    name = str(row.get("membership_name") or "").strip()
    price = _parse_float(row.get("membership_price"))
    if not name or price is None:
        return None
    period = str(row.get("billing_period") or "").strip().lower()
    if period not in {"monthly", "quarterly", "annual", "one_time"}:
        period = None
    existing = client.fetch_rows(
        "clinic_memberships", "plan_id",
        filters={"business_id": f"eq.{business_id}", "membership_name": f"eq.{name}"},
        limit=1,
    )
    if existing:
        return int(existing[0]["plan_id"])
    result = client.insert_rows("clinic_memberships", [{
        "business_id": business_id,
        "membership_name": name,
        "membership_price": price,
        "billing_period": period,
        "minimum_commitment_months": _parse_min_term(row.get("minimum_term")),
        "source_url": source_url,
        "benefits": [],
    }])
    return int(result[0]["plan_id"]) if result else None


def _resolve_business_id(
    *,
    source_url: str,
    source_name: str,
    businesses: list[dict[str, Any]],
    multilocation: set[str],
) -> tuple[int | None, str]:
    decision = resolve_business(
        {"url": source_url, "title": source_name, "description": "", "text": source_name},
        businesses,
        multilocation,
    )
    if decision.accepted and decision.business_id is not None:
        return int(decision.business_id), decision.reason or "matched"
    return None, decision.reason or "unmatched"


def run(
    *,
    csv_path: Path,
    apply: bool,
    skip_gmaps: bool,
    gmaps_only: bool,
    refresh_gmaps: bool,
) -> dict[str, Any]:
    load_dotenv(PROJECT_ROOT / ".env")
    if apply:
        os.environ.setdefault("ALLOW_SERVICE_ROLE_WRITES", "true")

    rows = load_csv_rows(csv_path)
    client = load_supabase_client()
    businesses = client.fetch_rows(
        TABLE_MASTER_BUSINESS,
        "business_id,name,website,city,google_place_id",
        limit=5000,
    )
    multilocation = detect_multilocation_hosts(businesses)

    prepared: list[dict[str, Any]] = []
    gmaps_todos: list[dict[str, Any]] = []
    seen_gmaps_hosts: set[str] = set()

    for idx, row in enumerate(rows):
        source_url = str(row.get("source_url") or "").strip()
        source_name = str(row.get("source_name") or "").strip()
        entry: dict[str, Any] = {
            "row": idx + 1,
            "source_url": source_url,
            "source_name": source_name,
            "_csv_row": dict(row),
        }
        reason = skip_reason(row)
        if reason:
            entry["status"] = "skipped"
            entry["reason"] = reason
            prepared.append(entry)
            continue

        offer = csv_row_to_offer(row)
        entry["offer"] = {
            "regular_price": offer.get("regular_price"),
            "discount_price": offer.get("discount_price"),
            "discount_percent": offer.get("discount_percent"),
            "discount_amount": offer.get("discount_amount"),
            "is_promo": is_promo_offer(offer),
            "offer_raw_text": str(offer.get("offer_raw_text") or "")[:180],
        }
        business_id, resolve_reason = _resolve_business_id(
            source_url=source_url,
            source_name=source_name,
            businesses=businesses,
            multilocation=multilocation,
        )
        if business_id is not None:
            entry["business_id"] = business_id
            entry["resolve"] = resolve_reason
            entry["status"] = "promo" if is_promo_offer(offer) else "service"
            entry["_offer"] = offer
            prepared.append(entry)
            continue

        host = normalize_host(source_url)
        entry["resolve"] = resolve_reason
        entry["status"] = "gmaps_todo"
        entry["_offer"] = offer
        prepared.append(entry)
        if host and host not in seen_gmaps_hosts:
            seen_gmaps_hosts.add(host)
            gmaps_todos.append(
                {
                    "host": host,
                    "source_url": source_url,
                    "source_name": source_name,
                    "title": source_name,
                    "city": guess_city(source_url, source_name),
                }
            )

    gmaps_result: dict[str, Any] = {"status": "skipped" if skip_gmaps else "none"}
    host_map: dict[str, int] = {}
    if gmaps_todos and not skip_gmaps:
        gmaps_result = run_gmaps_host_onboard(client, gmaps_todos, apply=apply, refresh=refresh_gmaps)
        host_map = {k: int(v) for k, v in (gmaps_result.get("host_map") or {}).items()}
        if apply and (gmaps_result.get("inserted") or host_map):
            businesses = client.fetch_rows(
                TABLE_MASTER_BUSINESS,
                "business_id,name,website,city,google_place_id",
                limit=5000,
            )
            multilocation = detect_multilocation_hosts(businesses)

    if gmaps_only:
        audit = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "apply": apply,
            "csv": str(csv_path),
            "gmaps_only": True,
            "gmaps_todos": gmaps_todos,
            "gmaps": gmaps_result,
            "rows": [{"row": e["row"], "status": e["status"], "source_url": e["source_url"]} for e in prepared],
        }
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        AUDIT_PATH.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return audit

    fc = None
    if apply:
        try:
            from utils.firecrawl_client import get_firecrawl_client

            fc = get_firecrawl_client()
            # ponytail: FC behind SSH tunnel — skip evidence scrape if unreachable
            _probe = __import__("urllib.request", fromlist=[""]).request
            try:
                _probe.urlopen(fc.api_url.rstrip("/v1") + "/health", timeout=3)
            except Exception:
                fc = None
        except Exception:
            pass

    results: list[dict[str, Any]] = []
    touched_business_ids: set[int] = set()

    # ponytail: pre-seed clinic_services for promo rows that reference unknown services
    if apply:
        for entry in prepared:
            if entry.get("status") in ("skipped", "gmaps_todo"):
                continue
            bid = entry.get("business_id")
            if bid is None:
                continue
            offer = entry.get("_offer") or {}
            if not is_promo_offer(offer):
                continue
            for item in (offer.get("items") or []):
                sn = str(item.get("service_name") or "Others").strip() or "Others"
                existing = client.fetch_rows(
                    "clinic_services", "service_id",
                    filters={"business_id": f"eq.{bid}", "service_name": f"eq.{sn}"},
                    limit=1,
                )
                if not existing:
                    try:
                        client.insert_rows("clinic_services", [{
                            "business_id": bid,
                            "service_name": sn,
                            "regular_price": 0.01,
                            "unit_type": str(item.get("unit_type") or "unit").strip() or "unit",
                            "service_category": str(offer.get("items", [{}])[0].get("service_category") or "Neurotoxin"),
                            "source_url": entry.get("source_url", ""),
                        }])
                    except requests.HTTPError:
                        pass  # race: another row already created it

    for entry in prepared:
        out = {k: v for k, v in entry.items() if not k.startswith("_")}
        if entry["status"] == "skipped":
            results.append(out)
            continue

        offer = entry.get("_offer") or {}
        business_id = entry.get("business_id")
        source_url = entry["source_url"]
        source_name = entry.get("source_name") or ""

        if not apply:
            if entry.get("status") == "gmaps_todo":
                out["status"] = "gmaps_todo"
            else:
                out["status"] = "promo" if is_promo_offer(offer) else "service"
            results.append(out)
            continue

        if business_id is None:
            business_id, resolve_reason = _resolve_business_id(
                source_url=source_url,
                source_name=source_name,
                businesses=businesses,
                multilocation=multilocation,
            )
            if business_id is None:
                host = normalize_host(source_url)
                mapped = host_map.get(host)
                if mapped is not None:
                    business_id = int(mapped)
                    out["business_id"] = business_id
                    out["resolve"] = "gmaps_host_map"
            if business_id is None:
                out["status"] = "onboard_failed"
                out["reason"] = resolve_reason
                results.append(out)
                continue
            if out.get("resolve") != "gmaps_host_map":
                out["business_id"] = business_id
                out["resolve"] = resolve_reason
            elif "business_id" not in out:
                out["business_id"] = business_id

        assert business_id is not None
        touched_business_ids.add(int(business_id))
        membership_plan_id: int | None = None
        if offer.get("is_membership_required"):
            csv_row = entry.get("_csv_row") or {}
            plan_id = _upsert_membership(client, csv_row, int(business_id), source_url)
            if plan_id is not None:
                membership_plan_id = plan_id
            else:
                offer = {**offer, "is_membership_required": False}
        evidence = str(offer.get("offer_raw_text") or "")
        if is_promo_offer(offer) and len(evidence) < 40 and fc is not None:
            markdown, md_source = load_markdown_for_url(client, fc, source_url)
            if markdown.strip():
                evidence = markdown
            out["evidence_source"] = md_source

        promotion_id = upsert_promotion(
            client,
            business_id=int(business_id),
            source_url=source_url,
            promotion_title=str(offer.get("offer_raw_text") or source_name or "Botox offer")[:200],
        )
        existing = client.fetch_rows(
            TABLE_PROMO_OFFER_MASTER,
            "id,offer_fingerprint",
            filters={"business_id": f"eq.{int(business_id)}", "is_active": "eq.true"},
            limit=500,
        )
        seen = {str(r.get("offer_fingerprint") or "") for r in existing if r.get("offer_fingerprint")}
        try:
            routed = route_and_persist_extraction(
                client,
                business_id=int(business_id),
                promotion_id=promotion_id,
                source_url=source_url,
                offers=[offer],
                evidence=evidence,
                membership_plan_id=membership_plan_id,
                seen_fingerprints=seen,
            )
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (409, 400):
                out["status"] = "duplicate_skipped" if exc.response.status_code == 409 else "persist_failed"
                out["reason"] = (exc.response.text or "")[:300]
                results.append(out)
                continue
            raise
        out["promotion_id"] = promotion_id
        out["routed"] = {
            "promos": routed["promos"],
            "services": routed["services"],
            "quarantined": routed["quarantined"],
            "skipped": routed["skipped"],
            "promo_writes": routed.get("promo_writes"),
            "service_writes": routed.get("service_writes"),
            "quarantine": routed.get("quarantine"),
        }
        if routed["promos"]:
            out["status"] = "promo"
        elif routed["services"]:
            out["status"] = "service"
        elif routed["quarantined"]:
            out["status"] = "quarantined"
        else:
            out["status"] = "skipped_persist"
        results.append(out)

    backfill: dict[str, Any] = {}
    if apply and touched_business_ids:
        backfill = backfill_unlinked_item_service_ids(client, business_ids=sorted(touched_business_ids))

    audit = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "apply": apply,
        "csv": str(csv_path),
        "row_count": len(rows),
        "gmaps_todos": gmaps_todos,
        "gmaps": gmaps_result,
        "host_map": host_map,
        "service_id_backfill": backfill,
        "summary": {
            "promo": sum(1 for r in results if r.get("status") == "promo"),
            "service": sum(1 for r in results if r.get("status") == "service"),
            "skipped": sum(1 for r in results if r.get("status") == "skipped"),
            "gmaps_todo": sum(1 for r in results if r.get("status") == "gmaps_todo"),
            "onboard_failed": sum(1 for r in results if r.get("status") == "onboard_failed"),
            "quarantined": sum(1 for r in results if r.get("status") == "quarantined"),
        },
        "results": results,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--skip-gmaps", action="store_true")
    parser.add_argument("--gmaps-only", action="store_true")
    parser.add_argument("--refresh-gmaps", action="store_true", help="Re-scrape gmaps-onboard-results.json")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    audit = run(
        csv_path=args.csv,
        apply=args.apply,
        skip_gmaps=args.skip_gmaps,
        gmaps_only=args.gmaps_only,
        refresh_gmaps=args.refresh_gmaps,
    )
    print(
        json.dumps(
            {"summary": audit.get("summary"), "gmaps": audit.get("gmaps"), "audit": str(AUDIT_PATH)},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
