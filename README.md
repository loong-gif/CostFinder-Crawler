# CostFinder

面向促销/价格信息抓取与入库的 Python 项目，当前主链路包含：
- 通用网页抓取（`main.py`）
- 站点促销页抓取并导出（`scripts/crawl_promo_website_staging.py`）
- Instagram/Facebook 日常促销入库（`scripts/daily_*_promo_ingestion.py`）

## 主要技术栈

- `Python 3.11+`：主开发语言与脚本运行环境
- `asyncio`：并发抓取与异步任务调度
- `Playwright`：动态页面渲染与浏览器自动化抓取
- `BeautifulSoup4 (bs4)`：页面结构解析与内容抽取
- `Jina Reader API`：站点页面读取与结构化抽取入口（`https://r.jina.ai/<url>`）
- `Requests`：调用 Supabase PostgREST 与外部 HTTP 接口
- `Supabase (PostgREST)`：业务数据读取与入库目标
- `Pandas`：抓取结果整理与导出
- `PaddleOCR`（可选）：OCR 兜底识别价格/促销文本
- `Apify CLI / Actors`：社媒（Instagram/Facebook）抓取链路执行

## 当前项目结构（主链路）

```text
costfinder/
├── main.py
├── requirements.txt
├── ARCHITECTURE.md
├── README.md
├── config/
│   ├── settings.py
│   ├── user_agents.py
│   └── readerlm_offer_schema.json
├── crawler/
│   ├── base_crawler.py
│   ├── browser_manager.py
│   ├── page_parser.py
│   ├── ocr_parser.py
│   ├── jina_reader_client.py
│   └── promo_site_crawler.py
├── utils/
│   ├── logger.py
│   ├── retry.py
│   ├── data_cleaner.py
│   ├── caption_price_filter.py
│   ├── instagram_promo_filter.py
│   ├── facebook_promo_filter.py
│   └── offer_extraction_llm.py
├── scripts/
│   ├── crawl_promo_website_staging.py
│   ├── daily_instagram_promo_ingestion.py
│   ├── daily_facebook_promo_ingestion.py
│   ├── start_lightpanda_cdp.sh
│   └── run_costfinder_with_lightpanda.sh
├── output/
│   ├── results/
│   ├── logs/
│   └── screenshots/
└── cf-crawler-bs4/
```

说明：`scripts/one_off/` 为一次性/阶段性脚本归档区，不属于日常主链路。

## 环境准备

```bash
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH=.playwright_browsers playwright install chromium
```

`.env` 常用变量（按需）：
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `JINA_READER_API_KEY`（也可通过根目录密钥文件加载）
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

### 3) 社媒日常入库

```bash
python scripts/daily_instagram_promo_ingestion.py
python scripts/daily_facebook_promo_ingestion.py
```

两者都支持 `--dry-run`（仅产出报告，不写入 Supabase）。

### 4) 使用 Lightpanda 作为外部 CDP（可选）

```bash
./scripts/start_lightpanda_cdp.sh
./scripts/run_costfinder_with_lightpanda.sh --limit 5
```

## 输出位置

- 结果文件：`output/results/`
- 日志文件：`output/logs/`
- 截图文件：`output/screenshots/`

## 子项目说明

`cf-crawler-bs4/` 是独立的 Apify Actor 子项目（包含其自身 `README.md`、`requirements.txt`、`tests/` 与 `src/`），与根目录脚本并行维护。

## 不纳入本 README 的内容

为保持主文档清晰，以下内容按“历史/临时用途”处理：
- `scripts/one_off/` 下的一次性修复、审计、回填脚本
- 根目录中临时分析/对齐类脚本（如历史数据处理脚本）
