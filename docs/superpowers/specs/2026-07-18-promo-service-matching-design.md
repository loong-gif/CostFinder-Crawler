# 促销服务与诊所服务目录匹配设计

## 目标

将促销页提取出的服务明细可靠关联到同一诊所的 `clinic_services`，覆盖注射、填充、激光、面部护理等不同服务，同时避免由大模型直接决定数据库外键。

## 核心原则

- 大模型提取网页原文，不输出或选择 `service_id`。
- `clinic_services.service_name` 继续保存现有标准化枚举值。
- `clinic_services.service_name_raw` 保存诊所官网上的原始服务名。
- `promo_offer_items.item_name` 保存促销页上的原始服务名。
- `promo_offer_items.service_id` 只能由应用层在同一 `business_id` 内确定性写入。
- 无唯一可靠匹配时保留 `NULL`，不阻止 Offer 入库。

## 数据模型

### `clinic_services`

新增：

- `service_name_raw text NULL`：诊所官网原始名称。

保留：

- `service_name`：标准化服务枚举。
- `unit_type`：计价单位。
- `service_area`：治疗部位。
- `regular_price`：常规价格，可为空。

如果 `service_name` 使用 PostgreSQL 原生 enum，短期沿用现状。标准服务种类开始频繁变化时，再评估迁移到独立服务标准表；本次不扩大范围。

### `promo_offer_items`

- `item_name`：促销页原始服务名。
- `service_id`：匹配成功后指向 `clinic_services`，否则为 `NULL`。
- `unit_type`、`service_area`、`quantity`：用于表达套餐明细并辅助消歧。

`promo_offer_master` 不新增 `service_name`。一个 Offer 可能包含多个服务，服务身份属于 items 层。

## 提取 Schema

`promotion_extraction_schema.json` 的 `items`：

- 保留 `item_name`、`quantity`、`unit_type`。
- 删除 `service_id`，避免让模型编造或错误选择数据库主键。

模型输出的 `item_name` 必须忠实保留页面原文。标准化和数据库关联均在提取完成后执行。

## 匹配流程

每个 Offer item 独立执行：

1. 清洗 `item_name` 的大小写、空白和非语义标点。
2. 使用现有 `service_name_dict.json` 将原始名称映射为标准 `service_name`。
3. 只查询相同 `business_id` 且标准 `service_name` 相同的 `clinic_services`。
4. 候选超过一条时，依次使用 `unit_type` 和 `service_area` 消歧。
5. 只有候选唯一时才写入 `promo_offer_items.service_id`。
6. 无候选、多个候选或标准化失败时，`service_id` 保持 `NULL`。

模糊匹配只生成审计候选，不自动写入外键。禁止跨 `business_id` 匹配。

## 未匹配与异常处理

- 未匹配 item 不影响 Offer 及其他 items 入库。
- 套餐允许部分 items 匹配成功、部分保持 `NULL`。
- 未匹配原因写入现有提取报告或变更审计结果，不新增专用数据库字段。
- 促销页出现但目录中不存在的服务，不自动创建 `clinic_services` 骨架行，因为促销信息不能证明该服务存在常规目录价。
- 数据库查询或写入失败时保留原始 item，记录错误，不写未经验证的 `service_id`。

## 测试要求

至少覆盖：

1. 标准服务名精确匹配。
2. 原始别名经词典映射后匹配。
3. 同标准名按 `unit_type` 消歧。
4. 同标准名按 `service_area` 消歧。
5. 多候选无法消歧时保持 `NULL`。
6. 未知服务保持 `NULL`。
7. 不允许关联其他 `business_id` 的服务。
8. 套餐 items 分别匹配，允许部分成功。

## 非目标

- 不让大模型生成或执行 SQL。
- 不让大模型直接选择 `service_id`。
- 不在 `promo_offer_master` 重复保存服务名。
- 不在本次设计中引入新的服务标准表或向量检索。
