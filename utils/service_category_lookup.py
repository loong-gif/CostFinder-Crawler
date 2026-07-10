"""Infer and normalize promo_offer_master.service_category."""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, List, Mapping, Optional, Tuple

from utils.align_service_names import infer_alignment

# Canonical taxonomy — aligned to promo_offer_master dominant values.
CANONICAL_SERVICE_CATEGORIES: Tuple[str, ...] = (
    "Fillers & Other Injectables",
    "Neurotoxins",
    "Facial",
    "Microneedling",
    "Laser Hair Removal",
    "Skin Treatments",
    "Package",
    "Others",
    "Laser",
    "Skincare Product",
    "Permanent Makeup",
    "Nail Services",
    "Chemical Peel",
    "Laser Tattoo Removal",
    "Body",
    "Gift Card",
    "Massage",
    "Acupuncture",
    "Ultherapy",
    "New Guest Offers",
    "CoolSculpting",
    "Hair Restoration",
    "Waxing",
    "Spa",
    "Add-on",
)

_CANONICAL_BY_FOLD: Dict[str, str] = {c.casefold(): c for c in CANONICAL_SERVICE_CATEGORIES}

# Legacy / pipeline / LLM shorthand -> canonical master category.
_CATEGORY_ALIASES: Dict[str, str] = {
    # Neurotoxins
    "neurotoxin": "Neurotoxins",
    "neuromodulators": "Neurotoxins",
    "botox": "Neurotoxins",
    "dysport": "Neurotoxins",
    # Fillers & injectables
    "dermal filler": "Fillers & Other Injectables",
    "fillers": "Fillers & Other Injectables",
    "filler": "Fillers & Other Injectables",
    "lip filler": "Fillers & Other Injectables",
    "sculptra": "Fillers & Other Injectables",
    "kybella": "Fillers & Other Injectables",
    "skinvive": "Fillers & Other Injectables",
    "threads": "Fillers & Other Injectables",
    "fillers & other injectables": "Fillers & Other Injectables",
    # Facial / skin
    "facials": "Facial",
    "facial treatment": "Facial",
    "skincare products": "Skincare Product",
    "skincare": "Skincare Product",
    "skin care": "Skincare Product",
    "retail": "Skincare Product",
    "skin": "Skin Treatments",
    "skin rejuvenation": "Skin Treatments",
    "skin resurfacing": "Skin Treatments",
    "skin tightening": "Skin Treatments",
    "acne treatment": "Skin Treatments",
    "lifting": "Skin Treatments",
    "full face": "Skin Treatments",
    # Microneedling / devices
    "morpheus8": "Microneedling",
    # Laser
    "laser treatment": "Laser",
    "laser treatments": "Laser",
    "laser skin rejuvenation": "Laser",
    "laser hair removal": "Laser Hair Removal",
    # Chemical peel
    "chemical peels": "Chemical Peel",
    # Package / misc buckets
    "packages": "Package",
    "package": "Package",
    "other": "Others",
    "treatment": "Others",
    "treatments": "Others",
    "therapy": "Others",
    "wellness": "Others",
    "wellness services": "Others",
    "weight loss": "Others",
    "consultation": "Others",
    "promotion": "Others",
    "loyalty reward": "Others",
    "treatment credit": "Others",
    "massages": "Massage",
    "pedicure": "Nail Services",
    "nail repair": "Nail Services",
    "body contouring": "Body",
    # Firecrawl / vision shorthand
    "membership": "Others",
    "memberships": "Others",
}

# Firecrawl enum values not already canonical in master.
FIRECRAWL_CATEGORY_MAP: Dict[str, str] = {
    "fillers": "Fillers & Other Injectables",
    "skin": "Skin Treatments",
    "skincare": "Skincare Product",
    "other": "Others",
}

# ponytail: Injectables removed from taxonomy; split into Neurotoxins vs Fillers only.
_INJECTABLES_FOLD = "injectables"
_INJECTABLE_SPLIT_TARGETS = frozenset({"Neurotoxins", "Fillers & Other Injectables"})

MASTER_CATEGORY_PROMPT = ", ".join(CANONICAL_SERVICE_CATEGORIES)

_JUNK_CATEGORY_PATTERN = re.compile(
    r"\b(maintain|correct|prevent|product of the month)\b",
    re.IGNORECASE,
)

# align_service_names aligned_service_category -> master column values
_ALIGNED_TO_MASTER: Dict[str, str] = {
    "neurotoxin": "Neurotoxins",
    "filler": "Fillers & Other Injectables",
    "filler_or_other_injectable": "Fillers & Other Injectables",
    "biostimulator": "Fillers & Other Injectables",
    "skin_booster": "Fillers & Other Injectables",
    "regenerative": "Skin Treatments",
    "thread_lift": "Fillers & Other Injectables",
    "fat_dissolver": "Fillers & Other Injectables",
    "filler_dissolver": "Fillers & Other Injectables",
    "injectable_other": "Fillers & Other Injectables",
    "steroid": "Fillers & Other Injectables",
    "vascular": "Fillers & Other Injectables",
    "mixed": "Others",
}

