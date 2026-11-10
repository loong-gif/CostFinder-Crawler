from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from utils.crawler_visibility_cascade import Cascade
from utils.crawler_visibility_media import length_px, significant, split_commas
from utils.crawler_visibility_rules import keyword_valid

INHERITED = frozenset({"visibility", "font-size", "text-indent"})
KEYWORD_PROPERTIES = frozenset({"display", "visibility", "content-visibility", "position", "overflow-x", "overflow-y"})
LENGTH_PROPERTIES = frozenset({"width", "height", "max-width", "max-height", "left", "right", "top"})
INITIAL: Dict[str, object] = {
    "display": "inline", "visibility": "visible", "opacity": 1.0, "content-visibility": "visible", "clip": False,
    "clip-path": False, "overflow-x": "visible", "overflow-y": "visible", "position": "static", "text-indent": 0.0,
    "font-size": False, **{name: None for name in LENGTH_PROPERTIES},
}
RELATIVE_FONT_UNITS = frozenset({"em", "ex", "ch", "cap", "ic", "lh"})
# 不渲染的元素；以及渲染但其中文字不属于页面正文的元素
NOT_RENDERED_TAGS = frozenset({"head", "script", "style", "template", "noscript", "title"})
TEXTLESS_TAGS = frozenset({"svg", "iframe", "object", "canvas", "select", "option", "datalist", "video", "audio"})
OFFSCREEN_PX = -100.0
# 不透明度低于 1% 的内容肉眼不可见（淡入动画常用 0.0001 作为初始值）
INVISIBLE_OPACITY = 0.01
VALUE_UNDECIDABLE = "css_value_undecidable"


@dataclass
class Box:
    values: Dict[str, object]
    custom: Dict[str, Tuple] = field(default_factory=dict)
    hidden: bool = False
    text_hidden: bool = False
    textless: bool = False
    details_closed: bool = False


ROOT = Box(dict(INITIAL))


def substitute(tokens: Sequence, custom: Dict[str, Tuple], visiting: FrozenSet[str] = frozenset()) -> Optional[List]:
    """替换顶层 var()；引用不存在且没有后备值、或引用成环时整个值无效，返回 None。"""
    out: List = []
    for token in tokens:
        if not (token.type == "function" and token.lower_name == "var"):
            out.append(token)
            continue
        name, fallback = _var_reference(token)
        resolved = None if name in visiting else _resolve_reference(name, fallback, custom, visiting)
        if resolved is None:
            return None
        out.extend(resolved)
    return out


def _var_reference(token) -> Tuple[str, Optional[Tuple]]:
    """var(--name, 后备值) 的（名称，后备值）；没有后备值时为 None。"""
    parts = split_commas(token.arguments)
    names = [t.value for t in significant(parts[0]) if t.type == "ident"] or [""]
    fallback = tuple(token.arguments)[len(parts[0]) + 1:] if len(parts) > 1 else None
    return names[0], fallback


def _resolve_reference(name: str, fallback: Optional[Tuple], custom: Dict[str, Tuple],
                       visiting: FrozenSet[str]) -> Optional[List]:
    # 自定义属性不存在或无效时取后备值
    value = custom.get(name)
    resolved = None if value is None else substitute(value, custom, visiting | {name})
    return substitute(fallback, custom, visiting) if resolved is None and fallback is not None else resolved


def _font_zero(token, parent_zero: bool) -> bool:
    if token.type in ("number", "percentage", "dimension") and token.value == 0:
        return True
    relative = token.type == "percentage" or (token.type == "dimension" and token.lower_unit in RELATIVE_FONT_UNITS)
    return relative and parent_zero


def _clip_rect_empty(token) -> bool:
    if not (token.type == "function" and token.lower_name == "rect"):
        return False
    # rect() 参数中的符号只有分隔用的逗号
    edges = [length_px(t) for t in significant(token.arguments) if t.type != "literal"]
    if len(edges) != 4 or None in edges:
        return False
    top, right, bottom, left = edges
    return right - left <= 1 or bottom - top <= 1


def _inset_half(token) -> bool:
    if not (token.type == "function" and token.lower_name == "inset"):
        return False
    first = significant(token.arguments)[:1]
    return bool(first) and first[0].type == "percentage" and first[0].value >= 50


def _opacity(token, parent: object) -> float:
    if token.type == "percentage":
        return token.value / 100
    return float(token.value) if token.type == "number" else 1.0


