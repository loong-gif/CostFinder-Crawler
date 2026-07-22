# CostFinder 架构护栏 Skill 设计

## 目标

在 `.cursor/skills/costfinder-architecture/` 创建项目级 Skill。处理爬虫、LLM 提取 Schema、数据表、外键匹配或数据流文档时自动加载，先理解端到端链路，再约束改动不破坏层级职责和关系。

## 方案

采用两文件结构：

- `SKILL.md`：触发条件、事实源优先级、核心架构不变量、变更检查流程。
- `architecture.md`：规范化 Mermaid 流程图、节点职责及关系说明。

不增加脚本或依赖。架构判断存在语义差异，静态校验器无法可靠判定；维护一份短参考比引入生成器更符合当前需求。

## 事实源优先级

发现架构描述冲突时按以下顺序判断：

1. 已部署方向的 SQL 迁移与当前 JSON Schema
2. 生产入口及其调用的运行代码
3. 测试所表达的契约
4. `README.md` 与 `docs/`
5. Skill 内架构快照

若前三级与 Skill 不一致，先指出漂移，再在同一改动中更新 Skill；不得让 Skill 覆盖代码事实。

## 规范架构

主数据流分为三层：

1. 数据源与抓取层：`master_business_info` 驱动 Firecrawl Search/Scrape，原始结果分别进入 `firecrawl_search_raw` 与 `firecrawl_scrape_raw`。
2. AI 提取与落库层：
   - 服务提取写入 `clinic_services`
   - 会员提取写入 `promo_membership_plans`
   - 促销活动提取写入 `clinic_promotions`
   - Offer 提取把交易级字段写入 `promo_offer_master`，把服务组成写入 `promo_offer_items`
3. 关系建立层：
   - `promo_offer_items.service_id` 关联 `clinic_services.service_id`
   - `promo_offer_master.membership_plan_id` 关联 `promo_membership_plans.plan_id`

Skill 使用当前项目名称 `promo_membership_plans`，不沿用图中的 `clinic_memberships`。`promo_offer_items` 是 M009 后服务关系的承载表，`promo_offer_master.service_id` 不再作为规范关系。

## 使用行为

Agent 在相关任务中应：

1. 读取 Skill 和架构参考。
2. 定位受影响节点、输入、输出及外键。
3. 检查所有调用方和下游消费者。
4. 保持原始抓取、结构化提取、关系匹配三种职责分离。
5. 修改架构契约时同步测试、README/数据流文档和 Skill 参考。

## 验证

用同一项目变更场景做前后对照：

- 基线：不提供 Skill，观察 Agent 是否误用旧表名、把服务外键放回 master，或遗漏跨层影响。
- 使用 Skill：要求 Agent 正确识别 `promo_offer_items.service_id` 与 `promo_membership_plans`，并列出受影响层级和同步文档。

完成后检查 YAML frontmatter、Skill 行数、引用路径和 Mermaid 语法，并更新根 `README.md` 的 Skill 使用入口。
