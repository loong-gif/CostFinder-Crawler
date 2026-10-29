"""促销/会员来源 URL 的身份规则；与数据库函数 fn_source_url_key / fn_promotion_hub_path 保持一致。

身份键 = 小写 host（去掉 www. 与默认端口）+ 小写 path（去掉末尾斜杠）+ 排序后的非跟踪 query 参数。
协议、fragment 不参与身份判定。
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

TRACKING_PARAM_PATTERN = r"^(utm_.*|fbclid|gclid|msclkid|mc_cid|mc_eid|_ga|_gl|hsa_.*)$"
# 路径段内出现促销关键词即视为 hub 段（与 promo_paths.PROMOTION_PATH_RE 同一组词）
HUB_SEGMENT_PATTERN = r"(^|[^a-z0-9])(specials?|promos?|promotions?|offers?|deals?)([^a-z0-9]|$)"

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_TRACKING_PARAM_RE = re.compile(TRACKING_PARAM_PATTERN, re.IGNORECASE)
_HUB_SEGMENT_RE = re.compile(HUB_SEGMENT_PATTERN, re.IGNORECASE)


def split_url(url: Any):
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("source_url is required")
    # 与 SQL 同口径：只有开头是 scheme:// 才视为带协议
    return urlsplit(raw if _SCHEME_RE.match(raw) else f"https://{raw}")


def source_url_key(url: Any) -> str:
    parts = split_url(url)
    host = parts.netloc.lower()
    host = re.sub(r"^www\.", "", host)
    host = re.sub(r":(80|443)$", "", host)
    path = parts.path.lower().rstrip("/")
    params = sorted(
        kv for kv in parts.query.split("&")
        if kv and not _TRACKING_PARAM_RE.match(kv.split("=", 1)[0])
    )
    return host + path + ("?" + "&".join(params) if params else "")


def hub_path(path: str) -> str | None:
    """返回最浅的促销 hub 前缀路径；路径本身就是最浅 hub 或不含 hub 段时返回 None。"""
    segments = [s for s in path.split("/") if s]
    for index, segment in enumerate(segments[:-1]):
        if _HUB_SEGMENT_RE.search(segment):
            return "/" + "/".join(segments[: index + 1])
    return None


def hub_url(url: Any) -> str | None:
    """子页面应合并到的 hub URL（保留原协议与 host，丢弃 query 与 fragment）。"""
    parts = split_url(url)
    parent = hub_path(parts.path)
    if parent is None:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parent, "", ""))
