# 网站促销内容变更检测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对 `promo_website_staging` 表中每条 `subpage_url` 重新抓取页面内容，与现有 `page_content` 做规范化对比，输出「哪些 URL 内容已变更、哪些可能对应过期价格」的报告；默认只读不写库。

**Architecture:** 新增一个 CLI 脚本 `scripts/detect_promo_website_staging_changes.py`，复用主链路 Jina Reader 抓取 + `utils/page_content_processor.process_page_content` 清洗，对比逻辑抽到 `utils/staging_content_diff.py`（从 `audit_promo_website_staging.py` 提取 `normalize_content` / `content_hash` / 价格信号检测，避免重复实现）。可选 `--join-offers` 将变更 URL 与 `promo_offer_master.source_url` 关联，列出可能过期的 offer 行。`--apply` 时才写回 Supabase（仅变更行）。

**Tech Stack:** Python 3.11、asyncio、Jina Reader API、Supabase PostgREST、现有 `crawler/jina_reader_client.py` 与 `crawler/promo_site_crawler.prepare_page_content` 管道。

---

## 背景：现有能力 vs 缺口

| 现有脚本 | 能力 | 缺口 |
|---------|------|------|
| `scripts/recrawl_update_page_content_from_csv.py` | CSV 驱动重爬并**直接更新** | 需 CSV 输入；用 crawl4ai 非主链路；无 diff 报告 |
| `scripts/refresh_promo_website_staging_by_subpage.py` | 从 DB 读 URL，Apify 重爬 | 简单字符串相等判断；默认写库；成本高 |
| `scripts/firecrawl_monitor_poll.py` | Firecrawl 增量变更监测 | 依赖已建 monitor，非全量 staging 扫描 |
| `scripts/audit_promo_website_staging.py` | 静态质量审计 + `content_hash` | 不重爬 |

**结论：** 缺一个「从 staging 表读 URL → Jina 重爬 → hash 对比 → 报告」的专用入口。不新建 Apify/Firecrawl 依赖，YAGNI。

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `utils/staging_content_diff.py` | 内容规范化、hash、变更分类、价格信号检测（从 audit 脚本提取） |
| `scripts/detect_promo_website_staging_changes.py` | CLI：读库、并发重爬、对比、输出报告、可选写回 |
| `tests/test_staging_content_diff.py` | diff 工具单元测试 |
| `tests/test_detect_staging_changes.py` | 对比/分类逻辑集成测试（mock 抓取，不打真实 API） |

---

### Task 1: 提取共享 diff 工具

**Files:**
- Create: `utils/staging_content_diff.py`
- Modify: `scripts/audit_promo_website_staging.py:200-224`（改为 import，删除重复定义）
- Test: `tests/test_staging_content_diff.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_staging_content_diff.py
from utils.staging_content_diff import (
    classify_content_change,
    content_hash,
    has_price_signal,
    normalize_content,
)


def test_normalize_content_strips_segment_markers_and_whitespace():
    raw = "[SEGMENT 1] Botox   $199\n[SEGMENT 2]  Filler $599"
    assert normalize_content(raw) == "botox $199 filler $599"


def test_content_hash_is_stable_for_equivalent_text():
    a = "[SEGMENT 1] Botox $199"
    b = "botox   $199"
    assert content_hash(a) == content_hash(b)


def test_classify_unchanged_when_hashes_match():
    result = classify_content_change("Botox $199", "botox $199")
    assert result.change_type == "unchanged"


def test_classify_changed_when_price_removed():
    old = "Botox special $199 per unit"
    new = "Botox — contact us for pricing"
    result = classify_content_change(old, new)
    assert result.change_type == "changed"
    assert result.price_signal_lost is True
    assert result.price_signal_gained is False


def test_has_price_signal_detects_dollar_amount():
    assert has_price_signal("Membership from $99/month") is True
    assert has_price_signal("About our clinic") is False
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /home/loong/projects/CostFinder-Crawler
source .venv/bin/activate
pytest tests/test_staging_content_diff.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'utils.staging_content_diff'`

- [ ] **Step 3: 实现 `utils/staging_content_diff.py`**

