# 自动优惠价格同步

只使用现有 `clinic_promotions`、`promo_offer_master`、`promo_offer_items` 和 `promo_offer_terms` 做数据更新。
程序没有建表、迁移、RPC、序列创建、触发器修改或其他 DDL 路径。常规服务价、会员费用、Business 和 Service 目录不在写入范围内。

## 服务器任务与开关

项目位于 `72.52.161.65` 的 `/home/loong/costfinder_monitor`，沿用 Windows 任务
`CostFinderFirecrawlMonitor` 及其启动文件 `run_monitor_validation.sh`。
主任务每天服务器当地时间 00:30 执行一次，时区为 Windows `Pacific Standard Time`
（自动处理夏令时）。触发器为 Daily；错过执行时间后允许补跑。2026-11-06 已用 `Get-ScheduledTask` 核实：
Daily、00:30，下次运行 2026-11-07 00:30。

### OCR 服务

图片里的促销与会员权益由服务器上的 PaddleOCR 识别（`utils/ocr_service.py`，PP-OCRv6 模型）。它运行在独立的
Python 3.12 环境 `/home/loong/ocr_service`（PaddlePaddle 3.x 没有 Python 3.14 的安装包；系统库 `libgomp` 解压在该目录的
`lib_root/`，不改系统；关闭 oneDNN 加速以避开 PaddlePaddle 3.x 的 `ConvertPirAttribute2RuntimeAttribute` 报错），
只监听服务器本机 `127.0.0.1:8082`，由 Windows 任务 `CostFinderOcrService`（登录时启动，
`wsl.exe -d Ubuntu -- /home/loong/costfinder_monitor/run_ocr_service.sh`，日志 `logs/ocr_service.log`）启动。
本机流水线的唯一入口是 `utils.paddle_ocr_markdown.ocr_lines`，经 `OCR_SERVICE_URL`（默认 `http://127.0.0.1:18082`）调用；
`render-pages` 与 `visibility-manifest` 在服务不可达时用 `ocr_tunnel` 自动建立 `ssh -N -L 127.0.0.1:18082:127.0.0.1:8082 firecrawl`
子进程，命令结束时关闭。服务暂时不可用时 `ocr_lines` 按递增间隔重试，仍失败即终止命令。
同一行的多个检测框按纵向位置合并为一行（`utils/ocr_rows.py`），服务名与价格因此保持在同一行。
`tests/test_image_ocr.py` 里三个依赖真实识别的测试在服务不可达时跳过。

### 监控队列

监控队列不写死，每次运行由数据库 `clinic_promotions` 和 `clinic_memberships` 的 `source_url` 动态生成
（`utils/monitor_queue.py`）：

- 出现在 `clinic_promotions` 的页面是促销页（同时出现在 `clinic_memberships` 的页面也按促销页处理），
  分桶 `daily_NN`，抓取后做浏览器渲染核验。
- 只出现在 `clinic_memberships` 的页面是会员页，分桶 `membership_NN`，只做 Firecrawl 抓取检测内容变化，不渲染；
  它们的事件在处理阶段标为 `excluded`（会员页只进 `clinic_memberships`）。
- 每桶最多 50 个页面。已有桶状态文件（`daily_NN.json` / `membership_NN.json`）里的页面保持原桶，新页面放进有空位的
  第一个桶，桶满再开新桶，所以基线始终对应同一批页面。数据库里新增或删除的 URL 下次运行自动入队或出队。
- `monitor --config <文件>` 仍可指定手写配置（到期抓取闭环用 `state/crawl_schedule/due_monitor.json`）。

### 动态轮询频率

每天的主任务只抓取轮询到期的页面（`utils/monitor_frequency.py`，状态文件 `state/poll_schedule.json`），
另外总是抓取已过 `campaign_end_date` 仍 active 的促销来源和数据质量复查队列里的来源。
每个页面按价格行实际变化的间隔分档：

