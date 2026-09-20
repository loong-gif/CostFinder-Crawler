---
name: costfinder-promotion-sync
description: Use when auditing or synchronizing CostFinder clinic_promotions against current web pages, screenshots, OCR, promotion_content, downstream Offers, services, items, or is_offer_extracted state.
---

# CostFinder 实时 Promotion 同步

以当前可验证网页为 active Promotion 的主要事实来源，并把已确认内容同步到完整的下游 Offer 结构。

涉及通用 Offer 价格和关系质量时遵循 [`costfinder-offer-quality`](../costfinder-offer-quality/SKILL.md)；涉及抓取或管线变更时遵循 [`costfinder-architecture`](../../../.cursor/skills/costfinder-architecture/SKILL.md)。本 Skill 不授权抓取之外的线上写入；审查任务只报告，用户要求修复时才更新数据。

## 判断当前事实

- 先读取 Promotion、现有 active/inactive Offers、Items、Services 和来源 URL 的当前状态，再决定差异。
- 当前 live 页面用于判断当前展示内容；已有 `promotion_content` 或历史截图用于追溯，不应和当前页面混成同一个无时间信息的证据块。
- 页面仍可访问不等于 Promotion 仍有效。结合页面日期、季节或节日语义、最后更新时间以及当前内容判断 stale；证据不足时保持待确认，不凭 URL 可达性直接激活。
- 普通 Markdown 缺少图片内促销时，使用截图/OCR 做交叉核验。只有确实获得可持久化的截图 URL 或文件时才声称已保存截图；视觉核验本身不等于 `screenshot` 字段已落库。

## 同步方式

1. 从 live 证据构建 expected Offers，每条保留能直接支持价格、适用范围和限制条件的原文。
2. 将 expected Offers 与当前 active Offers 做语义 diff，而不是只比较记录数量。
3. 当前页面不再支持的 Offer 设为 inactive；支持的 Offer 更新或创建，并补齐同 Business 的 `clinic_services` 映射和原子 `promo_offer_items`。
4. 不同服务价格、不同资格或不同优惠口径拆开；只有共同套餐总价下的组成服务放在同一 Offer。
5. 只有所有应有 Offer 都已处理、每个 active Item 都解析到 Service、价格口径自洽时，才设 `is_offer_extracted=true`。

能够明确同步的部分可以落库，但只要仍有泛服务范围、会员 qualifying service、图片上下文或其他无法可靠原子化的内容，保持 `is_offer_extracted=false` 并说明剩余边界。

## 历史与停用

- 历史 Promotion/Offer 默认停用而非硬删除，保留审计和关联记录。
- Promotion 停用时不得留下 active 下游 Offer。
- 不因当前页面变化而改写历史证据为仿佛当时就存在；需要时记录本次核验时间和新证据来源。

## 完成标准

回读本次目标 Promotion 的状态和全部 active Offers/Items，确认：

- expected Offer 集合与 live 页面一致；
- unsupported 旧 Offer 已 inactive；
- active Offer 无 unresolved Service；
- 价格、条件和 Item scope 均由对应证据支持；
- `is_offer_extracted` 准确表达完整或未完整状态。

报告中逐个 Promotion 标注“完成、部分完成、停用或待人工时效判断”，不要把上游内容更新描述成完整下游同步。
