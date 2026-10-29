#!/usr/bin/env python3
"""Extract page-level clinic promotions from a dated scrape-raw batch.

The command is intentionally dry-run by default.  ``--apply`` is required before
the script will insert or update ``clinic_promotions`` rows.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.promo_paths import (  # noqa: F401  (re-exported for existing importers)
    PROMOTION_PATH_RE,
    is_promotion_url_loose as is_promotion_page_url,
    sanitized_output_path as _output_path,
)
from utils.promo_extraction import (  # noqa: F401  (re-exported for existing importers)
    EMBEDDED_IMAGE_RE,
    MAX_LLM_EVIDENCE_CHARS,
    SYSTEM_PROMPT,
    _consensus_date,
    _date_is_supported_by_evidence,
    _normalized_evidence_text,
    _text_is_verbatim_evidence,
    _unique_text,
    _validate_extraction_payload,
    _validate_promotion_schema_item,
    _validated_schema_date,
    campaign_date_range_from_url,
    llm_extract_promotions,
    merge_promotion_items,
    split_evidence_for_llm,
    strip_embedded_images_for_llm,
)
from utils.firecrawl_scrape_raw_db import canonical_scrape_url
from utils.offer_extraction_llm import OpenAICompatibleClient, parse_json_payload
from utils.recent_raw_extraction import (
    promotion_evidence_markdown,
    resolve_business,
    validate_promotion,
)
from utils.rendered_visibility_gate import filter_visible_promotion_content
from utils.supabase_rest import SupabaseRestClient, get_supabase_writer_key


DEFAULT_BATCH_DATE = "2026-07-30"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_LLM_BASE_URL = "http://192.168.1.189:1234/v1"
DEFAULT_LLM_MODEL = "schematron-8b"
PAGE_SIZE = 1000
SCRAPE_SELECT = (
    "id,source_url,success,created_at,updated_at,markdown,markdown_ocr,metadata"
)
PROMOTION_SELECT = (
    "promotion_id,business_id,source_url,promotion_title,promotion_content,"
    "campaign_start_date,campaign_end_date,is_active"
)


def batch_utc_bounds(batch_date: str, timezone_name: str) -> tuple[datetime, datetime]:
    """Return the requested local calendar day as a half-open UTC interval."""
    requested_day = date.fromisoformat(str(batch_date).strip())
    requested_zone = ZoneInfo(str(timezone_name).strip())
    local_start = datetime.combine(requested_day, datetime_time.min, tzinfo=requested_zone)
    local_end = datetime.combine(
        requested_day + timedelta(days=1),
        datetime_time.min,
        tzinfo=requested_zone,
    )
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def resolve_local_llm_api_key(environ: Mapping[str, str]) -> str:
    """Use only a local-model credential; never forward a cloud LLM secret."""
    return str(environ.get("LOCAL_LLM_API_KEY") or "local-model").strip()


def _parse_timestamp(value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fetch_scrape_window(
    client: Any,
    *,
    timestamp_column: str,
    start: datetime,
    end: datetime,
    raw_ids: Optional[Sequence[int]],
) -> list[dict[str, Any]]:
    filters = {
        "success": "eq.true",
        timestamp_column: f"gte.{start.isoformat()}",
        # PostgREST permits applying multiple operators to one column as a comma
        # expression.  Passing ``and`` would make the test double diverge from the
        # production client, so the upper bound is expressed as a top-level and.
        "and": f"({timestamp_column}.lt.{end.isoformat()})",
    }
    if raw_ids:
        filters["id"] = f"in.({','.join(str(int(raw_id)) for raw_id in raw_ids)})"

    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.fetch_rows(
            "firecrawl_scrape_raw",
            SCRAPE_SELECT,
            filters=dict(filters),
            limit=PAGE_SIZE,
            offset=offset,
            order="id.asc",
        )
        rows.extend(row for row in page if isinstance(row, dict))
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def load_batch_scrapes(
    client: Any,
    *,
    batch_date: str,
    timezone_name: str,
    raw_ids: Optional[Sequence[int]] = None,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Union created/updated batch windows and keep the newest row per page URL."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    start, end = batch_utc_bounds(batch_date, timezone_name)
    rows = _fetch_scrape_window(
        client,
        timestamp_column="created_at",
        start=start,
        end=end,
        raw_ids=raw_ids,
    )
    rows.extend(
        _fetch_scrape_window(
            client,
            timestamp_column="updated_at",
            start=start,
            end=end,
            raw_ids=raw_ids,
        )
    )

    newest_by_url: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("success") is not True:
            continue
        source_url = str(row.get("source_url") or "").strip()
        canonical_url = canonical_scrape_url(source_url)
        if not canonical_url or not is_promotion_page_url(canonical_url):
            continue
        if not promotion_evidence_markdown(row).strip():
            continue
        current = newest_by_url.get(canonical_url)
        freshness = (_parse_timestamp(row.get("updated_at")), int(row.get("id") or 0))
        current_freshness = (
            _parse_timestamp((current or {}).get("updated_at")),
            int((current or {}).get("id") or 0),
        )
        if current is None or freshness > current_freshness:
            newest_by_url[canonical_url] = {**row, "source_url": canonical_url}

    selected = sorted(
        newest_by_url.values(),
        key=lambda row: (_parse_timestamp(row.get("updated_at")), int(row.get("id") or 0)),
    )
    return selected if limit is None else selected[:limit]


def _complete_promotion(row: dict[str, Any]) -> bool:
    return bool(
        str(row.get("promotion_title") or "").strip()
        and [value for value in row.get("promotion_content") or [] if str(value).strip()]
    )


def persist_page_promotion(
    client: Any,
    *,
    business_id: int,
    source_url: str,
    item: dict[str, Any],
    apply: bool = False,
    rendered_visibility: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Reuse, fill, or insert the one page-anchored promotion record."""
    candidate = copy.deepcopy(item)
    if apply:
        visibility = filter_visible_promotion_content(
            candidate.get("promotion_content") or [], rendered_visibility
        )
        if not visibility.accepted:
            return {"accepted": False, "action": "rejected", "reason": visibility.reason}
        candidate["promotion_content"] = list(visibility.content)
    validation = validate_promotion(candidate, "")
    if not validation.accepted:
        return {"accepted": False, "action": "rejected", "reason": validation.reason}

    canonical_url = canonical_scrape_url(source_url)
    if not canonical_url:
        return {"accepted": False, "action": "rejected", "reason": "missing_source_url"}
    existing_rows = client.fetch_rows(
        "clinic_promotions",
        PROMOTION_SELECT,
        filters={"business_id": f"eq.{int(business_id)}"},
        limit=1000,
    )
    anchors = [
        row
        for row in existing_rows
        if canonical_scrape_url(str(row.get("source_url") or "")) == canonical_url
    ]
    complete = next((row for row in anchors if _complete_promotion(row)), None)
    if complete is not None:
        candidate_start = candidate.get("campaign_start_date")
        candidate_end = candidate.get("campaign_end_date")
        missing_existing_dates = (
            complete.get("campaign_start_date") is None
            and complete.get("campaign_end_date") is None
        )
        if missing_existing_dates and candidate_start and candidate_end:
            update_payload: dict[str, Any] = {"updated_at": datetime.now(timezone.utc).isoformat()}
            update_payload.update({
                "campaign_start_date": candidate_start,
                "campaign_end_date": candidate_end,
            })
            if not apply:
                return {
                    "accepted": True,
                    "action": "would_update_dates",
                    "promotion_id": complete.get("promotion_id"),
                }
            promotion_id = complete.get("promotion_id")
            updated = client.update_row(
                "clinic_promotions",
                {"promotion_id": f"eq.{promotion_id}"},
                update_payload,
            )
            if len(updated) != 1:
                raise RuntimeError(
                    f"clinic_promotions date update returned {len(updated)} rows; expected exactly one"
                )
            return {"accepted": True, "action": "updated_dates", "promotion_id": promotion_id}
        action = "reused" if apply else "would_reuse"
        return {
            "accepted": True,
            "action": action,
            "promotion_id": complete.get("promotion_id"),
        }

    payload = {
        "business_id": int(business_id),
        "source_url": canonical_url,
        "promotion_title": str(candidate.get("promotion_title") or "").strip(),
        "promotion_content": list(candidate.get("promotion_content") or []),
        "campaign_start_date": candidate.get("campaign_start_date"),
        "campaign_end_date": candidate.get("campaign_end_date"),
        # A valid page extraction is not lifecycle evidence.  Explicit
        # historical/removed-page decisions belong to the evidence review
        # workflow, not to a date comparison here.
        "is_active": True,
    }
    skeleton = anchors[0] if anchors else None
    if not apply:
        return {
            "accepted": True,
            "action": "would_update" if skeleton is not None else "would_insert",
            "promotion_id": skeleton.get("promotion_id") if skeleton is not None else None,
        }

    if skeleton is not None:
        promotion_id = skeleton.get("promotion_id")
        update_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"business_id", "source_url"}
        }
        update_payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        updated = client.update_row(
            "clinic_promotions",
            {"promotion_id": f"eq.{promotion_id}"},
            update_payload,
        )
        if len(updated) != 1:
            raise RuntimeError(
                f"clinic_promotions update returned {len(updated)} rows; expected exactly one"
            )
        resolved_id = updated[0].get("promotion_id", promotion_id)
        return {"accepted": True, "action": "updated", "promotion_id": resolved_id}

    inserted = client.insert_rows("clinic_promotions", [payload])
    if len(inserted) != 1 or inserted[0].get("promotion_id") is None:
        raise RuntimeError(
            f"clinic_promotions insert returned {len(inserted)} rows; expected one identified row"
        )
    promotion_id = inserted[0]["promotion_id"]
    return {"accepted": True, "action": "inserted", "promotion_id": promotion_id}


