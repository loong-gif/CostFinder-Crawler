---
name: costfinder-discovery-to-db
description: 当需要把新商家、新发现的促销页、会员页从发现、渲染核验、抓取一路推进到写入 clinic_promotions / clinic_memberships、offer 和服务映射时使用。覆盖折叠面板与图片促销的深度渲染核验、未确认项自动核验、计划（dry run）与按计划写库。用户提到"重跑发现流程"、"按步骤写入数据库"、"先 dry run"、"折叠面板/图片促销"、"标准流程"时都应使用。
---

# CostFinder 标准抽取流程

所有步骤都通过 `python scripts/promo_pipeline.py <命令>` 运行。不写一次性脚本，缺少的能力加进 `utils/` 模块并注册为命令。规则与理由见 `docs/decisions-standard-extraction-pipeline.md` 和 `AGENTS.md`。

## 约束

- 数据库读写全部经 Supabase MCP（`execute_sql`，项目 `kdlpkjzcnbkjcvwsvlwn`）。Python 不连库、不使用 `DB_URL`，直接写库路径会被 `utils/pipeline_run.py` 拒绝。
- 写库命令三步：`read-sql` → 用 MCP 执行 read.sql，把 `snapshot` 列的值保存为同目录 `snapshot.json` → `plan --snapshot snapshot.json` 生成 plan.json → `apply-sql --plan plan.json` 生成 apply.sql → 审阅后用 MCP 执行（单个 DO 块，失败整体回滚）。
- 不新增表和列；硬约束放在数据库触发器、函数、约束与索引中（`supabase/migrations/20261103043251_promotion_identity_guards.sql`）。
- 未确认项直接进入 ego-lite 渲染核验，不向用户询问是否核验（见记忆 `render-verify-unconfirmed-pages`）。
- 抓取使用自托管 Firecrawl（`FIRECRAWL_SELF_HOSTED_API_URL`）。每个批次通过 `COSTFINDER_RAW_DIR` 隔离原始抓取文件。

## 步骤

### 1. 商家
1. `select-city` 选城市，`discover-businesses --city ... --state ... --out <places.json>` 调用 Google Maps scraper。
   - 按主题搜索发现诊所时：先 `topic-search --area ... --city ... --places <places.json> --out-dir <目录>`（自托管 `/v1/search`），命中只在网站域名对上目标城市诊所时保留，产出 `register_places.json`、`seeds.json` 与精度报告；没对上的诊所站点用 `discover-businesses --keyword "<域名主体> <城市>, <州>" --wait-timeout 150` 逐个补查后再用 `--hits` 重新匹配。
   - scraper 偶尔抓完后不写结果、任务停在 working 并阻塞后续任务：删除该任务（服务挂住时返回 500，重启服务后可删除），重启 `CostFinderGmapsScraper`（停止后等 10 秒再启动，首次启动常失败需再启动一次）。
2. `register-businesses` 按三步写入 `master_business_info`。筛选线：review_count>=50、score>=4.0、有 website。同一网站在其他城市已有、还没有 Place ID 的商家时，按连锁分店不确定处理（ambiguous），不链接也不新建。

### 2. 页面发现与渲染核验
1. `discover-pages` → `review-pages` → `retry-pages`，得到 `state/discovery/monitor_decisions_final.json`。站点链接来自 `/v2/map`；`--seeds <topic-search 的 seeds.json>` 把搜索命中的同站促销页放在候选最前。
2. `render-shards` 生成分片；每个分片后台运行 `scripts/run_render_review_deep.sh shardN.json outN.json 90000`（展开折叠面板、标签页，滚动懒加载，收集可见文本与大图）。
3. `render-verdicts` 对可见文本判定，未通过的页面对图片执行 PaddleOCR（监控服务器上的 OCR 服务，经 SSH 隧道调用）。
4. `finalize-pages` 应用结论（验证通过为 add，其余为 skip），断言不再有待确认项。

