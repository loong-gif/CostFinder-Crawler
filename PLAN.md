# Plan: 高优先级生产安全与 Slack 通知修复
_Locked via grill — by Claude + user_

## Goal

修复医美服务比价及优惠信息采集项目中会导致生产数据错误、状态错误或运行时崩溃的高优先级问题，并建立可靠的审计通知链路：所有有业务影响的数据库写操作经过可追踪的 pending/validator 流程，offer 生命周期只保留 `active`、`needs_review`、`ended`，失败不会推进监控游标；需要人工关注的结果通过本机 Hermes Agent 投递到 Slack `#costfinder-ops`，支持 AI 总结和 Block Kit 降级发送。

## Approach

1. 修复 Instagram ingestion 的 `InstagramTarget` 初始化错误，并为 active paths 增加回归测试。
2. 重构 offer 变更流程为事件优先：采集事件先进入 `pending`，validator 只允许高置信度且满足必填字段、价格/单位、canonical service/source、重复检查和 evidence location 校验的结果自动应用；其余进入 `needs_review`。人工拒绝只保留在事件审计结果中，不改变现有 active offer。
3. 统一 offer 生命周期为 `active` / `needs_review` / `ended`：成功抓取并明确确认优惠消失时，以 evidence 自动标记 `ended`；超时、空响应、阻断、解析失败或截断内容不得改变 active 数据。
4. 修复 evidence segment 与 `page_content` 的 provenance 合同，确保清洗、解析、recrawl 都保留可定位证据，并持久化 recrawl 产生的派生 evidence 字段。
5. 修复 SQL migrations：保护生产数据，禁止未确认的 destructive operation；统一使用 `business_id` 作为 canonical key；修复 M002 对不存在/已删除表的索引问题及 FK identity 不一致；补充可重复执行、dry-run/validation 和迁移失败记录。
6. 将业务数据变更与 `notification_outbox` 写入放入同一数据库事务；失败事务不推进 monitor cursor。外部 Slack 发送失败不回滚业务数据，由 outbox worker 重试。
7. 新增 `operation_runs` 与 `notification_outbox` schema：保存 run、通知类型、严重级别、目标、不可变 payload snapshot、幂等 notification_id、重试次数、next_attempt_at、错误和状态。worker 使用 1m/5m/15m/1h/6h 退避，4xx 进入 dead_letter，最多 5 次尝试。
8. 新增 Hermes notification worker，作为当前 Hermes gateway 用户的 user-level systemd 服务运行。worker 只消费 outbox，不参与业务决策；普通通知使用确定性模板，`needs_review` 或批量通知可调用 Hermes 默认模型总结，AI 失败自动降级为模板。Block Kit 优先，CLI 不支持时降级为结构化 Markdown 文本。
9. 配置 Slack `#costfinder-ops`，通知策略为每个 job/run 一条汇总；只在非零业务写入、错误、needs_review、schema migration 时发送，不发送 job-start/no-op 成功通知。每条最多展示 10 个 needs_review 项，其余保留在数据库/报告中；不发送 secrets、原始 payload 或 PII。
10. 加入 feature flags 与 rollout：`SLACK_NOTIFICATIONS_ENABLED=false`、`AUTO_APPLY_HIGH_CONFIDENCE_ENABLED=false`，依次支持 dry-run、Slack 通知、开启高置信度自动应用，并为每阶段提供回滚开关。
11. 增加 mocked Slack 默认测试及 Hermes adapter 测试，覆盖 2xx、429/5xx retry、4xx dead-letter、outbox 状态、幂等性、事务回滚、RPC/migration integration、monitor cursor 和关键 lifecycle/evidence 场景；最后运行 targeted tests、全量 pytest、compile/lint/static checks。

## Key decisions & tradeoffs

- 只保留 `active`、`needs_review`、`ended` 三个 offer 生命周期；rejected 是事件审计结果，不是 offer 状态。
- `pending` 是 validator 输入态，不直接修改 master；只有高置信度、规则校验全部通过的结果可 auto-apply。
- 明确确认优惠消失才可自动 `ended`；抓取失败保持 active，并记录失败/通知。
- 所有数据库写操作都要进入审计/通知范围，但按 run 汇总，避免逐行 Slack 噪声；outbox 自身状态更新不递归通知。
- 业务写入与 outbox 入库原子提交；Slack 外部失败由 durable outbox 重试，不回滚已提交业务数据。
- Hermes 仅承担发送和可选文本总结，不拥有数据库写权限，也不决定生命周期状态。
- 复用本机已运行的 Hermes gateway；不在 Firecrawl Windows/WSL 主机重复安装 Hermes，不增加公网 webhook。
- 默认使用 Slack mocked tests；真实 Slack 发送仅作为显式 opt-in smoke test。
- 生产迁移保留数据、可重复、先验证后执行；破坏性操作必须显式隔离并获得确认。

## Risks / open questions

