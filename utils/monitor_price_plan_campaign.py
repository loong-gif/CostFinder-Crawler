from __future__ import annotations

import calendar
from datetime import date
import re
from typing import Any

from utils.monitor_price_plan_common import _Review, _text

_MONTHS = {name.casefold(): number for number, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.casefold(): number for number, name in enumerate(calendar.month_abbr) if name})


def _date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _Review("invalid_campaign_date")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise _Review("invalid_campaign_date") from None


def _yearless(value: str, year: int) -> date | None:
    """没有年份的日期（"October 31"、"10/31"、只有月份的 "July"）按观察当年解释；只有月份时取当月最后一天。"""
    text = value.strip().rstrip(".").casefold()
    numeric = re.fullmatch(r"(\d{1,2})/(\d{1,2})", text)
    named = re.fullmatch(r"([a-z]+)\.?\s*(\d{1,2})?(?:st|nd|rd|th)?", text)
    try:
        if numeric:
            return date(year, int(numeric[1]), int(numeric[2]))
        if named and named[1] in _MONTHS:
            month = _MONTHS[named[1]]
            return date(year, month, int(named[2]) if named[2] else calendar.monthrange(year, month)[1])
    except ValueError:
        return None
    return None


def _on_page(value: date, text: str, yearless: bool) -> bool:
    month = rf"(?:{value.month}|{value.month:02d})"
    day = rf"(?:{value.day}|{value.day:02d})"
    month_name = value.strftime("%B")
    named = rf"(?:{month_name}|{month_name[:3]}\.?)"
    if yearless:
        patterns = [rf"{named}\s*{day}(?:st|nd|rd|th)?", rf"{day}(?:st|nd|rd|th)?\s+{named}", rf"{month}/{day}",
                    named]
    else:
        patterns = [re.escape(value.isoformat()), rf"{month}[-/]{day}[-/]{value.year}",
                    rf"{named}\s*{day}(?:st|nd|rd|th)?\s*,?\s+{value.year}",
                    rf"{day}(?:st|nd|rd|th)?\s+{named}\s*,?\s+{value.year}"]
        if 2000 <= value.year <= 2099:
            patterns.append(rf"{month}[-/]{day}[-/]{value.year % 100:02d}")
    return any(re.search(rf"(?<!\w){pattern}(?!\w)", text, re.I) for pattern in patterns)


def _campaign(offer: dict, evidence: dict) -> dict:
    fields = {
        "promotion_title": offer.get("campaign_title", evidence.get("promotion_title")),
        "campaign_start_date": offer.get("campaign_start_date", evidence.get("promotion_start_date", evidence.get("campaign_start_date"))),
        "campaign_end_date": offer.get("campaign_end_date", evidence.get("promotion_end_date", evidence.get("campaign_end_date"))),
    }
    fields["promotion_title"] = str(fields["promotion_title"] or "").strip() or None
    observed = evidence.get("observed_date")
    year = date.fromisoformat(observed).year if isinstance(observed, str) and observed else date.today().year
    text = str(evidence.get("date_text", evidence.get("markdown")) or "")
    for key in ("campaign_start_date", "campaign_end_date"):
        raw = fields[key]
        yearless = isinstance(raw, str) and not re.search(r"\d{4}", raw)
        if yearless:
            short = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2})", raw.strip())
            parsed = date(2000 + int(short[3]), int(short[1]), int(short[2])) if short else _yearless(raw, year)
            yearless = not short
        else:
            try:
                parsed = date.fromisoformat(_date(raw)) if raw else None
            except _Review:
                # 读不出的日期存 NULL，不阻断更新
                parsed = None
        # 页面上写的是没有年份的日期时，按观察当年对应
        page_yearless = parsed is not None and not yearless and parsed.year == year and not _on_page(parsed, text, False)
        fields[key] = (parsed.isoformat() if parsed and (_on_page(parsed, text, yearless)
                                                        or page_yearless and _on_page(parsed, text, True)) else None)
    if fields["campaign_start_date"] and fields["campaign_end_date"] and fields["campaign_start_date"] > fields["campaign_end_date"]:
        # 起止颠倒的日期无法判断哪一个错了，都存 NULL
        fields["campaign_start_date"] = fields["campaign_end_date"] = None
    return fields


def _campaign_key(row: dict) -> tuple:
    return (_text(row.get("promotion_title")), row.get("campaign_start_date"), row.get("campaign_end_date"))
