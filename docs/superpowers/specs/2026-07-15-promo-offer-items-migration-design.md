# Promo Offer Items 建表与迁移设计

## 目标

Offer 拆成两层：

- `promo_offer_master`：整单交易语义与价格
- `promo_offer_items`：服务组成（含单服务 Offer）

`service_id` 只保留在 `promo_offer_items`。

## Master 新增字段

| 字段 | 取值 | 含义 |
| --- | --- | --- |
| `offer_type` | `single` / `package` | 单服务 vs 套餐；单条 item 也可能是 package（如 20 units Botox package） |
| `price_model` | `total` / `per_unit` / `from` | 整包价 / 单位价 / 起步价 |

存量回填：`offer_type='single'`，`price_model='total'`。

## Items

| 字段 | 含义 |
| --- | --- |
| `offer_id` | 归属 Offer |
| `service_id` | 可选，FK → `clinic_services` |
| `item_name` | 显示名，通常来自目录服务 |
| `quantity` | 只有页面明确写数量时才填；单位价 Offer 保持 NULL |
| `unit_type` / `service_area` | 明细计量单位与部位 |

## 迁移步骤

1. 给 master 加 `offer_type`、`price_model` 与 CHECK
2. 建 `promo_offer_items`
3. 用 master.`service_id` JOIN `clinic_services` 回填一条 item（`quantity` 为 NULL）
4. 删除 master.`service_id`（含 `fk_offer_service`）
5. 校验每个 Offer 至少一条 item

## 写入约定

- 单服务 `$10/unit`：`offer_type=single`，`price_model=per_unit`，1 条 item，`quantity=NULL`
- 套餐 `10 Botox + 5 Filler = $x`：`offer_type=package`，`price_model=total`，2 条 item
- 查询按服务筛：`EXISTS` / `JOIN promo_offer_items`
