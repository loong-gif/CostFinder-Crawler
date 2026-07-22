# Masters Medspa LLM 服务与促销提取设计

## 域与入口

- 域名：`masters-medspa.com`
- 业务主键：`master_business_info.business_id = 2889`
- 服务输入：`firecrawl_search_raw.id IN (10, 11)`
- 促销输入：`firecrawl_scrape_raw.id IN (14, 15, 16)`

本次是一次性真实 LLM 实测，只写 `clinic_services` 与 `clinic_promotions`。

## 提取层

### 服务

将 Search raw 的 URL、标题和描述整理为带来源 URL 的证据块，使用
`service_extraction_schema.json` 调用当前 `.env` 配置的 Gemini 模型。

只接受满足下列条件的服务：

- `service_name` 属于 schema 枚举。
- `service_name_raw` 非空。
- `regular_price > 0`。
- `unit_type` 和 `service_category` 满足 live 表枚举；缺失时不猜测并拒绝该行。
- 价格必须能在对应 Search raw 证据中找到。

预期证据支持 Botox、Daxxify、Dysport、Jeuveau 的每单位常规价格。

### 促销

逐个读取已清洗的 Scrape raw markdown，使用
`promotion_extraction_schema.json` 提取折扣、限时活动或有明确优惠条件的促销。

常规定价、服务菜单、融资说明和市场均价不得视为促销。当前三页若没有真实促销，
模型应返回空 `promotions`，因此不得为了满足测试而创建占位行。

## 审计与写入

LLM 原始结构化输出和校验结果先保存到 `.firecrawl/masters-medspa/llm-extraction.json`。
只有通过确定性校验的记录才写库。

- `clinic_services`：按 `(business_id, service_name)` 查询后更新或插入。
- `clinic_promotions`：按 `(business_id, source_url)` 查询后更新或插入。
- `source_url` 与 `business_id` 由程序从 raw 行注入，不由 LLM 生成。
- `promotion_description` 将 schema 的分段数组以双换行拼接，保留原文顺序。
- 不调用当前与 live schema 漂移的 `seed_skeleton()` 或旧 promotion helper。

当前环境没有 `SUPABASE_WRITER_KEY`。LLM 提取在本地执行，审核后的幂等写入通过已连接的
Supabase MCP 完成。

## 错误处理

- LLM 未配置、HTTP 失败或结构化输出不合法时停止，不写数据库。
- 任一服务字段不满足 live 表约束时记录拒绝原因，不部分补猜。
- 促销为空是有效结果，不算失败。
- 写入后必须回查目标行，并确认未修改其它业务表。

## 验证

- 审计 JSON 包含模型、raw ID、来源 URL、原始输出、接受行和拒绝原因。
- 服务价格与 raw 证据逐项一致。
- `clinic_services.business_id = 2889` 的写入行满足非空和枚举约束。
- 没有真实促销时，`clinic_promotions.business_id = 2889` 仍为空。
- 重复运行不产生同业务、同服务或同来源 URL 的重复行。
