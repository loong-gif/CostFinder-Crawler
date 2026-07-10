#!/usr/bin/env python3
"""Correct promo_membership_plans.plan_name from actual rendered page content.

Uses Firecrawl to scrape the membership page (fresh, JS-rendered), falls back to
existing staging page_content when Firecrawl is unreachable. Sends content to LLM
with a prompt focused on extracting the EXACT plan/card names as they appear on the
page — not subsection headers or generic labels.

Usage:
    # Dry-run: show what would change
    python scripts/correct_membership_plan_names.py --dry-run --limit 3

    # Fix all domains
    python scripts/correct_membership_plan_names.py

    # Fix a specific source URL
    python scripts/correct_membership_plan_names.py --source-url wheelermedspa.com

    # Force re-scrape with Firecrawl even if staging content exists
    python scripts/correct_membership_plan_names.py --force-firecrawl --limit 1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.offer_extraction_llm import build_client_from_env, OpenAICompatibleClient
from utils.supabase_rest import SupabaseRestClient

OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
REPORT_PREFIX = "correct_membership_plan_names"


# ---------------------------------------------------------------------------
# LLM prompt — focused on exact plan names only
# ---------------------------------------------------------------------------

PLAN_NAME_SYSTEM_PROMPT = (
    "You extract membership plan names from aesthetic clinic membership pages.\n"
    "Your ONLY job is to return the exact plan/card names as they appear on the page.\n"
    "\n"
    "Rules:\n"
    "- A plan name is the title/heading of each membership tier or card.\n"
    '- Ignore subsection headers like "MONTHLY OPTIONS:", "QUARTERLY OPTIONS:", "ANNUAL OPTIONS:" — '
    "these are categories of treatments within a plan, not plan names.\n"
    "- Return ONLY the name part, strip the price (e.g. 'Basic Beauty' not 'Basic Beauty – $99/month').\n"
    "- Return the plan names in the order they appear top-to-bottom on the page.\n"
    "- There may be between 1 and 10 plans. Return at least 1 if any plan exists.\n"
    "- If no clear plan names are found, return an empty array.\n"
    "\n"
    "Output strict JSON with a single key 'plan_names' (array of strings).\n"
    'Example: {"plan_names": ["Basic Beauty", "Serious About Self Care", "I\'m Extra and I Know It", "Best Skin of My Life"]}'
)


def build_plan_name_messages(domain: str, url: str, page_content: str) -> List[Dict[str, str]]:
    content = page_content.strip()
    if len(content) > 8000:
        content = content[:8000] + "\n\n[...truncated...]"
    return [
        {"role": "system", "content": PLAN_NAME_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Domain: {domain}\n"
                f"Page: {url}\n\n"
                f"Page content:\n{content}"
            ),
        },
    ]


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def load_supabase_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


def fetch_all_membership_plans(
    client: SupabaseRestClient,
    source_url_filter: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Get all promo_membership_plans rows grouped for correction."""
    filters: Dict[str, str] = {}
    if source_url_filter:
        url_like = source_url_filter.strip().lower()
        filters["source_url"] = f"like.%{url_like}%"

    rows = client.fetch_rows(
        "promo_membership_plans",
        "plan_id,domain_name,plan_name,source_url,business_id",
        filters=filters or None,
        order="plan_id.asc",
    )
    if limit:
        rows = rows[:limit]
    return rows


def fetch_staging_content(
    client: SupabaseRestClient,
    source_url: str,
    domain_name: str,
) -> Optional[str]:
    """Fetch page_content from promo_website_staging for a given source URL."""
    try:
        rows = client.fetch_rows(
            "promo_website_staging",
            "page_content,crawl_timestamp",
            filters={"subpage_url": f"eq.{source_url}"},
            limit=1,
        )
    except Exception:
        # URL encoding issues; try LIKE
        try:
            rows = client.fetch_rows(
                "promo_website_staging",
                "page_content,crawl_timestamp",
                filters={"subpage_url": f"like.{source_url}%"},
                limit=1,
            )
        except Exception:
            return None
    if not rows:
        return None
    return str(rows[0].get("page_content") or "").strip() or None


# ---------------------------------------------------------------------------
# Firecrawl scrape (optional)
# ---------------------------------------------------------------------------