| 更新间隔 | 轮询间隔 |
| --- | --- |
| ≤ 21 天 | 7 天 |
| 22–60 天 | 15 天 |
| > 60 天 | 30 天 |

会员页更新慢，只有两档：

| 更新间隔 | 轮询间隔 |
| --- | --- |
| ≤ 90 天 | 60 天 |
| > 90 天 | 90 天 |

- 更新指页面状态为 `changed`（价格行集合变化）；只有正文摘要或截图摘要变化不算。
- 更新间隔 = max(最近 6 次变化之间间隔的中位数, 距最近一次变化的天数)。观察起点算作第一个端点；
  从未变化的页面取距首次观察的天数。所以新页面先每周查，持续不变就逐档放慢，一旦变得频繁就回到快档。
- 抓取失败不记变化，下次到期顺延一个档期；失败页仍由每小时重试任务处理。
- 文件不存在时，第一次运行从 `evidence/` 的抓取时间和 `events/` 里 `change_type=changed` 的事件重建档案。
- 每次运行结束，启动脚本调用 `promo_pipeline.py notify-run`，以「小扣」机器人（`FEISHU_APP_ID`）私信
  `FEISHU_NOTIFY_OPEN_ID`：抓取、档期、事件、写入开关与待执行 SQL、暂停重试和失败页面；没有生成报告时发送日志末尾。
  小时重试没有抓取页面也没有处理事件时不发送。凭据在服务器 `.env`（`FEISHU_APP_SECRET`）。
- `promo_pipeline.py monitor-schedule [--pages]` 只读查看各档页面数和今天到期数；
  `monitor --all-pages` 忽略档期抓取全部配置页面。

`Last Result` 为 2 是设计内的退出码，不是崩溃：`run_monitor_price_sync.py` 在任一监控有页面抓取错误或存在 `retryable` 来源时返回 2
（见脚本末尾 `return 2 if ...`），报告中 `failure_scope` 为 `page_or_source_only`，由每小时的 `CostFinderFirecrawlMonitorRetry` 重试。
2026-10-11 的运行因部分页面返回 `firecrawl_empty_markdown` 而得到 2，日志无 Traceback。
另设每小时的 `CostFinderFirecrawlMonitorRetry`，运行 `run_monitor_retry.sh`；它只重试最新的
transient 事件和最近一次失败晚于成功抓取的页面。两个入口共享 `.monitor.lock`，业务审查事件
不会被每小时重新打开。
数据质量审计入口为 `run_data_quality_daily.sh`，需另建每小时触发的 Windows 任务
`CostFinderDataQualityDaily`；脚本只在上海时间 **09** 点执行，其余小时直接退出，因此不受太平洋
夏令时影响。它运行 `run_data_quality_audit.py --recheck`：只读审计并定向重抓到期、缺权益和
超过七天未成功核验的来源，不带 `--apply`，也共享 `.monitor.lock`。
同一来源的抓取和浏览器/提取分别累计连续失败，达到 **3 轮**后暂停该阶段的
小时重试并加入审查清单。抓取一轮内的两个 HTTP 尝试只算一轮失败；重启程序或新事件
不会绕过来源上限。主任务在页面到期时照常抓取，成功抓取后清除抓取计数；暂停的提取阶段
只有在主任务取得该页新正文后才重新核验，提取成功后清零。仍失败时继续保持暂停。
某一阶段成功不会清空其他阶段的失败计数。
运行时使用专用 `.venv`。Firecrawl 固定使用 `http://127.0.0.1:3002`，收集原始正文；服务器已有的
Windows Edge 使用独立 `browser_profile` 在本机核验可见内容并保存整页截图。
每个临时页面都会关闭。WSL 1 无法运行原容器里的 Chromium，因此不依赖该容器做可见性证明。

