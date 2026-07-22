# Masters Medspa Firecrawl Raw Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搜索并抓取 `masters-medspa.com` 的 Botox 定价证据，将降噪后的原始结果幂等写入 Supabase raw 表。

**Architecture:** Firecrawl CLI 负责 Search/Scrape；项目现有 raw DB helper 负责指纹、URL 规范化和 Supabase upsert。Scrape 显式使用 `--only-main-content`，落库前再经过 `prepare_scrape_markdown()` 降噪。

**Tech Stack:** Firecrawl CLI、Python 3.11、现有 Supabase REST client、PostgreSQL。

## Global Constraints

- 目标固定为 `master_business_info.business_id=2889`、`masters-medspa.com`。
- 只写 `firecrawl_search_raw` 与 `firecrawl_scrape_raw`。
- 无价格信号的 Search 命中不得进入 Scrape。
- Scrape 必须启用 `onlyMainContent`，落库 markdown 必须经过 `prepare_scrape_markdown()`。
- 不改写价格、单位、服务名或限定条件。
- 当前目录不是 Git 仓库，不执行提交步骤。

---

### Task 1: Search、门控、Scrape 与 raw 入库

**Files:**
- Runtime output: `.firecrawl/masters-medspa/search-*.json`
- Runtime output: `.firecrawl/masters-medspa/scrape-*.json`
- Reuse: `utils/firecrawl_search_raw_db.py`
- Reuse: `utils/firecrawl_scrape_raw_db.py`
- Reuse: `utils/scrape_markdown.py`
- Reuse: `utils/search_scrape_gate.py`

**Interfaces:**
- Consumes: Firecrawl CLI Search/Scrape JSON、`master_business_info.business_id=2889`
- Produces: `firecrawl_search_raw.id` 与关联的 `firecrawl_scrape_raw.id`

- [ ] **Step 1: 记录业务表基线**

通过 Supabase 执行只读 SQL，记录四张业务层表行数：

```sql
select 'clinic_services' as table_name, count(*) from public.clinic_services
union all select 'clinic_memberships', count(*) from public.clinic_memberships
union all select 'clinic_promotions', count(*) from public.clinic_promotions
union all select 'promo_offer_master', count(*) from public.promo_offer_master;
```

Expected: 返回四行计数，执行结束后计数保持不变。

- [ ] **Step 2: 执行两条 Firecrawl Search**

```bash
mkdir -p ".firecrawl/masters-medspa"
firecrawl search 'site:masters-medspa.com botox ("per unit" OR "/unit" OR "unit price")' --limit 10 --scrape --scrape-formats markdown -o ".firecrawl/masters-medspa/search-botox-unit.json" --json
firecrawl search 'site:masters-medspa.com botox pricing injectables' --limit 10 --scrape --scrape-formats markdown -o ".firecrawl/masters-medspa/search-botox-pricing.json" --json
```

Expected: 两个 JSON 文件均包含 Firecrawl Search 响应；独立操作可并行执行。

- [ ] **Step 3: 保存 Search raw 并生成候选 URL**

使用 `web_rows_from_search_file()` 解析 Search 文件，调用：

```python
save_search_queries(
    client,
    website="masters-medspa.com",
    domain="masters-medspa.com",
    entries=[(query, web_rows)],
)
```

仅保留满足以下全部条件的候选：

```python
host_matches_domain(page.url, "masters-medspa.com") and search_page_has_price(page)
```

Expected: 每条查询获得一个 `firecrawl_search_raw.id`；候选 URL 仅来自目标域且含价格信号。

- [ ] **Step 4: 对候选 URL 执行 Main Content Scrape**

每个唯一候选 URL 执行：

```bash
firecrawl scrape "<candidate-url>" --only-main-content --format markdown,links -o ".firecrawl/masters-medspa/scrape-<sha256-prefix>.json"
```

Expected: 每个候选生成一个 Scrape JSON，包含 markdown，尽可能包含 links/metadata。

- [ ] **Step 5: 清洗并保存 Scrape raw**

读取 Scrape JSON，并在落库前执行：

```python
clean_markdown = prepare_scrape_markdown(raw_markdown)
```

清洗后仍含 Botox 与价格证据时，调用：

```python
save_scrape_response(
    client,
    scrape_request_fingerprint(url, only_main_content=True),
    url,
    {**payload, "markdown": clean_markdown},
    search_raw_id=matching_search_raw_id,
    success=True,
)
```

清洗后正文为空或价格证据消失时不写成功内容；调用同一 helper 保存 `success=False` 和明确错误信息。

- [ ] **Step 6: 回查并验证**

```sql
select id, search_query, success, updated_at
from public.firecrawl_search_raw
where search_query ilike '%masters-medspa.com%'
order by updated_at desc;

select id, source_url, search_raw_id, success, length(markdown) as markdown_length, updated_at
from public.firecrawl_scrape_raw
where source_url ilike '%masters-medspa.com%'
order by updated_at desc;
```

Expected:

- 两条 Search raw 均成功。
- Scrape URL 均属于 `masters-medspa.com`。
- 每条成功 Scrape 均有关联 `search_raw_id`，且 `markdown_length > 0`。
- markdown 中存在 Botox 与货币/每单位价格证据，无明显导航、页脚和重复菜单。
- 四张业务层表计数与 Step 1 一致。