def scrape_with_firecrawl(url: str) -> Optional[str]:
    """Try Firecrawl scrape for fresh rendered content. Returns markdown or None."""
    try:
        from utils.firecrawl_client import get_firecrawl_client

        fc = get_firecrawl_client()
        result = fc.scrape_url(url)
        doc = getattr(result, "data", result)
        if isinstance(doc, dict):
            markdown = str(doc.get("markdown") or "").strip()
            if markdown:
                return markdown
        # Also try raw result
        if isinstance(result, dict):
            data = result.get("data", result)
            if isinstance(data, dict):
                md = str(data.get("markdown") or data.get("content") or "").strip()
                if md:
                    return md
        return None
    except Exception as exc:
        print(f"  [firecrawl] scrape failed: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Regex-based plan name extraction (fallback when LLM unavailable)
# ---------------------------------------------------------------------------

# Patterns that look like subsection headers (NOT plan names)
_SECTION_HEADER_PATTERN = re.compile(
    r"^(?:#{1,4}\s*)?(?:" 
    r"monthly\s+options|quarterly\s+options|annual\s+options"
    r"|choose\s+(?:your\s+)?(?:membership|plan|package)"
    r"|what'?s?\s+included|benefits|features|pricing"
    r"|sign\s+up|get\s+started|join\s+(?:now|today)"
    r"|terms\s+(?:and\s+)?conditions|fine\s+print|details"
    r"|plus:|each\s+year|monthly|quarterly|annual"
    r")\s*:*\s*$",
    re.IGNORECASE,
)

# Lines that look like genuine plan names — title with pricing
_PLAN_NAME_PATTERNS = [
    # "### **BEAUTY**\n#### $99/MONTH" — bold heading only (no $ in line)
    re.compile(r'^#{1,4}\s*\*{0,2}([A-Za-z\u00c0-\u024f][A-Za-z\u00c0-\u024f0-9\s\'&,.\/\-()]+?)\*{0,2}\s*$'),
    # "Basic Beauty – $99/month" or "Plan Name - $199/mo" (must have /mo or /month or /year qualifier)
    re.compile(r'^([A-Za-z\u00c0-\u024f][A-Za-z\u00c0-\u024f0-9\s\'&,.\/\-()]{2,60}?)\s*[–—-]\s*\$[\d,]+\.?\d*\s*(?:\/\s*(?:mo|month|year|annual|yr)\b)', re.IGNORECASE),
    # "TRU SIGNATURE $199 MONTH" (name then dollar + period qualifier)
    re.compile(r'^([A-Za-z\u00c0-\u024f][A-Za-z\u00c0-\u024f0-9\s\'&,.\/\-()]{2,60}?)\s+\$[\d,]+\.?\d*\s+(?:per\s+)?(?:mo|month|year|annual|yr)\b', re.IGNORECASE),
]


def _is_plan_name(line: str) -> Optional[str]:
    """Try to extract a plan name from a line. Returns name or None."""
    stripped = line.strip()
    if not stripped or len(stripped) < 3:
        return None

    # Skip section headers
    if _SECTION_HEADER_PATTERN.match(stripped):
        return None

    # Skip button/action text
    if stripped.lower() in (
        "sign up", "join now", "learn more", "get started",
        "become a member", "select", "choose", "buy now",
        "subscribe", "enroll", "register",
    ):
        return None

    # Skip lines with "$XXX value" pattern (treatment options)
    if re.search(r'\$[\d,]+\.?\d*\s*\b(value|credit|treatment|bonus|free)\b', stripped, re.IGNORECASE):
        return None

    # Skip service-specific lines (contain common treatment keywords with no period qualifier)
    if re.search(r'\b(botox|filler|laser|facial|peel|microneedling|derma|tox|unit|syringe|injectable|hydrafacial|morpheus|tixel|sofwav)\b', stripped, re.IGNORECASE):
        # Only skip if there's no /month or /year qualifier
        if not re.search(r'\$[\d,]+\.?\d*\s*/\s*(?:mo|month|year|annual|yr)\b', stripped, re.IGNORECASE):
            return None

    # Normalize smart apostrophes
    stripped = stripped.replace('\u2019', "'").replace('\u2018', "'")

    for pattern in _PLAN_NAME_PATTERNS:
        m = pattern.match(stripped)
        if m:
            name = m.group(1).strip()
            name = name.replace('**', '').replace('*', '').strip()
            if len(name) >= 3 and not _SECTION_HEADER_PATTERN.match(name):
                return name
    return None


def extract_plan_names_regex(page_content: str) -> List[str]:
    """Extract plan names from page content using regex heuristics.
    
    Returns plan names in order of appearance.
    """
    lines = page_content.split('\n')
    plan_names: List[str] = []
    seen: Set[str] = set()

    for line in lines:
        name = _is_plan_name(line)
        if name:
            norm = name.lower().strip()
            if norm not in seen:
                seen.add(norm)
                plan_names.append(name)

    # Filter out clearly generic names
    generic = {"membership", "memberships", "pricing", "packages", "options"}
    filtered = [n for n in plan_names if n.lower().strip() not in generic]
    return filtered[:20]


def _without_proxy():
    """Context manager that temporarily clears proxy env vars.
    
    Google Gemini API TLS fails through 127.0.0.1:7890 proxy.
    The Supabase client already uses trust_env=False, so this only
    affects the LLM client's direct requests.
    """
    saved = {}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        saved[key] = os.environ.pop(key, None)
    return saved

def _restore_proxy(saved: Dict[str, Optional[str]]):
    for key, val in saved.items():
        if val is not None:
            os.environ[key] = val


def extract_plan_names(
    page_content: str,
    domain: str,
    url: str,
    llm_client: OpenAICompatibleClient,
) -> List[str]:
    """Ask LLM to extract exact plan names from page content."""
    messages = build_plan_name_messages(domain, url, page_content)
    saved_proxy = _without_proxy()
    try:
        payload = llm_client.create_json_response(messages)
    except Exception as exc:
        print(f"  [llm] extraction error: {exc}", file=sys.stderr)
        return []
    finally:
        _restore_proxy(saved_proxy)

    names = payload.get("plan_names", []) if isinstance(payload, dict) else []
    if not isinstance(names, list):
        names = []
    return [str(n).strip() for n in names if str(n).strip()]


def group_plans_by_url(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Group membership plan rows by source_url."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        url = str(row.get("source_url") or "").strip()
        if not url:
            continue
        groups.setdefault(url, []).append(row)
    return groups


def correct_url_plans(
    client: SupabaseRestClient,
    llm_client: OpenAICompatibleClient,
    source_url: str,
    domain: str,
    existing_plans: List[Dict[str, Any]],
    *,
    dry_run: bool = False,
    force_firecrawl: bool = False,
) -> Dict[str, Any]:
    """Correct plan_name for all plans at a given source_url."""
    result: Dict[str, Any] = {
        "source_url": source_url,
        "domain": domain,
        "existing_count": len(existing_plans),
        "plans_corrected": 0,
        "llm_names": [],
        "errors": [],
    }

    # 1. Get fresh page content
    page_content: Optional[str] = None
    firecrawl_used = False

    if force_firecrawl:
        page_content = scrape_with_firecrawl(source_url)
        if page_content:
            firecrawl_used = True
            print(f"  [fc] scraped {len(page_content)} chars via Firecrawl")

    if not page_content:
        page_content = fetch_staging_content(client, source_url, domain)
        if page_content:
            print(f"  [staging] using {len(page_content)} chars from staging")

    if not page_content:
        # Try Firecrawl as last resort
        page_content = scrape_with_firecrawl(source_url)
        if page_content:
            firecrawl_used = True
            print(f"  [fc fallback] scraped {len(page_content)} chars via Firecrawl")

    if not page_content:
        result["errors"].append("no_content")
        return result

    if len(page_content) < 40:
        result["errors"].append("content_too_short")
        return result

    # 2. Extract plan names — try regex first, fall back to LLM
    extracted_names = extract_plan_names_regex(page_content)
    source_label = "regex"

    if not extracted_names and llm_client is not None:
        extracted_names = extract_plan_names(page_content, domain, source_url, llm_client)
        source_label = "llm"

    if extracted_names:
        print(f"  [{source_label}] extracted {len(extracted_names)} names: {extracted_names}")
    result["extracted_names"] = extracted_names
    result["extraction_source"] = source_label

    if not extracted_names:
        result["errors"].append("no_names_extracted")
        return result

    # 3. Match extracted names to existing plans
    # If counts differ, try to match by position/pattern
    existing_sorted = sorted(existing_plans, key=lambda p: p.get("plan_id", 0))

    # For each extracted name, find the best existing plan match
    matched_pairs: List[Tuple[int, str, str]] = []  # (plan_id, old_name, new_name)
    unmatched_names: List[str] = []

    if len(extracted_names) == len(existing_sorted):
        # One-to-one mapping by position
        for i, plan in enumerate(existing_sorted):
            old_name = str(plan.get("plan_name") or "").strip()
            new_name = extracted_names[i]
            plan_id = int(plan["plan_id"])
            if old_name != new_name:
                matched_pairs.append((plan_id, old_name, new_name))
    else:
        # Different counts — try to match by similarity or position
        used_indices: Set[int] = set()
        for idx, plan in enumerate(existing_sorted):
            old_name = str(plan.get("plan_name") or "").strip()
            plan_id = int(plan["plan_id"])
            old_norm = re.sub(r"[^a-z0-9]+", "", old_name.lower())

            best_score = -1
            best_match = None
            for ni, new_name in enumerate(extracted_names):
                if ni in used_indices:
                    continue
                new_norm = re.sub(r"[^a-z0-9]+", "", new_name.lower())
                # Simple token overlap score
                old_tokens = set(old_norm.split())
                new_tokens = set(new_norm.split())
                if not old_tokens or not new_tokens:
                    continue
                overlap = len(old_tokens & new_tokens)
                score = overlap / max(len(old_tokens), len(new_tokens))
                # Position proximity bonus
                if abs(idx - ni) <= 1:
                    score += 0.3
                if score > best_score:
                    best_score = score
                    best_match = (ni, new_name)

            if best_match is not None and best_score > 0:
                ni, new_name = best_match
                if old_name != new_name:
                    matched_pairs.append((plan_id, old_name, new_name))
                used_indices.add(ni)

        # Remaining unmatched names
        for ni, new_name in enumerate(extracted_names):
            if ni not in used_indices:
                unmatched_names.append(new_name)

    # 4. Update database
    for plan_id, old_name, new_name in matched_pairs:
        print(f"  {plan_id}: '{old_name}' -> '{new_name}'")
        if not dry_run:
            try:
                client.update_row(
                    "promo_membership_plans",
                    {"plan_id": f"eq.{plan_id}"},
                    {"plan_name": new_name},
                )
            except Exception as exc:
                result["errors"].append(f"update_failed:{plan_id}:{exc}")
                continue
        result["plans_corrected"] += 1

    if unmatched_names:
        print(f"  [warn] {len(unmatched_names)} extracted names unmatched: {unmatched_names}")

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Correct membership plan names from actual page content")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    parser.add_argument("--limit", type=int, default=None, help="Only process first N rows")
    parser.add_argument("--source-url", default=None, help="Filter by source_url (LIKE match)")
    parser.add_argument("--force-firecrawl", action="store_true", help="Force Firecrawl scrape, skip staging fallback")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    sb_client = load_supabase_client()
    llm_client = build_client_from_env()
    if llm_client is None:
        print("Missing LLM_API_URL / LLM_MODEL / LLM_API_KEY in .env", file=sys.stderr)
        return 1

    # Fetch all membership plan rows
    rows = fetch_all_membership_plans(sb_client, source_url_filter=args.source_url, limit=args.limit)
    if not rows:
        print("No membership plans found to correct.")
        return 0

    print(f"Found {len(rows)} membership plan rows")

    # Group by source_url
    url_groups = group_plans_by_url(rows)
    print(f"Grouped into {len(url_groups)} distinct source URLs")

    results: List[Dict[str, Any]] = []
    total_corrected = 0
    total_errors = 0

    for source_url in sorted(url_groups.keys()):
        plans = url_groups[source_url]
        domain = str(plans[0].get("domain_name") or "")
        print(f"\n{'='*60}")
        print(f"[{domain}] {source_url} ({len(plans)} plans)")
        print(f"{'='*60}")

        result = correct_url_plans(
            sb_client,
            llm_client,
            source_url,
            domain,
            plans,
            dry_run=args.dry_run,
            force_firecrawl=args.force_firecrawl,
        )
        results.append(result)
        total_corrected += result["plans_corrected"]
        total_errors += len(result["errors"])
        print(f"  corrected: {result['plans_corrected']}, errors: {result.get('errors') or 'none'}")

    # Summary
    print(f"\n{'='*60}")
    print(f"SUMMARY — {'DRY RUN' if args.dry_run else 'LIVE RUN'}")
    print(f"  Sources processed: {len(results)}")
    print(f"  Plans corrected:   {total_corrected}")
    print(f"  Errors:            {total_errors}")

    # Save report
    output_dir = OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "total_corrected": total_corrected,
        "total_errors": total_errors,
        "results": results,
    }
    report_path = output_dir / f"{REPORT_PREFIX}_{stamp}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {report_path}")

    return 0 if total_errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