- 当前 Hermes token 已通过 systemd manager 环境导入；实现阶段必须将环境来源持久化到安全的 service EnvironmentFile 或 Hermes env 机制，避免重启丢失，且不得把 secrets 写入仓库或日志。
- Hermes CLI 当前已验证文本发送；Block Kit 需通过 Hermes gateway 能力验证，失败必须使用文本降级路径。
- Slack bot 需要保持已安装、具备 `chat:write`、`channels:read`、`groups:read` 并加入 `#costfinder-ops`。
- 数据库部署目标、现有 migration ledger 状态和生产 schema 差异需要在实施前读取并以 dry-run 结果为准。

## Out of scope

- 第二阶段的 telemetry token 清理、重复 infra adapter 合并、social unique constraint 和全面 migration ledger 重构，除非它们阻塞本阶段安全修复。
- 归档脚本的行为改造；`scripts/archive` 仅保留历史参考。
- 新增公网 webhook、Cloudflare tunnel 或 Firecrawl 服务器迁移。
- 让 Hermes/LLM 直接执行数据库更新、自动拒绝人工 review 或改变 offer 生命周期。


## Codex-driven revisions before implementation

- 建立 active-path writer inventory，覆盖 social ingestion、membership、staging recrawl、offer application 等所有业务写入；每个 writer 必须调用统一 mutation/RPC 层，禁止旁路写入。
- 将 `status` 与 `lifecycle_status` 合并为一个 canonical lifecycle 字段，清理 `missing_once`、`stale_candidate` 等旧状态，制定 backfill/cutover 和兼容读取窗口。
- evidence segment 在写入 change event 时必须实际持久化并填充 `affected_segment_ids`；每个自动应用字段必须能回溯到经过验证的 evidence location。
- 业务 mutation、outbox insert 和 monitor cursor update 必须由同一个数据库 RPC/direct-SQL transaction 完成，而不是多个 Supabase REST 请求；失败时 cursor 不推进。
- 为 outbox 明确定义 claim lease、并发 worker、锁/租约过期、attempt 编号、payload 大小与 redaction、terminal state 和幂等约束，并加入并发测试。
- Slack 目标持久化 immutable channel ID，同时保存名称仅用于展示；发送前验证频道存在，rename/private 变化不得依赖名称解析。
- Hermes adapter 先验证实际 Block Kit API；若不可用强制走文本 fallback。AI 运行使用受限 Hermes profile/service identity，禁止继承可执行任意 terminal 的权限；AI 只能生成展示文本。
- 将 `InstagramTarget` 改为显式 dataclass（或等价可实例化类型），覆盖每条真实 ingestion path 的回归测试。
- M002 先 preflight，再按依赖顺序执行；禁止无条件 DROP/TRUNCATE/cascade，约束和索引使用可重复的 conditional migration，并把 destructive 操作隔离为显式 approval gate。


## Codex round 2 revisions before implementation

- monitor poller 必须按 monitor 加 lease/advisory lock，并用 compare-and-set 更新 cursor；重复 poller 不能独立处理同一 check 或推进游标。
- validator 必须使用原子 `pending -> applying -> applied/needs_review` 状态迁移、行锁/lease，以及唯一 applied-event/mutation key，防止重复 auto-apply 和重复 offer。
- `ended` 的证据定义为一次完整、未截断、质量校验通过的成功快照明确确认优惠消失；抓取失败、空响应、阻断或不完整快照不能触发 ended。该规则保持已确认的单次明确消失语义，不引入连续观察要求。
- canonical lifecycle 必须有数据库 `CHECK` constraint；完成旧值 backfill 后，所有读取/写入走 canonical 字段，legacy status 只能通过兼容 view/trigger 或 deployment gate 访问。
- outbox 在持久化前完成 redaction，保存 payload hash；渲染 payload 为不可变 snapshot，只有投递状态字段可更新。通知幂等键必须出现在 provider-visible payload，并在超时后通过 message identity/reconciliation 降低重复发送。
- migration runner 增加 durable migration ledger、checksum、preflight/failure record 和跨进程 advisory lock；失败记录不能因业务事务回滚而消失。
- Hermes worker 使用独立 profile/service identity，最小环境、禁用 terminal/tool access，并通过 privilege test 验证 AI 只能生成展示文本；不得直接复用具备任意终端权限的主 Hermes agent profile。
- `business_id` 在 event/segment/evidence 链路中声明并 enforced FK；source-to-business 映射不唯一或缺失时拒绝 auto-apply 并进入 needs_review。
- rollout flags 使用持久化、版本化、原子读取的配置；明确进程崩溃/in-flight 行为、kill switch 和回滚步骤，禁止仅依赖未审计的进程环境变量。
- lifecycle cutover 前阻断旧 active-path/archive-adjacent writer 写 legacy `status`，或提供兼容视图/触发器并加部署门禁；migration ledger 仅覆盖本阶段实际执行的 migration，不扩展为第二阶段全面重构。


## Codex round 3 implementation contracts

