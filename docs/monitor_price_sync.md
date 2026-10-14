# 自动优惠价格同步

只使用现有 `clinic_promotions`、`promo_offer_master`、`promo_offer_items` 和 `promo_offer_terms` 做数据更新。
程序没有建表、迁移、RPC、序列创建、触发器修改或其他 DDL 路径。常规服务价、会员费用、Business 和 Service 目录不在写入范围内。

## 服务器任务与开关

项目位于 `72.52.161.65` 的 `/home/loong/costfinder_monitor`，沿用 Windows 任务
`CostFinderFirecrawlMonitor` 及其启动文件 `run_monitor_validation.sh`。
主任务每周日服务器当地时间 00:30 执行一次，时区为 Windows `Pacific Standard Time`
（自动处理夏令时）。触发器为 Weekly、间隔 1 周；错过执行时间后允许补跑。
另设每小时的 `CostFinderFirecrawlMonitorRetry`，运行 `run_monitor_retry.sh`；它只重试最新的
transient 事件和最近一次失败晚于成功抓取的页面。两个入口共享 `.monitor.lock`，业务审查事件
不会被每小时重新打开。
数据质量审计入口为 `run_data_quality_daily.sh`，需另建每小时触发的 Windows 任务
`CostFinderDataQualityDaily`；脚本只在上海时间 **09** 点执行，其余小时直接退出，因此不受太平洋
夏令时影响。它运行 `run_data_quality_audit.py --recheck`：只读审计并定向重抓到期、缺权益和
超过七天未成功核验的来源，不带 `--apply`，也共享 `.monitor.lock`。
同一来源的抓取、浏览器/提取和数据库恢复分别累计连续失败，达到 **3 轮**后暂停该阶段的
小时重试并加入审查清单。抓取一轮内的两个 HTTP 尝试只算一轮失败；重启程序或新事件
不会绕过来源上限。周任务仍抓取所有配置页面，成功抓取后清除抓取计数；暂停的提取阶段
只有在本轮周任务取得该页新正文后才重新核验，提取成功后清零。仍失败时继续保持暂停。
数据库恢复按原 journal 每周检查一次，完成恢复后清零；未知新增依旧禁止重复 POST，
不能通过新抓取证据替换部分写入计划。某一阶段成功不会清空其他阶段的失败计数。
运行时使用专用 `.venv`。Firecrawl 固定使用 `http://127.0.0.1:3002`，收集原始正文；服务器已有的
Windows Edge 使用独立 `browser_profile` 在本机核验可见内容并保存整页截图。
每个临时页面都会关闭。WSL 1 无法运行原容器里的 Chromium，因此不依赖该容器做可见性证明。

`state/price_sync_control.json` 的 `write_enabled` 默认且部署后始终为 `false`。
关闭时仍会采集、核验、生成保护名单和冻结差异计划。完成服务 key 的 REST 权限和写入验证后
才可改为 `true`。到期活动只在当天抓取成功后创建一次核验事件；未知年份保存 NULL，不阻断更新。历史 inactive 记录不自动恢复。
运行使用现有 `SUPABASE_SECRET_KEY` 或 `SUPABASE_SERVICE_ROLE_KEY`，
不需要数据库密码或 `DB_URL`。每次写入检查目标项目、完整来源旧状态和逐行 CAS。
Codex 内的 Supabase MCP 登录可用于读取和人工核验；服务器任务不能复用该登录。
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
| `manual_protections.json` | 人工确认、管理员操作和手工追加保护 |
| `review_queue.json`、`sync_report.json` | 来源审查清单与运行报告 |
| `retry_limits.json` | 按来源和阶段保存连续失败次数、最后原因及暂停状态；报告的 `paused_retries` 列出达到上限的项目 |
| `runs/*_full_monitor.json`、`runs/*_failure_retry.json` | 历次完整运行和失败重试报告，小时报告不覆盖周报告 |
| `journals/*.json` | 每步请求、返回 ID、完整来源回读、恢复位置和完成/审查回执 |

先把原始证据和事件可靠落盘，才推进快照。文件写入使用临时文件、原子替换和
文件/目录同步。整个任务和重试任务共用 `.monitor.lock`；运行重叠会退出。
同 URL 仅处理最新事件，价格反复变化不会被永久去重。

空正文、HTTP/浏览器失败、未加载的可见图片、无法核验的背景资源、未检查的
滚动区域/框架和未完成 OCR 都不能证明移除。部分证据允许已确认报价更新，
但禁止下架。完全没有抽到报价也不能证明移除；明确可见的“当前没有促销”声明
才可构成清空证据。活动日期过期或历史来源身份不清时进入审查。

## 匹配、价格与保护

已知会员来源、URL 明确标记的会员页面和混合会员页面进入审查，不创建促销。
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

## REST 分步写入与恢复

服务器通过现有 Supabase REST 客户端写入。GET/POST/PATCH 使用编码参数和返回行；
每条 PATCH 带 ID、来源关系、active 状态、`updated_at` 和目标字段旧值条件。
`promotion_content` 使用 JSONB 过滤值，`conditions` 使用 PostgreSQL text[] 过滤值。
没有新表、RPC、DDL、数据库密码、删除或 upsert 路径。

