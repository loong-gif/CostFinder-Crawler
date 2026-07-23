# CostFinder

面向促销/价格信息抓取与入库的 Python 项目，主链路：

- **日常**: Firecrawl monitor diff → 变更提取 → Supabase events/master
- **社媒**: Instagram/Facebook 日常促销入库

## 架构

五表关系与数据流转说明见 [docs/data-model-pipeline.md](docs/data-model-pipeline.md)（`master_business_info` / `promo_website_staging` / `clinic_services` / `promo_offer_master` / `promo_membership_plans`）。  
服务目录价格防污染与历史修复记录见 [docs/service-price-lineage.md](docs/service-price-lineage.md)。

改爬虫、Schema、表关系或数据流时，先读项目 Skill [`.cursor/skills/costfinder-architecture/SKILL.md`](.cursor/skills/costfinder-architecture/SKILL.md)。域级自测示例：`louloumedspa.com`（见 Skill 内 `examples/`）。
促销服务与诊所服务目录的关联约定见 [促销服务匹配设计](docs/superpowers/specs/2026-07-18-promo-service-matching-design.md)。
单域 Firecrawl Search/Scrape raw 入库示例见 [Masters Medspa 设计](docs/superpowers/specs/2026-07-20-masters-medspa-firecrawl-raw-design.md) 与 [执行计划](docs/superpowers/plans/2026-07-20-masters-medspa-firecrawl-raw.md)。
对应的真实 LLM 服务/促销提取实测见 [提取设计](docs/superpowers/specs/2026-07-20-masters-medspa-llm-extraction-design.md) 与 [执行计划](docs/superpowers/plans/2026-07-20-masters-medspa-llm-extraction.md)；本地审计结果保存在 `.firecrawl/masters-medspa/llm-extraction.json`。
本轮批量 raw 严格门店 AI 提取实测见 [设计](docs/superpowers/specs/2026-07-20-recent-raw-ai-extraction-design.md) 与 [实施计划](docs/superpowers/plans/2026-07-20-recent-raw-ai-extraction.md)；运行审计保存在 `.firecrawl/master-business-search/ai-extraction-audit.json`。
本轮促销（promotion 7–11）Offer 抽取写入实测审计保存在 `.firecrawl/master-business-search/offer-extraction-audit.json`。
促销 `promotion_content` 修复审计见 `.firecrawl/master-business-search/promotion-content-repair-audit.json`。

```text
Daily (production path):
  Firecrawl monitor/check diff
  → utils/change_driven_extractor.py
  → promo_offer_change_events
  → audited apply to promo_offer_master

Fallback (manual / monthly):
  scripts/monthly_refresh_promo_website_staging.py
  scripts/detect_promo_website_staging_changes.py

Social media:
  scripts/daily_instagram_promo_ingestion.py
  scripts/daily_facebook_promo_ingestion.py

Do not run both daily and fallback simultaneously.
```

## 主要技术栈

- `Python 3.11+`：主开发语言与脚本运行环境
- `asyncio`：并发抓取与异步任务调度
- `BeautifulSoup4 (bs4)`：页面结构解析与链接抽取
- `Firecrawl`（自部署）：统一网页抓取（scrape / crawl）、变更监测、门控重爬
- `firecrawl-py`：Firecrawl SDK
- `Requests`：调用 Supabase PostgREST 与外部 HTTP 接口
- `Supabase (PostgREST)`：业务数据读取与入库目标
- `Pandas`：抓取结果整理与导出
- `Apify CLI / Actors`：仅用于社媒（Instagram/Facebook）抓取

## 当前项目结构

