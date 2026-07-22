---
name: costfinder-architecture
description: Use when changing CostFinder crawlers, Firecrawl raw tables, LLM extraction schemas, Supabase table writes, service or membership FK matching, or pipeline docs in this repo.
---

# CostFinder 架构护栏

## 先读什么

1. 本文件（不变量 + 变更流程）
2. [architecture.md](architecture.md)（端到端流程图）
3. 域示例：[examples/louloumedspa.com.md](examples/louloumedspa.com.md)

## 事实源优先级

冲突时按序判断，**不得让 Skill 覆盖代码**：

1. SQL 迁移 + `schema/*.json`
2. 生产入口及其调用的运行代码（`utils/schema_contract.py` 为表名权威）
3. `tests/` 契约
4. `README.md` / `docs/`
5. 本 Skill 快照

## 三层职责（不可混层）

| 层 | 职责 | 典型表/产物 |
| --- | --- | --- |
| 抓取 | Search/Scrape 原样落库 | `firecrawl_search_raw`, `firecrawl_scrape_raw` |
| 提取 | Schema 约束的 LLM 结构化 | `clinic_services`, `clinic_memberships`, `clinic_promotions`, `promo_offer_master`, `promo_offer_items` |
| 关系 | 名称/门槛匹配写外键 | `promo_offer_items.service_id`, `promo_offer_master.membership_plan_id` |

## 核心不变量

- `master_business_info` 是所有业务行的根（`business_id`）。
- **Search raw** 交付会员骨架与服务目录输入；**Scrape raw** 交付促销/Offer 详情页输入。
- Search 命中若**无价格信号**（title/markdown/description），**不得**进入 Scrape（见 `utils/search_scrape_gate.py`）。
- 会员表 canonical 名：`clinic_memberships`（`utils/schema_contract.py`）。`promo_membership_plans` 仅 legacy/归档脚本。
- Offer 服务组成在 `promo_offer_items`；**不要**在 `promo_offer_master` 上恢复 `service_id`（M009 后规范）。
- `offer_extraction_schema` → master（交易级）+ items（服务行）；`service_extraction_schema` → `clinic_services`；`membership_extraction_schema` → `clinic_memberships`；`promotion_extraction_schema` → `clinic_promotions`。
- 原始抓取、结构化提取、外键匹配必须分步；不要在 scrape 层直接写最终外键（除非已有匹配模块）。
- **一次性脚本**（单域修复、临时迁移、诊断实验）**只能**放在仓库根 [`one-off/`](../../../one-off/)，**禁止**在 `scripts/` 根目录新增或重建 `scripts/archive/`。见 [`one-off/README.md`](../../../one-off/README.md)。

## 变更检查清单

改架构相关代码前：

- [ ] 画出受影响节点：输入 raw 表、schema、目标表、外键
- [ ] Grep 所有 caller / consumer
- [ ] 确认未跨层（raw → 提取 → 关系）
- [ ] 改契约则同步：`tests/`、`docs/data-model-pipeline.md`、本 Skill 参考

## 输出格式（规划/评审任务）

用固定小节，便于对照：

```markdown
## 域与入口
- domain / business_id / seed 表行

## 抓取层
- Search 查询与 firecrawl_search_raw 用途
- Scrape URL 列表与 firecrawl_scrape_raw 用途

## 提取层
- schema → 目标表（逐步）

## 关系层
- 匹配字段 → 外键列

## 风险与漂移
- 与 schema_contract / 迁移不一致处
```

## 自测

域级 trace 可对照示例跑断言：

```bash
python .cursor/skills/costfinder-architecture/scripts/validate_trace.py examples/louloumedspa.com.trace.json
```
