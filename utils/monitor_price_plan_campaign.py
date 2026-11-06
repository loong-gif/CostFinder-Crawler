from __future__ import annotations

from datetime import date
import re
from typing import Any

from utils.monitor_price_plan_common import _Review, _text


def _date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _Review("invalid_campaign_date")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise _Review("invalid_campaign_date") from None


def _campaign(offer: dict, evidence: dict) -> dict:
    fields = {
        "promotion_title": offer.get("campaign_title", evidence.get("promotion_title")),
        "campaign_start_date": offer.get("campaign_start_date", evidence.get("promotion_start_date", evidence.get("campaign_start_date"))),
        "campaign_end_date": offer.get("campaign_end_date", evidence.get("promotion_end_date", evidence.get("campaign_end_date"))),
    }
    fields["promotion_title"] = str(fields["promotion_title"] or "").strip() or None
    for key in ("campaign_start_date", "campaign_end_date"):
        if isinstance(fields[key], str) and not re.search(r"\d{4}", fields[key]):
            # Yearless model output (e.g. "October 31" or "10/31") is
            # unknown, not an invalid-date reason to quarantine the offer.
            short = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2})", fields[key].strip())
            fields[key] = (f"20{short[3]}-{int(short[1]):02d}-{int(short[2]):02d}" if short else None)
        fields[key] = _date(fields[key])
        value = fields[key]
        if value:
            parsed = date.fromisoformat(value)
            month = rf"(?:{parsed.month}|{parsed.month:02d})"
            day = rf"(?:{parsed.day}|{parsed.day:02d})"
            month_name = parsed.strftime("%B")
            named = rf"(?:{month_name}|{month_name[:3]}\.?)"
            patterns = [re.escape(value), rf"{month}[-/]{day}[-/]{parsed.year}",
                        rf"{named}\s*{day}(?:st|nd|rd|th)?\s*,?\s+{parsed.year}",
                        rf"{day}(?:st|nd|rd|th)?\s+{named}\s*,?\s+{parsed.year}"]
            if 2000 <= parsed.year <= 2099:
                patterns.append(rf"{month}[-/]{day}[-/]{parsed.year % 100:02d}")
            text = str(evidence.get("date_text", evidence.get("markdown")) or "")
            if not any(re.search(rf"(?<!\w){pattern}(?!\w)", text, re.I) for pattern in patterns):
                fields[key] = None
    if fields["campaign_start_date"] and fields["campaign_end_date"] and fields["campaign_start_date"] > fields["campaign_end_date"]:
        raise _Review("invalid_campaign_date_range")
    return fields


def _campaign_key(row: dict) -> tuple:
    return (_text(row.get("promotion_title")), row.get("campaign_start_date"), row.get("campaign_end_date"))
