# Masters Medspa LLM Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使用真实 Gemini 从既有 Firecrawl raw 数据提取服务和真实促销，审核后幂等写入 `clinic_services` 与 `clinic_promotions`。

**Architecture:** 临时 one-off 脚本从 Supabase raw 表读取输入，调用现有 structured LLM client 与 JSON Schema，输出本地审计 JSON。确定性校验通过后，由 Supabase MCP 写入 live 表；没有真实促销时不创建促销行。

**Tech Stack:** Python 3.11、`requests`、Gemini structured output、Supabase PostgreSQL、标准库 `unittest`。

## Global Constraints

- 域名固定为 `masters-medspa.com`，`business_id=2889`。
- 服务只读 `firecrawl_search_raw.id IN (10, 11)`。
- 促销只读 `firecrawl_scrape_raw.id IN (14, 15, 16)`。
- `source_url` 与 `business_id` 由程序注入，不接受 LLM 生成。
- 常规定价、融资说明和市场均价不得写为 promotion。
- 当前没有 `SUPABASE_WRITER_KEY`；数据库写入使用 Supabase MCP。
- one-off 脚本完成后删除；保留审计 JSON、设计和计划文档。
- 当前目录不是 Git 仓库，不执行提交步骤。

---

### Task 1: 以 TDD 实现提取结果校验

**Files:**
- Create temporarily: `one-off/masters_medspa_llm_extraction.py`
- Create temporarily: `tests/test_masters_medspa_llm_extraction.py`

**Interfaces:**
- Produces: `validate_service(item: dict, evidence: str) -> tuple[dict | None, str | None]`
- Produces: `validate_promotion(item: dict) -> tuple[dict | None, str | None]`

- [ ] **Step 1: 写失败的 unittest**

测试覆盖：

```python
def test_accepts_evidenced_botox_unit_price(self):
    accepted, error = module.validate_service(
        {
            "service_name_raw": "Botox",
            "service_name": "Botox",
            "service_category": "Neurotoxin",
            "regular_price": 15,
            "unit_type": "unit",
            "service_area": None,
            "source_url": "https://masters-medspa.com/pricing",
        },
        "Botox $15 / unit",
    )
    self.assertIsNone(error)
    self.assertEqual(accepted["regular_price"], 15.0)

def test_rejects_price_not_present_in_evidence(self):
    accepted, error = module.validate_service(
        {
            "service_name_raw": "Botox",
            "service_name": "Botox",
            "service_category": "Neurotoxin",
            "regular_price": 12,
            "unit_type": "unit",
            "service_area": None,
            "source_url": "https://masters-medspa.com/pricing",
        },
        "Botox $15 / unit",
    )
    self.assertIsNone(accepted)
    self.assertEqual(error, "price_not_in_evidence")

def test_rejects_regular_pricing_as_promotion(self):
    accepted, error = module.validate_promotion(
        {
            "promotion_title": "Pricing",
            "promotion_description": ["Botox $15 / unit"],
            "campaign_start_date": None,
            "campaign_end_date": None,
        }
    )
    self.assertIsNone(accepted)
    self.assertEqual(error, "no_promotion_signal")
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
python3 -m unittest tests/test_masters_medspa_llm_extraction.py -v
```

Expected: FAIL，因为校验函数尚不存在。

- [ ] **Step 3: 写最小校验实现**

`validate_service()` 必须校验：

```python
SERVICE_NAMES = {
    "Botox", "Dysport", "Daxxify", "Letybo", "Xeomin", "Jeuveau",
    "Dermal Filler", "Sculptra", "Juvéderm", "Restylane",
    "Belotero", "Radiesse", "Others",
}
SERVICE_CATEGORIES = {"Neurotoxin", "Filler", "others"}
UNIT_TYPES = {
    "unit", "syringe", "half_syringe", "vial", "treatment",
    "session", "package", "area", "ml", "mg", "others",
}
```

价格证据使用精确金额正则，不允许 `$15` 证据支持模型输出 `$12`。`validate_promotion()` 仅接受描述中含折扣、优惠、限时、免费加项、百分比折扣或原价/现价组合的记录。

