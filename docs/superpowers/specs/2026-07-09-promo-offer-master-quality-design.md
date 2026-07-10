# promo_offer_master 质量提升设计

**日期:** 2026-07-09  
**状态:** 已批准，实现中

## 问题

1. **43% 重复** — web_change_driven monitor 重提时只 INSERT，不去重不 UPDATE
2. **价格仅 22% 完整** — 双价少；discount_percent/amount 94% NULL；11 行折扣价高于原价
3. **其它脏数据** — service_category 26% NULL；bool 混用；unit/area 大小写不一致；23% 极短 offer_raw_text

## 方案（应用层指纹 + DB 唯一索引）

### 指纹

```text
offer_fingerprint = sha1(
  normalize_url(source_url) + "|" +
  normalize_service_name(service_name) + "|" +
  normalize_unit_type(unit_type)
)
```

不含价格。insert 前查同 `source_url` + `status=active` + 同指纹 → UPDATE，否则 INSERT。

### 历史去重

- 物理 DELETE 重复行（用户选择 B）
- 每组 fingerprint 只留 1 条 winner（打分：双价、文本长度、分类、时间）
- 删前重映射 `claims.deal_id` / `saved_deals.deal_id`（ON DELETE CASCADE）
- 清理后建 `UNIQUE (offer_fingerprint) WHERE status='active'`

### 价格

`normalize_offer_prices`：解析 → 文本回填 → discount>regular 交换 → 派生 amount/percent。  
单 `$N` 且两边皆空 → 只填 `regular_price`。

### 字段

- `unit_type`: units→unit
- `service_area`: lower
- bool: 统一 Python bool
- `service_category`: resolve_service_category
- 去掉对已删列 `cancellation_policy` 等的写入

## 运维顺序

1. 部署写入代码 + 单测
2. `m003` SQL：确保 `offer_fingerprint` 列存在
3. dry-run：dedupe → price backfill → field normalize
4. 正式：dedupe → 唯一索引 → price → fields → category
5. `audit_promo_offer_master` 对比指标