- Monitor lease contract：`promo_monitor_state` 增加 `lease_owner`、`lease_expires_at`、`cursor_version`；`claim_monitor`、`renew_monitor_lease`、`commit_monitor_cursor` 为原子 RPC。claim 条件为无 lease 或已过期，owner 使用 run UUID，commit 必须匹配 owner/version 且只允许单调 cursor；worker 崩溃由 expiry recovery 接管。
- Validator DDL contract：事件状态只允许 `pending -> applying -> applied|needs_review|rejected`；数据库 CHECK/trigger 拒绝其他转换；`UNIQUE(source_event_id, mutation_key)` 防止重复应用；stale applying lease 可由新 worker 按 lease expiry 安全回收。transition 与 mutation 在同一事务中完成。
- Legacy cutover gate：实施前逐项审计并阻断/迁移 `utils/change_driven_extractor.py`、`scripts/firecrawl_monitor_poll.py`、`scripts/daily_facebook_promo_ingestion.py`、`scripts/daily_instagram_promo_ingestion.py`、`utils/social_ingestion.py`、`crawler/staging_recrawl.py` 及其他 active writer；部署 gate 在发现直接写 legacy `status` 或 bypass mutation RPC 时失败。archive 脚本不运行，但其运行入口也必须被 gate 拒绝。
- Business identity contract：`promo_page_segments.business_id`、change events、offers 和 evidence 使用明确 parent FK；应用到 master 的 event/evidence 禁止 null business_id；source URL/domain 映射到多个 business 时事务直接 needs_review，不写 master。补充 FK、nullability、ambiguity rejection integration test。
- Outbox DDL contract：immutable columns（notification_id/run_id/type/severity/target/payload/payload_hash/created_at）由 trigger 禁止 UPDATE，delivery columns 才可更新；worker 角色只拥有 claim/delivery 状态更新权限。增加 `provider_message_id`、`provider_request_id`、`last_attempt_at` 和 reconciliation 状态，payload 先 redaction 后 hash/persist。
- Migration contract：定义 `schema_migrations(migration_id PK, checksum, status, preflight_json, approval_token_hash, started_at, completed_at, error)`；runner 先 acquire advisory lock、执行 preflight、校验 checksum，再提交 applied/failed ledger row。执行 migration 与记录失败使用独立事务/连接，失败记录不能随业务回滚；destructive migration 必须提供匹配 approval token。
- Hermes deployment contract：复用已安装 Hermes binary，但 notification worker 使用独立 Hermes profile/config 和 systemd unit；profile 禁用 terminal/shell/tools，只允许 Slack delivery/summary adapter，最小化环境变量，并在部署测试中证明 summary subprocess 无法执行任意命令。当前 gateway 仅用于已验证的 Slack transport，不作为 worker 的权限边界。
- Rollout contract：dry-run 至少覆盖一个完整调度周期且 invariant violations=0；Slack-only 连续两个周期无未处理 outbox/认证失败；auto-apply 先单 monitor canary。任一重复 mutation、非法 lifecycle、cursor rollback、FK ambiguity、outbox dead-letter 超阈值或 Hermes privilege test 失败立即 kill-switch；rollback 需验证 flags、cursor、outbox 和 lifecycle 查询结果。


## Codex round 4 final contracts

- Monitor cursor 不按 check_id 字符串排序；以 `(check_created_at, check_sequence, check_id)` 作为有序 cursor tuple，RPC 只接受严格大于已提交 tuple 的更新，并保留 check_id 做幂等。
- validator 使用现有 schema 的 `change_event_id` 作为 canonical event key；定义 `UNIQUE(change_event_id, mutation_key)`，mutation_key 由 canonical offer identity + normalized field patch hash 生成，禁止引入未存在的 `source_event_id`。
- `promo_offer_evidence` 明确增加 `business_id BIGINT NOT NULL`（先 backfill/preflight），FK 到 canonical `master_business_info.business_id`；`segment_id`/`offer_id` 也必须存在并通过约束或 trigger 验证其 business_id 一致，禁止 orphan/跨 business evidence。
- 旁路 writer 防护不依赖文件清单：为业务表撤销应用角色直接 INSERT/UPDATE/DELETE 权限，仅授予统一 SECURITY DEFINER mutation RPC 所需权限；writer inventory 与 deployment gate 作为辅助检查，新代码无法绕过数据库边界。
- Hermes 唯一支持的部署模型是独立 notification-worker systemd unit + 独立 profile/service identity；本机现有 gateway 仅作为已验证 transport 的参考/共享 Slack 安装，不被 worker 继承其 terminal/tool 权限。
- rollout 每阶段必须达到最低覆盖：至少 20 个完整 monitor/job runs 且覆盖 >=95% 本阶段 eligible monitors（两者都满足），同时 invariant violations=0；不足覆盖只能继续观察，不能 promotion。


## Final scope resolution after Codex review

- Database permission boundary is in scope: active writers move from `SUPABASE_SERVICE_ROLE_KEY` to a restricted writer role/API key that can invoke only approved mutation RPCs; service-role credentials are reserved for migrations and controlled administration.
- Add deployment/integration tests proving direct INSERT/UPDATE/DELETE against business tables fail for the writer role while approved RPCs succeed, and verify no active client retains service-role credentials.
