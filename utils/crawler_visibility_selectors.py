from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from typing import FrozenSet, Optional, Sequence, Tuple

import cssselect
import soupsieve
import tinycss2
from cssselect.parser import Class, CombinedSelector, Element, Hash

from utils.crawler_visibility_media import split_commas

CHROME_REJECTED_PSEUDO = re.compile(r"::?-(?:moz|ms|o)-", re.I)
PSEUDO_ELEMENT = re.compile(r"::|:(?:before|after|first-line|first-letter)\b", re.I)
# Chrome 支持而 soupsieve 不认识的状态伪类：静止页面上都不成立，与 soupsieve 中从不匹配的 :hover 等价
STATE_PSEUDO = re.compile(r":(?:-webkit-autofill|autofill|modal|fullscreen|-webkit-full-screen|picture-in-picture)"
                          r"(?![\w-])", re.I)
ANY_LINK = re.compile(r":-webkit-any-link(?![\w-])", re.I)
IndexKey = Tuple[str, Optional[str]]


@dataclass(frozen=True)
class SelectorPart:
    """选择器列表中的一个选择器：matcher 为 None 表示 soupsieve 无法编译，specificity 为 None 表示权重未知。"""

    matcher: object
    specificity: Optional[Tuple[int, int, int]]
    key: IndexKey
    required: FrozenSet[IndexKey]


def _index_key(tree) -> IndexKey:
    # 最右侧复合选择器中的 id、类名或标签名，用于缩小候选元素范围
    node = tree.subselector if isinstance(tree, CombinedSelector) else tree
    while not isinstance(node, Element):
        if isinstance(node, Hash):
            return "id", node.id
        if isinstance(node, Class):
            return "class", node.class_name
        node = node.selector
    return ("tag", node.element.lower()) if node.element else ("all", None)


def _not_lists_as_is(tokens: Sequence) -> str:
    # :not(a, b) 与 :not(:is(a, b)) 权重相同；cssselect 只接受后一种写法
    out = []
    for token in tokens:
        if token.type != "function":
            out.append(tinycss2.serialize([token]))
            continue
        inner = _not_lists_as_is(token.arguments)
        if token.lower_name == "not":
            inner = f":is({inner})"
        out.append(f"{token.name}({inner})")
    return "".join(out)


def _specificity(tokens: Sequence) -> Tuple[Optional[Tuple[int, int, int]], IndexKey]:
    """（选择器权重，候选元素索引键）；cssselect 不支持的写法权重未知，匹配到元素时页面无法判定。"""
    try:
        parsed = cssselect.parse(_not_lists_as_is(tokens).strip())[0]
    except cssselect.SelectorError:
        return None, ("all", None)
    return parsed.specificity(), _index_key(parsed.parsed_tree)


def _required(tokens: Sequence) -> FrozenSet[IndexKey]:
    """选择器顶层出现的类名与 id：文档中缺少任意一个时该选择器不可能匹配。"""
    names = set()
    for previous, token in zip([None, *tokens], tokens):
        if token.type == "hash":
            names.add(("id", token.value))
        elif token.type == "ident" and previous is not None and previous.type == "literal" and previous.value == ".":
            names.add(("class", token.value))
    return frozenset(names)


@functools.lru_cache(maxsize=65536)
def compile_selector(selector: str) -> Tuple[SelectorPart, ...]:
    """把选择器列表拆成单个选择器并编译；伪元素选择器不作用于元素本身而被略过。"""
    if CHROME_REJECTED_PSEUDO.search(selector):
        return ()  # Chrome 不认识其他浏览器前缀的伪类，整条规则被丢弃
    parts = []
    for part in split_commas(tinycss2.parse_component_value_list(ANY_LINK.sub(":any-link", STATE_PSEUDO.sub(":hover", selector)))):
        text = tinycss2.serialize(part).strip()
        if PSEUDO_ELEMENT.search(text):
            continue
        try:
            matcher = soupsieve.compile(text)
        except soupsieve.SelectorSyntaxError:
            matcher = None  # 选择器无法编译：能否匹配无法判定（判定结论，非兜底）
        parts.append(SelectorPart(matcher, *_specificity(part), _required(part)))
    return tuple(parts)