# Canonical service_name -> category (matches populated master rows where possible)
_EXACT_SERVICE_NAME_CATEGORY: Dict[str, str] = {
    "Botox": "Neurotoxins",
    "Dysport": "Neurotoxins",
    "Daxxify": "Neurotoxins",
    "Letybo": "Neurotoxins",
    "Neurotoxin": "Neurotoxins",
    "Xeomin": "Neurotoxins",
    "Jeuveau": "Neurotoxins",
    "Newtox": "Neurotoxins",
    "Lip Flip": "Neurotoxins",
    "Chin Tox": "Neurotoxins",
    "Dermal Filler": "Fillers & Other Injectables",
    "Dermal Fillers": "Fillers & Other Injectables",
    "Lip Filler": "Fillers & Other Injectables",
    "Under Eye Filler": "Fillers & Other Injectables",
    "Chin Filler": "Fillers & Other Injectables",
    "Nose Filler": "Fillers & Other Injectables",
    "Filler": "Fillers & Other Injectables",
    "Sculptra": "Fillers & Other Injectables",
    "Injected Sculptra": "Fillers & Other Injectables",
    "Kybella": "Fillers & Other Injectables",
    "Skinvive": "Fillers & Other Injectables",
    "SkinVive": "Fillers & Other Injectables",
    "SKINVIVE": "Fillers & Other Injectables",
    "PRP": "Skin Treatments",
    "PRF": "Skin Treatments",
    "Microneedling": "Microneedling",
    "RF Microneedling": "Microneedling",
    "SkinPen Microneedling": "Microneedling",
    "Morpheus8": "Microneedling",
    "Laser Hair Removal": "Laser Hair Removal",
    "Hydrafacial": "Facial",
    "Deluxe HydraFacial": "Facial",
    "Facial": "Facial",
    "Chemical Peel": "Chemical Peel",
    "Chemical Peels": "Chemical Peel",
    "IPL Photofacial": "Laser",
    "Ultherapy": "Skin Treatments",
    "CoolSculpting": "Body",
    "Forma": "Skin Treatments",
    "Package": "Package",
    "Others": "Others",
    "Brow Touch Up": "Permanent Makeup",
    "Lip Blush Touch Up": "Permanent Makeup",
    "IV Therapy": "Others",
    "NAD+ IV Therapy": "Others",
}

_PATTERN_RULES: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:botox|dysport|daxxify|letybo|xeomin|jeuveau|newtox|neurotox)\b", re.I), "Neurotoxins"),
    (re.compile(r"\blip flip\b|\bchin tox\b|\btox\b", re.I), "Neurotoxins"),
    (re.compile(r"\bfiller\b|\bsculptra\b|\bkybella\b|\brestylane\b|\bjuvederm\b|\bradiesse\b|\bversa\b", re.I), "Fillers & Other Injectables"),
    (re.compile(r"\bmicroneedling\b|\bmorpheus\b|\bskinpen\b|\brf micro", re.I), "Microneedling"),
    (re.compile(r"\blaser hair\b|\blaser removal\b|\blhr\b", re.I), "Laser Hair Removal"),
    (re.compile(r"\bhydrafacial\b|\bhydra facial\b", re.I), "Facial"),
    (re.compile(r"\bfacial\b|\bpeel\b", re.I), "Facial"),
    (re.compile(r"\bchemical peel\b", re.I), "Chemical Peel"),
    (re.compile(r"\bipl\b|\bphotofacial\b|\blaser (?:resurf|treat)", re.I), "Laser"),
    (re.compile(r"\bultherapy\b|\bforma\b|\bskin tight", re.I), "Skin Treatments"),
    (re.compile(r"\bcoolsculpt\b|\bemsculpt\b|\bbody contour", re.I), "Body"),
    (re.compile(r"\biv therapy\b|\bnad\+\b|\bvitamin infusion\b", re.I), "Others"),
    (re.compile(r"\bbrow\b|\blip blush\b|\bpermanent makeup\b|\bmicroblading\b", re.I), "Permanent Makeup"),
    (re.compile(r"\bprp\b|\bprf\b|\bexosome\b", re.I), "Skin Treatments"),
    (re.compile(r"\bpackage\b|\bbundle\b|\bcombo\b|\bbogo\b", re.I), "Package"),
    (re.compile(r"\btattoo removal\b|\blaser tattoo\b", re.I), "Laser Tattoo Removal"),
    (re.compile(r"\bweight loss\b|\bmetabolic reset\b|\bglp\b|\bsemaglutide\b|\btirzepatide\b", re.I), "Others"),
    (re.compile(r"\bdermaplan", re.I), "Facial"),
    (re.compile(r"\bmicrodermabrasion\b|\bsylfirm\b|\bbiorepeel\b", re.I), "Skin Treatments"),
    (re.compile(r"\bmanicure\b|\bnail\b", re.I), "Nail Services"),
    (re.compile(r"\btraptox\b|\bsweating\b|\bhyperhidrosis\b|\bplatysmal\b", re.I), "Neurotoxins"),
    (re.compile(r"\bultra laser\b|\bxerf\b|\blaser\b", re.I), "Laser"),
)

