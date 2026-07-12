# CostFinder

面向促销/价格信息抓取与入库的 Python 项目，主链路：

- **日常**: Firecrawl monitor diff → 变更提取 → Supabase events/master
- **社媒**: Instagram/Facebook 日常促销入库

## 架构

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
│   ├── audit_promo_offer_master.py
│   ├── audit_promo_website_staging.py
│   ├── apply_sql_migration.py       # 通用 SQL 迁移运维
│   ├── daily_instagram_promo_ingestion.py
│   ├── daily_facebook_promo_ingestion.py
│   └── archive/                     # 历史一次性脚本（不再维护）
├── tests/
├── CF_Extrator_Agent/
│   └── data/service_name_dict.json  # 共享服务名词典
├── output/
│   ├── results/
│   ├── logs/
│   └── monitor_results/
└── docs/
```

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

自部署 Firecrawl 引导见 [`docs/self_hosted_firecrawl_wsl.md`](docs/self_hosted_firecrawl_wsl.md)。

## 运行方式

### 1) 日常变更监测 + 提取（Firecrawl monitor）

这是**日常生产路径**。Firecrawl monitor 检测 promo/pricing 子页变更，change_driven_extractor 提取差异后写入 promo_offer_change_events。只有检测到有意义变更时才触发重爬。

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
- `--dry-run`：只检测与出报告，不重爬、不写 state
- `--max-crawl-pages`：Firecrawl crawl 单域最大页数
- `--crawl-timeout-secs`：Firecrawl crawl 超时

报告输出：`output/monitor_results/firecrawl_monitor_poll_*.json`

### 4) 审计与其它工具

```bash
python scripts/audit_expired_promo_offers.py
python scripts/audit_promo_offer_master.py
python scripts/audit_promo_website_staging.py
```

### 5) promo_offer_master 质量运维

生产链路已在写入时使用 `offer_fingerprint` 去重。历史数据去重、回填、规范化和迁移脚本已移至 [`scripts/archive/`](scripts/archive/)，仅用于审计或受控恢复。

如需部署 schema 变更，继续使用：

```bash
python scripts/apply_sql_migration.py config/sql/m003_promo_offer_fingerprint.sql
```

## 输出位置

- 结果文件：`output/results/`
- 监测报告：`output/monitor_results/`
- 审计报告：`reports/`（与 `output/` 同为运行产物，已 gitignore）
- 日志文件：`output/logs/`

## 脚本维护边界

- `scripts/` 只保留可重复运行、已文档化的生产链路和审计入口。
- `crawler/`、`utils/` 保存可复用的抓取、清洗、入库和业务规则。
- 已执行的数据修复、迁移、实验和临时诊断脚本放在 [`scripts/archive/`](scripts/archive/)，不纳入正常测试与调度。
- schema 部署 SQL 仍保留在 `config/sql/`，由 `scripts/apply_sql_migration.py` 执行。

## 不纳入本 README 的内容

- `scripts/archive/`：历史一次性脚本与部署 bootstrap，不再维护

## Production safety and notifications

Active writers require `SUPABASE_WRITER_KEY`. `SUPABASE_SERVICE_ROLE_KEY` is reserved for migrations and controlled administration; `ALLOW_SERVICE_ROLE_WRITES=true` is an explicit rollback-only diagnostic override.

The safety migration is `config/sql/m004_safety_invariants.sql`. Run it with `scripts/apply_sql_migration.py --dry-run` first, then apply only after reviewing the preflight output. It creates the migration ledger, canonical offer lifecycle checks, durable notification outbox, and outbox RPC transitions.

Set `SLACK_NOTIFICATIONS_ENABLED=true` only after validating the outbox schema. Install the user-level unit from `config/systemd/hermes-notification-worker.service` after configuring a restricted `SUPABASE_WRITER_KEY`; the worker uses the local Hermes CLI and deterministic text fallback. `#costfinder-ops` should be stored as its immutable Slack channel ID in outbox targets.
