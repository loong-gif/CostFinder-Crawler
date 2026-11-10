"""监控的页面可见性证据：ego-lite 渲染结果，或经 CSS 层叠过滤后判定完全可见的爬虫内容。

两种证据存为同一形状的字典（抓取证据的 ``rendered.browser``、事件目录的 ``browser.json``），由
``visibility_source`` 区分。爬虫证据只能由 ``CrawlerVisibility`` 生成：它没有截图、图块、逐宽度覆盖和商品选项截图，
也从不需要图片审查；含促销图片的页面不会被判定为干净，交给渲染。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from utils.monitor_rendered_browser import BREAKPOINT_NAMES, BREAKPOINTS

CRAWLER_SOURCE = "crawler_html_filtered"
BROWSER_SOURCE = "browser_render"
# 爬虫证据对应唯一的渲染宽度：层叠按这个宽度计算，增加渲染宽度时这里解包失败
((_, CRAWLER_WIDTH, _),) = BREAKPOINTS
# 只有浏览器渲染才有的内容
BROWSER_ONLY_FIELDS = frozenset({"screenshot_path", "image_tiles", "breakpoint_coverage", "frames", "visual_digest",
                                 "variant_captures", "product_details", "prior_visual_receipt"})


@dataclass(frozen=True)
class CrawlerVisibility:
    url: str
    final_url: str
    http_status: int
    title: str
    text: str
    context_digest: str
    captured_at: str

    def as_json(self) -> dict:
        return {**asdict(self), "visibility_source": CRAWLER_SOURCE, "accessible": True, "complete": True,
                "image_review_required": False, "failures": []}


def visibility_source(evidence: dict) -> str:
    """证据的来源；没有标记的是浏览器渲染（包括人工核验）。爬虫证据带有浏览器内容或图片审查时抛出 ValueError。"""
    source = evidence.get("visibility_source", BROWSER_SOURCE)
    if source == BROWSER_SOURCE:
        return source
    if source != CRAWLER_SOURCE:
        raise ValueError(f"unknown_visibility_source: {source}")
    if (any(evidence.get(name) for name in BROWSER_ONLY_FIELDS) or evidence.get("image_review_required") is not False
            or evidence.get("accessible") is not True or evidence.get("complete") is not True):
        raise ValueError("invalid_crawler_visibility")
    return source


def coverage_complete(evidence: dict) -> bool:
    """证据覆盖了全部渲染宽度：爬虫证据按唯一的渲染宽度计算；渲染证据要求每个宽度都完整。"""
    if visibility_source(evidence) == CRAWLER_SOURCE:
        return True
    coverage = evidence.get("breakpoint_coverage")
    return bool(coverage) and set(coverage) == BREAKPOINT_NAMES and all(
        row.get("complete") is True for row in coverage.values())
