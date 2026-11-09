from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, NamedTuple


class MatchRule(NamedTuple):
    entity: str
    category: str
    pattern: re.Pattern[str]


NONSERVICE_RULES = [
    (re.compile(r"\bmembership\b", re.IGNORECASE), "membership"),
    (re.compile(r"\bpromotion\b|\brewards?\b|\bsign-up bonus\b", re.IGNORECASE), "promotion"),
    (re.compile(r"\bgift card\b|\bgiveaway\b", re.IGNORECASE), "gift_card"),
    (re.compile(r"\bconsultation\b", re.IGNORECASE), "consultation"),
    (re.compile(r"\bmodel call\b", re.IGNORECASE), "model_call"),
]

ENTITY_RULES: List[MatchRule] = [
    MatchRule("Botox", "neurotoxin", re.compile(r"\bbotox\b", re.IGNORECASE)),
    MatchRule("Dysport", "neurotoxin", re.compile(r"\bdysport\b", re.IGNORECASE)),
    MatchRule("Xeomin", "neurotoxin", re.compile(r"\bxeomin\b", re.IGNORECASE)),
    MatchRule("Jeuveau", "neurotoxin", re.compile(r"\bjeuveau\b|\bjeaveau\b", re.IGNORECASE)),
    MatchRule("Daxxify", "neurotoxin", re.compile(r"\bdaxxify\b", re.IGNORECASE)),
    MatchRule("Letybo", "neurotoxin", re.compile(r"\bletybo\b", re.IGNORECASE)),
    MatchRule("Newtox", "neurotoxin", re.compile(r"\bnewtox\b", re.IGNORECASE)),
    MatchRule(
        "Neurotoxin",
        "neurotoxin",
        re.compile(r"\bneurotox(?:in|ins?)\b|\btox\b|\bwrinkle relaxers?\b", re.IGNORECASE),
    ),
    MatchRule("Juvederm", "filler", re.compile(r"\bjuvederm\b|\bvoluma\b|\bvolux\b|\bvolbella\b|\bvollure\b", re.IGNORECASE)),
    MatchRule("Restylane", "filler", re.compile(r"\brestylane\b|\bkysse\b|\bdefyne\b|\brefyne\b|\blyft\b|\bsilk\b|\bcontour\b", re.IGNORECASE)),
    MatchRule("RHA", "filler", re.compile(r"\brha\b", re.IGNORECASE)),
    MatchRule("Revanesse Versa", "filler", re.compile(r"\brevanesse\b|\bversa\b", re.IGNORECASE)),
    MatchRule("Belotero", "filler", re.compile(r"\bbelotero\b", re.IGNORECASE)),
    MatchRule("Dermal Filler", "filler", re.compile(r"\bfiller\b|\bfillers\b|\blip augmentation\b|\blip plump\b|\bbiofiller\b|\bfacial balancing\b|\bliquid facelift\b|\bnose job\b", re.IGNORECASE)),
    MatchRule("Sculptra", "biostimulator", re.compile(r"\bsculptra\b|\bcollagen stimulator\b|\bcollagen booster\b|\bcollagen restore\b", re.IGNORECASE)),
    MatchRule("Radiesse", "biostimulator", re.compile(r"\bradiesse\b", re.IGNORECASE)),
    MatchRule("SkinVive", "skin_booster", re.compile(r"\bskinvive\b", re.IGNORECASE)),
    MatchRule("PRF", "regenerative", re.compile(r"\bprf\b|\bez[\s-]*gel\b|\bezgel\b|platelet rich fibrin", re.IGNORECASE)),
    MatchRule("PRP", "regenerative", re.compile(r"\bprp\b|platelet rich plasma", re.IGNORECASE)),
    MatchRule("PRFM", "regenerative", re.compile(r"\bprfm\b", re.IGNORECASE)),
    MatchRule("Exosome", "regenerative", re.compile(r"\bexosome\b", re.IGNORECASE)),
    MatchRule("Kybella", "fat_dissolver", re.compile(r"\bkybella\b|\bdeoxycholic acid\b", re.IGNORECASE)),
    MatchRule("Phat Potion", "fat_dissolver", re.compile(r"\bphat potion\b", re.IGNORECASE)),
    MatchRule("PDO Threads", "thread_lift", re.compile(r"\bpdo\b|\bthreads?\b|\bthread lift\b|\bcat eye\b", re.IGNORECASE)),
    MatchRule("Hyaluronidase", "filler_dissolver", re.compile(r"\bhyaluronidase\b|\bhylenex\b|\bdissolv(?:e|ing|er)\b", re.IGNORECASE)),
    MatchRule("Kenalog", "steroid", re.compile(r"\bkenalog\b|\bsteroid scar\b", re.IGNORECASE)),
    MatchRule("Sclerotherapy", "vascular", re.compile(r"\bsclerotherapy\b|\bvein injection\b", re.IGNORECASE)),
    MatchRule("Injectable Treatment", "injectable_other", re.compile(r"\binjectables?\b|\binjectable hydrator\b|\bheeltox\b", re.IGNORECASE)),
]

