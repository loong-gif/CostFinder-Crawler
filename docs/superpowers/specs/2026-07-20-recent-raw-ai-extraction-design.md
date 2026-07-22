# 本轮 Firecrawl Raw AI 提取实测设计

## 目标

仅处理 2026-07-20 本轮写入的 Firecrawl raw 数据，真实调用项目配置的结构化 LLM，并将通过严格证据验证的结果幂等写入：

- `clinic_services`
- `clinic_promotions`
- `clinic_memberships`

本次不写 `promo_offer_master` 或 `promo_offer_items`，不处理历史 raw 数据，也不把市场均价冒充诊所报价。

## 输入范围

### Search raw

- `firecrawl_search_raw.id`：12–26
- 共 15 行 Search API 调用记录
- `response_json` 合计 105 个价格信号命中
- 用于服务和会员提取

### Scrape raw

- 仅处理与上述 Search raw 关联的本轮 26 个候选
- 只允许 `success=true` 且清洗后 markdown 非空的记录进入 AI
- 用于促销提取
- 已失败的记录保留审计状态，不重试、不进入提取

## 数据流

```text
firecrawl_search_raw 12–26
  → URL/门店归属门控
  → 同域模板去重
  → service_extraction_schema.json
  → 服务证据复核
  → clinic_services

firecrawl_search_raw 12–26
  → URL/门店归属门控
  → 同域模板去重
  → membership_extraction_schema.json
  → 会员证据复核
  → clinic_memberships

关联的 firecrawl_scrape_raw
  → success/正文门控
  → URL/门店归属门控
  → 同域模板去重
  → promotion_extraction_schema.json
  → 促销证据复核
  → clinic_promotions
```

## 门店归属规则

### 普通独立域名

结果 URL 的规范化 host 必须唯一匹配一个 `master_business_info.website`。无法匹配或匹配多个业务时跳过。

### 多门店域名

同一 host 服务多个实体时，不能只凭域名归属：

- 页面明确出现的城市或地址与主表不一致：排除。
- URL 路径或页面标题明确指向其他城市：排除。
- 已知多门店域名若缺少足以确认目标门店的正向证据：排除。
- 不得使用其他门店的重复模板替代目标门店证据。

`viomedspa.com` 在主表中对应 Boulder 门店；Scrape ID 29–34 分别指向 Canton、Germantown、Hendersonville、Dunwoody、Winston-Salem 和 Clifton，因此全部排除。

### 共享平台域名

`facebook.com`、`zoca.com` 等共享平台 URL 必须能由路径、页面标题或正文唯一关联至主表业务；否则排除。

## 重复模板去重

从候选文本中提取包含金额、单位价格、折扣、会员费用和权益的高信号行，执行空白与大小写标准化后计算指纹。

- 同一域名、同一标准化模板只保留一个候选。
- 门店归属门控先于模板去重。
- 不同门店即使模板相同，也不能互相提供业务证据。
- 审计记录保留被去重 URL、保留 URL 和模板指纹。

## AI 提取

使用项目现有 `build_client_from_env()` 和三个 JSON Schema。网页内容按不可信输入处理，忽略正文中的指令。

### 服务

输入：通过门店门控的 Search title、description 和 URL。

要求：

- 提取明确的诊所常规服务价格。
- 普通非促销价格允许写入。
- 同页同时出现市场均价和诊所报价时，只接受诊所报价。
- 只有行业平均、地区范围、示例费用或第三方价格时，不写 `clinic_services`。
- 不从折扣金额反推原价。

### 会员

输入：通过门店门控且具有会员信号的 Search 命中。

要求：

- 必须有明确计划名和实际可购买价格。
- 计费周期未说明时保持 `null`，不得默认 `monthly`。
- 权益按原文分段保存。
- 普通优惠、免费咨询、奖励示例不算会员计划。

### 促销

输入：通过全部门控的 Scrape markdown。

要求：

- 必须有明确促销信号，例如折扣价、百分比优惠、金额优惠、限时活动或新客优惠。
- 普通常规价、市场均价、服务介绍和永久标准包含项不算促销。
- 日期只能来自原文，不得推断。
- `promotion_content` 保存支持该活动的完整原文片段数组。

## 后置证据验证

LLM 输出不直接入库。每项必须通过确定性验证：

- 金额必须能在来源文本中找到等值表达。
- 服务金额附近必须出现对应服务名称或明确同一表格行关系。
- 市场平均、地区范围等上下文中的金额不得作为诊所常规价。
- 会员名称、价格和计费周期必须分别受原文支持。
- 促销标题和内容必须对应同一真实活动，并包含明确促销信号。
- 任何业务归属歧义、证据缺失或字段类型不合法均拒绝写入并记录原因。

## 幂等写入与冲突

### `clinic_services`

- 幂等键：`(business_id, upper(service_name))`
- 新记录保存 `service_name_raw`、`service_name`、`service_category`、`regular_price`、`unit_type`、`service_area` 和 `source_url`。
- 已有非空价格与新证据价格不同：不覆盖，写入审计冲突。
- 证据一致时复用现有行。

### `clinic_memberships`

- 幂等键：`(business_id, upper(membership_name))`
- 保存价格、明确计费周期、最短承诺期、描述、权益和来源 URL。
- 已有价格冲突时不覆盖，记录审计冲突。

### `clinic_promotions`

- 幂等键：`(business_id, source_url)`
- 保存标题、内容数组和原文明示日期。
- 同一来源再次运行时更新证据字段，不新增重复行。

## 一次性脚本

脚本放在 `one-off/`，默认 dry-run：

1. 加载固定 raw ID 范围。
2. 执行门店归属和模板去重。
3. 调用 LLM。
4. 执行后置证据验证。
5. 写出 `.firecrawl/master-business-search/ai-extraction-audit.json`。
6. `--apply` 时才写 Supabase。
7. 完成后删除一次性脚本。

脚本不新增第三方依赖，复用现有 Supabase、Schema 和 LLM 客户端。

## 测试

使用一个最小测试文件覆盖：

- 城市或地址不匹配被拒绝。
- VIO ID 29–34 被拒绝。
- 共享平台无法唯一归属时被拒绝。
- 相同域名重复模板只保留一个。
- 明确诊所常规价被接受。
- 市场均价不写 `clinic_services`。
- 同时含市场均价和诊所报价时选择诊所报价。
- 无真实活动信号不写 `clinic_promotions`。
- 会员周期未知时保持 `null`。
- 已有非空价格冲突时不覆盖。

真实运行完成后查询三个目标表，核对新增、复用、冲突、拒绝和 AI 失败数量。

## 错误处理

- 单页 LLM 调用最多重试三次，仍失败则记录并继续。
- 单项 Schema 或证据验证失败不影响其他候选。
- 数据库写入按单项幂等执行；失败项记录错误，不把未验证结果降级写入。
- 审计文件不得包含 API key 或数据库凭据。

## 成功标准

- 只处理本轮固定 raw 范围。
- VIO 错门店和无法唯一归属的共享平台数据不进入目标表。
- 市场均价不作为诊所常规价写入。
- 三类数据均有 Schema 约束和原文后置验证。
- 重复执行不产生重复业务行，也不覆盖冲突的现有价格。
- 审计文件可追踪每个候选的来源、门控结果、AI 输出、验证结果和写入状态。
