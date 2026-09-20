"""Application-side helpers for the clinic-service taxonomy.

The database owns normalization and category assignment. These helpers only
recognize the repository's supported display names so callers can query and
deduplicate by canonical ``service_name`` without writing generated columns.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any


SERVICE_CATEGORY_ENUM_VALUES = (
    "Neurotoxin",
    "Dermal Filler",
    "Microneedling",
    "Facial",
    "Laser & Light",
    "Laser Hair Removal",
    "Wellness",
    "Regenerative Injectable",
    "Body Contouring",
    "Skin Tightening",
)

SERVICE_NAME_ENUM_VALUES = (
    "Botox", "Dysport", "Xeomin", "Jeuveau", "Daxxify", "Letybo", "Myobloc", "Neurotoxin",
    "Dermal Filler", "Juvéderm", "Restylane", "Radiesse", "Belotero", "RHA Filler", "Skinvive", "Filler Dissolving",
    "Morpheus8", "RF Microneedling", "SkinPen Microneedling", "Sylfirm X", "Microchanneling", "Microneedling",
    "Hydrafacial", "DiamondGlow", "Dermaplaning", "Glo2 Facial", "Facial",
    "IPL Photofacial", "BBL Photofacial", "LED Light Therapy", "CO2 Laser", "CoolPeel", "Halo Laser", "Moxi Laser", "Picoway", "Vbeam", "ClearSilk", "Laser Genesis", "Laser Treatment",
    "Laser Hair Removal",
    "NAD Therapy", "IV Therapy", "Vitamin Injection", "Weight Loss", "Hormone Therapy", "Peptide Therapy",
    "Sculptra", "Chemical Peel", "Kybella", "Thread Lift", "Tattoo Removal", "Skin Tightening",
    "Regenerative Injectable", "PRP", "PRF", "Exosome Therapy", "Body Contouring", "CoolSculpting", "Wellness", "Massage",
    "Pearl Fractional", "Customized Facial", "Lunchtime Peel", "PRX Skin Perfecting", "Perfect Derma Peel", "Sofwave", "Tixel", "Lipo Dissolve",
    "Versa", "Evolysse", "EZ Gel", "PDRN", "PRFM", "Fraxel", "Clear + Brilliant", "Emsculpt", "VI Peel", "Microdermabrasion", "Hair Restoration",
    "Ultherapy", "Thermage", "HIFU", "Forma", "Emface",
)

GENERATED_TAXONOMY_COLUMNS = frozenset({"service_category"})
CLINIC_SERVICE_WRITE_COLUMNS = frozenset(
    {"business_id", "service_name", "regular_price", "unit_type", "service_area", "source_url", "updated_at"}
)


def _normalized_name(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


_SERVICE_NAME_ENUMS = {
    _normalized_name(service_name): service_name
    for service_name in SERVICE_NAME_ENUM_VALUES
}
_SERVICE_NAME_ALIASES = {
    "dermal fillers": "Dermal Filler",
    "revanesse": "Versa",
    "ez gel": "EZ Gel",
    "ezgel": "EZ Gel",
    "nad injections": "NAD Therapy",
    "morpheus8 body": "Morpheus8",
    "prp microneedling": "Microneedling",
    "ha microneedling": "Microneedling",
    "voluma": "Juvéderm",
    "vollure": "Juvéderm",
    "volbella": "Juvéderm",
    "semaglutide": "Weight Loss",
    "tirzepatide": "Weight Loss",
    "ozempic": "Weight Loss",
    "wegovy": "Weight Loss",
    "mounjaro": "Weight Loss",
    "glp1": "Weight Loss",
    "glp 1": "Weight Loss",
    "clear and brilliant": "Clear + Brilliant",
}
_SERVICE_NAME_ENUMS.update(_SERVICE_NAME_ALIASES)


def service_name_enum_hint(value: Any) -> str | None:
    """Return a known canonical service_name for an application-supported display name.

    Unknown values deliberately return ``None``. The live table only accepts
    these approved enum values, so callers must not send arbitrary source text
    to ``clinic_services.service_name``.
    """
    return _SERVICE_NAME_ENUMS.get(_normalized_name(value))


def without_generated_taxonomy_columns(payload: dict[str, Any]) -> dict[str, Any]:
    """Prevent API payloads from trying to write generated taxonomy columns."""
    return {key: value for key, value in payload.items() if key not in GENERATED_TAXONOMY_COLUMNS}


def live_clinic_service_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep writes within the live table's writable-column boundary."""
    out = {
        key: value
        for key, value in without_generated_taxonomy_columns(payload).items()
        if key in CLINIC_SERVICE_WRITE_COLUMNS
    }
    hint = service_name_enum_hint(out.get("service_name"))
    if "service_name" in out:
        if hint is None:
            raise ValueError("unsupported clinic service enum")
        out["service_name"] = hint
    return out