```text
costfinder/
├── requirements.txt
├── README.md
├── service_name_dict.json       # 共享服务名词典（LLM 提取 canonical 名）
├── pytest.ini
├── config/
│   ├── settings.py
│   └── user_agents.py
├── crawler/
│   ├── fetch_engine.py          # FirecrawlFetchEngine
│   ├── promo_site_crawler.py
│   └── staging_recrawl.py       # Firecrawl crawl 重爬 + staging 同步
├── utils/
│   ├── supabase_rest.py         # 共享 Supabase REST 客户端
│   ├── change_driven_extractor.py  # 变更驱动提取（生产路径）
│   ├── offer_extraction_llm.py     # LLM 提取 + service canonicalizer
│   ├── firecrawl_client.py
│   ├── monitor_target_urls.py
│   ├── staging_content_diff.py
│   ├── offer_evidence_segments.py
│   ├── align_service_names.py       # 服务名对齐（审计用）
│   ├── logger.py
│   └── vision_promo_ocr.py
├── scripts/
│   ├── firecrawl_monitor.py         # 创建 Firecrawl monitors
│   ├── firecrawl_monitor_poll.py    # 轮询 + 变更提取（日常路径）
│   ├── monthly_refresh_promo_website_staging.py  # 月度回退
│   ├── detect_promo_website_staging_changes.py   # 按需全量比对
│   ├── audit_expired_promo_offers.py
│   ├── audit_extraction_quality.py  # 五表 + raw lineage 统一审计
│   ├── apply_extraction_repairs.py  # 确定性修复（默认 dry-run）
│   ├── audit_promo_offer_master.py
│   ├── audit_promo_website_staging.py
│   ├── apply_sql_migration.py       # 通用 SQL 迁移运维
│   ├── daily_instagram_promo_ingestion.py
│   └── daily_facebook_promo_ingestion.py
├── one-off/                         # 一次性脚本（AI/人工临时任务，见 one-off/README.md）
├── tests/
├── schema/                          # LLM 输出约束；service_name 枚举与共享词典同步
│   ├── promotion_extraction_schema.json
│   ├── offer_extraction_schema.json
│   ├── membership_extraction_schema.json
│   └── service_extraction_schema.json
├── output/
│   ├── results/
│   ├── logs/
│   └── monitor_results/
└── docs/
```

## 提取 Schema 约定

`schema/` 下四个 JSON Schema 约束 LLM 结构化输出。每个 schema 顶层首字段为必填 `explanation`（字符串），用于让模型先写简短、可审计的证据摘要（依据、歧义、空值原因），再输出业务数组（`promotions` / `offers` / `memberships` / `services`）。

- `explanation` 是生成提示与审计元数据，**不是**入库业务字段；下游解析可忽略。
- JSON Schema 标准不保证属性输出顺序；首字段位置是对结构化输出生成器的顺序提示，并非真正的两次模型调用解耦。
- `offer_extraction_schema.json` 一次提取报价及其嵌套 `items`，并排除 example / comparison 等非真实报价；`items.service_name` 与 `service_extraction_schema.json` 使用相同枚举，per-unit 报价未声明购买数量时 `quantity=null`，套餐没有明确单项价格时 `unit_price=null`。
- `membership_extraction_schema.json` 仅输出页面可证实的会员业务字段；`business_id` 与 `source_url` 由程序注入，`benefits` 保留为独立的分段原文数组。

## 环境准备

```bash
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -r requirements.txt
```

`.env` 常用变量（按需）：
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `FIRECRAWL_API_KEY`
- `FIRECRAWL_API_URL`（自部署实例，如 `http://72.52.161.65:3002`）
- `FIRECRAWL_CRAWL_MAX_PAGES`（默认 50）
- `FIRECRAWL_CRAWL_TIMEOUT_SECS`（默认 1800）
- `TZ`（默认影响社媒脚本本地日期判断）

## 消费者认证（Frontend）

Consumer auth: Supabase email/password via the frontend Header modal.

Schema prerequisite: `config/sql/m017_consumer_auth_profiles.sql`（`profiles` 表、RLS、消费者建档触发器）。

Deal claims prerequisite: `config/sql/m018_claims.sql`（`claims` 表、`claim_status` 枚举、RLS）。

