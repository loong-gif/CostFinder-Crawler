"""Tests for service_category_lookup."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.align_service_names import infer_alignment
from utils.service_category_lookup import (
    CANONICAL_SERVICE_CATEGORIES,
    build_service_name_category_index,
    infer_service_category,
    infer_service_category_for_offer,
    normalize_service_category,
    resolve_service_category,
)


def test_infer_alignment_accepts_none_source_category():
    result = infer_alignment("Botox", None)
    assert result["aligned_service_category"] == "neurotoxin"


def test_exact_name_botox():
    cat, method, conf = infer_service_category("Botox", "")
    assert cat == "Neurotoxins"
    assert method == "exact_name"
    assert conf == "high"


def test_pattern_laser_hair_removal():
    cat, method, _ = infer_service_category("Full Leg Laser Hair Removal", "")
    assert cat == "Laser Hair Removal"
    assert method == "pattern"


def test_sibling_index_wins():
    index = build_service_name_category_index(
        [
            {"service_name": "Botox", "service_category": "Neurotoxins"},
            {"service_name": "Botox", "service_category": "Injectables"},
        ]
    )
    cat, method, conf = infer_service_category("Botox", "", sibling_index=index)
    assert cat == "Neurotoxins"
    assert method == "sibling_mode"
    assert conf == "high"


def test_build_offer_update_payload_fills_category():
    from utils.change_driven_extractor import build_offer_update_payload

    payload = build_offer_update_payload(
        {"service_name": "Botox", "offer_raw_text": "Botox $10/unit", "discount_price": 10}
    )
    assert payload["service_category"] == "Neurotoxins"


def test_normalize_injectables_deprecated():
    assert normalize_service_category("Injectables") is None


def test_remap_injectables_botox_to_neurotoxins():
    from utils.service_category_lookup import remap_injectables_category

    assert remap_injectables_category("Botox") == "Neurotoxins"


def test_remap_injectables_filler_to_fillers():
    from utils.service_category_lookup import remap_injectables_category

    assert remap_injectables_category("Juvederm Voluma") == "Fillers & Other Injectables"


def test_resolve_splits_injectables_category():
    cat, method, _ = resolve_service_category("Dysport", "Injectables")
    assert cat == "Neurotoxins"
    assert method == "injectables_split"


def test_normalize_aliases():
    assert normalize_service_category("Neurotoxin") == "Neurotoxins"
    assert normalize_service_category("Facials") == "Facial"
    assert normalize_service_category("PACKAGES") == "Package"
    assert normalize_service_category("Fillers") == "Fillers & Other Injectables"
    assert normalize_service_category("Neurotoxins") == "Neurotoxins"


def test_normalize_junk_to_others():
    assert normalize_service_category("MAINTAIN \t6 Hydrate & Glow") == "Others"


def test_resolve_prefers_normalized_category():
    cat, method, conf = resolve_service_category("Botox", "Neurotoxin")
    assert cat == "Neurotoxins"
    assert method == "normalized"
    assert conf == "high"


def test_canonical_list_excludes_injectables():
    assert "Fillers & Other Injectables" in CANONICAL_SERVICE_CATEGORIES
    assert "Neurotoxins" in CANONICAL_SERVICE_CATEGORIES
    assert "Injectables" not in CANONICAL_SERVICE_CATEGORIES


def test_min_confidence_blocks_low():
    cat, method, _ = infer_service_category_for_offer(
        {"service_name": "Mystery Promo XYZ"},
        min_confidence="medium",
    )
    assert cat is None
    assert method == "unresolved"
