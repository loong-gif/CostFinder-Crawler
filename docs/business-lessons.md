# CostFinder 业务经验与未完成事项

整理自 2026-07 至 2026-11 的历史会话。数据库数量、网页价格、服务器状态均为当时记录；实施前先读取当前代码与数据库定义，其中 taxonomy、标题、blanket 与 fingerprint 规则经历多次修改，以当前拥有这些规则的代码和实时数据库为准。

## 项目背景与资料入口

项目包含抓取、抽取、规范化、Supabase 写入与消费者前端。2026-07-25 的记录中，爬虫仓库为 `CostFinder-Crawler`，前端仓库为 `costfinders_v3`，两个仓库需要分别检查工作区、提交和部署。

CostFinder 的 Supabase 项目标识为 `kdlpkjzcnbkjcvwsvlwn`。恢复数据库任务前读取实际连接信息，避免使用其他项目。核心对象包括 `master_business_info`、`clinic_services`、`clinic_memberships`、`clinic_promotions`、`promo_offer_master`、`promo_offer_items`、条款及价格历史表。

持久入口：

- `docs/data-model-pipeline.md`：数据流程、字段语义与发布门槛。
- `.agents/skills/costfinder-offer-quality/SKILL.md`：证据、价格口径、Item、Service 映射与验收。
- `.agents/skills/costfinder-promotion-sync/SKILL.md`：来源复核、日期与状态同步。
- `.agents/skills/costfinder-safe-backfill/SKILL.md`：冻结差异、版本保护、冲突跳过、写入回读。
- `schema/live_schema_snapshot.json`、`schema/offer_extraction_schema.json`、`schema/membership_extraction_schema.json` 与 `supabase/migrations/`：契约入口，实时数据库定义优先用于判断当前行为。

## 业务经验

### 来源证据与页面完整性

抓取到文字、结构检查通过或模型给出可信解释，只能分别证明这些步骤的结果。页面发布及 Offer 激活还需要可见证据、同一服务归属、完整卡片与适用条件。文章、普通价目表、服务介绍、导航页、thank-you 页面、长期会员权益均须按实际页面类型处理。

原价、售价、节省金额、最低购买量、交付数量、赠送数量和赠品价值必须区分。不得从节省金额反推出售价或原价，不得从其他卡片搬入金额。原文只写月份时，年份保持未知。来源数字之间有算术冲突时，保留复核状态；同一页的价格与资格不能拆散到不同优惠。

截图遮挡、缺少图片文字、分页未覆盖或只读取局部页面时，页面缺失本身不能证明旧优惠已移除。到期、消失与来源无法访问是不同状态，停用需要对应证据。历史促销照片与当前网页须区分日期，写入前复核当前来源。

### Item、Service 与价格语义

- `item_name` 保留来源中的优惠项目名称，`service_name` 用于规范化匹配。自定义标题不应随服务名称更新被覆盖；标题与标准 Service 名称不同，本身不是 QA 错误。
- 同一商家的具体品牌、部位和治疗方法需要来源支持。通用 Neurotoxin 不能猜测成 Botox；具体 filler 不应自动压成其他服务。服务不存在且缺少可信目录时保留待复核，不使用 `Other` 制造映射。
- 多 Item 数量不能自动证明 package；`is_package` 与实际 Item 数量是不同概念。重建错误 Offer 时先核对完整服务清单，再整体处理 Items，避免新增正确 Item 后遗留错误占位 Item。
- 2026-09-15 起，数据库 `promo_offer_items.quantity` 已重命名为 `delivered_units`，提取层继续使用内部 `quantity` 并显式映射。恢复旧脚本前检查其字段引用。
- 未知 `unit_type` 保持 `NULL`。2026-09-22 已清理默认 `treatment` 及 `others`；合法枚举、单位与 service taxonomy 应共同检查，应用、抽取 schema 与数据库 enum 必须一致。
- `service_area` 的最终规则将 `full face` 归为 `face`，复合部位设为 `NULL`；原始完整区域信息保存在来源文字。
- 总价、每单位价格、起价 `from`、套餐总价、最低购买量分别保存。套餐总价不能回填为常规单位价格。只有同服务、同单位、同数量、同资格基础可比较的价格才能计算差额与百分比。
- 常规原价需有无条件、同规格来源依据；新客、会员与其他有条件价格需要保留资格。未知权益或单位不由默认值填充。

### 全品类优惠、会员与完成标志

"10% off all products and services"等全品类优惠保持整体语义，不复制成一批具体治疗项目并展示到治疗页面。此类 Offer 的 `service_id=NULL`；允许 canonical blanket Offer 的迁移方案是否已应用，需查实时定义，已生成的文件不等于已部署。

每次促销数据更新后运行 `AGENTS.md` 规定的 blanket-offer 检查，除非有明确记录的有意保留，结果应为 0。普通 active Offer 的无 Item、无可信 Service、跨商家 Service 与失效父来源需要分别检查。

`is_offer_extracted` 由数据库触发器 `trg_sync_is_offer_extracted` 派生，规则见 `AGENTS.md`。会员来源权益不完整时进入待复核，未知权益不使用空数组宣称完整。

Promotion 与 Membership 可能描述相同优惠。`membership_duplicate_check.py` 按同商家、名称、价格与实质条件检查重复；服务 ID 本身不能代表优惠身份。新客与有条件老客优惠不能因名称和金额相同而合并。

