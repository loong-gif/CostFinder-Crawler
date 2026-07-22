# 会员计划抽取 Schema 设计

## 目标

新增 `schema/membership_extraction_schema.json`，从单个诊所页面提取一个或多个可购买的会员计划，为 `clinic_memberships` 提供业务字段。

## 输出结构

顶层沿用现有提取 Schema 约定：

- `explanation`：必填的证据摘要，仅用于审计，不入库。
- `memberships`：会员计划数组；页面没有价格明确、可购买的会员计划时输出空数组。

每个会员计划包含：

- `membership_name`：页面中的计划名称。
- `membership_price`：页面明确给出的正数价格；不得推断。
- `billing_period`：`monthly`、`quarterly`、`annual`、`one_time` 或 `null`，与数据库枚举一致。
- `minimum_commitment_months`：页面明确说明的最短承诺月数，未知为 `null`。
- `membership_description`：计划的简短原文描述，未知为 `null`。
- `benefits`：字符串数组，每项是一段独立、完整的权益原文；不分类、不拆数值、不推断，无明确权益时为 `[]`。

## 字段边界

LLM 不生成 `plan_id`、`business_id`、`source_url`、`created_at` 或 `updated_at`。`business_id` 和 `source_url` 由调用程序从抓取上下文注入，其余字段由数据库生成。

只有页面明确给出会员名称、正数价格和计费关系时才生成计划。免费咨询、普通促销、奖励示例和不可购买的介绍不属于会员计划。

## 验证

- JSON 文件可被标准库解析。
- 使用 Draft-07 结构约束必填字段、枚举、正数价格和非负承诺月数。
- 用已抓取的 Skinjectables membership 页面做一次结构化抽取测试，确认 `$179/month`、三个月最低承诺和分段权益可正确表达。
