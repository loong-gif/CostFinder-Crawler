---
name: costfinder-safe-backfill
description: Use when planning or applying a CostFinder bulk repair, historical backfill, Supabase data migration, frozen dry-run plan, or concurrent-safe update of promotion, membership, offer, item, service, or conditions data.
---

# CostFinder 安全回填

让批量修复可审核、可跳过冲突，并且不会在执行期间被持续写入链路重新污染。

数据语义和验收门禁遵循 [`costfinder-offer-quality`](../costfinder-offer-quality/SKILL.md)。若只是一次性诊断或单域修复，同时遵守 [`costfinder-architecture`](../../../.cursor/skills/costfinder-architecture/SKILL.md) 的脚本放置约定。

## 先判断缺陷是否仍在产生

- 找到真实生产入口和共享落库点。若当前链路仍会写入同类坏数据，先做最小 forward fix，再回填历史；否则回填过程中会继续出现新目标。
- 只修改所有真实入口共同经过的 schema、证据校验器或持久化点。不要逐个扩改不经过该路径的 CSV、历史 repair 或临时工具。
- 只有已观察到并发写入会破坏结果时才增加数据库不变量；按实际写入顺序覆盖可绕过路径。

## Dry-run 计划

- 默认只读，按执行时谓词选择目标，例如 active 且目标字段为 NULL；不要把检查时的总数写死。
- 将目标 ID 和必要的版本字段冻结到一个带时间戳、文件名无空格的计划文件中。
- 按真实来源分组：Promotion 用 `promotion_id`，Membership 用 `membership_plan_id`。同一来源尽量一次处理其全部 Offers，避免重复调用和跨 Offer 串条件。
- 计划只保存应用和审核需要的字段：目标 ID、来源标识、关键原文、预期值、状态、原因以及 Offer/source 的 `updated_at`。不要保存整页副本、confidence、source hash、checksum 或没有消费端的元数据。
- 来源不足、匹配含糊、返回缺失或证据不成立的记录进入 `review`，保持原值；不要用二次模型或自动在线扩搜掩盖不确定性，除非用户明确扩大任务范围。

## Apply

只应用计划中的 ready 记录，并重新检查决定写入安全性的条件：

- ID 与来源关系仍匹配；
- 仍处于计划要求的 active/inactive 状态；
- 目标字段仍保持计划预期的旧值；
- Offer 与 source 的 `updated_at` 未变化。

冲突记录跳过并写入 apply 结果，不自动重跑、不覆盖人工值。保留字段已有的状态语义，例如 `NULL` 表示未可靠处理、空数组表示已确认没有内容。

Schema 或长期业务不变量用正式迁移；可复用运维流程可以保留脚本；真正一次性的脚本放 `one-off/`。不要为了回滚想象、兼容未发生的格式或一次性计数增加框架。

## 验收与停止条件

- 对冻结目标集做一次回读：ready 与计划一致，review 未被写入，冲突记录已跳过，范围外记录未改变。
- 再运行本次修复会改变结论的跨表业务门禁，不用固定全库数量代替冻结集合验证。
- 写入失败或连接层没有返回执行结果时，按未确认处理并读取真实状态；不能从“请求已发送”推断成功。
- 验收通过后停止。不要为已经确认的同一集合继续增加重复核验轮次。

审计产物记录范围、决策、实际修改、跳过原因和门禁结果；瞬时数量、临时 URL 状态和一次性记录 ID 不写成永久规则。
