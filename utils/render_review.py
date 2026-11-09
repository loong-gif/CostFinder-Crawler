"""Browser-render review of pages whose visibility could not be judged from crawler HTML.

``needs_review`` pages are queued, rendered in ego-lite, and each evidence line is checked against the
rendered text. A page is confirmed only when its price or discount tokens appear in text a visitor can see.
"""
from __future__ import annotations

import hashlib
import io
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

import requests
import snowballstemmer
from PIL import Image

from utils.paddle_ocr_markdown import ocr_lines

AMOUNT_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{1,2})?")
PERCENT_RE = re.compile(r"\d{1,3}(?:\.\d+)?\s?%")
VISIBLE_FRACTION = 0.5
MAX_ATTEMPTS = 3


def normalize_anchor(token: str) -> str:
    """Same normalisation as the in-page evaluator: lower case, no spaces/commas, no trailing .00."""
    value = re.sub(r"\s+", "", token.lower()).replace(",", "")
    return re.sub(r"(\d)\.00(?!\d)", r"\1", value)


def claim_anchors(line: str) -> List[str]:
    found: List[str] = []
    for regex in (AMOUNT_RE, PERCENT_RE):
        for match in regex.finditer(str(line)):
            anchor = normalize_anchor(match.group(0))
            if anchor not in found:
                found.append(anchor)
    return found


def item_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def shard(items: List[Dict[str, Any]], count: int) -> List[List[Dict[str, Any]]]:
    """Round-robin so every shard gets a similar mix of sites and sizes."""
    shards: List[List[Dict[str, Any]]] = [[] for _ in range(max(1, count))]
    for index, item in enumerate(items):
        shards[index % len(shards)].append(item)
    return [s for s in shards if s]


LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
MIN_SUBSTANTIVE_WORDS = 4


def plain_text(value: str) -> str:
    text = LINK_RE.sub(r"\1", str(value))
    text = re.sub(r"[*#>|_~`]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def corpus_text(texts: Iterable[str]) -> str:
    joined = " ".join(plain_text(t) for t in texts)
    joined = re.sub(r"(\d),(\d{3})", r"\1\2", joined)
    joined = re.sub(r"\$\s+", "$", joined)
    joined = re.sub(r"\s+%", "%", joined)
    return re.sub(r"(\d)\.00(?!\d)", r"\1", joined)


def is_substantive(claim: Mapping[str, Any]) -> bool:
    if claim.get("anchors"):
        return True
    return len(plain_text(claim.get("line", "")).split()) >= MIN_SUBSTANTIVE_WORDS


NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
                "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "single": "1",
                "once": "1", "twice": "2"}
# 同一事实的常见写法统一为一个词：月份缩写、按年/按月的说法
WORD_FORMS = {"jan": "january", "feb": "february", "mar": "march", "apr": "april", "jun": "june", "jul": "july",
              "aug": "august", "sep": "september", "sept": "september", "oct": "october", "nov": "november",
              "dec": "december", "annually": "year", "annual": "year", "yearly": "year", "monthly": "month",
              "weekly": "week", "daily": "day"}
CLAUSE_SPLIT = re.compile(r"(?<!\d)[.;](?!\d)\s*")
STEMMER = snowballstemmer.stemmer("english")
STOPWORDS = frozenset(
    "the and for with your you our are any all per each from this that will can not but has have into onto "
    "its their they them who what when where which while also only more than then such may must get gets".split())
TOKEN_RE = re.compile(r"\$\d+(?:\.\d+)?|\d+(?:\.\d+)?%|[a-z0-9]+")
MIN_WINDOW_TOKENS = 80
KEY_WORD_SHARE = 0.6


def _tokens(text: str) -> List[str]:
    """corpus_text 规范化后的词：数字词转为数字，金额与百分比整体成词，简单复数还原。"""
    out = []
    for token in TOKEN_RE.findall(corpus_text([text])):
        token = WORD_FORMS.get(token, NUMBER_WORDS.get(token, token))
        out.append(STEMMER.stemWord(token) if token.isalpha() else token)
    return out


def _key_words(line: str) -> tuple[set, set]:
    """返回 (必须出现的数值, 全部关键词)。金额、百分比与数量（单位数、月数、次数）都是事实，必须全部出现。"""
    tokens = _tokens(line)
    required = {normalize_anchor(a) for a in claim_anchors(line)} | {t for t in tokens if t.isdigit()}
    words = {t for t in tokens if t not in STOPWORDS and (len(t) > 2 or t.isdigit())}
    return required, words | required


def claim_matches(claim: Mapping[str, Any], texts: Iterable[str]) -> bool:
    """声明能在页面可见文本中追溯（允许改写）。声明按句号、分号拆成分句，每个分句各自追溯：
    同一段相邻文本窗口内包含该分句的全部金额、百分比与数量，且包含至少六成关键词。
    窗口不少于 80 个词（同一张方案卡片的价格与说明常相隔数十个词），或分句关键词数的三倍。"""
    page = [t for text in texts for t in _tokens(text)]
    if claim.get("is_name"):
        anchors, keys = _key_words(str(claim["line"]))
        return bool(keys - anchors) and _name_matches(keys - anchors, anchors, page)
    clauses = [c for c in CLAUSE_SPLIT.split(str(claim.get("line", ""))) if _key_words(c)[1]]
    return bool(clauses) and all(_clause_matches(clause, page) for clause in clauses)