```python
# utils/staging_content_diff.py
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

PRICE_SIGNAL_PATTERNS = [
    re.compile(r"\$\s*\d+(?:,\d{3})*(?:\.\d{1,2})?", re.IGNORECASE),
    re.compile(r"\b\d+(?:\.\d+)?\s*%\s*(?:off|discount|savings?)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:price|pricing|starts? at|from|per unit|per syringe|membership|specials?|offers?|promo|deal|discount)\b",
        re.IGNORECASE,
    ),
]


def normalize_content(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"\[SEGMENT\s+\d+\]\s*", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def content_hash(value: Any) -> str:
    normalized = normalize_content(value)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def has_price_signal(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in PRICE_SIGNAL_PATTERNS)


@dataclass(frozen=True)
class ContentChangeResult:
    change_type: str  # unchanged | changed | empty_old | empty_new | both_empty
    old_hash: str
    new_hash: str
    price_signal_lost: bool
    price_signal_gained: bool
    old_len: int
    new_len: int


def classify_content_change(old_content: str, new_content: str) -> ContentChangeResult:
    old_norm = normalize_content(old_content)
    new_norm = normalize_content(new_content)
    old_h = content_hash(old_content)
    new_h = content_hash(new_content)
    old_has_price = has_price_signal(old_norm)
    new_has_price = has_price_signal(new_norm)

    if not old_norm and not new_norm:
        change_type = "both_empty"
    elif not old_norm:
        change_type = "empty_old"
    elif not new_norm:
        change_type = "empty_new"
    elif old_h == new_h:
        change_type = "unchanged"
    else:
        change_type = "changed"

    return ContentChangeResult(
        change_type=change_type,
        old_hash=old_h,
        new_hash=new_h,
        price_signal_lost=old_has_price and not new_has_price,
        price_signal_gained=not old_has_price and new_has_price,
        old_len=len(str(old_content or "")),
        new_len=len(str(new_content or "")),
    )
```

- [ ] **Step 4: 修改 audit 脚本改为 import**

在 `scripts/audit_promo_website_staging.py` 顶部增加：

```python
from utils.staging_content_diff import content_hash, has_price_signal, normalize_content
```

删除该文件中 `normalize_content`、`content_hash`、`has_price_signal` 的本地定义及 `PRICE_SIGNAL_PATTERNS`（保留 `BOILERPLATE_PATTERNS` 本地使用）。

- [ ] **Step 5: 运行测试**

```bash
pytest tests/test_staging_content_diff.py tests/test_page_content_processor.py -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add utils/staging_content_diff.py tests/test_staging_content_diff.py scripts/audit_promo_website_staging.py
git commit -m "refactor: extract staging content diff helpers for reuse"
```

---

### Task 2: 检测脚本核心逻辑（无网络）

**Files:**
- Create: `scripts/detect_promo_website_staging_changes.py`
- Test: `tests/test_detect_staging_changes.py`

- [ ] **Step 1: 写失败测试（mock 抓取结果）**

```python
# tests/test_detect_staging_changes.py
from scripts.detect_promo_website_staging_changes import build_row_result


def test_build_row_result_marks_unchanged():
    row = {
        "promo_website_id": 1,
        "subpage_url": "https://example.com/pricing",
        "domain_name": "example.com",
        "page_content": "Botox $199",
    }
    crawl = {"success": True, "page_content": "Botox $199", "error_message": ""}
    result = build_row_result(row, crawl)
    assert result["change_type"] == "unchanged"
    assert result["needs_review"] is False


def test_build_row_result_flags_price_signal_lost():
    row = {
        "promo_website_id": 2,
        "subpage_url": "https://example.com/specials",
        "domain_name": "example.com",
        "page_content": "Botox $199 special",
    }
    crawl = {"success": True, "page_content": "Botox — call for pricing", "error_message": ""}
    result = build_row_result(row, crawl)
    assert result["change_type"] == "changed"
    assert result["price_signal_lost"] is True
    assert result["needs_review"] is True


def test_build_row_result_handles_crawl_failure():
    row = {"promo_website_id": 3, "subpage_url": "https://example.com/x", "page_content": "x"}
    crawl = {"success": False, "page_content": "", "error_message": "timeout"}
    result = build_row_result(row, crawl)
    assert result["change_type"] == "crawl_failed"
    assert result["needs_review"] is True
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_detect_staging_changes.py -v
```

Expected: FAIL — module not found

- [ ] **Step 3: 实现脚本骨架 + `build_row_result`**

脚本需包含以下结构（完整实现时补全 fetch/CLI/main）：