_CONF_RANK = {"low": 1, "medium": 2, "high": 3}


def _fold_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _is_deprecated_injectables(raw: Any) -> bool:
    return _fold_key(str(raw or "")) == _INJECTABLES_FOLD


def remap_injectables_category(service_name: str) -> str:
    """Split deprecated Injectables bucket into Neurotoxins or Fillers & Other Injectables."""
    category, _, _ = infer_service_category_for_offer(
        {"service_name": service_name, "service_category": ""},
        min_confidence="low",
    )
    if category in _INJECTABLE_SPLIT_TARGETS:
        return category
    return "Fillers & Other Injectables"


def normalize_service_category(raw: Any) -> Optional[str]:
    """Map raw category text to canonical master taxonomy; None when empty."""
    text = re.sub(r"\s+", " ", str(raw or "").strip())
    if not text:
        return None

    key = _fold_key(text)
    if key == _INJECTABLES_FOLD:
        return None
    if key in _CANONICAL_BY_FOLD:
        return _CANONICAL_BY_FOLD[key]
    if key in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[key]
    if key in FIRECRAWL_CATEGORY_MAP:
        return FIRECRAWL_CATEGORY_MAP[key]

    if _JUNK_CATEGORY_PATTERN.search(text):
        return "Others"

    return None


def resolve_service_category(
    service_name: str = "",
    raw_category: Any = "",
    *,
    sibling_index: Optional[Mapping[str, str]] = None,
    min_confidence: str = "medium",
) -> Tuple[Optional[str], str, str]:
    """Normalize explicit category, else infer from service_name."""
    if _is_deprecated_injectables(raw_category):
        target = remap_injectables_category(service_name)
        return target, "injectables_split", "medium"

    normalized = normalize_service_category(raw_category)
    if normalized:
        return normalized, "normalized", "high"

    return infer_service_category_for_offer(
        {"service_name": service_name, "service_category": raw_category},
        sibling_index=sibling_index,
        min_confidence=min_confidence,
    )


def build_service_name_category_index(
    rows: List[Mapping[str, Any]],
) -> Dict[str, str]:
    """Most common non-empty service_category per exact service_name."""
    counts: Dict[str, Counter[str]] = {}
    for row in rows:
        name = str(row.get("service_name") or "").strip()
        category = str(row.get("service_category") or "").strip()
        if not name or not category:
            continue
        if _is_deprecated_injectables(category):
            canonical = remap_injectables_category(name)
        else:
            canonical = normalize_service_category(category) or category
        counts.setdefault(name, Counter())[canonical] += 1
    return {name: counter.most_common(1)[0][0] for name, counter in counts.items()}


def infer_service_category(
    service_name: str,
    source_category: str = "",
    *,
    sibling_index: Optional[Mapping[str, str]] = None,
) -> Tuple[Optional[str], str, str]:
    """Return (category, method, confidence). category is None when unresolved."""
    name = str(service_name or "").strip()
    if not name:
        return None, "empty_name", "low"

    if sibling_index and name in sibling_index:
        return normalize_service_category(sibling_index[name]) or sibling_index[name], "sibling_mode", "high"

    if name in _EXACT_SERVICE_NAME_CATEGORY:
        return _EXACT_SERVICE_NAME_CATEGORY[name], "exact_name", "high"

    for pattern, category in _PATTERN_RULES:
        if pattern.search(name):
            return category, "pattern", "medium"

    alignment = infer_alignment(name, source_category or "")
    aligned = str(alignment.get("aligned_service_category") or "").strip()
    conf = str(alignment.get("alignment_confidence") or "low")

    if aligned.startswith("nonservice_"):
        return None, f"nonservice:{aligned}", conf

    if aligned and aligned != "unknown":
        master = _ALIGNED_TO_MASTER.get(aligned)
        if master:
            return master, "infer_alignment", conf

    existing = normalize_service_category(source_category)
    if existing:
        return existing, "source_category", "medium"

    return None, "unresolved", "low"


def infer_service_category_for_offer(
    offer: Mapping[str, Any],
    *,
    sibling_index: Optional[Mapping[str, str]] = None,
    min_confidence: str = "medium",
) -> Tuple[Optional[str], str, str]:
    """Infer category for an offer dict; respects min_confidence threshold."""
    category, method, confidence = infer_service_category(
        str(offer.get("service_name") or ""),
        str(offer.get("service_category") or ""),
        sibling_index=sibling_index,
    )
    if category is None:
        return None, method, confidence
    if _CONF_RANK.get(confidence, 0) < _CONF_RANK.get(min_confidence, 2):
        return None, f"below_{min_confidence}:{method}", confidence
    return category, method, confidence