Supabase Auth prerequisite: 在 Dashboard 关闭 **Confirm email**，注册后直接建立会话。

部署迁移：

```bash
python scripts/apply_sql_migration.py config/sql/m017_consumer_auth_profiles.sql
```

前端本地开发见 `frontend/.env.example`；运行 `cd frontend && npm run dev`。

## 运行方式

### 1) 日常变更监测 + 提取（Firecrawl monitor）

这是**日常生产路径**。Firecrawl monitor 检测 promo/pricing 子页变更，change_driven_extractor 提取差异后写入 promo_offer_change_events。只有检测到有意义变更时才触发重爬。

**模块职责（重构后）**

| 模块 | 职责 |
| --- | --- |
| [`scripts/firecrawl_monitor_poll.py`](scripts/firecrawl_monitor_poll.py) | 轮询 monitor、选择待处理 check、协调 change-driven 与 Apify 回退、推进游标 |
| [`utils/change_driven_extractor.py`](utils/change_driven_extractor.py) | 从 diff 构建 LLM 输入、校验 action、生成 change events、门禁后写入 master |
| [`crawler/staging_recrawl.py`](crawler/staging_recrawl.py) | Firecrawl crawl 重爬与 `promo_website_staging` 同步 |

**生产路径回归**

```bash
python -m pytest tests/test_change_driven_extractor.py tests/test_monitor_db_update_flow.py tests/test_monitor_target_urls.py -v
python scripts/firecrawl_monitor_poll.py --dry-run --limit 1   # 需 FIRECRAWL + SUPABASE_WRITER_KEY
```

**后续复杂度治理批次（未在本轮实施）**

1. 社媒入库：合并 `daily_instagram_promo_ingestion.py` 与 `daily_facebook_promo_ingestion.py` 的重复 payload/回退逻辑
2. 网页抓取：拆分 `promo_site_crawler.py`、`staging_recrawl.py` 的纯提取与 I/O 提交
3. 审计写入：将 `promo_offer_audit.py`、`audit_expired_promo_offers.py`、`membership_plans.py` 的规则拆为小型可测函数

### 2) 社媒日常入库

```bash
python scripts/daily_instagram_promo_ingestion.py
python scripts/daily_facebook_promo_ingestion.py
```

两者都支持 `--dry-run`（仅产出报告，不写入 Supabase）。社媒链路仍使用 Apify Actors，与网页抓取分离。

### 3) Firecrawl 变更监测 + 门控重爬

Firecrawl monitor 检测 promo/pricing 子页变更，只有检测到有意义变更时才调用 Firecrawl `crawl` 重爬并回写 `promo_website_staging`。

Monitor 目标 URL 来自 `promo_website_staging.subpage_url`：按 [`utils/monitor_target_urls.py`](utils/monitor_target_urls.py) 为每个域名选取 1–2 条高分 promo/special/pricing 页。

### 3) 月度回退（fallback）—— staging 全量刷新

```bash
python scripts/monthly_refresh_promo_website_staging.py --once-per-month
```

按需全量比对 staging 变更：

```bash
python scripts/detect_promo_website_staging_changes.py --limit 20
python scripts/detect_promo_website_staging_changes.py --apply --limit 50
```

> ⚠️ **不要同时运行日常路径和 fallback。**

1. 在 Supabase SQL Editor 执行 [`config/sql/promo_monitor_state.sql`](config/sql/promo_monitor_state.sql)（若未建表，脚本会降级到 `output/monitor_state_fallback.json`）。
2. 在 `.env` 中配置 `FIRECRAWL_API_KEY`、`FIRECRAWL_API_URL`、`SUPABASE_URL`、`SUPABASE_SERVICE_ROLE_KEY`。
3. 为待监测域名创建 Firecrawl monitors：

```bash
python scripts/firecrawl_monitor.py create-all --limit 10
python scripts/firecrawl_monitor.py list
```

