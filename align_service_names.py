#!/usr/bin/env python3
"""
Align medspa service names into normalized labels plus entity-level mappings.

Outputs:
1. Row-level preview CSV with added alignment columns.
2. Aggregated mapping CSV for each raw service_name.
3. Manual review CSV containing medium/low-confidence mappings.
"""

from __future__ import annotations

import argparse
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Dict, List, NamedTuple

import pandas as pd


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Align service_name values into normalized names and entities.")
    parser.add_argument(
        "--input",
        default="/Users/wyl/Downloads/4.3 CF - qa (1).csv",
        help="Input CSV path.",
    )
    parser.add_argument(
        "--output",
        default="/Users/wyl/Downloads/4.3_CF_qa_alignment_preview.csv",
        help="Output CSV with added alignment columns.",
    )
    parser.add_argument(
        "--aligned-original-output",
        default="/Users/wyl/Downloads/4.3_CF_qa_service_name_aligned.csv",
        help="Output CSV that preserves the original schema and writes back aligned service_name values.",
    )
    parser.add_argument(
        "--mapping-output",
        default="/Users/wyl/Downloads/4.3_CF_qa_service_name_mapping.csv",
        help="Aggregated mapping CSV path.",
    )
    parser.add_argument(
        "--review-output",
        default="/Users/wyl/Downloads/4.3_CF_qa_service_name_manual_review.csv",
        help="Manual review CSV path.",
    )
    return parser.parse_args()


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


def infer_alignment(name: str, source_category: str = "") -> Dict[str, str]:
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


def expand_multi_entity_rows(df: pd.DataFrame) -> pd.DataFrame:
    expanded_rows = []
    for _, row in df.iterrows():
        entity_list = [item.strip() for item in str(row.get("aligned_entity_list", "")).split("|") if item.strip()]
        category_list = [item.strip() for item in str(row.get("aligned_category_list", "")).split("|") if item.strip()]
        if not entity_list:
            entity_list = [str(row.get("aligned_service_name_canonical", "")).strip()]
        if not category_list:
            category_list = [str(row.get("aligned_service_category", "")).strip()]
        if len(category_list) == 1 and len(entity_list) > 1:
            category_list = category_list * len(entity_list)
        if len(category_list) != len(entity_list):
            category_list = [str(row.get("aligned_service_category", "")).strip()] * len(entity_list)

        split_needed = len(entity_list) > 1
        for split_index, (entity, category) in enumerate(zip(entity_list, category_list), start=1):
            new_row = row.copy()
            new_row["aligned_service_name_canonical"] = entity
            new_row["aligned_service_category"] = category
            new_row["aligned_split_from_multi_entity"] = "TRUE" if split_needed else "FALSE"
            new_row["aligned_split_entity_index"] = str(split_index)
            new_row["aligned_split_entity_count"] = str(len(entity_list))
            expanded_rows.append(new_row)

    return pd.DataFrame(expanded_rows)


def build_mapping(df: pd.DataFrame) -> pd.DataFrame:
    grouped = (
        df.groupby(
            [
                "service_name",
                "aligned_service_name_normalized",
                "aligned_service_name_canonical",
                "aligned_service_category",
                "alignment_confidence",
                "needs_manual_review",
                "alignment_note",
            ],
            dropna=False,
            as_index=False,
        )
        .size()
        .reset_index()
        .rename(columns={"service_name": "raw_service_name", "size": "row_count"})
    )
    grouped = grouped[
        [
            "raw_service_name",
            "row_count",
            "aligned_service_name_normalized",
            "aligned_service_name_canonical",
            "aligned_service_category",
            "alignment_confidence",
            "needs_manual_review",
            "alignment_note",
        ]
    ]
    return grouped.sort_values(
        by=["needs_manual_review", "row_count", "raw_service_name"],
        ascending=[False, False, True],
    )


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    aligned_original_output_path = Path(args.aligned_original_output)
    mapping_path = Path(args.mapping_output)
    review_path = Path(args.review_output)

    df = pd.read_csv(input_path, dtype=str).fillna("")
    original_columns = list(df.columns)
    if "service_name" not in df.columns:
        raise KeyError("Column 'service_name' not found in input CSV.")

    alignment_records = [
        infer_alignment(service_name, source_category)
        for service_name, source_category in zip(df["service_name"], df.get("service_category", ""))
    ]
    alignment_df = pd.DataFrame(alignment_records)
    result_df = pd.concat([df, alignment_df], axis=1)
    expanded_result_df = expand_multi_entity_rows(result_df)
    aligned_original_df = expanded_result_df[original_columns].copy()
    aligned_original_df["service_name"] = expanded_result_df["aligned_service_name_canonical"]
    mapping_df = build_mapping(expanded_result_df)
    review_df = mapping_df[mapping_df["needs_manual_review"] == "TRUE"].copy()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    aligned_original_output_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.parent.mkdir(parents=True, exist_ok=True)

    expanded_result_df.to_csv(output_path, index=False, encoding="utf-8-sig")
    aligned_original_df.to_csv(aligned_original_output_path, index=False, encoding="utf-8-sig")
    mapping_df.to_csv(mapping_path, index=False, encoding="utf-8-sig")
    review_df.to_csv(review_path, index=False, encoding="utf-8-sig")

    category_counts = Counter(result_df["aligned_service_category"])
    confidence_counts = Counter(result_df["alignment_confidence"])

    print(f"wrote preview csv: {output_path}")
    print(f"wrote aligned original csv: {aligned_original_output_path}")
    print(f"wrote mapping csv: {mapping_path}")
    print(f"wrote manual review csv: {review_path}")
    print(f"rows before expansion: {len(result_df)}")
    print(f"rows after expansion: {len(expanded_result_df)}")
    print(f"unique raw service_name: {result_df['service_name'].nunique()}")
    print(f"unique normalized service_name: {result_df['aligned_service_name_normalized'].nunique()}")
    print(f"unique canonical entities: {result_df['aligned_service_name_canonical'].nunique()}")
    print("confidence counts:")
    for label, count in sorted(confidence_counts.items()):
        print(f"  {label}: {count}")
    print("category counts:")
    for label, count in sorted(category_counts.items()):
        print(f"  {label}: {count}")
    print(f"manual review names: {len(review_df)}")


if __name__ == "__main__":
    main()