`state/price_sync_control.json` 的 `write_enabled` 默认且部署后始终为 `false`。
关闭时仍会采集、核验、生成保护名单和冻结差异计划，但不生成待执行 SQL；改为 `true` 后，
通过门禁的冻结计划渲染为 `state/pending_sql/` 下的 SQL 文件。到期活动只在当天抓取成功后创建一次核验事件；未知年份保存 NULL，不阻断更新。历史 inactive 记录不自动恢复。
服务器只用 `SUPABASE_SECRET_KEY` 或 `SUPABASE_SERVICE_ROLE_KEY` 经 REST 只读，服务名称枚举
经只读函数 `fn_clinic_service_names()` 读取；服务器不连数据库、不写数据库，也不使用 `DB_URL`。
所有写入由会话经 Supabase MCP 执行（见下文「待执行 SQL」）。
OCR/提取服务返回 402 额度耗尽或 429 限流时保留待重试事件；401/403 凭据被拒绝
时进入审查。失败不能作为改价或移除证据，不通过自动购买额度解决。
写入门禁按来源及候选判断：部分抓取失败、其他来源待审查和小时任务返回 2，均不关闭全局
写入开关，也不阻止独立、证据明确的来源继续同步。来源内存在不确定候选时，明确的兄弟优惠
可以更新，但保留审查项和 `is_offer_extracted=false`，不据此停用未匹配的优惠。
已经核验完成的批次可保存为 `state/verified_batch_receipts.json`：每次运行重新比较指定记录
及其字段、版本，结果写入 `sync_report.json` 的 `previously_verified`。这只确认已完成的部分，
不会重放旧计划、覆盖后来修改，或把整个来源的剩余审查标为通过。
凭据只放在本地/服务器 `.env`，不要放进聊天、报告或 Git。

安装运行依赖使用 `requirements_monitor_price_sync.txt`。更新程序用
`scripts/deploy_monitor_price_sync.py`，需要同步专用凭据时添加
`--include-credentials`；传输走 SSH 标准输入，旧文件保存到服务器
`price_sync_backups`，新部署不会打开写入。

## 证据与事件

所有运行记录在服务器 `state`，可用 `scripts/export_monitor_price_sync.py`
导出到项目 `output/monitor_price_sync/server_state`：

| 文件 | 含义 |
| --- | --- |
| `daily_01.json` 至 `daily_04.json` | 每页独立基线，失败页保留旧值 |
| `evidence/*.json` | Firecrawl 原始响应、原文、价格事实 |
| `failed_evidence/*.json` | 空正文、错误响应及每次失败重试的原始响应；网络未返回响应时记录错误 |
| `events/*.json` | 待处理事件、完成状态及重试状态 |
| `sync/<事件ID>/browser.json`、`screenshot.png`、`ocr.json` | 浏览器可见性、截图和 OCR |
| `sync/<事件ID>/extraction.json`、`evidence.json`、`visibility.json` | 结构化提取和逐条证据门禁 |
| `sync/<事件ID>/plan.json`、`plan_attempt_*.json` | 冻结计划和保留的旧版计划 |
| `manual_protections.json` | 手工追加的保护条目和 ID 列表 |
| `review_queue.json`、`sync_report.json` | 来源审查清单与运行报告 |
| `retry_limits.json` | 按来源和阶段保存连续失败次数、最后原因及暂停状态；报告的 `paused_retries` 列出达到上限的项目 |
| `runs/*_full_monitor.json`、`runs/*_failure_retry.json` | 历次完整运行和失败重试报告，小时报告不覆盖周报告 |
| `pending_sql/*.sql` | 待执行的写入：每个冻结计划一个 DO 块，文件头含计划 ID、plan sha256、来源 URL、业务 ID 与各表操作数 |
| `pending_sql/applied/` | 已经 MCP 执行并标记的文件 |

先把原始证据和事件可靠落盘，才推进快照。文件写入使用临时文件、原子替换和
文件/目录同步。整个任务和重试任务共用 `.monitor.lock`；运行重叠会退出。
同 URL 仅处理最新事件，价格反复变化不会被永久去重。

