"""Build SAG documents from the current structured CostFinder tables."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class StructuredCorpusDocument:
    """A traceable, single-record SAG source document."""

    record_type: str
    record_id: str
    business_id: str
    source_url: str
    updated_at: str
    content: str


@dataclass(frozen=True)
class MaterializedDocument:
    document: StructuredCorpusDocument
    path: Path


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    if value in (None, "", [], {}):
        return "none"
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if not isinstance(value, str) else value


def _document(
    *,
    record_type: str,
    record_id: Any,
    business_id: Any,
    source_url: Any,
    updated_at: Any,
    fields: Iterable[tuple[str, Any]],
) -> StructuredCorpusDocument:
    source = _text(source_url)
    lines = [
        "# CostFinder structured record",
        "",
        f"Record Type: {record_type}",
        f"Record ID: {_text(record_id)}",
        f"Business ID: {_text(business_id)}",
        f"Source URL: {source or 'unavailable'}",
        f"Updated At: {_text(updated_at) or 'unavailable'}",
        "",
        "## Facts",
    ]
    lines.extend(f"{label}: {_json(value)}" for label, value in fields if value not in (None, "", [], {}))
    return StructuredCorpusDocument(
        record_type=record_type,
        record_id=_text(record_id),
        business_id=_text(business_id),
        source_url=source,
        updated_at=_text(updated_at),
        content="\n".join(lines) + "\n",
    )


def build_structured_documents(
    *,
    services: Sequence[Mapping[str, Any]],
    memberships: Sequence[Mapping[str, Any]],
    promotions: Sequence[Mapping[str, Any]],
) -> list[StructuredCorpusDocument]:
    """Convert every current structured record into an independently traceable document."""
    documents: list[StructuredCorpusDocument] = []
    for row in services:
        documents.append(
            _document(
                record_type="clinic_service",
                record_id=row.get("service_id"),
                business_id=row.get("business_id"),
                source_url=row.get("source_url"),
                updated_at=row.get("updated_at"),
                fields=(
                    ("Service Name", row.get("service_name")),
                    ("Service Name Raw", row.get("service_name_raw")),
                    ("Regular Price", row.get("regular_price")),
                    ("Unit Type", row.get("unit_type")),
                    ("Service Category", row.get("service_category")),
                    ("Service Area", row.get("service_area")),
                ),
            )
        )
    for row in memberships:
        documents.append(
            _document(
                record_type="clinic_membership",
                record_id=row.get("plan_id"),
                business_id=row.get("business_id"),
                source_url=row.get("source_url"),
                updated_at=row.get("updated_at"),
                fields=(
                    ("Membership Name", row.get("membership_name")),
                    ("Membership Price", row.get("membership_price")),
                    ("Billing Period", row.get("billing_period")),
                    ("Minimum Commitment Months", row.get("minimum_commitment_months")),
                    ("Benefits", row.get("benefits")),
                ),
            )
        )
    for row in promotions:
        documents.append(
            _document(
                record_type="clinic_promotion",
                record_id=row.get("promotion_id"),
                business_id=row.get("business_id"),
                source_url=row.get("source_url"),
                updated_at=row.get("updated_at"),
                fields=(
                    ("Promotion Title", row.get("promotion_title")),
                    ("Campaign Start Date", row.get("campaign_start_date")),
                    ("Campaign End Date", row.get("campaign_end_date")),
                    ("Active", row.get("is_active")),
                    ("Promotion Content", row.get("promotion_content")),
                ),
            )
        )
    return documents


def materialize_documents(
    documents: Sequence[StructuredCorpusDocument], directory: Path
) -> list[MaterializedDocument]:
    """Write deterministic Markdown files acceptable to SAG's path-only ingest API."""
    directory.mkdir(parents=True, exist_ok=True)
    materialized: list[MaterializedDocument] = []
    for document in documents:
        digest = hashlib.sha256(document.content.encode("utf-8")).hexdigest()[:16]
        path = directory / f"{document.record_type}_{document.record_id}_{digest}.md"
        path.write_text(document.content, encoding="utf-8")
        materialized.append(MaterializedDocument(document=document, path=path))
    return materialized