SINGLE_VALUE_INTERPRETERS = {
    **{name: lambda token, parent: length_px(token) for name in LENGTH_PROPERTIES},
    "opacity": _opacity,
    "text-indent": lambda token, parent: length_px(token) or 0.0,
    "font-size": lambda token, parent: _font_zero(token, bool(parent)),
    "clip": lambda token, parent: _clip_rect_empty(token),
    "clip-path": lambda token, parent: _inset_half(token),
}


def interpret(name: str, tokens: List, parent: object) -> object:
    parts = significant(tokens)
    if name in KEYWORD_PROPERTIES and parts:
        return parts[0].lower_value  # 关键字已校验，display 的多关键字写法取第一个
    return SINGLE_VALUE_INTERPRETERS[name](parts[0], parent) if len(parts) == 1 else INITIAL[name]


def _resolved(name: str, declared: Optional[Tuple], custom: Dict[str, Tuple]) -> Optional[List]:
    tokens = None if declared is None else substitute(declared, custom)
    # 替换 var() 后无效的值按 unset 处理
    return tokens if tokens is not None and keyword_valid(name, tokens) else None


def _single_keyword(tokens: Sequence) -> str:
    parts = significant(tokens)
    return parts[0].lower_value if len(parts) == 1 and parts[0].type == "ident" else ""


def computed_value(name: str, declared: Optional[Tuple], parent: Box, custom: Dict[str, Tuple],
                   reasons: Set[str]) -> object:
    tokens = _resolved(name, declared, custom)
    keyword = _single_keyword(tokens or ())
    if tokens is None or keyword == "unset":
        return parent.values[name] if name in INHERITED else INITIAL[name]
    if keyword == "inherit":
        return parent.values[name]
    if keyword in ("revert", "revert-layer"):
        reasons.add(VALUE_UNDECIDABLE)
        return INITIAL[name]
    return INITIAL[name] if keyword == "initial" else interpret(name, tokens, parent.values[name])


def _offscreen(values: Dict[str, object]) -> bool:
    offsets = [values[name] for name in ("left", "right", "top")]
    return values["position"] != "static" and any(v is not None and v <= OFFSCREEN_PX for v in offsets)


def _clipped_to_nothing(values: Dict[str, object]) -> bool:
    clipping = values["overflow-x"] != "visible" or values["overflow-y"] != "visible"
    sizes = [values[name] for name in ("width", "height", "max-width", "max-height")]
    return clipping and any(v is not None and v <= 1 for v in sizes)


def hides_subtree(values: Dict[str, object]) -> bool:
    return (values["display"] == "none" or values["opacity"] < INVISIBLE_OPACITY
            or values["content-visibility"] == "hidden" or bool(values["clip-path"])
            or (values["position"] in ("absolute", "fixed") and bool(values["clip"]))
            or _offscreen(values) or _clipped_to_nothing(values))


def hides_text(values: Dict[str, object]) -> bool:
    return values["visibility"] in ("hidden", "collapse") or bool(values["font-size"]) or (
        values["text-indent"] <= OFFSCREEN_PX)


def _custom(declared: Dict[str, Tuple], parent: Box) -> Dict[str, Tuple]:
    own = {name: value for name, value in declared.items() if name.startswith("--")}
    if not own:
        return parent.custom
    merged = {**parent.custom, **own}
    resolved = {name: substitute(value, merged, frozenset({name})) for name, value in own.items()}
    merged.update({name: tuple(value) for name, value in resolved.items() if value is not None})
    for name in [name for name, value in resolved.items() if value is None]:
        merged.pop(name)
    return merged


def compute_boxes(cascade: Cascade) -> Tuple[Dict[int, Box], Set[str]]:
    boxes: Dict[int, Box] = {}
    reasons: Set[str] = set()
    for element in cascade.index.elements:
        parent = boxes.get(id(element.parent), ROOT)
        declared = cascade.declared(element)
        custom = _custom(declared, parent)
        values = {name: computed_value(name, declared.get(name), parent, custom, reasons) for name in INITIAL}
        folded = parent.details_closed and not (
            element.name == "summary" and element is element.parent.find("summary", recursive=False))
        boxes[id(element)] = Box(
            values, custom,
            hidden=parent.hidden or folded or element.name in NOT_RENDERED_TAGS or hides_subtree(values),
            text_hidden=hides_text(values), textless=parent.textless or element.name in TEXTLESS_TAGS,
            details_closed=element.name == "details" and not element.has_attr("open"),
        )
    return boxes, reasons