空正文、HTTP/浏览器失败、未加载的可见图片、无法核验的背景资源、未检查的
滚动区域/框架和未完成 OCR 都不能证明移除。部分证据允许已确认报价更新，
但禁止下架。完全没有抽到报价也不能证明移除；明确可见的“当前没有促销”声明
才可构成清空证据。活动日期过期或历史来源身份不清时进入审查。

## 匹配、价格与保护

已知会员来源、URL 明确标记的会员页面和混合会员页面只进 `clinic_memberships`：促销监控把对应事件标为
`excluded`，不渲染、不创建促销，也不进入审查清单。
这一检查覆盖原 URL、同域重定向后的 URL 和旧的已冻结计划。
会员表仅只读获取来源归属，不读取或同步会员费、权益。

已有优惠按唯一的来源、服务组成、部位、资格和限制条件匹配，改价保留 Offer
和 Item ID；Offer/活动归属不能唯一确定时进入审查。Blanket Offer 的服务关联保持 NULL，
不能假借一个具体服务。具体服务不能唯一映射时保存网页原名和 `service_id=NULL`，
价格、数量和条款仍同步；已有唯一匹配且正确的服务关联保留。缺映射不阻断独立优惠，
不阻断已有完整页面证据支持的移除，但保持 `is_offer_extracted=false`。
日期没有明确年份时写 NULL，不猜年份，也不沿用过时日期；明确已过期的页面报价仍不能激活。

`state/visual_verifications.json` 保存已完成的 Ego Lite 核验引用和原始时间。
只有 URL、原始抓取时间及正文都与核验对象完全一致，且页面完整、已查看截图时，
才复用该次核验的可见正文/图片转录。不能仅因后来的 Markdown 一致就复用旧图片价格。
不同抓取仍走当前浏览器与 OCR；复用记录不改变原始核验时间，也不直接重放旧数据库计划。

条件文字不同且仍有相同服务组成、资格和计价基础的未匹配 active 优惠时，
整来源进入审查，避免把条件转述误当作新增和下架；明确仍展示旧优惠时允许独立新增。

- 单位价且数量可变/最低购买门槛：`delivered_units=NULL`，Master 折后价及
  Item 单位价均为来源单位价；最低购买数量保留为条件。
- 明确固定交付数量的单位报价：Master 保存数量对应总额，原价使用相同口径；
  Item 单位价按现有触发器总额除以交付数量计算，避免二次除法。
- 套餐只给总价时，Item 单位价为 NULL；切换到套餐时显式清空旧单位价。
- 已有合法的 Master 折后价 NULL、明确单位价和百分比/金额优惠表示可保留。
  无法确认的价格或数量不会用猜测结果覆盖旧值。

保护文件可追加 `offer_ids`、`item_ids`、`promotion_ids`，或
`entries` 中的 `{table,id,reason}`。表名使用现有三个业务表名。
程序从现有业务确认和管理员记录持续补充保护，保留手动追加项。
保护包括父活动/优惠的子记录及触发器间接修改；一项冲突会阻止整来源更新。

## 待执行 SQL

服务器把每个通过门禁（`write_enabled`、图片证据、证据不超过 24 小时）的冻结计划渲染为一个
DO 块，写入 `state/pending_sql/<UTC 时间戳>_<事件ID>.sql`，事件状态为 `pending_sql`。
DO 块是一条语句，执行失败整体回滚，不存在部分写入：

1. 先锁定相关行（插入促销时锁表），核对来源完整集合与计划 before 的每一行全部列和
   `updated_at`、保护项（存在性、直接操作、父子级联）；不一致即 raise。
2. 写入：新 Promotion 使用当前最大 ID + 1，Offer/Item 使用默认序列；Offer 先以 inactive 插入，
   子行写完后再按计划激活；每条 DML 核对影响行数恰为 1。