**轮询闭环（本地 cron）**

```bash
python scripts/firecrawl_monitor_poll.py --dry-run --limit 5
python scripts/firecrawl_monitor_poll.py
python scripts/firecrawl_monitor_poll.py --monitor-id <id> --dry-run
```

常用参数：
- `--dry-run`：只检测与出报告，不重爬、不推进游标（`last_check_id`）；但首次解析域名时仍可能写入 `promo_monitor_state` 的 domain mapping（`upsert_mapping`），与 README 旧表述「完全不写 state」不同，属既有行为
- `--max-crawl-pages`：Firecrawl crawl 单域最大页数
- `--crawl-timeout-secs`：Firecrawl crawl 超时

报告输出：`output/monitor_results/firecrawl_monitor_poll_*.json`

### 4) 审计与其它工具

```bash
python scripts/audit_extraction_quality.py      # 五表质量审计（非零退出码 = 有 blocking 问题）
python scripts/apply_extraction_repairs.py      # 修复计划 dry-run
python scripts/apply_extraction_repairs.py --batch service_price_lineage  # 服务目录价溯源修复
python scripts/apply_extraction_repairs.py --apply
python scripts/audit_expired_promo_offers.py
python scripts/audit_promo_offer_master.py      # legacy：仅 promo_offer_master
python scripts/audit_promo_website_staging.py
```

### 5) promo_offer_master 质量运维

生产链路已在写入时使用 `offer_fingerprint` 去重。

如需部署 schema 变更，继续使用：

```bash
python scripts/apply_sql_migration.py config/sql/m016_extraction_quality_guardrails.sql
```

## 输出位置

- 结果文件：`output/results/`
- 监测报告：`output/monitor_results/`
- 审计报告：`reports/`（与 `output/` 同为运行产物，已 gitignore）
- 日志文件：`output/logs/`

## 脚本维护边界

- `scripts/` 只保留可重复运行、已文档化的生产链路和审计入口。
- **`one-off/`** 是**唯一**允许放置一次性脚本的位置（迁移修复、单域 bootstrap、临时诊断等）。AI 产出的一次性脚本**必须**放在 [`one-off/`](one-off/)，详见 [`one-off/README.md`](one-off/README.md)。
- `crawler/`、`utils/` 保存可复用的抓取、清洗、入库和业务规则。
- schema 部署 SQL 仍保留在 `config/sql/`，由 `scripts/apply_sql_migration.py` 执行。
- 已完成的一次性脚本在任务结束后从 `one-off/` 删除；更早的历史见 `git log`。

## Production safety and notifications

Active writers require `SUPABASE_WRITER_KEY`. `SUPABASE_SERVICE_ROLE_KEY` is reserved for migrations and controlled administration; `ALLOW_SERVICE_ROLE_WRITES=true` is an explicit rollback-only diagnostic override.

The production schema is currently the legacy schema (`promo_offer_master.status` plus an existing `promo_monitor_state`). Do not run the old `offer_evidence_pipeline.sql` or legacy M004 directly. Use the staged migrations below, each with a dry-run and a read-only preflight first:

```text
config/sql/m004_safety_invariants.sql       # outbox + operation_runs only
config/sql/m005_monitor_state_leases.sql   # existing monitor table only
config/sql/m006_evidence_pipeline_bootstrap.sql # new evidence tables only
```

M006 deliberately does not change `promo_offer_master.status`; lifecycle compatibility/backfill remains a separate reviewed migration.

Set `SLACK_NOTIFICATIONS_ENABLED=true` only after validating the outbox schema. Install the user-level unit from `config/systemd/hermes-notification-worker.service` after configuring a restricted `SUPABASE_WRITER_KEY`; the worker uses the local Hermes CLI and deterministic text fallback. `#costfinder-ops` should be stored as its immutable Slack channel ID in outbox targets.