**来源内多个请求不具备事务原子性，可能留下部分更新。** 写入前检查完整来源集合、
所有旧值、版本、最终结构和保护关系；受保护父记录、子记录或触发器可能修改的兄弟记录
均提前阻断。受影响的已有 Offer 先设 inactive，完成标志先设 false；新 Promotion/Offer
先保持 inactive。内容、总额和原子 Item 分步写入，每步回读验证请求值、已知触发器结果、
来源成员集合及未触及字段。全部结构、价格和来源门禁通过后才激活并恢复完成标志。
移除保留原 ID，使用 inactive。

Master/Item 插入省略主键，使用现有数据库默认序列。Promotion 没有默认序列，
保存当前最大 ID + 1 候选后执行普通 INSERT；其他写入者抢占候选时进入审查，绝不 upsert
覆盖冲突记录。已返回的新增 ID 和完整返回行在下一次网络请求前可靠落盘。

价格总额遵循现有触发器：Master 折后总价按 `coalesce(delivered_units,1)` 除到 Item，
固定数量不能再次除法。`is_package` 的 Master 变更本身不会清空 Item 单位价，随后必须
显式更新 Item；逐请求约束在 inactive 状态也生效。套餐切回非套餐而无法在同一请求中
触发合法价格同步时，写入前进入审查。来源从完整价格转为同值百分比/金额优惠时，
触发器可能清除旧派生值；程序在 inactive 阶段以独立请求恢复来源支持的明确优惠。

journal 在每步 DML 前保存请求和恢复位置，收到响应立即保存返回值，再保存完整回读。
未知结果和恢复规则：

1. PATCH 后回读与已验证旧状态完全相同：使用原冻结旧值继续；与唯一预期触发器结果相同：
   确认该步成功，继续后续步骤，不重复已经成功的改价。
2. POST 已返回 ID 但后续 GET 失败：按已保存的 ID/返回行核验完整状态，核验通过可继续。
3. POST 未收到返回 ID：保留新增候选供人工审查，即使暂未读到新增行也不自动重复 POST，
   因为先前请求仍可能迟到提交。现有 schema 没有可安全复用的请求幂等键。
4. CAS 不命中、来源集合/字段/版本变化、触发器结果不符、请求被拒绝或新保护冲突：
   整来源暂停审查，保留实际部分状态。禁止删除 journal 或重新提取绕过恢复边界。

每次启动先恢复旧 journal，再处理新事件。恢复也重新检查来源业务归属、会员来源和
重定向边界；旧计划不能绕过新发现的来源限制。无法恢复的来源阻断该 URL 后续事件，
其他来源继续执行。旧 journal 恢复完成后，同来源较新的事件延后到下一次新快照和证据核验，
不使用恢复前的快照生成可写计划。超过 24 小时的冻结证据不继续 DML，进入审查。
只有全部最终字段和 active 结构回读正确，并通过 AGENTS.md Blanket 检查的 REST 等价查询，
才标记 completed。检查覆盖全库 active Blanket master 与非空 service_id 的 Item。

本地 journal 锁仅串行化此监控程序。跨请求父子行没有数据库锁，其他写入者在最后读取与
请求之间修改触发器涉及的子行，无法由父行 CAS 完全排除；后续回读发现差异时停止，但已经
发生的请求不能回滚。需要人工处理的部分更新明确保留在审查回执，不能宣称恢复了 SQL 事务保证。
旧 SQL journal 不使用 REST 重放；未完成的旧 journal 要单独审查。

首次补齐先冻结整个新批次计划，再执行新来源 DML。小时入口运行
`run_monitor_retry.sh`，等同于 `scripts/run_monitor_price_sync.py --retry-transient-only`：
只回抓最近失败的页面，并且只处理每个 URL 最新的 retryable 事件。
`--retry-reviews` 可重新核验没有部分写入回执的审查事件；含 `resume_required` 的事件
保留原冻结计划，必须先解决原 journal。

过期但能证明从未发送写入请求的 REST journal 归档为 `superseded`，允许用新抓取证据重新规划。
初次 GET 失败尚无 `current`，或新的持久化 `requests_sent=0`，才构成这种证明；零已完成步骤、
清空 pending 或一次旧态回读都不能证明请求从未发出。任何已发送或结果未知的请求继续按原日志
恢复/审查。重建必须取得晚于旧计划且 24 小时内的抓取证据；缺少时由小时重试单独回抓该页。
恢复时人工保护按该来源实际会触达的父子记录判断，其他诊所新增保护不阻断本来源。

## 验证边界

本地针对规划、证据队列、SQL 历史 writer 和 REST 分步恢复的测试不连接生产数据库。
REST 测试按真实触发器的每次请求边界模拟状态，包括固定数量总额除法、套餐切换、
百分比/金额优惠恢复、间接保护、CAS/集合冲突、PATCH 超时前后状态、已确认新增 ID 的回读失败、
未知 POST 禁止重复、来源审查隔离和旧 journal 的会员来源门禁。
模拟测试不能替代线上 service key 权限、现有默认序列和实际触发器验收。
首次上线必须通过真实写入后的数据库回读验收。实际部署、写入结果和未解决限制见导出的运行报告。
