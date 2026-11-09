# 标准抽取流程决策记录

## 目标

1. 停止编写一次性脚本，所有数据处理通过一条标准流程完成：
   google-maps-scraper 发现商家 → 由商家域名发现促销页面与会员页面 → 爬取促销与会员页面（爬虫层到此结束）→ 从原始返回抽取 `clinic_promotions` / `clinic_memberships` → 从 `clinic_memberships.benefits` / `clinic_promotions.promotion_content` 抽取 `promo_offer_master` 与 `promo_offer_items` → 从 offer 识别 service 并回填 `clinic_services`。
2. AI 容易出错的规则用代码与数据库触发器硬性约束，不依赖提示词。

## 已知事实（2026-10-29 只读查询生产库 kdlpkjzcnbkjcvwsvlwn）

- `clinic_promotions` 共 298 行，其中 144 行 active。
- `clinic_promotions` 没有 `(business_id, source_url)` 唯一索引；重复分组 22 个，其中 active 重复分组 2 个。
- `clinic_memberships` 的 `(business_id, source_url, membership_name)` 没有重复。
- `clinic_service_name_enum` 共 84 个值；本地 `schema/offer_extraction_schema.json` 的 `service_name` 只有 12 个品牌加 Others。
- `clinic_promotions` 现有触发器：`trg_cascade_promotion_active_to_offers`、`trg_queue_active_empty_promotion`；`clinic_memberships` 没有触发器。
- 当前没有 active 且 `campaign_end_date` 已过期的促销。
- 折扣过滤、父级 hub 合并、过期过滤三个步骤的代码只在 Box 服务器上运行过，仓库里只有 `receipt.json` 与输出文件。

## 已确定

1. **数据库约束范围**：允许新增触发器、函数、CHECK 约束、唯一索引（含表达式索引）；禁止新增表和列。
   理由：促销身份等规则只有唯一索引能在并发写入下真正挡住，触发器和 Python 层都挡不住；不新增表和列，保持数据模型不变。同步修改 AGENTS.md 中 Monitor 同步部分的 schema 禁令。
2. **促销身份键**：`(business_id, 规范化 source_url)` 唯一，一个页面一行促销，offer 挂在该行下。URL 规范化由数据库函数实现，Python 使用同一套规则，替换现有 8 个各自实现的规范化函数。建唯一索引前先按流程规则合并现有 22 组重复。
   理由：同一页面的不同写法（协议、www、尾部斜杠、跟踪参数）必须视为同一页面，否则唯一索引形同虚设。
3. **流程直接写数据库**：过滤、合并、去重全部在代码中完成；Google Sheet 只作为只读报告，不再作为中间状态。
   理由：表格读写截断曾导致数据丢失；AI 能确认的事项不交给人工，人工只处理来源或 offer 身份确实无法由证据确定的情况。
4. **代码必须进仓库**：所有处理代码先提交到仓库，Box 与监控主机只运行仓库中的命令；部署时校验远端代码与仓库提交一致，不一致拒绝运行。
   理由：折扣过滤与父级 hub 合并的代码只在 Box 上运行过，仓库中没有源码，规则无法复现。

5. **父级 hub 合并**：子页面的上级路径含促销关键词（specials / promotions / offers / deals）时一律合并到 hub；hub 不在库中则新建 hub 行，标题取 hub 页面 HTML `<title>`；子页面的 offer 挂到 hub 下，子行不保留。
   理由：hub 页面常只放链接，按内容可见性判定会让大量子页面单独成行。
6. **促销标题**：由代码从自托管 Firecrawl 抓取结果取 `source_url` 页面 `<title>`（metadata.title）原文，不加工，不经过 LLM；通用标题（`Home`、空值、仅站点名）依次退到 `og:title`、第一个一级标题，均无则拒绝写入。数据库触发器拒绝空标题。
   理由：标题原样取自页面，杜绝 AI 改写为活动短名或正文；页面 `<title>` 常与页面内标题行相同，因此不以「标题等于正文某行」作为拒绝条件。