EXACT_NAME_MAP: Dict[str, str] = {
    "botox": "Botox",
    "botox/ dysport": "Botox/Dysport",
    "botox / dysport": "Botox/Dysport",
    "botox/dysport": "Botox/Dysport",
    "jeuveau": "Jeuveau",
    "jeaveau": "Jeuveau",
    "lip fillers": "Lip Fillers",
    "lip filler": "Lip Filler",
    "under eye filler": "Under Eye Filler",
    "undereye filler": "Under Eye Filler",
    "xeomin - 40 units": "Xeomin - 40 Units",
    "wrinkle relaxers": "Wrinkle Relaxers",
    "wrinkle relaxer injectables": "Wrinkle Relaxer Injectables",
    "skinvivetm": "SkinVive",
}

TEXT_REPLACEMENTS = [
    (re.compile(r"[®©]"), ""),
    (re.compile(r"\bTM\b", re.IGNORECASE), ""),
    (re.compile(r"JUV[ÉE]DERM", re.IGNORECASE), "Juvederm"),
    (re.compile(r"Juverderm", re.IGNORECASE), "Juvederm"),
    (re.compile(r"Juvderm", re.IGNORECASE), "Juvederm"),
    (re.compile(r"\bSKINVIVE\b", re.IGNORECASE), "SkinVive"),
    (re.compile(r"\bXEOMIN\b", re.IGNORECASE), "Xeomin"),
    (re.compile(r"\bBOTOX\b", re.IGNORECASE), "Botox"),
    (re.compile(r"\bKYBELLA\b", re.IGNORECASE), "Kybella"),
    (re.compile(r"\bRADIESSE\b", re.IGNORECASE), "Radiesse"),
    (re.compile(r"\bJeaveau\b", re.IGNORECASE), "Jeuveau"),
    (re.compile(r"\bundereye\b", re.IGNORECASE), "Under Eye"),
    (re.compile(r"\s*/\s*"), "/"),
    (re.compile(r"\s+-\s+"), " - "),
]

NEUROTOXIN_PROCEDURE_PATTERN = re.compile(
    r"lip flip|brow lift|gummy smile|bunny lines|crow'?s feet|forehead|glabellar|frown lines|"
    r"jelly roll|masseter|bruxism|migraine|hyperhidrosis|excessive sweating|nefertiti|"
    r"chin dimpling|nasal recontouring|jaw clenching|shoulder slimming|plantar fasciitis|"
    r"quiktox|brotox|classic areas tox|wrinkle package",
    re.IGNORECASE,
)

