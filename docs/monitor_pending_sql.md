# 监控待执行 SQL、Variant 对账与批次闸门

监控整体说明见 [`monitor_price_sync.md`](monitor_price_sync.md)。

## 待执行 SQL

服务器把每个通过门禁（图片证据、证据不超过 24 小时）的冻结计划渲染为一个
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

### 本机无人值守执行

写入只在本机（Mac）执行，监控主机不持有写权限。`python scripts/promo_pipeline.py pending-sql --apply`
（`utils/pending_sql_apply.py`）：

1. 从 macOS 钥匙串读取可写访问令牌（服务名 `costfinder-supabase-write`）；缺少时立即终止并给出创建方法，不拉取、不执行。
2. 经 ssh（主机别名 `firecrawl`，目录 `$BASE`，见 `utils/monitor_host.py`）把服务器 `$BASE/state/pending_sql/`
   里待执行的文件拉到 `output/self_hosted_monitor_state/pending_sql/`；本地已有或已登记执行的文件不再拉回，
   自托管 Firecrawl 故障期间生成的文件不执行（`utils.pending_sql.FIRECRAWL_OUTAGES`）。
3. 用官方 `mcp` 客户端（streamable HTTP，`Authorization: Bearer <令牌>`）连接托管 Supabase MCP
   `https://mcp.supabase.com/mcp?project_ref=kdlpkjzcnbkjcvwsvlwn&features=database`，按相对路径的文件名顺序
   对每个文件调用一次 `execute_sql`。一次调用的全部语句在同一个事务里提交，发送内容为：
   - 已执行检查：`admin_audit_log` 中已有同一文件（`target_id` 为相对路径）、同一内容（`details.sql_sha256`）的
     `pending_sql_applied` 记录时 raise `pending_sql_already_applied`，文件内容不再执行；
   - 文件本身：DO 块自行核对 before 快照、保护项与写后不变量，任一不符整体回滚；
   - 登记行：`admin_audit_log`（`admin_id` 为 `automated_data_repair`，`action` 为 `pending_sql_applied`）。
   文件含事务控制语句（`begin`/`commit`）时终止，因为登记行与写入必须在同一事务提交。
4. 执行成功或已执行检查命中时登记已执行：先在服务器上把同一文件移入 `applied/`（已在 `applied/` 时不变），
   再移动本地文件；下次 monitor 运行时事件变为 `completed`。
5. 文件自身的 SQL 报错（服务返回 `Failed to run sql query: ERROR:  <SQLSTATE>: ...`，例如 DO 块的 `stale_before:`、
   `stale_plan:`、`write_not_one_row`、`blanket_check_failed:` 等核对，DO 块整体回滚，没有写入）：先在服务器上、再在本地
   把文件移入 `pending_sql/rejected/`，错误原文存为同目录的 `<文件>.error.txt`，继续执行下一个文件。
   监控的 `settle()`（`utils/monitor_pending_sql.py`）只把 `applied/` 中的文件视为完成，文件既不在 `pending_sql/`
   也不在 `applied/` 时事件回到 `retryable`（`retry_reason: pending_sql_missing`），下次运行按新证据重新规划。
6. 与文件无关的失败停止本批，命令退出码为 1，该文件与其后的文件在两侧都保持待执行，错误写入本地 `<文件>.error.txt`
   （下一次执行成功或被拒时删除）：
   - SQLSTATE 25006（只读事务）：钥匙串中的令牌只有只读权限，提示换成可写令牌；
   - 不是 SQL 报错的工具错误；连接失败、401/403 与 MCP 协议错误直接抛出异常终止。
7. 输出逐个列出 applied、rejected（附核对名，非核对类 SQL 报错附 SQLSTATE）与 stopped 的文件，最后一行为
   `summary: applied N, rejected M, stopped 0|1`。

任何一步在中途终止后再次运行，结果与一次完整运行相同：已提交的文件由登记行识别为已执行，只补做文件移动；
服务器与本地的 `applied/`、`rejected/` 移动在文件已移入时不再移动。远程移动调用监控主机部署的
`utils/pending_sql.py`（`mark_applied`、`mark_rejected`）。

定时：`pending-sql --install-schedule` 把仓库中的 `config/launchd/com.costfinder.pending-sql-apply.plist` 复制到
`~/Library/LaunchAgents/` 并加载，每小时第 50 分钟运行 `pending-sql --apply`，输出写入
`output/launchd/pending_sql_apply.log`。

会话中仍可手动执行：`pending-sql` 拉取并列出每个文件的头部摘要，经 Supabase MCP `execute_sql` 执行后用
`pending-sql --mark-applied <文件>` 登记。执行后运行批次闸门（`batch-gate`）。

小时入口 `run_monitor_retry.sh` 运行 `promo_pipeline.py monitor --retry-transient-only`：
只回抓最近失败的页面，并且只处理每个 URL 最新的 retryable 事件。审查事件每次完整运行都会重新选入，
缓存计划超过 24 小时或渲染过期即重算。

## Variant 对账与批次闸门 F–I

Shopify 商品页抓取在 `browser.json` 中写入 `variant_expected`、`variant_skipped`、
`variant_captures` 与（集合页）`product_variant_reconciliation`。规划器在
`variant_reconciliation_mismatch` 时不写入该条并重新抓取；`from` 起价仅在页面有 variant 对账记录且集合
未抓全时不写入（`starting_price_variant_set_incomplete`），无 variant 的普通起价页仍按非阻断提示写入下限。

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
待执行 SQL，并回读核对结果。`pending-sql --apply` 的执行顺序、出错停止与中断后重跑由
`tests/test_pending_sql_apply.py` 在本机 PostgreSQL（`pgserver`）上的 MCP 服务验证。
