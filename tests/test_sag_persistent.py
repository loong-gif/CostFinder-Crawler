from __future__ import annotations

import pytest

from utils.sag_persistent import normalize_entity_name, parse_events, vector_literal


def test_parse_events_rejects_incomplete_records_and_normalizes_confidence() -> None:
    events = parse_events(
        {
            "events": [
                {"title": "", "summary": "ignored", "content": "ignored", "category": "", "entities": []},
                {
                    "title": "Membership discount",
                    "summary": "Glow Club gives 10% off.",
                    "content": "Glow Club members receive 10% off Botox.",
                    "category": "membership",
                    "entities": [{"type": "product", "name": "Botox", "role_description": "discounted service", "confidence": 2}],
                },
            ]
        }
    )
    assert len(events) == 1
    assert events[0].entities[0].confidence == 1.0
    assert normalize_entity_name("  Botox   Cosmetic ") == "botox cosmetic"


def test_vector_literal_requires_a_nonempty_numeric_vector() -> None:
    assert vector_literal([1, 0.5]) == "[1.0,0.5]"
    with pytest.raises(ValueError, match="must not be empty"):
        vector_literal([])


def test_pilot_migration_keeps_sag_unexposed_and_versioned() -> None:
    migration = (
        __import__("pathlib").Path(__file__).parents[1]
        / "supabase"
        / "migrations"
        / "20260727013009_sag_persistent_pilot.sql"
    ).read_text(encoding="utf-8")
    assert "create schema if not exists sag" in migration
    assert "create extension if not exists vector with schema extensions" in migration
    assert "revoke all on schema sag from public, anon, authenticated" in migration
    assert "create table if not exists sag.index_version" in migration
    assert "unique (index_version_id, record_type, record_id)" in migration
    assert "create table if not exists sag.index_job" in migration
