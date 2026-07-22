# 测试用例：louloumedspa.com

用于验证架构 Skill 是否引导 Agent 走规范三层流水线。

## 域与入口

- **domain**: `louloumedspa.com`
- **seed**: `master_business_info.website` → `business_id`（爬虫/提取均挂此 FK）
- **已知会员页**: `https://louloumedspa.com/membership/`（归档 URL 清单中有记录）

## 抓取层

| 步骤 | 动作 | 落库 |
| --- | --- | --- |
| Search | `include_domains=louloumedspa.com`，发现 pricing / membership / specials URL | `firecrawl_search_raw` |
| 筛选 | 从 search 结果挑会员页、价目页、促销详情 URL | — |
| Scrape | `onlyMainContent=true` 抓取详情页 markdown | `firecrawl_scrape_raw` |

**不变量**：服务/会员骨架来自 **search raw**；促销/Offer 详情来自 **scrape raw**。

## 提取层

| 顺序 | 输入 | Schema | 目标表 |
| --- | --- | --- | --- |
| 1.1 | search raw（会员相关 URL/markdown） | `membership_extraction_schema.json` | `clinic_memberships` |
| 1.2 | search raw（价目页） | `service_extraction_schema.json` | `clinic_services` |
| 2.1 | 已有 `clinic_services` | — | 推进促销提取 |
| 2.2 | scrape raw（specials/pricing 详情） | `promotion_extraction_schema.json` | `clinic_promotions` |
| 2.3 | scrape raw + 活动上下文 | `offer_extraction_schema.json` | `promo_offer_master` + `promo_offer_items` |

## 关系层

| 匹配 | 来源 | 外键 |
| --- | --- | --- |
| 服务名 | `promo_offer_items.item_name` / schema `service_name` ↔ `clinic_services.service_name` | `promo_offer_items.service_id` |
| 会员门槛 | offer `is_membership_required` + 计划名 ↔ `clinic_memberships` | `promo_offer_master.membership_plan_id` |

## 常见错误（基线失败模式）

1. 写 `promo_membership_plans` 而非 `clinic_memberships`
2. 在 `promo_offer_master` 写 `service_id`（M009 后应在 items）
3. 用 scrape raw 做服务目录提取（应用 search raw）
4. 跳过 `promo_offer_items`，只写 master

## 机器可读 trace

见同目录 `louloumedspa.com.trace.json`；用 `validate_trace.py` 校验。
