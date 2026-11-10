from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Set, Tuple

import tinycss2

from utils.crawler_visibility_rules import (
    AUTHOR_ORIGIN, SELECTOR_UNDECIDABLE, UNLAYERED, Declaration, Rule, declarations,
)
from utils.crawler_visibility_selectors import IndexKey, SelectorPart, compile_selector

Key = Tuple


class ElementIndex:
    def __init__(self, soup) -> None:
        self.elements = soup.find_all()
        self.groups: Dict[IndexKey, List] = defaultdict(list)
        for element in self.elements:
            self.groups["tag", element.name].append(element)
            if element.get("id"):
                self.groups["id", element["id"]].append(element)
            for name in element.get("class", []):
                self.groups["class", name].append(element)

    def candidates(self, part: SelectorPart) -> List:
        if any(name not in self.groups for name in part.required):
            return []
        return self.elements if part.key[0] == "all" else self.groups.get(part.key, [])

    def matches(self, part: SelectorPart) -> List:
        return [element for element in self.candidates(part) if part.matcher.match(element)]


def cascade_key(decl: Declaration, origin: int, inline: int, layer: Tuple[float, ...], specificity: Tuple,
                order: Tuple[int, ...]) -> Key:
    """层叠排序键：重要性与来源、行内样式、层、选择器权重、出现顺序。

    重要声明中层的先后反转；浏览器默认样式这里没有重要声明，来源的先后不必反转。
    """
    if decl.important:
        return 1, origin, inline, tuple(-value for value in layer), specificity, order
    return 0, origin, inline, layer, specificity, order


def _decided(rule: Rule, parts: Tuple[SelectorPart, ...]) -> bool:
    compiled = all(part.matcher is not None and part.specificity is not None for part in parts)
    return compiled and not rule.undecidable and not rule.nested


class Cascade:
    """每个元素每个相关属性胜出的声明值。"""

    def __init__(self, soup, rules: List[Rule]) -> None:
        self.index = ElementIndex(soup)
        self.winners: Dict[int, Dict[str, Tuple[Key, Tuple]]] = defaultdict(dict)
        self.reasons: Set[str] = set()
        for rule in rules:
            self._apply_rule(rule)
        for element in self.index.elements:
            if element.get("style"):
                self._apply_inline(element)

    def _offer(self, element, decl: Declaration, key: Key) -> None:
        offered = (key, decl.value)
        current = self.winners[id(element)].get(decl.name, offered)
        self.winners[id(element)][decl.name] = max(current, offered, key=lambda entry: entry[0])

    def _affects_page(self, parts: Tuple[SelectorPart, ...]) -> bool:
        # 无法编译的选择器只能按顶层类名与 id 是否都在文档中判断可能匹配
        return any(self.index.candidates(part) if part.matcher is None else self.index.matches(part) for part in parts)

    def _apply_rule(self, rule: Rule) -> None:
        """规则作用于本页元素却无法判定（条件、嵌套规则、无法编译或权重未知的选择器）时页面无法判定。"""
        parts = compile_selector(rule.selector)
        if not _decided(rule, parts):
            if self._affects_page(parts):
                self.reasons.add(rule.undecidable or SELECTOR_UNDECIDABLE)
            return
        for part in parts:
            for element in self.index.matches(part):
                for index, decl in enumerate(rule.declarations):
                    key = cascade_key(decl, rule.origin, 0, rule.layer, part.specificity, (rule.order, index))
                    self._offer(element, decl, key)

    def _apply_inline(self, element) -> None:
        found, _ = declarations(tinycss2.parse_component_value_list(element["style"]))
        for index, decl in enumerate(found):
            self._offer(element, decl, cascade_key(decl, AUTHOR_ORIGIN, 1, (UNLAYERED,), (), (index,)))

    def declared(self, element) -> Dict[str, Tuple]:
        return {name: value for name, (_, value) in self.winners.get(id(element), {}).items()}
