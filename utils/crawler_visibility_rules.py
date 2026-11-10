from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

import tinycss2

from utils.crawler_visibility_media import (
    media_matches, media_text_matches, significant, split_commas, supports_matches,
)
from utils.crawler_visibility_sheets import document_sheets, parse_sheet, sheet_imports

# 与隐藏有关的属性；overflow 简写展开为两个方向
HIDING_PROPERTIES = frozenset({
    "display", "visibility", "opacity", "content-visibility", "clip", "clip-path", "width", "height", "max-width",
    "max-height", "overflow-x", "overflow-y", "position", "left", "right", "top", "text-indent", "font-size",
})
# 关键字属性中 Chrome 接受的值；不接受的声明在解析时即被丢弃，不参与层叠
DISPLAY_KEYWORDS = frozenset({
    "none", "contents", "block", "inline", "inline-block", "flex", "inline-flex", "grid", "inline-grid", "flow",
    "flow-root", "list-item", "table", "inline-table", "table-row-group", "table-header-group", "table-footer-group",
    "table-row", "table-cell", "table-column-group", "table-column", "table-caption", "ruby", "ruby-text", "run-in",
    "math", "-webkit-box", "-webkit-inline-box",
})
KEYWORD_VALUES = {
    "display": DISPLAY_KEYWORDS, "visibility": frozenset({"visible", "hidden", "collapse"}),
    "position": frozenset({"static", "relative", "absolute", "fixed", "sticky", "-webkit-sticky"}),
    "overflow-x": frozenset({"visible", "hidden", "clip", "scroll", "auto", "overlay"}),
    "overflow-y": frozenset({"visible", "hidden", "clip", "scroll", "auto", "overlay"}),
    "content-visibility": frozenset({"visible", "hidden", "auto"}),
}
GLOBAL_KEYWORDS = frozenset({"inherit", "initial", "unset", "revert", "revert-layer"})
UNLAYERED = float("inf")
UA_ORIGIN, AUTHOR_ORIGIN = 0, 1
# 浏览器默认样式中作者样式可以覆盖的隐藏规则
USER_AGENT_CSS = "[hidden]{display:none} dialog:not([open]){display:none} input[type=hidden i]{display:none}"
# 条件无法在静止页面上判定的块；@starting-style 只在过渡开始时生效，静止页面上不适用
UNDECIDABLE_BLOCKS = frozenset({"container", "scope"})
CONDITION_UNDECIDABLE = "css_condition_undecidable"
SELECTOR_UNDECIDABLE = "css_selector_undecidable"
STYLESHEET_UNAVAILABLE = "stylesheet_unavailable"


@dataclass(frozen=True)
class Declaration:
    name: str
    value: Tuple
    important: bool


@dataclass(frozen=True)
class Rule:
    selector: str
    declarations: Tuple[Declaration, ...]
    origin: int
    layer: Tuple[float, ...]
    order: int
    undecidable: Optional[str]
    nested: bool

def declarations(tokens: Sequence) -> Tuple[List[Declaration], bool]:
    """解析声明块，返回（相关声明，是否含嵌套规则）。"""
    found: List[Declaration] = []
    nested = False
    for item in tinycss2.parse_blocks_contents(tokens):
        if item.type in ("qualified-rule", "at-rule"):
            inner, inner_nested = declarations(item.content or ())
            nested = nested or bool(inner) or inner_nested
        elif item.type == "declaration":
            found.extend(_expand(item))
    return found, nested


def keyword_valid(name: str, tokens: Sequence) -> bool:
    """关键字属性的值是否为 Chrome 接受的关键字；display 可由多个关键字组成。"""
    parts = significant(tokens)
    if name not in KEYWORD_VALUES:
        return True
    if not parts or any(t.type != "ident" for t in parts):
        return False
    words = [t.lower_value for t in parts]
    if len(words) == 1:
        return words[0] in KEYWORD_VALUES[name] or words[0] in GLOBAL_KEYWORDS
    return name == "display" and len(words) <= 3 and all(word in DISPLAY_KEYWORDS for word in words)


def _expand(item) -> List[Declaration]:
    name, value = item.lower_name, tuple(item.value)
    if item.name.startswith("--"):
        return [Declaration(item.name, value, item.important)]
    if name == "overflow":
        parts = significant(value)
        y_value = (parts[1],) if len(parts) == 2 else value
        found = [Declaration("overflow-x", (parts[0],) if parts else value, item.important),
                 Declaration("overflow-y", y_value, item.important)]
    else:
        found = [Declaration(name, value, item.important)] if name in HIDING_PROPERTIES else []
    return [decl for decl in found if var_names(decl.value) or keyword_valid(decl.name, decl.value)]


