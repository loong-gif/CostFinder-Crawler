"""促销写入规则；正则与日期口径同数据库迁移 20261103043251_promotion_identity_guards.sql 保持一致。"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from dateutil import parser as dateutil_parser

from utils.source_url import HUB_SEGMENT_PATTERN, hub_path, split_url

# 明确折扣证据：百分比/金额 off、save/discount/BOGO/free（免费咨询除外）、原价对比、删除线价格、买 X 送 Y
DISCOUNT_EVIDENCE_PATTERN = (
    r"([0-9]{1,3}(\.[0-9]+)? ?%|\$ ?[0-9][0-9,]*(\.[0-9]+)?) ?off([^a-z]|$)"
    r"|(^|[^a-z])(save|saves|saving|savings|discount|discounted|bogo|free(?! consult)|half off|half price"
    r"|special price|sale price|promo price|promotional price|limited time)([^a-z]|$)"
    r"|(^|[^a-z])(was|reg|regular|regularly|originally|retail|value|valued at|normally|usually|now)"
    r"[.: ]*(price[: ]*)?\$ ?[0-9]"
    r"|~~ ?\$ ?[0-9]"
    r"|(^|[^a-z])buy [0-9a-z]+[^.]{0,30}get([^a-z]|$)"
)
# 数据库以美国西海岸日期判定过期（最晚进入新一天的美国本土时区）
CAMPAIGN_TIMEZONE = "America/Los_Angeles"

_DISCOUNT_RE = re.compile(DISCOUNT_EVIDENCE_PATTERN, re.IGNORECASE)
_HUB_SEGMENT_RE = re.compile(HUB_SEGMENT_PATTERN, re.IGNORECASE)


class PromotionRejected(ValueError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def today() -> date:
    return datetime.now(ZoneInfo(CAMPAIGN_TIMEZONE)).date()


def is_promo_path(url: Any) -> bool:
    return any(_HUB_SEGMENT_RE.search(s) for s in split_url(url).path.split("/") if s)


def has_discount_evidence(texts: Iterable[str]) -> bool:
    return bool(_DISCOUNT_RE.search("\n".join(str(t or "") for t in texts)))


def _collapse(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def check_promotion(source_url: str, title: str, content: list[str], *, is_active: bool,
                    campaign_end_date: date | None) -> None:
    """写入前判定；与数据库触发器 fn_guard_clinic_promotion 同口径，违反即抛出 PromotionRejected。"""
    if hub_path(split_url(source_url).path) is not None:
        raise PromotionRejected("hub_child_url", source_url)
    if not _collapse(title):
        raise PromotionRejected("empty_title", source_url)
    if not is_active:
        return
    if campaign_end_date is not None and campaign_end_date < today():
        raise PromotionRejected("expired_campaign", campaign_end_date.isoformat())
    if not is_promo_path(source_url) and not has_discount_evidence([title, *content]):
        raise PromotionRejected("price_page_without_discount", source_url)


def parse_campaign_date(value: Any, *, reference: date | None = None) -> date | None:
    """解析活动日期；只有月日时取当年，两位年份按 dateutil 规则补全；无法解析时抛出 ParserError。"""
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    year_now = (reference or today()).year
    return dateutil_parser.parse(text, default=datetime(year_now, 1, 1)).date()
