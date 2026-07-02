# CostFinder

面向促销/价格信息抓取与入库的 Python 项目，当前主链路包含：
- 通用网页抓取（`main.py`）
- 站点促销页抓取并导出（`scripts/crawl_promo_website_staging.py`）
- Firecrawl 变更监测 + 门控重爬（`scripts/firecrawl_monitor*.py`）
- Instagram/Facebook 日常促销入库（`scripts/daily_*_promo_ingestion.py`）

## 主要技术栈

- `Python 3.11+`：主开发语言与脚本运行环境
- `asyncio`：并发抓取与异步任务调度
- `BeautifulSoup4 (bs4)`：页面结构解析与内容抽取
- `Jina Reader API`：站点页面读取与结构化抽取主入口（`https://r.jina.ai/<url>`）
- `Requests`：调用 Supabase PostgREST 与外部 HTTP 接口
- `Supabase (PostgREST)`：业务数据读取与入库目标
- `Pandas`：抓取结果整理与导出
- `PaddleOCR`（可选）：OCR 兜底识别价格/促销文本
- `Apify CLI / Actors`：社媒（Instagram/Facebook）抓取链路执行
- `Firecrawl`：站点变更监测（changeTracking + 轮询触发重爬）
- `Playwright / Lightpanda`（可选）：仅用于少量 JS-heavy 页面诊断、补抓与 OCR 辅助，不属于主抓取链路

## 当前项目结构（主链路）

```text
costfinder/
├── main.py
├── requirements.txt
├── README.md
├── config/
│   ├── settings.py
│   ├── user_agents.py
│   └── readerlm_offer_schema.json
├── crawler/
│   ├── browser_manager.py
│   ├── page_parser.py
│   ├── ocr_parser.py
│   ├── fetch_engine.py
│   ├── jina_reader_client.py
│   ├── promo_site_crawler.py
│   └── staging_recrawl.py
├── utils/
│   ├── logger.py
│   ├── retry.py
│   ├── data_cleaner.py
│   ├── caption_price_filter.py
│   ├── instagram_promo_filter.py
│   ├── facebook_promo_filter.py
│   ├── offer_extraction_llm.py
│   └── page_content_processor.py
├── scripts/
│   ├── crawl_promo_website_staging.py
│   ├── firecrawl_monitor.py
│   ├── firecrawl_monitor_poll.py
│   ├── daily_instagram_promo_ingestion.py
│   ├── daily_facebook_promo_ingestion.py
│   └── lightpanda_crawl_needs_ocr.py
├── output/
│   ├── results/
│   ├── logs/
│   └── screenshots/
└── CF_Extrator_Agent/
```

说明：`scripts/one_off/` 为一次性/阶段性脚本归档区，不属于日常主链路。

## 环境准备

```bash
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -r requirements.txt
```

主链路默认不安装浏览器抓取依赖。

如需使用浏览器型诊断/补抓脚本，再额外安装可选依赖和浏览器运行时：

```bash
uv pip install -r requirements_browser_tools.txt
PLAYWRIGHT_BROWSERS_PATH=.playwright_browsers playwright install chromium
```

`.env` 常用变量（按需）：
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `JINA_READER_API_KEY`（也可通过根目录密钥文件加载）
- `FIRECRAWL_API_KEY`（Firecrawl 变更监测）
- `TZ`（默认影响社媒脚本本地日期判断）

## 运行方式

### 1) 通用价格抓取

```bash
python main.py
```

常用参数：

```bash
python main.py --limit 20
python main.py --workers 5
python main.py --format csv json excel
python main.py --debug
python main.py --ocr-only
```

### 2) 站点促销页抓取（Jina Reader）

```bash
python scripts/crawl_promo_website_staging.py --limit 20 --concurrency 3
```

该入口已固定使用 `Jina Reader API`，不再提供浏览器引擎切换。

### 3) 社媒日常入库

```bash
python scripts/daily_instagram_promo_ingestion.py
python scripts/daily_facebook_promo_ingestion.py
```

两者都支持 `--dry-run`（仅产出报告，不写入 Supabase）。

### 4) Firecrawl 变更监测 + 门控重爬

先用 Firecrawl 云监测检测页面是否变更，只有检测到有意义变更时才调用 Apify actor 重爬（复用 actor 侧清洗逻辑）并回写 `promo_website_staging`。

**一次性准备**

1. 在 Supabase SQL Editor 执行 [`config/sql/promo_monitor_state.sql`](config/sql/promo_monitor_state.sql)（若未建表，脚本会降级到 `output/monitor_state_fallback.json`）。
2. 在 `.env` 中配置 `FIRECRAWL_API_KEY`、`SUPABASE_URL`、`SUPABASE_SERVICE_ROLE_KEY`。
3. 为待监测域名创建 Firecrawl monitors（会写入 `promo_monitor_state` 映射）：

```bash
python scripts/firecrawl_monitor.py create-all --limit 10
python scripts/firecrawl_monitor.py list
```

**轮询闭环（本地 cron，无需公网 webhook）**

```bash
# 首次建议 dry-run，确认会触发哪些域名
python scripts/firecrawl_monitor_poll.py --dry-run --limit 5

# 正式跑：仅处理增量 check，有变更才重爬
python scripts/firecrawl_monitor_poll.py

# 单 monitor 调试
python scripts/firecrawl_monitor_poll.py --monitor-id <id> --dry-run
```

常用参数：
- `--dry-run`：只检测与出报告，不重爬、不写 state
- `--limit N`：只处理前 N 个 monitor
- `--monitor-id <id>`：只处理指定 monitor
- `--since-check <check_id>`：从指定 check 之后重新处理
- `--force-latest`：跳过 baseline 初始化，直接处理最新 check

**cron 示例**（Firecrawl 每日 check 完成后执行，例如 UTC 09:00）：

```cron
0 9 * * * cd /path/to/costfinder && .venv/bin/python scripts/firecrawl_monitor_poll.py >> output/logs/firecrawl_monitor_poll.log 2>&1
```

**辅助脚本**

```bash
python scripts/analyze_all_monitors.py          # 查看全部 monitor 健康度与变更概览
python scripts/firecrawl_monitor.py checks --monitor-id <id>
python scripts/firecrawl_monitor.py check-detail --monitor-id <id> --check-id <id>
```

报告输出：`output/monitor_results/firecrawl_monitor_poll_*.json`

### 5) 浏览器诊断/补抓工具（可选）

以下脚本保留用于个别 `Jina` 抓不到、需要渲染后文本或 OCR 的页面排查，不属于日常主链路：

```bash
python scripts/playwright_crawl.py
python scripts/lightpanda_crawl_needs_ocr.py
python scripts/one_off/recrawl_update_page_content_playwright_from_csv.py
```

这些工具依赖 [requirements_browser_tools.txt](/Users/wyl/costfinder/requirements_browser_tools.txt:1)，不会随主链路默认安装。

## 输出位置

- 结果文件：`output/results/`
- 监测报告：`output/monitor_results/`
- 日志文件：`output/logs/`
- 截图文件：`output/screenshots/`

## 子项目说明

`CF_Extrator_Agent/` 是独立的 offer 抽取/评测子项目，与根目录抓取链路并行维护。

## 不纳入本 README 的内容

为保持主文档清晰，以下内容按“历史/临时用途”处理：
- `scripts/one_off/` 下的一次性修复、审计、回填脚本
- 根目录中临时分析/对齐类脚本（如历史数据处理脚本）