class RuleCollector:
    """按层叠顺序收集作者样式与浏览器默认样式中的相关规则。"""

    def __init__(self, stylesheets: Mapping[str, Optional[str]], width: float) -> None:
        self.stylesheets = stylesheets
        self.width = width
        self.rules: List[Rule] = []
        self.reasons: Set[str] = set()
        self.layers: Dict[Tuple[str, ...], int] = {}

    def collect(self, soup, page_url: str) -> List[Rule]:
        self._walk(parse_sheet(USER_AGENT_CSS), UA_ORIGIN, False, ())
        for ref in document_sheets(soup, page_url):
            matched = media_text_matches(ref.media, self.width)
            if matched is False:
                continue
            if ref.url is None:
                self._sheet(ref.css, ref.base_url, matched is None, set())
            else:
                self._external(ref.url, matched is None, set())
        return _keep_needed(self.rules)

    def _external(self, url: str, undecided: bool, visiting: Set[str]) -> None:
        css = self.stylesheets.get(url)
        if css is None:
            self.reasons.add(STYLESHEET_UNAVAILABLE)
        elif url not in visiting:
            self._sheet(css, url, undecided, visiting | {url})

    def _sheet(self, css: str, base_url: str, undecided: bool, visiting: Set[str]) -> None:
        rules = parse_sheet(css)
        for url, media in sheet_imports(rules, base_url):
            matched = media_matches(media, self.width)
            if matched is not False:
                self._external(url, undecided or matched is None, visiting)
        self._walk(rules, AUTHOR_ORIGIN, undecided, ())

    def _walk(self, rules: Sequence, origin: int, undecided: bool, layer: Tuple[str, ...]) -> None:
        for rule in rules:
            if rule.type == "qualified-rule":
                self._qualified(rule, origin, undecided, layer)
            elif rule.type == "at-rule" and rule.content is not None:
                self._block(rule, origin, undecided, layer)
            elif rule.type == "at-rule" and rule.lower_at_keyword == "layer":
                for name in _layer_names(rule.prelude):
                    self._layer_key(layer + name)

    def _block(self, rule, origin: int, undecided: bool, layer: Tuple[str, ...]) -> None:
        keyword = rule.lower_at_keyword
        content = tinycss2.parse_rule_list(rule.content)
        if keyword in ("media", "supports"):
            matched = media_matches(rule.prelude, self.width) if keyword == "media" else supports_matches(rule.prelude)
            if matched is not False:
                self._walk(content, origin, undecided or matched is None, layer)
        elif keyword in UNDECIDABLE_BLOCKS:
            self._walk(content, origin, True, layer)
        elif keyword == "layer":
            names = _layer_names(rule.prelude) or [(f"anonymous-{len(self.layers)}",)]
            self._layer_key(layer + names[0])
            self._walk(content, origin, undecided, layer + names[0])

    def _layer_key(self, path: Tuple[str, ...]) -> Tuple[float, ...]:
        # 层按首次出现的先后排序；嵌套层排在父层内部，父层自身的规则排在其子层之后
        prefixes = [path[:size + 1] for size in range(len(path))]
        for prefix in prefixes:
            self.layers.setdefault(prefix, len(self.layers))
        return tuple(float(self.layers[prefix]) for prefix in prefixes) + (UNLAYERED,)

    def _qualified(self, rule, origin: int, undecided: bool, layer: Tuple[str, ...]) -> None:
        found, nested = declarations(rule.content)
        if found or nested:
            self.rules.append(Rule(tinycss2.serialize(rule.prelude).strip(), tuple(found), origin,
                                   self._layer_key(layer), len(self.rules),
                                   CONDITION_UNDECIDABLE if undecided else None, nested))


def _layer_names(prelude: Sequence) -> List[Tuple[str, ...]]:
    names = []
    for part in split_commas(prelude):
        idents = tuple(t.value for t in significant(part) if t.type == "ident")
        if idents:
            names.append(idents)
    return names


def var_names(tokens: Sequence) -> Set[str]:
    names: Set[str] = set()
    for token in tokens:
        if token.type in ("function", "() block", "[] block", "{} block"):
            inner = token.arguments if token.type == "function" else token.content
            if token.type == "function" and token.lower_name == "var":
                names |= {t.value for t in significant(inner)[:1] if t.type == "ident"}
            names |= var_names(inner)
    return names


def _needed_custom(rules: List[Rule]) -> Set[str]:
    """隐藏相关属性（递归）引用到的自定义属性名。"""
    custom: Dict[str, Set[str]] = {}
    needed: Set[str] = set()
    for rule in rules:
        for decl in rule.declarations:
            if decl.name.startswith("--"):
                custom.setdefault(decl.name, set()).update(var_names(decl.value))
            else:
                needed |= var_names(decl.value)
    frontier = set(needed)
    while frontier:
        frontier = set().union(*(custom.get(name, set()) for name in frontier)) - needed
        needed |= frontier
    return needed


def _keep_needed(rules: List[Rule]) -> List[Rule]:
    """只保留隐藏相关属性以及它们（递归）引用到的自定义属性。"""
    needed = _needed_custom(rules)
    kept = []
    for rule in rules:
        decls = tuple(d for d in rule.declarations if not d.name.startswith("--") or d.name in needed)
        if decls or rule.nested:
            kept.append(replace(rule, declarations=decls))
    return kept
