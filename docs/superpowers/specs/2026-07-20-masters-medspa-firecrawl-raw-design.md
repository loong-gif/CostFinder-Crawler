# Masters Medspa Botox 原始抓取设计

## 目标

以 `master_business_info.business_id = 2889`、域名 `masters-medspa.com` 为单域样例，
通过 Firecrawl Search 和 Scrape 获取 Botox 公开定价证据，并仅写入：

- `firecrawl_search_raw`
- `firecrawl_scrape_raw`

本次不运行 LLM 提取，不修改 `clinic_services`、促销或报价业务表。

## 抓取层

1. 使用两条互补查询发现域内 Botox 定价页：
   - `site:masters-medspa.com botox ("per unit" OR "/unit" OR "unit price")`
   - `site:masters-medspa.com botox pricing injectables`
2. 每条 Search API 响应独立保存为一行 `firecrawl_search_raw`。
3. 仅保留 `masters-medspa.com` 域内、正文或摘要含价格信号的结果。
4. 对候选 URL 调用 Scrape API，显式设置 `onlyMainContent=true` 和 `blockAds=true`。
5. 使用项目现有 `prepare_scrape_markdown()` 清洗 markdown，再保存清洗结果、links 和 metadata。
6. 清洗仅删除导航、页脚、广告、重复菜单等噪声，不改写价格、单位、服务名或限定条件。
7. 每条 Scrape 记录通过 `search_raw_id` 关联发现它的 Search 记录。

## 幂等与错误处理

- Search 使用现有 `search_request_fingerprint`，相同查询重复执行时更新原行。
- Scrape 使用规范化 URL 和现有 `scrape_request_fingerprint`，相同页面重复执行时更新原行。
- 无价格信号的 Search 命中不得进入 Scrape。
- `firecrawl_scrape_raw.markdown` 保存经过 `prepare_scrape_markdown()` 降噪、可供后续 LLM 提取的正文。
- 清洗后正文为空或不再包含价格证据时，记录失败或跳过，不把无证据内容交给后续提取。
- 单个 Scrape 失败时记录 `success=false` 和错误信息，不伪造页面内容。

## 验证

执行后回查：

- `firecrawl_search_raw` 至少包含本次查询及成功状态。
- `firecrawl_scrape_raw` 的 URL 属于目标域，且关联有效 `search_raw_id`。
- 成功 Scrape 行包含非空的已清洗 markdown，能找到 Botox 与价格证据，且无明显导航或页脚噪声。
- 业务层表行数不因本次任务变化。

## 边界

本设计只覆盖 `masters-medspa.com`。批量处理全部 Medical spa 应在单域验证后另行设计并加入并发、额度和失败恢复策略。
