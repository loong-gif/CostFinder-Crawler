from __future__ import annotations

import functools
import re
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

import tinycss2

# 1440 宽的桌面屏幕上 em/rem 按浏览器默认字号 16px 计算
FONT_PX = 16.0
MATCHING_MEDIA_TYPES = frozenset({"all", "screen"})
# ego-lite 桌面渲染的环境：鼠标指针、设备像素比 1、未开启减弱动效与强制配色、脚本可用
DISCRETE_FEATURES = {"hover": "hover", "any-hover": "hover", "pointer": "fine", "any-pointer": "fine",
                     "prefers-reduced-motion": "no-preference", "forced-colors": "none", "scripting": "enabled"}
FALSY_VALUES = frozenset({"none", "no-preference"})
DEVICE_PIXEL_RATIO = 1.0
RANGE_PREFIXES = {"min-": ">=", "max-": "<=", "": "="}
FEATURE_NAME = re.compile(r"(?:-webkit-)?(min-|max-)?(.+)")
MALFORMED = "malformed"
COMPARE = {
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b, "=": lambda a, b: a == b,
}


def significant(tokens: Iterable) -> List:
    return [t for t in tokens if t.type not in ("whitespace", "comment")]


def split_commas(tokens: Sequence) -> List[List]:
    parts: List[List] = [[]]
    for token in tokens:
        if token.type == "literal" and token.value == ",":
            parts.append([])
        else:
            parts[-1].append(token)
    return parts


def _all_true(results: Sequence[Optional[bool]]) -> Optional[bool]:
    if False in results:
        return False
    return None if None in results else True


def _any_true(results: Sequence[Optional[bool]]) -> Optional[bool]:
    if True in results:
        return True
    return None if None in results else False


def length_px(token) -> Optional[float]:
    """长度记号换算为 px；无法换算时返回 None。"""
    if token.type == "number" and token.value == 0:
        return 0.0
    if token.type != "dimension":
        return None
    unit = token.lower_unit
    if unit == "px":
        return float(token.value)
    return float(token.value) * FONT_PX if unit in ("em", "rem") else None


def _dppx(token) -> Optional[float]:
    if token.type != "dimension":
        return None
    factor = {"dppx": 1.0, "x": 1.0, "dpi": 1 / 96, "dpcm": 2.54 / 96}.get(token.lower_unit)
    return None if factor is None else token.value * factor


def _numeric(base: str, token, width: float) -> Optional[Tuple[float, Optional[float]]]:
    """（屏幕上的实际值，条件中的值）；不认识的特性返回 None。"""
    if base in ("width", "device-width"):
        return width, length_px(token)
    if base == "resolution":
        return DEVICE_PIXEL_RATIO, _dppx(token)
    if base == "device-pixel-ratio":
        return DEVICE_PIXEL_RATIO, float(token.value) if token.type == "number" else None
    return None


def _is_ident(token, *names: str) -> bool:
    return token.type == "ident" and token.lower_value in names


def _range_items(tokens: Sequence) -> Optional[List]:
    # 区间写法 (width >= 48rem)、(400px < width <= 800px)：操作数与比较符交替出现；其他符号在比较时被拒绝
    items: List = []
    for token in tokens:
        if token.type == "literal":
            if items and items[-1] in ("<", ">") and token.value == "=":
                items[-1] += "="
            else:
                items.append(token.value)
        elif _is_ident(token, "width", "device-width"):
            items.append(None)
        else:
            px = length_px(token)
            if px is None:
                return None
            items.append(px)
    return items


def _range_feature(tokens: Sequence, width: float) -> Optional[bool]:
    items = _range_items(tokens)
    if not items or len(items) not in (3, 5) or items.count(None) != 1:
        return None
    operands, operators = items[0::2], items[1::2]
    if any(op not in COMPARE for op in operators) or any(isinstance(value, str) for value in operands):
        return None
    values = [width if value is None else value for value in operands]
    return all(COMPARE[op](values[i], values[i + 1]) for i, op in enumerate(operators))


def _feature(tokens: Sequence, width: float) -> Optional[bool]:
    if len(tokens) == 1 and tokens[0].type == "ident":
        expected = DISCRETE_FEATURES.get(tokens[0].lower_value)
        return None if expected is None else expected not in FALSY_VALUES
    if not (len(tokens) == 3 and tokens[0].type == "ident" and tokens[1].type == "literal" and tokens[1].value == ":"):
        return _range_feature(tokens, width)
    return _name_value_feature(tokens[0].lower_value, tokens[2], width)