3. 写后核对：请求字段与实际值一致、派生优惠与公式一致、受保护行未变化、来源成员集合与所有权
   未变化、active Offer 的结构与价格不变量（有子项、数量大于 0、套餐无单价、单价乘数量等于折后价、
   Blanket 不链接服务、服务不跨业务、父促销 active 且未过期），最后执行 AGENTS.md 的 Blanket 检查。

价格总额遵循现有触发器：Master 折后总价按 `coalesce(delivered_units,1)` 除到 Item，
固定数量不能再次除法；套餐只给总价时 Item 单位价为 NULL。

会话中的执行步骤：

1. 把服务器 `$BASE/state/pending_sql/` 拉回本地（例如 `output/self_hosted_monitor_state/pending_sql/`）。
2. `python scripts/promo_pipeline.py pending-sql --dir <目录>` 列出待执行文件及头部摘要。
3. 审阅后用 Supabase MCP `execute_sql` 执行整个文件。
4. 成功后 `pending-sql --mark-applied <文件>` 移到 `applied/`；下次 monitor 运行时事件变为 `completed`。
   文件丢失时事件回到 `retryable` 并重新渲染。
5. 执行后运行批次闸门（`batch-gate`）。

小时入口 `run_monitor_retry.sh` 运行 `promo_pipeline.py monitor --retry-transient-only`：
只回抓最近失败的页面，并且只处理每个 URL 最新的 retryable 事件。
`--retry-reviews` 可重新核验审查事件。

## Variant 对账与批次闸门 F–I

Shopify 商品页抓取在 `browser.json` 中写入 `variant_expected`、`variant_skipped`、
`variant_captures` 与（集合页）`product_variant_reconciliation`。规划器在
`variant_reconciliation_mismatch` 时阻断写入；`from` 起价仅在页面有 variant 对账记录且集合
未抓全时阻断（`starting_price_variant_set_incomplete`），无 variant 的普通起价页仍按非阻断提示写入下限。

`promo_offer_items.delivered_units` 是 `smallint`，计划中必须写整数。
集合页汇总时按 `(business_id, variant_id)` 去重，避免同一商品在多个 collection 重复建 offer。

会员价与普通价：**非会员档写入会员 offer 的 `regular_price`**，不单独建非会员 offer。
`visible_variant_offers` 会配对同规格 Member / Non-Member 变体，只输出会员 offer 并带上
非会员售价作为 `regular_price`。文本 offer 同理：`merge_member_tiers` 在 item 与 `price_model`
相同、非会员价更高且唯一配对时并入会员 offer；配对按 `/products/<handle>` 比较，不受 collection
与商品页 URL 差异影响。

每次经 MCP 执行写入 SQL 后运行批次闸门 A–J（`promo_pipeline.py batch-gate`）：

| 检查 | 含义 |
| --- | --- |
| F | 文案含 `from $` 但 `price_model` 不是 `from` |
| G | `total` offer 的文案/conditions 写明固定数量 N（≥2 units/sessions/syringes/treatments），但没有任何 item 交付 N；门槛表述（minimum、at least、N+、or more、up to、max）不算 |
| H | 会员 offer 在来源中声明 non-member 价，但 `regular_price` 缺失、≤ 折扣价或与声明不符 |
| I | 无 item 的 offer（上架一律报；未上架超过 24h 的空壳报出） |

只读审计 `run_data_quality_audit.py` 使用同名规则前缀（F/G/H/I）。

## 验证边界

本地针对规划、证据队列和 SQL 渲染的测试不连接生产数据库。渲染出的每个 DO 块都经
PostgreSQL 解析器（pglast）校验语法，测试对 SQL 文本断言前置核对、写入顺序与写后核对。
语法校验与文本断言不能替代真实触发器下的执行验收：首次上线时先在会话中经 MCP 执行少量
待执行 SQL，并回读核对结果。
