"""Live schema snapshot contract tests."""
from __future__ import annotations

import json
from pathlib import Path

from utils.schema_contract import (
    CLINIC_MEMBERSHIP_COLUMNS,
    CLINIC_PROMOTION_COLUMNS,
    CLINIC_SERVICE_COLUMNS,
    OFFER_ITEM_COLUMNS,
    OFFER_MASTER_COLUMNS,
)

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = json.loads((ROOT / "schema" / "live_schema_snapshot.json").read_text(encoding="utf-8"))


def test_offer_master_columns_match_snapshot() -> None:
    assert list(OFFER_MASTER_COLUMNS) == SNAPSHOT["promo_offer_master"]


def test_offer_item_columns_match_snapshot() -> None:
    assert list(OFFER_ITEM_COLUMNS) == SNAPSHOT["promo_offer_items"]


def test_service_columns_match_snapshot() -> None:
    assert list(CLINIC_SERVICE_COLUMNS) == SNAPSHOT["clinic_services"]


def test_membership_columns_match_snapshot() -> None:
    assert list(CLINIC_MEMBERSHIP_COLUMNS) == SNAPSHOT["clinic_memberships"]


def test_promotion_columns_match_snapshot() -> None:
    assert list(CLINIC_PROMOTION_COLUMNS) == SNAPSHOT["clinic_promotions"]
