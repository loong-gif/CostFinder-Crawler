#!/usr/bin/env python3
"""Shared Firecrawl scrape helpers: hidden-element stripping + price-container extraction.

Centralizes the `discover_scrape_path()` / `scrape_one()` logic that used to be
duplicated in scripts/verify_clinic_service_prices.py and
scripts/one_off/audit_membership_benefits.py, and adds a feature on top:

- Elements hidden via `hidden`, `aria-hidden="true"`, all-breakpoint Elementor
  `elementor-hidden-*` classes, or known screen-reader-only classes are removed
  from the DOM (via a Firecrawl `executeJavascript` action) before the final
  markdown is generated. Deliberately attribute/class-based, not a generic
  `getComputedStyle().display === 'none'` check: the latter also strips
  collapsed-by-default accordion/tab pricing menus (real, currently-collapsed
  content, not the "leaked off-season price" bug this feature targets), which
  would delete real prices from both `price_text` and the `markdown` backup
  before Firecrawl even captures them. Verified live via a real browser: on
  skinjectables.com/specials/ the all-breakpoint-hidden $8/$9 blocks are
  correctly identified via class match alone (no computed style needed), while
  a Bootstrap-accordion pricing page stayed intact once the computed-style
  check was dropped.
- The same JS climbs from each visible price hit to a "top-level" `div`
  container (see CLIMB_RULES below) and returns those containers.

Output contract (no DB schema change, only richer local scrape results):

    ScrapeResult(
        ok=True,
        markdown="...",       # full-page markdown, hidden nodes already stripped (backup)
        price_text="...",     # concatenated price-container text (primary evidence)
        containers=[...],     # [{className, id, text}] debug detail
        error="",
    )

If the JS action fails or finds no price container, `price_text` falls back to
an `html.parser`-based scan of the returned `html` (same attribute/class-based
hidden detection, no computed style — see `_node_is_hidden_static`), and if
that also finds nothing, `price_text` falls back to the full markdown —
matching the pre-existing behavior of callers that only ever looked at
markdown.

CLIMB_RULES (kept identical in the JS string below and the Python fallback so
the two paths agree on what a "price container" is):

  1. A price hit is the innermost element whose text matches PRICE_RE and is
     shorter than 240 chars (skips giant footer "prices start at $X" blocks).
  2. Climb to the nearest ancestor `div`; stop at main/body/header/footer/nav.
  3. Keep climbing to the parent `div` while it adds fewer than 400 chars of
     extra text (i.e. the parent is "almost only" this price block).
  4. If >=2 climbed boxes share the same parent, merge them into that parent
     (one promo container instead of several tiny ones).
  5. Drop any box that is nested inside another selected box (keep the outer).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit
import hashlib

try:
    import httpx
except ImportError:  # pragma: no cover
    raise ImportError("httpx required: pip install httpx")

# Same shape as verify_clinic_service_prices.PRICE_RE, trimmed to "does this
# text contain a price" (no unit capture needed for container detection).
PRICE_RE = re.compile(r"(?:USD\s*)?\$\s*\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?", re.I)

STOP_TAGS = {"main", "body", "header", "footer", "nav", "html"}
MAX_LEAF_CHARS = 240
MAX_PARENT_EXTRA_CHARS = 400

HIDDEN_EXCLUDE_TAGS = [
    "[hidden]",
    "[aria-hidden='true']",
    ".elementor-hidden-desktop.elementor-hidden-tablet.elementor-hidden-mobile",
    ".sr-only",
    ".visually-hidden",
    ".screen-reader-text",
    ".d-none",
]

# executeJavascript action: strip hidden nodes, then climb price divs (rules 1-5 above).
_CLIMB_JS = r"""
(() => {
  // Attribute/class-based on purpose (mirrors the Python html.parser fallback,
  // see _node_is_hidden_static): a generic computed-style display:none check
  // also strips collapsed-by-default accordion/tab pricing menus, which are
  // real visible-on-click content, not the "leaked off-season price" bug this
  // targets (verified live: it deletes real prices before Firecrawl even
  // captures markdown/html).
  var ELEMENTOR_HIDDEN = ['elementor-hidden-desktop', 'elementor-hidden-tablet', 'elementor-hidden-mobile'];
  var OTHER_HIDDEN = ['sr-only', 'visually-hidden', 'visuallyhidden', 'screen-reader-text', 'd-none'];
  function isHiddenEl(el) {
    if (el.hidden) return true;
    if (el.getAttribute && el.getAttribute('aria-hidden') === 'true') return true;
    var cls = el.classList;
    if (!cls) return false;
    if (ELEMENTOR_HIDDEN.every(function (c) { return cls.contains(c); })) return true;
    if (OTHER_HIDDEN.some(function (c) { return cls.contains(c); })) return true;
    return false;
  }
  var all = Array.prototype.slice.call(document.querySelectorAll('body *'));
  for (var i = all.length - 1; i >= 0; i--) {
    var el = all[i];
    if (!el.isConnected) continue;
    if (isHiddenEl(el)) el.remove();
  }

  var PRICE_RE = /(?:USD\s*)?\$\s*\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?/;
  var STOP = { MAIN: 1, BODY: 1, HEADER: 1, FOOTER: 1, NAV: 1, HTML: 1 };

  function textOf(el) { return (el.textContent || '').trim(); }

  function hasPriceChild(el) {
    for (var i = 0; i < el.children.length; i++) {
      if (PRICE_RE.test(textOf(el.children[i]))) return true;
    }
    return false;
  }

  var leaves = [];
  document.body.querySelectorAll('*').forEach(function (el) {
    var tag = el.tagName;
    if (tag === 'SCRIPT' || tag === 'STYLE') return;
    var text = textOf(el);
    if (!text || !PRICE_RE.test(text)) return;
    if (hasPriceChild(el)) return;
    if (text.length >= 240) return;
    leaves.push(el);
  });

  function nearestDivOrStop(el) {
    var cur = el;
    while (cur && cur.tagName !== 'DIV' && !STOP[cur.tagName]) cur = cur.parentElement;
    return cur;
  }

  function climb(leaf) {
    var box = nearestDivOrStop(leaf);
    if (!box || STOP[box.tagName]) return null;
    while (true) {
      var parent = box.parentElement;
      if (!parent || STOP[parent.tagName]) break;
      var pdiv = nearestDivOrStop(parent);
      if (!pdiv || STOP[pdiv.tagName] || pdiv === box) break;
      var extra = textOf(pdiv).length - textOf(box).length;
      if (extra < 400) { box = pdiv; continue; }
      break;
    }
    return box;
  }

  var boxes = [];
  leaves.forEach(function (l) {
    var b = climb(l);
    if (b) boxes.push(b);
  });

  var byParent = new Map();
  boxes.forEach(function (b) {
    var p = b.parentElement;
    if (!byParent.has(p)) byParent.set(p, []);
    var arr = byParent.get(p);
    if (arr.indexOf(b) === -1) arr.push(b);
  });

  var merged = [];
  var used = new Set();
  boxes.forEach(function (b) {
    if (used.has(b)) return;
    var siblings = byParent.get(b.parentElement) || [];
    if (siblings.length >= 2 && b.parentElement && !STOP[b.parentElement.tagName]) {
      var parentBox = nearestDivOrStop(b.parentElement);
      if (parentBox && !STOP[parentBox.tagName]) {
        merged.push(parentBox);
        siblings.forEach(function (s) { used.add(s); });
        return;
      }
    }
    merged.push(b);
    used.add(b);
  });

  function contains(outer, inner) {
    return outer !== inner && outer.contains(inner);
  }
  var uniqMerged = Array.from(new Set(merged));
  var final = uniqMerged.filter(function (b) {
    return !uniqMerged.some(function (other) { return contains(other, b); });
  });

  var containers = final.map(function (b) {
    return { className: b.className || '', id: b.id || '', text: textOf(b) };
  });
  return JSON.stringify(containers);
})();
"""


@dataclass
class ScrapeResult:
    ok: bool
    markdown: str = ""
    price_text: str = ""
    containers: list[dict[str, str]] = field(default_factory=list)
    error: str = ""


def normalize_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def url_key(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode()).hexdigest()[:16]


def discover_scrape_path(client: httpx.Client, base: str, key: str) -> str:
    """Prefer /v2/scrape (onlyCleanContent); fall back to /v1 only if v2 is down."""
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    for path, body in (
        (
            "/v2/scrape",
            {
                "url": "https://example.com",
                "formats": ["markdown"],
                "onlyMainContent": True,
                "onlyCleanContent": True,
                "waitFor": 500,
            },
        ),
        (
            "/v1/scrape",
            {
                "url": "https://example.com",
                "formats": ["markdown"],
                "onlyMainContent": True,
                "waitFor": 500,
            },
        ),
    ):
        try:
            r = client.post(f"{base}{path}", headers=headers, json=body, timeout=30.0)
            if r.status_code == 200 and (r.json() or {}).get("success"):
                return path
        except Exception:
            continue
    return "/v2/scrape"


# --- html.parser fallback tree (used only when the JS action fails or finds 0 boxes) ---


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent", "text_parts")

    def __init__(self, tag: str, attrs: dict[str, Optional[str]], parent: Optional["_Node"]):
        self.tag = tag
        self.attrs = attrs
        self.children: list["_Node"] = []
        self.parent = parent
        self.text_parts: list[str] = []


class _TreeBuilder(HTMLParser):
    _VOID = {
        "br", "hr", "img", "input", "meta", "link", "area", "base",
        "col", "embed", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {}, None)
        self.stack: list[_Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        node = _Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self._VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        self.stack[-1].children.append(_Node(tag, dict(attrs), self.stack[-1]))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if self.stack:
            self.stack[-1].text_parts.append(data)


_HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_ELEMENTOR_HIDDEN_CLASSES = {
    "elementor-hidden-desktop",
    "elementor-hidden-tablet",
    "elementor-hidden-mobile",
}
_OTHER_HIDDEN_CLASSES = {
    "sr-only",
    "visually-hidden",
    "visuallyhidden",
    "screen-reader-text",
    "d-none",
}


def _node_classes(node: _Node) -> set[str]:
    return set((node.attrs.get("class") or "").split())


def _node_is_hidden_static(node: _Node) -> bool:
    """Hidden-detection without computed styles: inline style + known classes only.

    Only treats an Elementor "hide on X" class as hidden when *all three*
    breakpoint classes are present together (always hidden regardless of
    viewport) — a single breakpoint class is ambiguous without knowing which
    viewport Firecrawl rendered at, so it's left alone here.
    """
    if "hidden" in node.attrs:
        return True
    if (node.attrs.get("aria-hidden") or "").lower() == "true":
        return True
    style = node.attrs.get("style") or ""
    if _HIDDEN_STYLE_RE.search(style):
        return True
    classes = _node_classes(node)
    if _ELEMENTOR_HIDDEN_CLASSES.issubset(classes):
        return True
    if classes & _OTHER_HIDDEN_CLASSES:
        return True
    return False


def _strip_hidden(node: _Node) -> None:
    node.children = [c for c in node.children if not _node_is_hidden_static(c)]
    for c in node.children:
        _strip_hidden(c)


def _full_text(node: _Node) -> str:
    # ponytail: doesn't preserve exact document order between text_parts and
    # child text (html.parser fallback only needs aggregate text/length, not
    # a faithful reconstruction) — upgrade to lxml if that ever matters.
    parts = list(node.text_parts)
    for c in node.children:
        parts.append(_full_text(c))
    return "".join(parts)


def _find_price_leaves(node: _Node, out: list[_Node]) -> None:
    if node.tag in ("script", "style"):
        return
    text = _full_text(node).strip()
    if text and PRICE_RE.search(text):
        child_has_price = any(PRICE_RE.search(_full_text(c)) for c in node.children)
        if not child_has_price and len(text) < MAX_LEAF_CHARS:
            out.append(node)
    for c in node.children:
        _find_price_leaves(c, out)


def _nearest_div_or_stop(node: Optional[_Node]) -> Optional[_Node]:
    cur = node
    while cur is not None and cur.tag != "div" and cur.tag not in STOP_TAGS:
        cur = cur.parent
    return cur


def _climb(leaf: _Node) -> Optional[_Node]:
    box = _nearest_div_or_stop(leaf)
    if box is None or box.tag in STOP_TAGS:
        return None
    while True:
        parent = box.parent
        if parent is None or parent.tag in STOP_TAGS:
            break
        pdiv = _nearest_div_or_stop(parent)
        if pdiv is None or pdiv.tag in STOP_TAGS or pdiv is box:
            break
        extra = len(_full_text(pdiv)) - len(_full_text(box))
        if extra < MAX_PARENT_EXTRA_CHARS:
            box = pdiv
            continue
        break
    return box


def _contains(outer: _Node, inner: _Node) -> bool:
    if outer is inner:
        return False
    n: Optional[_Node] = inner
    while n is not None:
        if n is outer:
            return True
        n = n.parent
    return False


def find_price_containers_html(html: str) -> list[dict[str, str]]:
    """Fallback container extraction (rules 1-5) when the JS action produced nothing."""
    if not html or not html.strip():
        return []
    builder = _TreeBuilder()
    try:
        builder.feed(html)
    except Exception:
        return []
    root = builder.root
    _strip_hidden(root)

    leaves: list[_Node] = []
    _find_price_leaves(root, leaves)
    boxes = [b for b in (_climb(l) for l in leaves) if b is not None]

    by_parent: dict[Optional[int], list[_Node]] = {}
    for b in boxes:
        by_parent.setdefault(id(b.parent) if b.parent else None, []).append(b)

    merged: list[_Node] = []
    used: set[int] = set()
    for b in boxes:
        if id(b) in used:
            continue
        siblings = by_parent.get(id(b.parent) if b.parent else None, [])
        uniq_sib: list[_Node] = []
        seen_ids: set[int] = set()
        for s in siblings:
            if id(s) not in seen_ids:
                seen_ids.add(id(s))
                uniq_sib.append(s)
        if len(uniq_sib) >= 2 and b.parent is not None and b.parent.tag not in STOP_TAGS:
            parent_box = _nearest_div_or_stop(b.parent)
            if parent_box is not None and parent_box.tag not in STOP_TAGS:
                merged.append(parent_box)
                used.update(id(s) for s in uniq_sib)
                continue
        merged.append(b)
        used.add(id(b))

    uniq_merged: list[_Node] = []
    seen_ids2: set[int] = set()
    for b in merged:
        if id(b) not in seen_ids2:
            seen_ids2.add(id(b))
            uniq_merged.append(b)

    final = [b for b in uniq_merged if not any(_contains(other, b) for other in uniq_merged)]

    containers: list[dict[str, str]] = []
    for b in final:
        text = _full_text(b).strip()
        if not text:
            continue
        containers.append(
            {
                "className": b.attrs.get("class") or "",
                "id": b.attrs.get("id") or "",
                "text": text,
            }
        )
    return containers


def _containers_to_price_text(containers: list[dict[str, str]]) -> str:
    return "\n\n".join(c["text"] for c in containers if c.get("text"))


def scrape_one(
    client: httpx.Client,
    base: str,
    path: str,
    key: str,
    url: str,
    wait_for: int = 3000,
) -> ScrapeResult:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body: dict[str, Any] = {
        "url": url,
        "formats": ["markdown", "html"],
        "onlyMainContent": False,
        "excludeTags": HIDDEN_EXCLUDE_TAGS,
        "waitFor": wait_for,
        "actions": [{"type": "executeJavascript", "script": _CLIMB_JS}],
    }
    if path.startswith("/v2"):
        body["onlyCleanContent"] = True

    last_err = ""
    for attempt in range(2):  # 1 retry
        try:
            r = client.post(f"{base}{path}", headers=headers, json=body, timeout=120.0)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                time.sleep(0.5)
                continue
            data = r.json()
            if not data.get("success"):
                last_err = str(data.get("error") or data.get("message") or "success=false")
                time.sleep(0.5)
                continue
            payload = data.get("data") or {}
            md = payload.get("markdown") or ""
            html = payload.get("html") or ""
            if not md.strip():
                last_err = "empty markdown"
                time.sleep(0.5)
                continue

            containers: list[dict[str, str]] = []
            js_returns = ((payload.get("actions") or {}).get("javascriptReturns")) or []
            if js_returns:
                raw_value = js_returns[0].get("value")
                try:
                    parsed = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
                    if isinstance(parsed, list):
                        containers = [c for c in parsed if isinstance(c, dict) and c.get("text")]
                except (json.JSONDecodeError, TypeError):
                    containers = []

            if not containers:
                containers = find_price_containers_html(html)

            price_text = _containers_to_price_text(containers) or md
            return ScrapeResult(ok=True, markdown=md, price_text=price_text, containers=containers, error="")
        except Exception as e:
            last_err = str(e)
            time.sleep(0.8)
    return ScrapeResult(ok=False, error=last_err)


def load_cache_jsonl(path: Optional[Path]) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path or not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            u = obj.get("source_url") or obj.get("url")
            md = obj.get("markdown") or ""
            if u and md:
                out[normalize_url(u)] = md
                out[u] = md
    return out