def normalize_llm_api_url(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("llm_base_url is required")
    return base if base.endswith("/chat/completions") else f"{base}/chat/completions"


@dataclass
class ProxySafeOpenAICompatibleClient(OpenAICompatibleClient):
    """OpenAI-compatible client that never inherits shell proxy settings."""

    session: requests.Session = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.session = requests.Session()
        self.session.trust_env = False

    def create_json_response(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0}
        if json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "promotion_extraction",
                    "strict": True,
                    "schema": {
                        key: value
                        for key, value in json_schema.items()
                        if key not in {"$schema", "title", "description"}
                    },
                },
            }
        else:
            body["response_format"] = {"type": "json_object"}
        response = self.session.post(
            self.api_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.timeout,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = str(getattr(response, "text", "") or "").strip()
            status_code = getattr(response, "status_code", "unknown")
            raise RuntimeError(
                f"LLM request failed: status={status_code}; response={detail[:1000]}"
            ) from exc
        choices = response.json().get("choices") or []
        if not choices:
            raise RuntimeError("LLM response missing choices")
        content = (choices[0].get("message") or {}).get("content", "")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "") for part in content if isinstance(part, dict)
            )
        payload = parse_json_payload(content, {})
        if not isinstance(payload, dict):
            raise ValueError("LLM returned a non-object payload")
        return payload