FILLER_PROCEDURE_PATTERN = re.compile(
    r"8 point lift|facial harmony|facial balancing|face sculpting|liquid facelift|lip|cheek|"
    r"chin|jawline|temple|tear trough|nasolabial|earlobe|under eye|under eyes|nose bridge|"
    r"pucker|plump harmonization|get chiseled|rejuvenation lift|refreshed & refined|"
    r"natural glow package|full-face balancing|full facial balancing|naked lips|iconique lips",
    re.IGNORECASE,
)


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("’", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def exact_key(value: str) -> str:
    key = normalize_text(value).casefold()
    return key


def standardize_specific_name(value: str) -> str:
    text = normalize_text(value)
    if not text:
        return ""

    for pattern, replacement in TEXT_REPLACEMENTS:
        text = pattern.sub(replacement, text)

    text = normalize_text(text)

    mapped = EXACT_NAME_MAP.get(exact_key(text))
    if mapped:
        return mapped

    if text.islower():
        text = text.title()

    return text


def detect_nonservice(name: str) -> str:
    for pattern, label in NONSERVICE_RULES:
        if pattern.search(name):
            return label
    return ""


def dedupe_preserve_order(items: List[str]) -> List[str]:
    seen = set()
    ordered = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def infer_alignment(name: str, source_category: str | None = "") -> Dict[str, str]:
    source_category = source_category or ""
    normalized_name = standardize_specific_name(name)
    if not normalized_name:
        return {
            "aligned_service_name_normalized": "",
            "aligned_service_name_canonical": "",
            "aligned_service_category": "",
            "aligned_entity_list": "",
            "aligned_category_list": "",
            "alignment_confidence": "low",
            "needs_manual_review": "TRUE",
            "alignment_note": "Empty service_name",
        }

    nonservice = detect_nonservice(normalized_name)
    if nonservice:
        return {
            "aligned_service_name_normalized": normalized_name,
            "aligned_service_name_canonical": normalized_name,
            "aligned_service_category": f"nonservice_{nonservice}",
            "aligned_entity_list": normalized_name,
            "aligned_category_list": f"nonservice_{nonservice}",
            "alignment_confidence": "high",
            "needs_manual_review": "FALSE",
            "alignment_note": "Detected non-treatment commercial label",
        }

    matched_entities: List[str] = []
    matched_categories: List[str] = []
    for rule in ENTITY_RULES:
        if rule.pattern.search(normalized_name):
            matched_entities.append(rule.entity)
            matched_categories.append(rule.category)

    matched_entities = dedupe_preserve_order(matched_entities)
    matched_categories = dedupe_preserve_order(matched_categories)

    if not matched_entities:
        if NEUROTOXIN_PROCEDURE_PATTERN.search(normalized_name):
            return {
                "aligned_service_name_normalized": normalized_name,
                "aligned_service_name_canonical": "Neurotoxin",
                "aligned_service_category": "neurotoxin",
                "aligned_entity_list": "Neurotoxin",
                "aligned_category_list": "neurotoxin",
                "alignment_confidence": "high",
                "needs_manual_review": "FALSE",
                "alignment_note": "Procedure pattern matched neurotoxin service",
            }
        if FILLER_PROCEDURE_PATTERN.search(normalized_name):
            return {
                "aligned_service_name_normalized": normalized_name,
                "aligned_service_name_canonical": "Dermal Filler",
                "aligned_service_category": "filler",
                "aligned_entity_list": "Dermal Filler",
                "aligned_category_list": "filler",
                "alignment_confidence": "medium",
                "needs_manual_review": "TRUE",
                "alignment_note": "Procedure pattern matched filler-style service",
            }
        if "neurotoxin" in source_category.casefold():
            return {
                "aligned_service_name_normalized": normalized_name,
                "aligned_service_name_canonical": "Neurotoxin",
                "aligned_service_category": "neurotoxin",
                "aligned_entity_list": "Neurotoxin",
                "aligned_category_list": "neurotoxin",
                "alignment_confidence": "medium",
                "needs_manual_review": "TRUE",
                "alignment_note": "Used source_category fallback for neurotoxin",
            }
        if "fillers & other injectables" in source_category.casefold():
            return {
                "aligned_service_name_normalized": normalized_name,
                "aligned_service_name_canonical": normalized_name,
                "aligned_service_category": "filler_or_other_injectable",
                "aligned_entity_list": normalized_name,
                "aligned_category_list": "filler_or_other_injectable",
                "alignment_confidence": "low",
                "needs_manual_review": "TRUE",
                "alignment_note": "Used source_category fallback for filler/injectable",
            }
        return {
            "aligned_service_name_normalized": normalized_name,
            "aligned_service_name_canonical": normalized_name,
            "aligned_service_category": "unknown",
            "aligned_entity_list": normalized_name,
            "aligned_category_list": "unknown",
            "alignment_confidence": "low",
            "needs_manual_review": "TRUE",
            "alignment_note": "No entity rule matched",
        }

    if len(matched_categories) == 1 and len(matched_entities) == 1:
        return {
            "aligned_service_name_normalized": normalized_name,
            "aligned_service_name_canonical": matched_entities[0],
            "aligned_service_category": matched_categories[0],
            "aligned_entity_list": matched_entities[0],
            "aligned_category_list": matched_categories[0],
            "alignment_confidence": "high",
            "needs_manual_review": "FALSE",
            "alignment_note": "Single clear entity match",
        }

    if len(matched_categories) == 1:
        return {
            "aligned_service_name_normalized": normalized_name,
            "aligned_service_name_canonical": "; ".join(matched_entities),
            "aligned_service_category": matched_categories[0],
            "aligned_entity_list": "|".join(matched_entities),
            "aligned_category_list": "|".join([matched_categories[0]] * len(matched_entities)),
            "alignment_confidence": "medium",
            "needs_manual_review": "TRUE",
            "alignment_note": "Multiple entities within one category",
        }

    return {
        "aligned_service_name_normalized": normalized_name,
        "aligned_service_name_canonical": "; ".join(matched_entities),
        "aligned_service_category": "mixed",
        "aligned_entity_list": "|".join(matched_entities),
        "aligned_category_list": "|".join(matched_categories),
        "alignment_confidence": "low",
        "needs_manual_review": "TRUE",
        "alignment_note": "Multiple categories matched",
    }