def claim_matches_sources(claim: Mapping[str, Any], sources: Iterable[Iterable[str]]) -> bool:
    """声明的内容来自多个页面（促销页与卡片链接到的条款页）时，每一行的每个分句在任一来源页面上能追溯即可；
    单个分句仍须在同一页面的相邻窗口内追溯，不跨页面拼接。"""
    pages = [[t for text in texts for t in _tokens(text)] for texts in sources]
    if claim.get("is_name"):
        anchors, keys = _key_words(str(claim["line"]))
        return bool(keys - anchors) and any(_name_matches(keys - anchors, anchors, page) for page in pages)
    clauses = [c for line in str(claim.get("line", "")).splitlines() for c in CLAUSE_SPLIT.split(line)
               if _key_words(c)[1]]
    return bool(clauses) and all(any(_clause_matches(clause, page) for page in pages) for clause in clauses)


def _name_matches(words: set, anchors: set, page: List[str]) -> bool:
    """方案名称这类短分句：名称的词必须全部出现且紧挨在一起（零散出现在页面各处不算），
    价格等数值出现在名称前后的常规窗口内（价格常与名称隔着一行说明）。"""
    span = len(words) + 2
    for start in range(len(page)):
        if not words <= set(page[start:start + span]) or page[start] not in words:
            continue
        around = set(page[max(0, start - MIN_WINDOW_TOKENS):start + span + MIN_WINDOW_TOKENS])
        if anchors <= around:
            return True
    return False


def _clause_matches(clause: str, page: List[str]) -> bool:
    anchors, keys = _key_words(clause)
    width = max(MIN_WINDOW_TOKENS, 3 * len(keys))
    need = -(-int(KEY_WORD_SHARE * 100) * len(keys) // 100)
    counts: Dict[str, int] = {}
    for index, token in enumerate(page):
        if token in keys:
            counts[token] = counts.get(token, 0) + 1
        if index >= width:
            old = page[index - width]
            if old in keys:
                counts[old] -= 1
                if not counts[old]:
                    del counts[old]
        if len(counts) >= need and anchors <= counts.keys():
            return True
    return False


def deep_verdict(claims: List[Mapping[str, Any]], visible_texts: Iterable[str], image_texts: Iterable[str] = ()
                 ) -> Dict[str, Any]:
    """Verdict from the text visible after expanding panels, plus text read from visible images."""
    visible_texts = list(visible_texts)
    image_texts = list(image_texts)
    judged = [c for c in claims if is_substantive(c)]
    if not judged:
        return {"verdict": "not_visible", "reason": "no_substantive_evidence_line"}
    in_text = [c for c in judged if claim_matches(c, visible_texts)]
    in_image = [c for c in judged if c not in in_text and image_texts and claim_matches(c, image_texts)]
    matched = len(in_text) + len(in_image)
    if matched / len(judged) >= VISIBLE_FRACTION:
        source = "image_text" if in_image and not in_text else "expanded_page_text" if in_text else "page_text"
        return {"verdict": "verified", "reason": f"{matched}_of_{len(judged)}_claims_visible", "source": source}
    return {"verdict": "not_visible", "reason": f"only_{matched}_of_{len(judged)}_claims_visible_after_expanding"}


RASTER_CONTENT_TYPES = ("application/octet-stream", "binary/octet-stream")


def _get_image(url: str) -> requests.Response:
    """下载图片；连接故障与 5xx 按递增间隔重试，重试用尽时连接故障终止命令，HTTP 错误状态交给调用方判定。"""
    for attempt in range(1, MAX_ATTEMPTS):
        try:
            response = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            if response.status_code < 500:
                return response
        except (requests.ConnectionError, requests.Timeout):
            pass
        time.sleep(5 * attempt)
    return requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})


def ocr_image(url: str, image_dir: Path) -> Dict[str, str]:
    """渲染页面上一张可见图片的 OCR 文字 {"url", "text"}。访问者同样加载不到的图片（HTTP 错误状态）与非位图
    （SVG、网页）记为 {"url", "unavailable": 原因}，不提供可见性证据；识别服务故障由 ocr_lines 重试，仍失败即终止命令。"""
    target = image_dir / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".png")
    if not target.exists():
        response = _get_image(url)
        if response.status_code >= 400:
            return {"url": url, "unavailable": f"http_{response.status_code}"}
        content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type == "image/svg+xml" or not (content_type.startswith("image/")
                                                   or content_type in RASTER_CONTENT_TYPES):
            return {"url": url, "unavailable": f"not_raster_image:{content_type}"}
        # OCR 服务只解码常见位图格式：GIF 等先转成 PNG
        png = io.BytesIO()
        Image.open(io.BytesIO(response.content)).convert("RGB").save(png, "PNG")
        target.write_bytes(png.getvalue())
    return {"url": url, "text": "\n".join(ocr_lines(target.read_bytes()))}


def build_deep_items(decisions: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """等待渲染核验的页面（needs_review 且决策为 add_with_review，含上次渲染未得到可用页面的），每个网址一次。"""
    items: List[Dict[str, Any]] = []
    seen = set()
    for record in decisions:
        if (record.get("status") != "needs_review" or record.get("decision") != "add_with_review"
                or record["url"] in seen):
            continue
        seen.add(record["url"])
        claims = [{"id": i, "line": line[:240], "anchors": claim_anchors(line)}
                  for i, line in enumerate(record.get("evidence") or [])]
        items.append({"id": item_id(record["url"]), "url": record["url"], "business_id": record.get("business_id"),
                      "business": record.get("business"), "claims": claims})
    return items
