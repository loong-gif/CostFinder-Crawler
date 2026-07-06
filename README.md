# CostFinder

面向促销/价格信息抓取与入库的 Python 项目，当前主链路包含：
- 站点促销页抓取并导出（`scripts/crawl_promo_website_staging.py`）
- Firecrawl 统一网页抓取 + 变更监测 + 门控重爬（`scripts/firecrawl_monitor*.py`）
- Instagram/Facebook 日常促销入库（`scripts/daily_*_promo_ingestion.py`）

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

## 当前项目结构（主链路）

```text
costfinder/
├── requirements.txt
├── README.md
├── config/
│   ├── settings.py
│   └── user_agents.py
├── crawler/
│   ├── fetch_engine.py          # FirecrawlFetchEngine
│   ├── promo_site_crawler.py
│   └── staging_recrawl.py       # Firecrawl crawl 重爬 + staging 同步
├── utils/
│   ├── firecrawl_client.py      # 共享 Firecrawl SDK 工厂
│   ├── logger.py
│   ├── offer_extraction_llm.py
│   └── page_content_processor.py
├── scripts/
│   ├── crawl_promo_website_staging.py
│   ├── firecrawl_monitor.py
│   ├── firecrawl_monitor_poll.py
│   ├── detect_promo_website_staging_changes.py
│   ├── monthly_refresh_promo_website_staging.py
│   ├── daily_instagram_promo_ingestion.py
│   └── daily_facebook_promo_ingestion.py
└── output/
    ├── results/
    ├── logs/
    └── monitor_results/
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

### 1) 站点促销页抓取（Firecrawl）

```bash
python scripts/crawl_promo_website_staging.py --limit 20 --concurrency 3
```

所有页面抓取经 `FirecrawlFetchEngine` → 自部署 Firecrawl `scrape` API。

### 2) 社媒日常入库

```bash
python scripts/daily_instagram_promo_ingestion.py
python scripts/daily_facebook_promo_ingestion.py
```

两者都支持 `--dry-run`（仅产出报告，不写入 Supabase）。社媒链路仍使用 Apify Actors，与网页抓取分离。

### 3) Firecrawl 变更监测 + 门控重爬

Firecrawl monitor 检测 promo/pricing 子页变更，只有检测到有意义变更时才调用 Firecrawl `crawl` 重爬并回写 `promo_website_staging`。

Monitor 目标 URL 来自 `promo_website_staging.subpage_url`：按 [`utils/monitor_target_urls.py`](utils/monitor_target_urls.py) 为每个域名选取 1–2 条高分 promo/special/pricing 页。

与 [`scripts/detect_promo_website_staging_changes.py`](scripts/detect_promo_website_staging_changes.py) 的分工：
- **Firecrawl monitor**：每日增量 diff + `meaningful` 门控，有变更时触发 Firecrawl 全域重爬
- **staging detect**：对 staging 全表 `subpage_url` 做 Firecrawl 重爬 + hash 对比，适合周期性全量审计

**一次性准备**

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

### 4) 检测 staging 页面内容是否变更

对 `promo_website_staging` 中每条 `subpage_url` 用 Firecrawl 重爬，与现有 `page_content` 做 hash 对比。

```bash
python scripts/detect_promo_website_staging_changes.py --limit 20
python scripts/detect_promo_website_staging_changes.py --apply --limit 50
```

### 5) 月度全量刷新

```bash
python scripts/monthly_refresh_promo_website_staging.py --once-per-month
```

## 输出位置

- 结果文件：`output/results/`
- 监测报告：`output/monitor_results/`
- 日志文件：`output/logs/`

## 不纳入本 README 的内容

- `scripts/one_off/` 已移除；历史一次性脚本不再维护
- 根目录临时分析/对齐类脚本