### 3. 抓取
`COSTFINDER_RAW_DIR=output/discovery_raw/<批次> python scripts/promo_pipeline.py crawl` 抓取决定为 add 的页面。

### 4. 促销与会员
`extract-pages` 按三步执行（plan 需 `--raw-dir <批次> --visibility-manifest <清单>`）；检查计划中的 rejected 原因后再生成 apply.sql。代码强制：一页一行（hub 子页面并入 hub）、标题取页面 `<title>`、价目页无折扣证据不写、过期活动不写、同一句的 markdown 变体合并为一行。

可见性清单由 `visibility-manifest` 生成：先不带清单运行 `extract-pages plan`，再 `visibility-manifest shard --plan <plan.json>`，后台运行 `scripts/run_render_review_deep.sh <目录>/visibility_shard.json <目录>/visibility_render.json 90000`，然后 `visibility-manifest build --plan <plan.json>`，最后带 `--visibility-manifest <目录>/visibility_manifest.json` 重新 plan 与 apply-sql。清单格式：

```json
{"<url>": {"status": "page_accessible", "claimResults": [{"recordType": "promotion", "claim": "...", "status": "visible_match"}]}}
```

### 5. offer
`extract-offers` 按三步从 `promotion_content` / `benefits` 抽取 offer。服务枚举取自快照。

### 6. 服务
`link-services` 按三步识别服务、补建 `clinic_services`（regular_price 为 NULL）、回填 `service_id`。

### 7. 收尾
每批写入后运行 `batch-gate` 与 AGENTS.md 的 blanket-offer SQL，结果必须为零行，或把保留行记录在批次结果里。监控队列由 `clinic_promotions` / `clinic_memberships` 的 source_url 动态生成，写入数据库的 add 页面下次监控运行时自动入队，不需要维护 URL 清单。

## 维护命令
- `recrawl-sources`：已有促销/会员重新抓取。`read-sql --promotion-id N --plan-id N` → `read-snapshot` → `decisions --snapshot` 写 decisions.json，再 `COSTFINDER_RAW_DIR=<目录>/raw crawl --decisions <目录>/decisions.json`，之后按第 4、5 步执行。`extract-offers` 对原文互相包含的旧 offer 唯一匹配时沿用其 ID，同一候选包含的多条旧 offer 保留最长者、其余停用。
- `read-snapshot <read.sql>`：用 Supabase CLI 只读执行 read.sql，写同目录 snapshot.json。
- `merge-promotions`：按三步合并重复促销与 hub 子页面。
- `enforce-promotion-rules`：按三步停用违反写入规则的现有启用促销；`plan --deactivate <promotion_id> <原因>`（可重复）只停用人工确认的促销，下游 offer 由触发器一并停用。
- `crawl`、`discover-pages` 与 monitor 先用 Firecrawl fastMode；请求失败、metadata 状态码 ≥ 400（404/410 除外）、正文为空或拦截页时改用 ego-lite 渲染，不使用 Firecrawl 的浏览器引擎；`crawl --engine ego --url <URL>` 指定引擎直接抓取。crawl 同时抓取卡片链接到的条款锚点段落（raw 的 `linked_terms`），extract-pages 由代码把段落追加到对应卡片，可见性清单一并渲染条款页。
- 连锁分店共用一个网站时，discover-pages 只发现一次并复制给各分店，后续每家分店各写一行。per_unit offer 在 link-services 关联 service 之后，对同一批促销重新运行 `extract-offers`（按 `--promotion-id`）完成激活。`link-services read-sql --business-id N` 只处理指定商家。
- `delete-offer-shells`：按三步删除没有 item、没有用户引用的停用 offer（门禁 I）。
- `deactivate-expired`：生成停用过期促销的 apply.sql。
- `monitor`、`quality`：已有促销的复查与数据质量审计；监控主机上运行时只把待执行 SQL 写入 `state/pending_sql/`。
- `pending-sql`：列出待执行的 SQL 文件；用 MCP 执行后标记为已执行。
