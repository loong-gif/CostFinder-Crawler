---
name: costfinder-offer-quality
description: Use when reviewing, extracting, importing, repairing, or accepting CostFinder promotion or membership Offers, promo_offer_master rows, promo_offer_items, service mappings, price fields, conditions, or active-state data quality.
---

# CostFinder Offer 数据质量

让 active Offer 表达来源可以证明的单一商业条款，并让 master、item、service、promotion 或 membership 的状态形成闭环。

涉及抓取层、提取入口或表间架构时，同时遵循 [`costfinder-architecture`](../../../.cursor/skills/costfinder-architecture/SKILL.md)。本 Skill 不授权修改线上数据；按用户当前请求决定只审查还是执行修复。

## 事实与归因
`promotion_content`、membership `benefits` 和其他 `source_content` 是抽取、过滤或持久化后的派生证据。
- 原始 Markdown 缺少内容，才归到抓取、渲染或 OCR；原始内容存在而派生证据缺失，检查提取、过滤和持久化；派生证据正确而 Offer 错误，检查 Offer 抽取和后处理。
- 没有对应原始快照或抓取标识时，明确说明无法逐条完成 crawler-versus-extraction 归因，不要猜。

## Offer 语义

- 只保存来源直接支持的字段。没有原价基准时，不反推 `regular_price`、`discount_amount` 或 `discount_percent`。
- 只有当来源给出所需数值且计价基准一致时才计算派生价格；否则保留可证明字段，其余置空或进入 review。
- `total`、`per_unit`、百分比优惠和整单减免必须保持各自计价口径。最低购买数量是门槛，不是固定交付数量。
- 一个 Offer 只表达一套共同价格与资格条件。不同服务、价格或适用条件拆成不同 Offer；套餐可以有多个原子 Item。
- `promo_offer_items.item_name` 保留来源原文的商业条目名，不用推断的 `clinic_services.service_name` 替换；`service_id` 是独立的同 `business_id` 关联，广域 offer 保持 NULL（见 `AGENTS.md`）。
- `conditions` 的存储形态以当前 schema 和 `utils/offer_evidence_validator.py` 为准。保留 `NULL`（未可靠处理）与空数组（已确认无额外条件）的区别，不重复 `is_membership_required` 或 `is_new_customer_required` 已表达的条件。

## Active 状态闭环

Firecrawl Monitor 同步例外见 `AGENTS.md` 与 `docs/monitor_price_sync.md`。
该入口不以 `active_null_service_items=0` 作为价格同步门禁；其他入口的下述要求仍适用。

active 数据至少满足：

- active Offer 的上游 Promotion 或 Membership 仍有效；
- active Offer 至少有一个 Item；
- active Item 的 `service_id` 已解析（广域 offer 除外）；
- `per_unit` Item 有与 Offer 口径一致的 `unit_price`；
- Item 与 Service 不跨 Business、不名称错配、不在同一 Offer 内重复映射。

`is_offer_extracted=true` 表示来源、Offer、Service 映射和原子 Items 已完成，而不是“模型已经运行”。部分可抽取、范围仍含糊或存在 unresolved Item 时保持 false。

并发写入真实存在时，单次清理不算修复。沿实际写入顺序把已确认的不变量放到共享写入边界；例如既拦截 unresolved Item 写入，也拦截随后把 master 重新激活的路径。不要为未发生的并发或输入添加额外防御层。

## 修复边界

- Membership Offer 使用 `membership_plan_id IS NOT NULL AND promotion_id IS NULL` 限定；Promotion Offer 使用相反来源关系。不要用模糊名称或时间范围代替数据血缘。
- 来源不再支持且不能可靠表示的历史 Offer，优先设为 inactive 并记录原因；除非任务明确要求且关系已核实，不硬删除业务历史。
- 修复现有值时保留用户或人工审核结果；无法证明正确替代值时不要覆盖。

## 验收

只运行会改变结论或后续动作的检查。根据改动范围选择并回读这些已知门禁：

- `active_offer_without_item = 0`
- `active_null_service_items = 0`
- `active_per_unit_missing_unit_price = 0`
- `cross_business_mapping = 0`
- `item_service_name_mismatch = 0`
- `duplicate_offer_service_pairs = 0`
- `active_offer_on_inactive_promotion = 0`

字段非空、脚本退出成功或旧报告中的零值都不能替代当前数据库回读。写入超时、迁移失败或无返回结果时按未确认处理；先读取真实状态，再决定是否重试。完成报告要区分“结构安全”“部分同步”和“业务闭环完成”。