- [ ] **Step 4: 运行测试并确认 GREEN**

Run:

```bash
python3 -m unittest tests/test_masters_medspa_llm_extraction.py -v
```

Expected: 3 tests pass。

### Task 2: 真实 LLM 提取并生成审计 JSON

**Files:**
- Modify temporarily: `one-off/masters_medspa_llm_extraction.py`
- Runtime output: `.firecrawl/masters-medspa/llm-extraction.json`

**Interfaces:**
- Consumes: Supabase raw IDs、`.env` LLM 配置、两个 schema JSON
- Produces: `accepted_services`、`rejected_services`、`accepted_promotions`、`rejected_promotions`

- [ ] **Step 1: 从 Supabase MCP 导出最小输入**

回查 Search raw 的 `response_json` 和 Scrape raw 的 `source_url, markdown`，写入已忽略的：

```text
.firecrawl/masters-medspa/llm-input.json
```

- [ ] **Step 2: 调用 Gemini structured output**

服务 prompt 明确要求：

```text
Extract explicit regular, non-promotional clinic service prices only.
Every row must preserve the visible service name and source URL.
Do not use blog market averages as this clinic's own price.
```

促销 prompt 明确要求：

```text
Extract only concrete discounts, limited-time campaigns, bundles, or free add-ons.
Regular price menus, financing, and general marketing are not promotions.
Return an empty promotions array when no promotion exists.
```

- [ ] **Step 3: 校验并生成审计文件**

审计 JSON 必须包含：

```json
{
  "business_id": 2889,
  "domain": "masters-medspa.com",
  "model": "gemini-3.1-flash-lite",
  "raw_ids": {"search": [10, 11], "scrape": [14, 15, 16]},
  "service_llm_output": {},
  "promotion_llm_outputs": [],
  "accepted_services": [],
  "rejected_services": [],
  "accepted_promotions": [],
  "rejected_promotions": []
}
```

- [ ] **Step 4: 检查审计结果**

Expected:

- 接受的每个服务价格均能在 Search raw 找到。
- 博客 `$10–$20/unit` 市场均价不成为服务行。
- 常规定价页不成为促销。
- 结构化输出不含 raw 表之外的事实。

### Task 3: 幂等写入并验证

**Files:**
- Read: `.firecrawl/masters-medspa/llm-extraction.json`
- Update: `README.md`
- Delete after completion: `one-off/masters_medspa_llm_extraction.py`
- Delete after completion: `tests/test_masters_medspa_llm_extraction.py`

**Interfaces:**
- Consumes: Task 2 审核通过的记录
- Produces: `clinic_services` / `clinic_promotions` live 行

- [ ] **Step 1: 记录写入前行数**

```sql
select count(*) from public.clinic_services where business_id=2889;
select count(*) from public.clinic_promotions where business_id=2889;
```

- [ ] **Step 2: 通过 Supabase MCP 幂等写入**

服务按 `(business_id, service_name)` 先更新后插入，字段限定为：

```text
business_id, service_name, service_name_raw, service_category,
regular_price, unit_type, service_area, source_url, updated_at
```

促销只有在 `accepted_promotions` 非空时按 `(business_id, source_url)` 更新或插入。

- [ ] **Step 3: 回查 live 表**

```sql
select service_id, service_name, service_name_raw, service_category,
       regular_price, unit_type, service_area, source_url
from public.clinic_services
where business_id=2889
order by service_name;

select promotion_id, promotion_title, promotion_description,
       campaign_start_date, campaign_end_date, source_url
from public.clinic_promotions
where business_id=2889
order by promotion_id;
```

Expected: 服务行与审计文件一致；若无真实促销，第二个查询返回空数组。

- [ ] **Step 4: 运行最终自检**

```bash
python3 -m unittest tests/test_masters_medspa_llm_extraction.py -v
```

Expected: 全部通过。

- [ ] **Step 5: 更新 README 并清理一次性代码**

README 记录本次 LLM 提取审计文档；删除 one-off 脚本和其临时测试，保留 `.firecrawl` 审计结果供本地复核。