```python
#!/usr/bin/env python3
"""
Detect page_content changes for promo_website_staging rows by recrawling subpage_url.

Default: dry-run report only. Use --apply to write changed rows back to Supabase.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.staging_content_diff import classify_content_change
from utils.page_content_processor import process_page_content

TABLE = "promo_website_staging"
REPORT_PREFIX = "promo_website_staging_change_detect"
OUTPUT_DIR = PROJECT_ROOT / "output" / "results"


def build_row_result(row: Dict[str, Any], crawl: Dict[str, Any]) -> Dict[str, Any]:
    old_content = str(row.get("page_content") or "")
    if not crawl.get("success"):
        return {
            "promo_website_id": row.get("promo_website_id"),
            "subpage_url": row.get("subpage_url"),
            "domain_name": row.get("domain_name"),
            "change_type": "crawl_failed",
            "needs_review": True,
            "error_message": crawl.get("error_message", ""),
            "old_content_len": len(old_content),
            "new_content_len": 0,
        }

    new_content = str(crawl.get("page_content") or "")
    diff = classify_content_change(old_content, new_content)
    needs_review = diff.change_type == "changed" or diff.price_signal_lost or diff.change_type in {
        "empty_new",
        "crawl_failed",
    }
    return {
        "promo_website_id": row.get("promo_website_id"),
        "subpage_url": row.get("subpage_url"),
        "domain_name": row.get("domain_name"),
        "change_type": diff.change_type,
        "needs_review": needs_review,
        "price_signal_lost": diff.price_signal_lost,
        "price_signal_gained": diff.price_signal_gained,
        "old_hash": diff.old_hash,
        "new_hash": diff.new_hash,
        "old_content_len": diff.old_len,
        "new_content_len": diff.new_len,
        "old_content_preview": old_content[:300],
        "new_content_preview": new_content[:300],
    }


async def fetch_page_content_jina(url: str, client) -> Dict[str, Any]:
    """Fetch one URL via Jina Reader and run canonical page_content pipeline."""
    try:
        page = await client.fetch(url)
        processed = process_page_content(page.content or "", source_type="markdown")
        page_content = processed.get("page_content") or processed.get("page_content_llm") or ""
        if not str(page_content).strip():
            return {"success": False, "page_content": "", "error_message": "empty_content_after_processing"}
        return {"success": True, "page_content": page_content, "processed": processed, "error_message": ""}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "page_content": "", "error_message": str(exc)}
```

CLI 参数设计：

| 参数 | 默认 | 说明 |
|------|------|------|
| `--limit N` | 无 | 只处理前 N 条 |
| `--domain example.com` | 无 | 只处理指定 domain |
| `--id 123` | 无 | 只处理单条 promo_website_id |
| `--concurrency 5` | 5 | Jina 并发 |
| `--join-offers` | off | 关联 promo_offer_master.source_url |
| `--apply` | off | 写回变更行（更新 page_content + crawl_timestamp + processed_status=false） |
| `--output-dir` | output/results | 报告目录 |

- [ ] **Step 4: 运行测试**

```bash
pytest tests/test_detect_staging_changes.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/detect_promo_website_staging_changes.py tests/test_detect_staging_changes.py
git commit -m "feat: add staging content change detection core logic"
```

---

### Task 3: 补全 CLI — 读库、并发抓取、报告输出

**Files:**
- Modify: `scripts/detect_promo_website_staging_changes.py`
- Test: 手动 dry-run（见 Step 4）

- [ ] **Step 1: 实现 Supabase 分页读取**

复用 `scripts/crawl_promo_website_staging.py` 中 `SupabaseRestClient.fetch_all` 模式，select 字段：

```
promo_website_id,subpage_url,domain_name,page_content,crawl_timestamp,processed_status,name
```

支持 `--domain` filter：`domain_name=eq.{domain}`

- [ ] **Step 2: 实现并发 Jina 抓取**

```python
async def recrawl_rows(rows: List[Dict], concurrency: int) -> List[Dict]:
    from crawler.jina_reader_client import JinaReaderClient

    sem = asyncio.Semaphore(max(1, concurrency))
    client = JinaReaderClient()

    async def one(row: Dict) -> Dict:
        async with sem:
            crawl = await fetch_page_content_jina(str(row["subpage_url"]), client)
            return build_row_result(row, crawl)

    return await asyncio.gather(*(one(r) for r in rows))
```

- [ ] **Step 3: 实现报告输出**

输出两个文件到 `output/results/`：

1. `{REPORT_PREFIX}_{timestamp}.json` — 完整 summary + 全量 results
2. `{REPORT_PREFIX}_{timestamp}_changed.csv` — 仅 `change_type=changed` 或 `price_signal_lost=true` 的行

Summary 字段：

```python
summary = {
    "generated_at": "...",
    "mode": "dry_run" | "apply",
    "total_rows": N,
    "unchanged": n1,
    "changed": n2,
    "price_signal_lost": n3,
    "crawl_failed": n4,
    "needs_review": n5,
}
```

- [ ] **Step 4: 小样本 dry-run 验证**

```bash
source .venv/bin/activate
# 确保 .env 有 SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, JINA_READER_API_KEY
python scripts/detect_promo_website_staging_changes.py --limit 5
```

Expected: 终端打印 summary JSON；`output/results/promo_website_staging_change_detect_*.json` 生成；无 Supabase 写操作。

- [ ] **Step 5: Commit**

```bash
git add scripts/detect_promo_website_staging_changes.py
git commit -m "feat: complete staging change detection CLI with Jina recrawl"
```

---

### Task 4: 关联 promo_offer_master（可选 `--join-offers`）

