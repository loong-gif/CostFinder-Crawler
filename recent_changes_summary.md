# 项目最近改动总结

本文档总结当前仓库最近一次主要改动，基于提交 `f5c964a`。

## 一、改动概览

这次改动新增了两条关键链路：

1. `crawler/promo_site_crawler.py`：新增站内促销页/价格页发现爬虫，负责从站点中自动识别更可能包含价格、促销、会员、服务套餐等内容的页面，并生成适合后续分析的结构化内容。
2. `scripts/recrawl_update_page_content_playwright_from_csv.py`：新增基于 CSV 的批量重抓脚本，使用 Playwright 渲染页面内容后回写到 Supabase 的 `promo_website_staging.page_content` 字段。

## 二、核心能力变化

### 1. 站内发现逻辑更强

`crawler/promo_site_crawler.py` 这次不只是“抓页面”，而是加入了更完整的站内发现与筛选能力：

- 会从站点入口页和一组常见促销路径开始探测，例如 `pricing`、`membership`、`specials`、`offers`、`services` 等。
- 会根据链接文本、URL 路径和关键词信号给候选页面打分。
- 会排除明显无关页面，例如博客、隐私政策、登录页、购物车、FAQ 等。
- 会对抓到的页面内容做分段、去噪、去重和排序，再生成 `page_content_llm` 供后续模型或规则继续使用。

### 2. 内容抽取更偏向“价格/促销信号”

新的内容清洗与分段逻辑重点强化了这些信号：

- 价格表达式，例如 `$99`、`USD 199`
- 折扣与促销表达式，例如 `10% off`、`save $20`
- 会员、套餐、服务项目、限时优惠等词汇
- markdown/渲染文本中的结构化片段

这意味着输出内容会更聚焦在促销页真正有价值的信息，而不是整页原始噪音。

### 3. 支持 Playwright 渲染重抓

`scripts/recrawl_update_page_content_playwright_from_csv.py` 提供了一个更适合动态站点的批处理入口：

- 从 CSV 读取 `subpage_url`
- 使用 Playwright 打开页面并等待渲染
- 滚动页面并提取 `document.body.innerText`
- 调用 `prepare_page_content()` 生成清洗后的页面内容
- 将结果回写到 Supabase

它还支持：

- `--use-lightpanda`：优先连接 CDP 上的 Lightpanda
- `--allow-local-fallback`：Lightpanda 失败时回退到本地 Chrome
- `--dry-run`：只抓取和预览，不写库
- `--max-urls`：限制处理数量，方便小批量验证

## 三、数据写回方式

脚本会把抓取结果按是否已存在分成两类：

- 已存在 `subpage_url` 的记录：执行更新
- 不存在的记录：执行插入

写回字段主要包括：

- `crawl_timestamp`
- `subpage_url`
- `page_content`
- `domain_name`
- `processed_status`
- `name`

同时会输出一份运行报告到 `output/results/`，报告文件名形如：

- `promo_website_staging_playwright_recrawl_update_YYYYMMDD_HHMMSS.json`

## 四、对项目的实际影响

### 正向影响

- 提升了促销页发现率，尤其适合深层站点和导航复杂的网站。
- 内容清洗更聚焦，后续做规则抽取或 LLM 抽取时噪音更少。
- Playwright 重抓链路更适合 JS-heavy 网站。
- 支持 Lightpanda/CDP，方便和现有浏览器基础设施联动。

### 需要注意的点

- 该脚本依赖环境变量 `SUPABASE_URL` 和 `SUPABASE_SERVICE_ROLE_KEY`。
- `--use-lightpanda` 需要可用的 CDP endpoint。
- 批量更新会并发写库，执行前建议先用 `--dry-run` 验证一轮。

## 五、相关文件

- [crawler/promo_site_crawler.py](/Users/wyl/costfinder/crawler/promo_site_crawler.py)
- [scripts/recrawl_update_page_content_playwright_from_csv.py](/Users/wyl/costfinder/scripts/recrawl_update_page_content_playwright_from_csv.py)

## 六、简短结论

这次改动的重点是把“发现促销页”和“重抓并回写内容”两步串得更完整了：前者提升发现精度，后者提升动态页面的可抓取性和数据回填效率。
