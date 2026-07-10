"""Skincare / retail catalog persistence helpers."""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from utils.membership_plan_lookup import normalize_plan_name
from utils.retail_paths import is_retail_catalog_url

_PRICE_RE = re.compile(r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)")


def _parse_fee(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


@lru_cache(maxsize=1)
def _treatment_service_names() -> frozenset[str]:
    from utils.offer_extraction_llm import get_standardized_service_names

    skip = {"Skincare Product", "Package", "Others", "Membership", "Free Consultation", "Consultation"}
    return frozenset(name for name in get_standardized_service_names() if name not in skip)


def is_skincare_product_offer(offer: Dict[str, Any]) -> bool:
    service = str(offer.get("service_name") or "").strip()
    if service == "Skincare Product":
        return True
    source_url = str(offer.get("source_url") or "")
    if not is_retail_catalog_url(source_url):
        return False
    if service in _treatment_service_names():
        return False
    if service in {"", "Others", "detected", "Package"}:
        return False
    return bool(service or str(offer.get("offer_raw_text") or "").strip())


def infer_product_name(offer: Dict[str, Any]) -> str:
    for field in ("display_service_name", "raw_service_name"):
        value = str(offer.get(field) or "").strip()
        if value and value.lower() not in {"skincare product", "others"}:
            return value
    service = str(offer.get("service_name") or "").strip()
    if service and service not in {"Skincare Product", "Others", "detected"}:
        return service
    raw = str(offer.get("offer_raw_text") or "").strip()
    cleaned = _PRICE_RE.sub("", raw).strip(" -–|/")
    return cleaned or service or "Unknown Product"


def infer_product_prices(offer: Dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    sale = _parse_fee(offer.get("discount_price"))
    regular = _parse_fee(offer.get("original_price") or offer.get("regular_price"))
    raw = str(offer.get("offer_raw_text") or "")
    prices = [_parse_fee(match.group(1)) for match in _PRICE_RE.finditer(raw)]
    prices = [price for price in prices if price is not None]
    if sale is None and prices:
        sale = prices[-1]
    if regular is None and len(prices) >= 2:
        regular = prices[0]
    return regular, sale


def staging_context_from_offer(
    offer: Dict[str, Any],
    staging_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    source_url = str(offer.get("source_url") or "").strip()
    if staging_row:
        return {
            "domain_name": str(staging_row.get("domain_name") or offer.get("source_name") or "").strip().lower(),
            "subpage_url": str(staging_row.get("subpage_url") or source_url).strip(),
            "promo_website_id": staging_row.get("promo_website_id"),
            "business_id": staging_row.get("business_id") if staging_row.get("business_id") is not None else offer.get("business_id"),
        }
    domain_name = str(offer.get("source_name") or "").strip().lower()
    if not domain_name and source_url:
        domain_name = urlparse(source_url).netloc.replace("www.", "").lower()
    return {
        "domain_name": domain_name,
        "subpage_url": source_url,
        "business_id": offer.get("business_id"),
    }


def build_skincare_product_insert_row(
    offer: Dict[str, Any],
    staging_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ctx = staging_context_from_offer(offer, staging_row)
    regular, sale = infer_product_prices(offer)
    row: Dict[str, Any] = {
        "domain_name": ctx["domain_name"],
        "product_name": infer_product_name(offer),
        "source_url": ctx["subpage_url"],
        "offer_raw_text": str(offer.get("offer_raw_text") or "").strip() or None,
    }
    if ctx.get("promo_website_id") is not None:
        row["promo_website_id"] = ctx["promo_website_id"]
    if ctx.get("business_id") is not None:
        row["business_id"] = ctx["business_id"]
    if regular is not None:
        row["regular_price"] = regular
    if sale is not None:
        row["discount_price"] = sale
    elif regular is not None and "regular_price" in row:
        row["discount_price"] = regular
    return row


def find_existing_product_id(client: Any, source_url: str, product_name: str) -> Optional[int]:
    norm_url = str(source_url or "").strip().rstrip("/")
    name_norm = normalize_plan_name(product_name)
    if not norm_url or not name_norm:
        return None
    for url in (norm_url, f"{norm_url}/"):
        try:
            rows = client.fetch_rows(
                "promo_products_master",
                "product_id,product_name,source_url",
                filters={"source_url": f"eq.{url}"},
                limit=200,
            )
        except Exception:
            continue
        for row in rows:
            if normalize_plan_name(row.get("product_name")) == name_norm:
                return int(row["product_id"])
    return None