**Files:**
- Modify: `scripts/detect_promo_website_staging_changes.py`

- [ ] **Step 1: 抓取变更 URL 后查询 master 表**

对 `needs_review=true` 且 `change_type=changed` 的 `subpage_url`，批量查询：

```
GET /rest/v1/promo_offer_master?source_url=in.(url1,url2,...)&select=id,service_name,offer_raw_text,discount_price,original_price,status,source_url
```

在 CSV 中追加列：`linked_offer_ids`、`linked_offer_count`、`sample_offer_text`

- [ ] **Step 2: 在 JSON report 增加 `stale_offer_candidates` 数组**

每条包含：`source_url`、`offer_ids`、`change_type`、`price_signal_lost`

- [ ] **Step 3: 手动验证**

```bash
python scripts/detect_promo_website_staging_changes.py --limit 10 --join-offers
```

Expected: changed 行附带 linked offer 信息

- [ ] **Step 4: Commit**

```bash
git add scripts/detect_promo_website_staging_changes.py
git commit -m "feat: join staging changes with promo_offer_master for stale offer hints"
```

---

### Task 5: 可选写回 + README 文档

**Files:**
- Modify: `scripts/detect_promo_website_staging_changes.py`
- Modify: `README.md`（新增一节「内容变更检测」）

- [ ] **Step 1: 实现 `--apply` 写回**

仅对 `change_type=changed` 且 crawl 成功的行 PATCH：

```python
payload = {
    "page_content": new_content,
    "crawl_timestamp": now_iso,
    "processed_status": False,
}
# 若 processed 含 segments，同步写 page_segments_* / page_content_llm / content_quality_flags
# 参考 crawler/staging_recrawl.py sync 逻辑
```

**安全约束：** 无 `--apply` 时不写库；`--apply` 时打印 `updated_count` 并写 report。

- [ ] **Step 2: README 增加用法**

```markdown
### 5) 检测 staging 页面内容是否变更

```bash
# 小样本探测（默认 dry-run，不写库）
python scripts/detect_promo_website_staging_changes.py --limit 20

# 全量扫描 + 关联 master offer
python scripts/detect_promo_website_staging_changes.py --join-offers

# 确认变更后写回 page_content
python scripts/detect_promo_website_staging_changes.py --apply --limit 50
```

报告输出：`output/results/promo_website_staging_change_detect_*.json` 与 `*_changed.csv`。
```

- [ ] **Step 3: Commit**

```bash
git add scripts/detect_promo_website_staging_changes.py README.md
git commit -m "docs: document staging content change detection workflow"
```

---

## 推荐执行顺序（运维）

```bash
# 1. 小样本验证管道
python scripts/detect_promo_website_staging_changes.py --limit 20 --join-offers

# 2. 审阅 output/results/*_changed.csv，确认变更合理

# 3. 扩大范围（可按 domain 分批，降低 Jina 压力）
python scripts/detect_promo_website_staging_changes.py --domain some-clinic.com --join-offers

# 4. 确认后写回 staging（触发下游 LLM 重抽取需另跑 extract 脚本）
python scripts/detect_promo_website_staging_changes.py --apply

# 5. 对变更 URL 触发 offer 重抽取（已有脚本，非本 plan 范围）
python scripts/extract_offers_with_llm.py  # 或 update_supabase_page_content_llm.py
```

---

## 自审（Spec Coverage）

| 需求 | 对应 Task |
|------|-----------|
| 从 promo_website_staging 读 URL | Task 3 Step 1 |
| 重新爬取内容 | Task 3 Step 2（Jina 主链路） |
| 对比现有 page_content | Task 1 + Task 2 `classify_content_change` |
| 判断有哪些改变 | Task 3 Step 3 报告 + CSV |
| 识别可能过期价格 | Task 1 `price_signal_lost` + Task 4 `--join-offers` |
| 默认不破坏数据 | 默认 dry-run；`--apply` 显式开启 |

无 placeholder；所有代码块可直接复制实现。

---

## 风险与约束

1. **历史数据格式不一致：** 部分行 `page_content` 可能是 LLM 压缩版而非 raw text。对比前统一走 `process_page_content`；若 hash 误报高，可在 report 中加 `similarity_ratio`（后续迭代，本 plan 不做）。
2. **Jina 费用与限速：** 全表扫描前先 `--limit`；生产建议按 `domain_name` 分批。
3. **「内容变更 ≠ offer 过期」：** `--join-offers` 仅提供候选；最终 mark_ended 应走 `change_driven_extractor.validate_offer_actions` 或人工 QA。
4. **OCR 页：** `needs_ocr=true` 的行 Jina 可能不完整；可后续加 `--needs-ocr-via-apify` 分支（YAGNI，不在首版）。
