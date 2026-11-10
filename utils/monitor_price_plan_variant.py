from __future__ import annotations

import re


_UNIT_IN_TEXT = re.compile(r"\b(\d+)\s*(units?|sessions?|syringes?|treatments?)\b", re.I)


def _variant_reconciliation_records(evidence: dict) -> list[dict]:
    recon = evidence.get("product_variant_reconciliation") or []
    if recon:
        return recon
    if evidence.get("variant_expected") is not None:
        return [{"variant_expected": evidence.get("variant_expected", 0),
                 "variant_skipped": evidence.get("variant_skipped") or [],
                 "variant_capture_count": len(evidence.get("variant_captures") or [])}]
    return []


def _variant_catalog_complete(evidence: dict) -> bool:
    records = _variant_reconciliation_records(evidence)
    if records:
        return all((rec.get("variant_capture_count") or 0) + len(rec.get("variant_skipped") or [])
                   == (rec.get("variant_expected") or 0) for rec in records if rec.get("variant_expected"))
    if evidence.get("variant_expected"):
        return (len(evidence.get("variant_captures") or []) + len(evidence.get("variant_skipped") or [])
                == evidence["variant_expected"])
    return evidence.get("variant_capture_complete") is True


def _check_variant_reconciliation(evidence: dict, raw_offers: list, warning) -> None:
    for rec in _variant_reconciliation_records(evidence):
        expected = int(rec.get("variant_expected") or 0)
        if expected <= 0:
            continue
        skipped = rec.get("variant_skipped") or []
        captured = int(rec.get("variant_capture_count") or 0)
        if captured + len(skipped) != expected:
            warning("variant_reconciliation_mismatch", None)
    purchasable = sum(int(rec.get("variant_capture_count") or 0) for rec in _variant_reconciliation_records(evidence))
    if not purchasable:
        purchasable = len(evidence.get("variant_captures") or [])
    variant_offers = [offer for offer in raw_offers if offer.get("purchase_variant")]
    if purchasable and len(variant_offers) != purchasable:
        warning("variant_reconciliation_mismatch", None)
    for index, raw in enumerate(raw_offers):
        if not raw.get("purchase_variant"):
            continue
        blob = " ".join([str(raw.get("offer_raw_text") or ""), *(
            c if isinstance(c, str) else str(c.get("requirement", "")) for c in (raw.get("conditions") or []))])
        if not _UNIT_IN_TEXT.search(blob):
            continue
        for item in raw.get("items") or []:
            delivered = item.get("delivered_units", item.get("quantity"))
            if delivered is None or delivered == 1:
                warning("variant_reconciliation_mismatch", index)