def _load_all_businesses(client: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.fetch_rows(
            "master_business_info",
            "business_id,name,website,address,city",
            limit=PAGE_SIZE,
            offset=offset,
            order="business_id.asc",
        )
        rows.extend(row for row in page if isinstance(row, dict))
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


def _metadata_text(row: dict[str, Any], field_name: str) -> str:
    metadata = row.get("metadata")
    return str(metadata.get(field_name) or "") if isinstance(metadata, dict) else ""


def run(
    *,
    client: Any,
    llm: Any,
    batch_date: str = DEFAULT_BATCH_DATE,
    timezone_name: str = DEFAULT_TIMEZONE,
    raw_ids: Optional[Sequence[int]] = None,
    limit: Optional[int] = None,
    apply: bool = False,
    schema: Optional[dict[str, Any]] = None,
    rendered_visibility: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the isolated page-by-page extraction and return a reconciliation audit."""
    extraction_schema = schema or json.loads(
        (PROJECT_ROOT / "schema" / "promotion_extraction_schema.json").read_text(
            encoding="utf-8"
        )
    )
    scrapes = load_batch_scrapes(
        client,
        batch_date=batch_date,
        timezone_name=timezone_name,
        raw_ids=raw_ids,
        limit=limit,
    )
    businesses = _load_all_businesses(client)
    # Host ambiguity is decided from master_business_info matches. Promo slugs such
    # as summer/fall/winter-specials must not be mistaken for location paths.
    multilocation_hosts: set[str] = set()

    results: list[dict[str, Any]] = []
    actions: dict[str, int] = {}
    accepted_count = 0
    gate_rejected_count = 0
    error_count = 0
    extracted_item_count = 0

    for row in scrapes:
        raw_id = row.get("id")
        source_url = str(row.get("source_url") or "")
        evidence = promotion_evidence_markdown(row)
        source = {
            "url": source_url,
            "title": _metadata_text(row, "title"),
            "description": _metadata_text(row, "description"),
            "text": evidence,
        }
        gate = resolve_business(source, businesses, multilocation_hosts)
        if not gate.accepted or gate.business_id is None:
            gate_rejected_count += 1
            results.append(
                {
                    "raw_id": raw_id,
                    "source_url": source_url,
                    "status": "business_gate_rejected",
                    "reason": gate.reason,
                }
            )
            continue
        try:
            payload = llm_extract_promotions(
                llm,
                source_url=source_url,
                evidence=evidence,
                schema=extraction_schema,
            )
            items = payload.get("promotions") or []
            extracted_item_count += len(items)
            if not items:
                results.append(
                    {
                        "raw_id": raw_id,
                        "business_id": gate.business_id,
                        "source_url": source_url,
                        "status": "no_promotions",
                    }
                )
                continue
            merged = merge_promotion_items(
                items,
                evidence,
                webpage_title=_metadata_text(row, "title"),
                source_url=source_url,
            )
            persistence = persist_page_promotion(
                client,
                business_id=gate.business_id,
                source_url=source_url,
                item=merged,
                apply=apply,
                rendered_visibility=(rendered_visibility or {}).get(source_url),
            )
            action = str(persistence.get("action") or "unknown")
            actions[action] = actions.get(action, 0) + 1
            if persistence.get("accepted"):
                accepted_count += 1
            results.append(
                {
                    "raw_id": raw_id,
                    "business_id": gate.business_id,
                    "source_url": source_url,
                    "status": "accepted" if persistence.get("accepted") else "rejected",
                    "promotions_extracted": len(items),
                    "promotion_title": merged.get("promotion_title"),
                    "promotion_content": merged.get("promotion_content") or [],
                    **persistence,
                }
            )
        except ValueError as exc:
            if str(exc) == "no validated promotions":
                results.append(
                    {
                        "raw_id": raw_id,
                        "business_id": gate.business_id,
                        "source_url": source_url,
                        "status": "rejected",
                        "reason": "no_validated_promotions",
                    }
                )
                continue
            error_count += 1
            results.append(
                {
                    "raw_id": raw_id,
                    "business_id": gate.business_id,
                    "source_url": source_url,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:1000],
                }
            )
        except Exception as exc:
            error_count += 1
            results.append(
                {
                    "raw_id": raw_id,
                    "business_id": gate.business_id,
                    "source_url": source_url,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:1000],
                }
            )

    return {
        "apply": apply,
        "batch_date": batch_date,
        "timezone": timezone_name,
        "raw_ids": list(raw_ids or []),
        "limit": limit,
        "reconciliation": {
            "scrapes_loaded": len(scrapes),
            "results": len(results),
            "business_gate_rejected": gate_rejected_count,
            "promotion_items_extracted": extracted_item_count,
            "page_promotions_accepted": accepted_count,
            "errors": error_count,
            "actions": actions,
            "balanced": len(results) == len(scrapes),
        },
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write accepted rows")
    parser.add_argument("--batch_date", default=DEFAULT_BATCH_DATE)
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--raw_id", action="append", type=int, default=[])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--visibility-manifest",
        type=Path,
        default=None,
        help="Rendered visibility report produced by the ego browser gate; required with --apply",
    )
    parser.add_argument("--llm_base_url", default=DEFAULT_LLM_BASE_URL)
    parser.add_argument("--llm_model", default=DEFAULT_LLM_MODEL)
    parser.add_argument(
        "--output",
        default=str(
            PROJECT_ROOT
            / ".firecrawl"
            / "master-business-search"
            / "clinic_promotions_scrape_raw_extract_audit.json"
        ),
    )
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    if not supabase_url:
        raise RuntimeError("Missing SUPABASE_URL")
    client = SupabaseRestClient(supabase_url, get_supabase_writer_key())
    if args.apply and args.visibility_manifest is None:
        raise RuntimeError("--visibility-manifest is required with --apply")
    rendered_visibility: Mapping[str, Mapping[str, Any]] = {}
    if args.visibility_manifest is not None:
        payload = json.loads(args.visibility_manifest.read_text(encoding="utf-8"))
        rendered_visibility = payload.get("results", payload)
    llm = ProxySafeOpenAICompatibleClient(
        api_url=normalize_llm_api_url(args.llm_base_url),
        api_key=resolve_local_llm_api_key(os.environ),
        model=args.llm_model,
    )
    audit = run(
        client=client,
        llm=llm,
        batch_date=args.batch_date,
        timezone_name=args.timezone,
        raw_ids=tuple(args.raw_id) or None,
        limit=args.limit,
        apply=args.apply,
        rendered_visibility=rendered_visibility,
    )
    output = _output_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
