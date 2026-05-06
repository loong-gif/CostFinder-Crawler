# CostFinder - 葡萄酒价格爬虫系统

## 项目概述
高效爬取多个葡萄酒电商网站的价格信息,支持JavaScript动态加载页面,采用截图+OCR技术解决复杂渲染问题。

## 技术栈
- **Python 3.11+**
- **Jina Reader API**: 站内页面抓取与内容抽取（`https://r.jina.ai/<url>`）
- **Playwright**: 浏览器自动化,处理JavaScript动态加载
- **PaddleOCR**: 高精度OCR识别(备选:Tesseract/EasyOCR)
- **BeautifulSoup4**: HTML解析
- **Pandas**: 数据处理
- **asyncio**: 异步并发

## 系统架构

### 核心模块
```
costfinder/
├── config/
│   ├── settings.py          # 配置管理
│   └── user_agents.py       # User-Agent池
├── crawler/
│   ├── base_crawler.py      # 基础爬虫类
│   ├── browser_manager.py   # 浏览器管理
│   ├── page_parser.py       # 页面解析器
│   └── ocr_parser.py        # OCR解析器
├── utils/
│   ├── logger.py            # 日志管理
│   ├── retry.py             # 重试机制
│   └── data_cleaner.py      # 数据清洗
├── output/
│   ├── screenshots/         # 截图存储
│   ├── results/             # 爬取结果
│   └── logs/                # 运行日志
├── main.py                  # 主程序入口
└── requirements.txt         # 依赖管理
```

## 技术路线

### 1. 爬取策略(三层fallback)
- **Level 1**: 直接DOM解析(最快,优先)
- **Level 2**: 等待动态加载 + DOM解析
- **Level 3**: 全页截图 + OCR识别(兜底)

### 2. 性能优化
- 异步并发(最多10个页面同时爬取)
- 浏览器实例复用
- 智能等待策略(避免过度等待)
- 缓存已爬取数据

### 3. 反爬虫对策
- 随机User-Agent
- 随机延迟(1-3秒)
- Stealth模式(隐藏自动化特征)
- 可选代理IP池

### 4. 数据提取目标
- 葡萄酒名称
- 年份/产区
- 价格(美元)
- 库存状态
- 商家评分(如有)
- 来源URL

## 快速开始

### 1. 安装依赖
```bash
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH=.playwright_browsers playwright install chromium
```

可选：如果你有外部 CDP 浏览器（例如独立启动的浏览器服务），可设置：
`CRAWL4AI_CDP_URL=http://127.0.0.1:9222`

### 1.1 使用 Lightpanda 作为外部 CDP 浏览器（推荐）
```bash
# 启动 Lightpanda CDP（Docker）
./scripts/start_lightpanda_cdp.sh

# 探活（返回 Browser/Protocol 信息即正常）
curl -s http://127.0.0.1:9222/json/version

# 使用 Lightpanda 运行爬虫
./scripts/run_costfinder_with_lightpanda.sh --limit 5
```

如果你的 Lightpanda 不在默认地址，可覆盖：
```bash
CRAWL4AI_CDP_URL=http://127.0.0.1:9333 ./scripts/run_costfinder_with_lightpanda.sh --limit 5
```

### 2. 运行爬虫
```bash
# 标准模式(混合策略)
python main.py

# 仅OCR模式
python main.py --ocr-only

# 指定并发数
python main.py --workers 5

# 调试模式(显示浏览器)
python main.py --debug
```

### 2.1 运行 promo_website_staging 抓取（Jina Reader）
```bash
python scripts/crawl_promo_website_staging.py --limit 20 --concurrency 3
```

### 2.2 使用 Inference API（schematron-v2-small）做结构化抽取
```bash
python scripts/extract_with_schematron_v2_small_api.py \
  --url https://laserlightsc.com/specials/ \
  --source-name "Laser Light Skin Clinic" \
  --model inference-net/schematron-v2-small \
  --output output/results/laserlightsc_specials_schematron_v2_small_result.json
```

`api_key.txt` 中需要包含 `SCHEMATRON_API_KEY=...`

可选环境变量：
- `JINA_READER_BASE_URL`（默认 `https://r.jina.ai`）
- `JINA_READER_TIMEOUT`（默认 `45` 秒）
- `JINA_READER_USE_JSON_MODE`（默认 `true`）
- `JINA_READER_NO_CACHE`（默认 `false`）
- `JINA_READER_API_KEY`（可选，Reader API Key）
- `JINA_READER_RESPOND_WITH`（例如 `readerlm-v2`）
- `JINA_READER_JSON_SCHEMA`（JSON 字符串，用于结构化输出）
- `JINA_READER_JSON_SCHEMA_FILE`（JSON Schema 文件路径，默认 `config/readerlm_offer_schema.json`）
- `JINA_READER_INSTRUCTION`（自然语言抽取指令）