### 安全写入与验收

执行数据修复前保存来源、旧值和精确差异；写入按已冻结计划并使用 `updated_at` 等版本条件保护，冲突记录单独保留。写入成功响应后还需回读字段和关联对象，因为触发器可能重写输入。

关注源表、Master、Items、条款、价格历史及前端展示之间的真实联动。先写 inactive Master、补齐 Items 后激活是安全路径之一。价格更新可能新增自动价格历史，"仅修改现有业务字段"仍会触发审计记录。

判断修复完成使用目标范围和全局不变量的实际结果：孤立 Item、跨商家映射、inactive 来源下 active Offer、active 重复、价格口径关系以及来源完整性。SQL 返回一项统计并不表示数据库阻止写入，触发器与外部验收逻辑要分别检查。

辅助 QA/backup 对象不放入公开 Data API schema。2026-09-18 已将 23 个辅助对象移至 `private`，恢复任务仍需读取实时 RLS 和权限。

## 数据管道与运维经验

流程为 Firecrawl 抓取 → AI 按 schema 提取 → Python 清洗、证据校验、Service 匹配 → 写入 Master/Items → 数据库规范化与质量门槛。模型输出与入库字段并不一一相同，Python 自行判断 `price_model`、单位、数量、名称和指纹。

2026-11-05 的管道包括：独立来源页分类与来源复核；卡片按标题及价格事实绑定；会员计划按来源权益清单检查；Tesseract OCR 将图片链接、原文和元数据关联页面。OCR 元数据保存在原始抓取 `response_json`，避免写入数据库不存在的列。语法编译、模块导入和真实页面片段检查只证明所覆盖步骤，完整批次须单独验收。

Firecrawl 失败要区分服务、网络、目标站点和模型。2026-09-25 曾发生 Btrfs 只读导致 Docker 与抓取异常；恢复文件系统后抓取恢复，目标网站 403、连接重置、Gemini 额度和 Webhook 404 仍各自存在。重复配置相同密钥不能增加可用额度。

Monitor 抓取运行、检测差异、发送通知和自动更新 Offer 是独立步骤。定时抓取成功不能说明整条同步流程有效。

前端经验：稳定排序与分页读取商家；来源字段进入 JSON-LD 前做正确转义；Realtime 订阅范围和匿名 SELECT 可读范围共同决定暴露的数据。生产构建曾多次失败于后台 `BusinessAuthProvider`，页面检查通过不等于全站构建通过。

## 未完成事项

| 记录日期 | 待处理事项 |
|---|---|
| 2026-11-07 | `link-services` 的 `apply-sql` 关联 item 时，`fn_recompute_offer_fingerprint_from_items` 重算指纹，与已有 active offer 相同会触发 `idx_promo_offer_active_fingerprint`（仅约束 active 行）的唯一冲突，整个 DO 块回滚。`split-offers` 拆出的同文重复 offer（如 8350、10090、10091）关联到同一 `service_id` 后必然出现这种冲突。已在 MCP 中手工逐条处理：撞上重复时停用后到的 offer 再完成关联，随后对已完整关联的 inactive offer 逐条尝试激活，冲突者保持 inactive。`utils/service_linking.py` 的 `render_apply` 尚未写入这段逻辑，下次关联遇到重复指纹仍会整体回滚。另有约 760 条 item 因泛称或多服务歧义未能关联。 |
| 2026-11-05 | 真实来源端到端抽取尚无完整通过证据。会员、OCR、缓存、卡片匹配已有局部修复与验证记录。项目禁止 mock，不使用 FakeLLM。 |
| 2026-10-23 | Offer 8381 单价写入后仍回读为 $770，需查触发器及价格来源。11100、11102 数量与价格，8087 阶梯价格，10751 混搭，fingerprint 与 Service 映射候选仍需逐项证据。 |
| 2026-10-23 | 图片真值核验进度 572/681，剩 109；未冻结真值，未完成盲读评测与发布验收。进度路径 `output/monitor_price_sync/remaining_sync_20261020/`。 |
| 2026-10-23 | 760 家候选商家已遍历；dry-run 记录为 641 页已验证，仍有待抽取与缺证据记录，业务写入 0。先读取 `one-off/outputs/candidates_20261020/handoff_20261023.md`、`dry_run_summary.md` 与覆盖 JSON，核对断点和数据库回读时间。 |
| 2026-10-27 | 已有 7 条 Luxe Room 价格字段和 4 条 Item 关联更新记录；其余未映射、重复、日期和消费者展示未完成全量核验。 |
| 2026-10-13 | `fn_offer_items_signature` 的实际定义未取得，赠品、单位及空值是否参与身份仍未核实。历史中存在多次不同 fingerprint 设计，当前规则以实时函数、调用方和最新需求共同确定。 |
| 2026-10-01 | 原价同步迁移 `20261001022038_sync_offer_regular_price_from_service_inputs.sql` 仅生成，未应用；Offer 原价是否仅接受本条原文、是否停止从 Service 派生，尚待确认。 |
| 2026-08-01 及更早 | Realtime 部署、城市 profile 迁移、凭据轮换与登录账号验收有未完成记录。恢复前检查现有部署、迁移及权限。 |
