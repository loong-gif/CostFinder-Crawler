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

会话中的执行步骤：

1. `python scripts/promo_pipeline.py pending-sql --pull` 经 ssh（主机别名 `firecrawl`，目录 `$BASE`，
   见 `utils/monitor_host.py`）把服务器 `$BASE/state/pending_sql/` 里待执行的文件拉到
   `output/self_hosted_monitor_state/pending_sql/`，并列出每个文件的头部摘要；本地已有或已登记执行的文件不再拉回。
2. 用 Supabase MCP `execute_sql` 执行整个文件；DO 块自行核对 before 快照、保护项与写后不变量，任一不符整体回滚。
3. 成功后 `pending-sql --mark-applied <文件>`：先在服务器上把同一文件移入 `applied/`，再移动本地文件；
   服务器移动失败即终止，本地文件保持待执行。下次 monitor 运行时事件变为 `completed`。
   文件丢失时事件回到 `retryable` 并重新渲染。
4. 执行后运行批次闸门（`batch-gate`）。

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
待执行 SQL，并回读核对结果。