### 2.2 使用 Schematron V2 Small 处理网页文本（Inference.net）
```bash
# 可选：先导出环境变量（官方文档推荐）
export INFERENCE_API_KEY=<your-inference-api-key>

# 处理任意网页文本并输出结构化 JSON
python scripts/extract_with_schematron_v2_small.py \
  --url "https://example.com" \
  --instruction "提取页面里的关键服务、价格和联系方式，返回结构化 JSON"
```

说明：
- 脚本默认模型：`inference-net/schematron-v2-small`
- API Base 默认：`https://api.inference.net/v1`
- 默认使用 Jina Reader 抓取网页正文（`--fetch-mode jina`）
- 可选直连目标 URL 抓取（`--fetch-mode direct`，`requests + BeautifulSoup`）
- 若未设置 `INFERENCE_API_KEY`，脚本会回退读取项目根目录 `api_key.txt` 中的 `SCHEMATRON_API_KEY`
- 可通过 `--schema-file config/xxx.json` 提示模型按指定 JSON Schema 输出

### 3. 查看结果
```bash
# CSV格式
output/results/wine_prices_YYYYMMDD_HHMMSS.csv

# JSON格式
output/results/wine_prices_YYYYMMDD_HHMMSS.json
```

## 核心算法

### 智能解析流程
1. **快速检测**: 先尝试直接获取价格元素
2. **动态等待**: 检测到异步加载则等待(最多10秒)
3. **OCR兜底**: 前两步失败则截图OCR

### OCR区域识别
- 价格区域: 通常在页面右侧/商品卡片
- 关键词匹配: `$`, `price`, `USD`
- 正则提取: `\$?\d+\.\d{2}`

## 配置说明

### settings.py
```python
# 并发设置
MAX_WORKERS = 10
REQUEST_TIMEOUT = 30

# 重试设置
MAX_RETRIES = 3
RETRY_DELAY = 2

# OCR设置
OCR_ENGINE = "paddleocr"  # paddleocr/tesseract/easyocr
OCR_LANG = "en"

# 截图设置
SCREENSHOT_QUALITY = 90
FULL_PAGE_SCREENSHOT = True
```

## 输出格式

### CSV示例
```csv
Brand,Wine_Name,Year,Region,Price_USD,Stock,Merchant,URL,Crawl_Time
THACHER,Petillant Naturel,2024,Paso Robles,24.99,In Stock,thewinecountry.com,https://...,2025-12-11 10:30:00
```

## 性能指标
- 平均速度: 3-5秒/页面
- 并发处理: 10页面/批次
- 预计总时间: ~10分钟(162个URL)
- OCR准确率: >95%

## 错误处理
- 网络超时: 自动重试3次
- 反爬虫检测: 自动降速
- OCR失败: 记录到错误日志
- 价格缺失: 标记为"N/A"

## 未来优化
- [ ] 增加代理IP池
- [ ] 支持更多OCR引擎
- [ ] 实时数据库存储
- [ ] Web可视化界面
- [ ] 定时任务调度
- [ ] 价格变化监控

## Flask Fine Wines 爬虫

项目包含专门的 Flask Fine Wines 价格爬虫 (`flask_price_crawler.py`)，用于爬取 `flaskfinewines.com` 的商品价格和链接。

### 功能特性
- ✅ 提取主商品信息(名称、价格、SKU、库存、描述)
- ✅ 提取推荐商品/相关商品
- ✅ 自动提取商品链接(完整URL)
- ✅ 支持多商品去重
- ✅ CSV格式输出

### 快速使用

#### 单个URL爬取
```bash
# 测试爬取(显示浏览器)
python3 test_flask_crawler.py

# 命令行使用
python3 flask_price_crawler.py --url "https://flaskfinewines.com/products/xxx" --headless
```

#### 批量爬取(从input_websites.txt读取)
```bash
# 方法1: 使用批量脚本(推荐)
python3 batch_crawl.py --headless

# 方法2: 使用Shell脚本
./run_batch_crawl.sh --headless

# 方法3: 直接使用flask_price_crawler.py
python3 flask_price_crawler.py --file input_websites.txt --headless

# 只处理前10个URL(测试)
python3 batch_crawl.py --headless --max-urls 10
```

**批量爬取详细说明**: 请参考 [BATCH_CRAWL_README.md](./BATCH_CRAWL_README.md)

### 原理说明
详细的技术原理和工作流程请参考: **[FLASK_CRAWLER_PRINCIPLES.md](./FLASK_CRAWLER_PRINCIPLES.md)**

原理文档包含:
- 🏗️ 整体架构和技术栈
- 🔄 完整工作流程
- 🔧 核心技术原理(浏览器自动化、页面滚动、数据提取)
- 📊 多策略数据提取方法
- 🛡️ 反爬虫对策
- ⚠️ 错误处理机制
- ⚡ 性能优化策略

## 依赖版本
详见 `requirements.txt`

## 许可证
MIT License