7. **价目页过滤**：URL 路径不含 promo / special 类关键词时，内容必须有明确折扣证据（% off、$ off、save、BOGO、原价与优惠价对比），单纯标价或套餐价不写入。Python 抽取层判定，数据库触发器用同一组正则拒绝插入。
   理由：正则漏判时立即报错可见；错误数据进库后不可见。
8. **过期活动**：写入时 `campaign_end_date` 已过期的丢弃，空日期保留；库中促销过结束日期后由每日任务设为 `is_active=false`，下游 offer 由 `trg_cascade_promotion_active_to_offers` 一并停用；触发器拒绝 active 且已过期的写入；只有月日的日期取当年，两位年份补全为 20xx。
   理由：停用保留历史，可追溯。
9. **写入安全**：写入一律按身份键 upsert，禁止清空后写回；读取快照时逐表核对 count 与行数，不一致即报错终止；MCP 固定指向项目 `kdlpkjzcnbkjcvwsvlwn`（`.mcp.json`）。
   理由：读取截断后清空重写曾丢失数据；名为 costfinder 的其他实例不是生产库。
10. **offer 服务枚举**：offer 抽取 schema 的 `service_name` 枚举在运行时从数据库 `clinic_service_name_enum` 读取生成，删除本地 12 品牌加 Others 的写死枚举。
    理由：本地枚举与线上 84 个值不一致，导致大量 Others 与空 offer。
11. **会员身份键**：`(business_id, 规范化 source_url, membership_name)` 唯一索引；当前无重复。
12. **offer 重新抽取**：`promotion_content` 或 `benefits` 变化时重新抽取；按 fingerprint 唯一匹配时保留原 Offer/Item ID 与已有有效 service 映射（遵循 AGENTS.md）。

13. **服务回填**：offer 识别出商家 `clinic_services` 中没有的服务时，新建 `clinic_services` 行（`service_name` 取枚举值，`regular_price` 为 NULL，offer 价不作为原价），再回填 `service_id`；blanket offer 按 AGENTS.md 不关联服务。
    理由：第 6 段的目的就是回填 `clinic_services`。
14. **208 批**：新流程实现后从本地保存的原始爬取结果重新运行，与表格中约 452 行逐行比对，差异逐条解释，作为新流程验收。
    理由：直接导入表格结果会绕过全部硬约束。
15. **禁止一次性脚本**：pre-commit 钩子拒绝向 `one-off/` 新增文件；所有数据库写入函数只在流程入口命令设置的运行标识存在时放行；现有一次性脚本中仍有价值的逻辑并入流程模块，其余整体移到 `archive/one-off/`。
    理由：提示词拦不住临时脚本直接调用写入函数。
16. **渲染可见性校验**：爬取后、写入前，未确认的页面自动交给 ego-lite 渲染校验，沿用 `utils/rendered_visibility_gate.py`。
17. **Monitor 同步**：继续作为已有促销的复查流程，写入改走与标准流程相同的写入层，受同样的唯一索引与触发器约束。

18. **读取自动完成，写入走 Supabase MCP**：Python 不写数据库，不使用 `DB_URL`，任何直接写库路径（REST 写、rpc 写、psycopg）由 `utils/pipeline_run.forbid_direct_write` 拒绝。写库命令分两步：`plan` 生成只读快照查询 read.sql，由 `utils.pipeline_db.fresh_snapshot` 用 Supabase CLI（`--linked`）自动执行并保存为 snapshot.json（加载时逐表核对 count 与行数），再生成计划（`--snapshot` 用已保存的快照重新生成计划）；`apply-sql` 把计划渲染为单个 DO 块，块内核对计划生成后数据未变化、写入、写后核对，经 MCP 执行，失败整体回滚。
    理由：数据库凭据只保留在 MCP 授权中；每次写入都是可审阅的 SQL 文件。
19. **监控主机只生成待执行 SQL**：无人值守的 monitor、数据质量、停用过期任务把 SQL 写入 `state/pending_sql/`，不写库；`pending-sql` 自动拉回并列出，会话经 MCP 执行，`--mark-applied` 同时登记本地与监控主机。监控主机只读需要的服务枚举经 REST 调用只读函数 `fn_clinic_service_names()`。
    理由：无人值守任务无法调用 MCP，写入改为会话中可审阅的步骤。

## 待定问题

无。