def _name_value_feature(name: str, value, width: float) -> Optional[bool]:
    if name in DISCRETE_FEATURES:
        return value.type == "ident" and value.lower_value == DISCRETE_FEATURES[name]
    prefix, base = FEATURE_NAME.fullmatch(name).groups()
    numeric = _numeric(base, value, width)
    if numeric is None or numeric[1] is None:
        return None
    return COMPARE[RANGE_PREFIXES[prefix or ""]](*numeric)


def _in_parens(token, feature: Callable):
    if token.type == "function":
        return None  # 写法合法但含义未知的函数
    if token.type != "() block":
        return MALFORMED
    inner = significant(token.content)
    if inner and (inner[0].type == "() block" or _is_ident(inner[0], "not")):
        return _condition(inner, feature)
    return feature(inner)


def _negated(words: Sequence, feature: Callable):
    inner = _in_parens(words[1], feature) if len(words) == 2 else MALFORMED
    return inner if inner is None or inner is MALFORMED else not inner


def _condition(words: Sequence, feature: Callable):
    """条件成立返回 True/False，无法判定返回 None，语法错误返回 MALFORMED。"""
    if words and _is_ident(words[0], "not"):
        return _negated(words, feature)
    connectors = words[1::2]
    if len(words) % 2 == 0 or not all(_is_ident(word, "and", "or") for word in connectors):
        return MALFORMED
    joiners = {word.lower_value for word in connectors}
    results = [_in_parens(word, feature) for word in words[0::2]]
    if len(joiners) > 1 or MALFORMED in results:
        return MALFORMED
    return _any_true(results) if joiners == {"or"} else _all_true(results)


def _query(tokens: Sequence, width: float) -> Optional[bool]:
    # 语法错误的查询按规范视为 not all，不成立
    negate, words = _media_prefix(significant(tokens))
    feature = functools.partial(_feature, width=width)
    typed = bool(words) and words[0].type == "ident" and not _is_ident(words[0], "not")
    matched = _typed_query(words, feature) if typed else _condition(words, feature)
    if matched is MALFORMED:
        return False
    return (not matched) if negate and matched is not None else matched


def _media_prefix(words: Sequence) -> Tuple[bool, Sequence]:
    """媒体类型前的 not / only：返回（是否取反，其余部分）。"""
    prefixed = len(words) > 1 and words[1].type == "ident" and _is_ident(words[0], "not", "only")
    return prefixed and words[0].lower_value == "not", words[1:] if prefixed else words


def _typed_query(words: Sequence, feature: Callable):
    """以媒体类型开头的查询：类型之后只能用 and 连接条件。"""
    matched: Optional[bool] = words[0].lower_value in MATCHING_MEDIA_TYPES
    rest = words[1:]
    if not rest:
        return matched
    valid = _is_ident(rest[0], "and") and not any(_is_ident(w, "or") for w in rest)
    condition = _condition(rest[1:], feature) if valid else MALFORMED
    return MALFORMED if condition is MALFORMED else _all_true([matched, condition])


def media_matches(tokens: Sequence, width: float) -> Optional[bool]:
    """媒体查询列表在桌面渲染环境（给定屏幕宽度）下是否成立；无法判定时返回 None。"""
    if not significant(tokens):
        return True
    results = [_query(query, width) for query in split_commas(tokens)]
    if True in results:
        return True
    return False if all(result is False for result in results) else None


def _supports_feature(tokens: Sequence) -> Optional[bool]:
    # 自定义属性声明在 Chrome 中总是受支持；其他声明与 selector() 的支持情况无法判定
    custom = len(tokens) >= 2 and tokens[0].type == "ident" and tokens[0].value.startswith("--")
    return True if custom and tokens[1].type == "literal" and tokens[1].value == ":" else None


def supports_matches(tokens: Sequence) -> Optional[bool]:
    """@supports 条件在 Chrome 中是否成立；无法判定时返回 None，语法错误时不成立。"""
    result = _condition(significant(tokens), _supports_feature)
    return False if result is MALFORMED else result


def media_text_matches(text: str, width: float) -> Optional[bool]:
    return media_matches(tinycss2.parse_component_value_list(text or ""), width)
