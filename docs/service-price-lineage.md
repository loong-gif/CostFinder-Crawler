# 服务目录价格溯源与防污染

> 2026-07-23 落地。守卫模块：`utils/service_price_guard.py`  
> 修复入口：`scripts/apply_extraction_repairs.py --batch service_price_lineage`

## 背景

`clinic_services.regular_price` 应为**常规目录单位价**。此前存在三类污染：

1. 博客/促销 URL 写入服务目录
2. 套餐总价未归一化为单价（如 `$245/20U`）
3. 子疗程误映射（如 Lip Flip `$60` 覆盖 Botox `$12/unit`）

## 守卫规则

| 规则 | 行为 |
|------|------|
| `url_path_score < 0` | 拒绝写入（blog/promotion/specials 等） |
| 市场均价/区间描述 | 拒绝（`typically ranges` 等） |
| 套餐总价 + 明示 N units | 归一化为 `regular_price / N`，`unit_type=unit` |
| `service_name=Botox` + raw 含 lip flip | 拒绝（独立疗程，非单位价目录） |
| 来源 URL 评分 | 仅在新来源 ≥ 已有来源时覆盖 `source_url` |

写入路径：`upsert_extracted_service`、`validate_service`、`apply_fields`（拒绝 ineligible 来源价）、`offer_to_clinic_fields`（跳过 promo 来源 offer 回填）。

## 已修复数据（Supabase）

| service_id | 诊所 | 修复内容 |
|---:|---|---|
| 28 | QUIKTOX | `$245 area` → **`$12.25/unit`**（245÷20U） |
| 33 | Glow Up | 清空博客来源价；Offer 31/32、promotion 13 置 `is_active=false` |
| 34 | LA Queen | 目录 **`$9/unit`**（450÷50U 常规单价）；Offer 35 保留总价 450/350、qty 50、优惠价 $7/unit |
| 23 | Alchemy Face Bar | Lip Flip `$60` 误映射 → **Botox/Dysport/Xeomin `$12/unit`**（[cosmetic-injectables](https://alchemyfacebar.com/pages/cosmetic-injectables)） |

Schema：`m009_clinic_services_regular_price_nullable` — `regular_price` 可空，便于清空污染价后保留服务身份。

## 运行

```bash
# 审计
python scripts/audit_extraction_quality.py

# 修复计划（默认 dry-run）
python scripts/apply_extraction_repairs.py --batch service_price_lineage
python scripts/apply_extraction_repairs.py --batch service_price_lineage --apply
```

## 测试

```bash
pytest tests/test_service_price_guard.py tests/test_recent_raw_extraction.py tests/test_clinic_services_search.py
```
