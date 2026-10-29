# 51 条 clinic_services.regular_price dry-run

**状态：待确认。** 本报告及 JSON ledger 只读生成；未执行任何数据库写入。

## 本批范围

- Supabase 项目：`costfinder`（`kdlpkjzcnbkjcvwsvlwn`）
- 固定 baseline：217 条原有非空 regular_price
- 纳入：31 条 `FAIL_MISMATCH` + 20 条 `FAIL_UNSUPPORTED`
- 排除：82 条 `PASS_DIRECT`、5 条 `PASS_DERIVED`、79 条 `UNVERIFIABLE`
- 本次核对到 51/51 service_id 存在，当前 regular_price 与 QA 清单完全一致

## 候选动作汇总

| 动作 | 数量 | dry-run 结果 |
|---|---:|---|
| `CLEAR_PRICE` | 47 | 候选 `regular_price = NULL`；其中需按后续分类再修 unit/service/variant/source 的行标出 follow-up |
| `UPDATE_PRICE` | 2 | 758 → 13、2376 → 5；两条均因现有关联 Offer 折扣价高于候选正价而阻塞 |
| `FIX_AREA_MAPPING` | 2 | 2846 → Bikini、2847 → Underarm；金额 395 / 220 保留 |
| 关联 Offer | 39 services | 104 item links、102 distinct offers |

## 51 条逐行候选

| Batch | ID | QA | Service | 当前值 | dry-run 候选 | 关联 Offer | 判定依据 |
|---|---:|---|---|---:|---|---:|---|
| A | 14 | FAIL_MISMATCH | Botox | 12 /unit | CLEAR_PRICE → NULL | 3 offers / 3 items | 当前来源已不支持 $12/unit，替代的当前正常价尚无充分页面证据。 |
| C | 35 | FAIL_MISMATCH | Dermal Filler | 499 /syringe | CLEAR_PRICE → NULL | 3 offers / 3 items | 来源是具体唇部/variant filler 定价，不能作为 generic filler 的统一 syringe 正常价。 |
| A | 236 | FAIL_MISMATCH | Botox | 15 /unit | CLEAR_PRICE → NULL | 4 offers / 4 items | 当前产品价格结构明显低于 $15/unit；具体可复核的非会员正常价未确认。 |
| A | 260 | FAIL_MISMATCH | Botox | 10 /unit | CLEAR_PRICE → NULL | 3 offers / 3 items | 现有 $10 不受当前来源支持；$11 为会员价，虽然 QA 提到 $14 普通价，当前源页尚未提供可复核的 $14 正常价。 |
| A | 269 | FAIL_UNSUPPORTED | Dermal Filler | 879 /syringe | CLEAR_PRICE → NULL | 5 offers / 5 items | 文章没有 $879 normal-price 证据。 |
| A | 292 | FAIL_UNSUPPORTED | Botox | 12 /unit | CLEAR_PRICE → NULL | 5 offers / 5 items | 当前 service page 没有 $12 normal-price 证据；其他会员内容不是正常价证明。 |
| C | 297 | FAIL_MISMATCH | Dermal Filler | 800 /syringe | CLEAR_PRICE → NULL | 1 offers / 1 items | 来源给出 filler $600–$800/syringe 范围，不支持 generic filler 固定 $800。 |
| A | 322 | FAIL_UNSUPPORTED | Botox | 13 /unit | CLEAR_PRICE → NULL | 1 offers / 1 items | 页面只证明 $12/unit membership benefit，不支持 $13 normal price。 |
| A | 359 | FAIL_UNSUPPORTED | Dermal Filler | 550 /syringe | CLEAR_PRICE → NULL | 2 offers / 2 items | exact service page 没有 normal-price 证据。 |
| C | 686 | FAIL_UNSUPPORTED | Botox | 8 /unit | CLEAR_PRICE → NULL | 1 offers / 1 items | 首页只写 starting $8，未说明 $8/unit。 |
| C | 687 | FAIL_MISMATCH | Dermal Filler | 450 /syringe | CLEAR_PRICE → NULL | 1 offers / 1 items | $450 对应 Under Eye Filler，当前行却是无区域/variant 的 generic Dermal Filler。 |
| B | 758 | FAIL_MISMATCH | Botox | 18 /unit | UPDATE_PRICE → 13; blocked: per-unit Offer discount exceeds candidate price | 10 offers / 10 items | Cienega 当前价格页列 Botox $13/unit；旧 $18 不符。两个 per-unit linked offers 的 discount_price 均为 $14，高于候选正价 $13，migration 会保留旧 Offer 正价；先解决冲突。 |
| C | 803 | FAIL_MISMATCH | Dermal Filler | 800 /syringe | CLEAR_PRICE → NULL | 1 offers / 1 items | $800 对应特定产品/variant；同源 generic filler 有多个档位。 |
| A | 1320 | FAIL_MISMATCH | Botox | 11 /unit | CLEAR_PRICE → NULL | 3 offers / 3 items | Gold Coast 当前页面未支持 $11/unit；$15 证据来自 2024 PDF，不能单独证明当前正常价。 |
| C | 1780 | FAIL_MISMATCH | Botox | 7 /unit | CLEAR_PRICE → NULL | 0 offers / 0 items | 来源只支持通用 Neurotoxin $7，不能收窄成 Botox $7/unit。 |
| A | 1808 | FAIL_UNSUPPORTED | Botox | 14 /unit | CLEAR_PRICE → NULL | 0 offers / 0 items | 来源介绍 Botox，但没有公开价格。 |
| A | 1898 | FAIL_MISMATCH | Botox | 9 /unit | CLEAR_PRICE → NULL | 1 offers / 1 items | 当前页面的 $12/$9 处于产品销售价/特殊价语境，不能证明旧 $9 是当前正常 Botox 单位价。 |
| C | 1939 | FAIL_MISMATCH | Dysport | 14 /unit | CLEAR_PRICE → NULL | 0 offers / 0 items | 来源写 $14 per 3 Dysport units；禁止在本 dry-run 擅自换算成 $4.67/unit。 |
| A | 1950 | FAIL_UNSUPPORTED | Botox | 12.5 /unit | CLEAR_PRICE → NULL | 3 offers / 3 items | monthly-specials 页面不能支撑 $12.50 normal Botox 单位价。 |
| A | 1951 | FAIL_UNSUPPORTED | Dysport | 4.75 /unit | CLEAR_PRICE → NULL | 4 offers / 4 items | monthly-specials 页面不能支撑 $4.75 normal Dysport 单位价。 |
| A | 1952 | FAIL_UNSUPPORTED | Xeomin | 11 /unit | CLEAR_PRICE → NULL | 3 offers / 3 items | monthly-specials 页面不能支撑 $11 normal Xeomin 单位价。 |
| C | 1953 | FAIL_MISMATCH | Dermal Filler | 2000 /syringe | CLEAR_PRICE → NULL | 3 offers / 3 items | $2,000 属于多 syringe/package 结构，不能当作单 syringe 正常价。 |
| A | 2253 | FAIL_MISMATCH | Dysport | 14 /unit | CLEAR_PRICE → NULL | 5 offers / 5 items | 当前来源的 Dysport 单位价不是 $14；本批没有可靠的替代正常价证据。 |
| A | 2306 | FAIL_MISMATCH | Xeomin | 8 /unit | CLEAR_PRICE → NULL | 1 offers / 1 items | SmoothMD 当前页面含 $8/$12 等销售价结构，不能证明旧 $8 是当前正常 Xeomin 单位价。 |
| C | 2328 | FAIL_UNSUPPORTED | Dermal Filler | 750 /syringe | CLEAR_PRICE → NULL | 0 offers / 0 items | chin-filler 页面没有足够证据支持 generic $750/syringe。 |
| C | 2333 | FAIL_MISMATCH | Juvéderm | 850 /syringe | CLEAR_PRICE → NULL | 3 offers / 3 items | $850 仅适用于 Volux；其他 Juvéderm variants 为不同价位。 |
| C | 2345 | FAIL_MISMATCH | Juvéderm | 239 /syringe | CLEAR_PRICE → NULL | 1 offers / 1 items | $239 是 half filler，不能作为完整 syringe 单位价，也不做 ×2 推算。 |
| A | 2347 | FAIL_UNSUPPORTED | Dysport | 11.5 /unit | CLEAR_PRICE → NULL | 5 offers / 5 items | $11.50 明确是 membership benefit price，不是 regular/non-member 价格。 |
| C | 2350 | FAIL_MISMATCH | Dysport | 13.98 /unit | CLEAR_PRICE → NULL | 3 offers / 3 items | $13.98 是换算/等效剂量数值，不是 Dysport 的正常单位价。 |
| B | 2376 | FAIL_MISMATCH | Dysport | 18 /unit | UPDATE_PRICE → 5; blocked: per-unit Offer discount exceeds candidate price | 2 offers / 2 items | Cienega Dysport 页面列 $5/unit；旧 $18 不符。per-unit linked offer 的 discount_price 为 $14，高于候选正价 $5，migration 会保留旧 Offer 正价；另一个无 delivered_units 的 from Offer 不具备确定派生输入。先解决冲突。 |
| C | 2498 | FAIL_UNSUPPORTED | Radiesse | 325 /syringe | CLEAR_PRICE → NULL | 1 offers / 1 items | 来源只支持 starting $325，没有明确 /syringe。 |
| A | 2502 | FAIL_UNSUPPORTED | Dermal Filler | 879 /syringe | CLEAR_PRICE → NULL | 0 offers / 0 items | 文章没有 $879 normal-price 证据。 |
| C | 2507 | FAIL_MISMATCH | Restylane | 700 /syringe | CLEAR_PRICE → NULL | 3 offers / 3 items | Restylane variants 为不同价格，不支持 generic Restylane 固定 $700/syringe。 |
| C | 2514 | FAIL_MISMATCH | Dysport | 15 /unit | CLEAR_PRICE → NULL | 0 offers / 0 items | Dysport 行链接到 dermal-fillers 来源页，来源与 service 不匹配。 |
| A | 2530 | FAIL_UNSUPPORTED | Dermal Filler | 599 /syringe | CLEAR_PRICE → NULL | 2 offers / 2 items | event/training special 不是 normal regular price。 |
| C | 2534 | FAIL_MISMATCH | Radiesse | 975 /syringe | CLEAR_PRICE → NULL | 0 offers / 0 items | $975 购买 1.8 syringes；按计划的保守口径不做 package 到单支的推算。 |
| A | 2544 | FAIL_UNSUPPORTED | Dermal Filler | 650 /syringe | CLEAR_PRICE → NULL | 4 offers / 4 items | exact Nubella filler 页面没有公开 normal price。 |
| C | 2638 | FAIL_UNSUPPORTED | Dermal Filler | 350  | CLEAR_PRICE → NULL | 0 offers / 0 items | Executive Esthetics 服务页列 filler 服务但没有价格；unit_type 也未明确。 |
| C | 2639 | FAIL_MISMATCH | Dermal Filler | 975 /syringe | CLEAR_PRICE → NULL | 0 offers / 0 items | Generic filler 行吸收了具体 package/biostimulator 定价，不是单支 generic filler 正常价。 |
| C | 2650 | FAIL_MISMATCH | Radiesse | 1500  | CLEAR_PRICE → NULL | 0 offers / 0 items | $1,500 对应 Radiesse Neck Wash；普通 Radiesse 标价 $850，但 unit_type 为空，需先确认服务/单位语义。 |
| C | 2656 | FAIL_UNSUPPORTED | Hydrafacial | 259 /session | CLEAR_PRICE → NULL | 7 offers / 7 items | Hydrafacial bundle 缺少明确的单次 normal price 结构。 |
| C | 2765 | FAIL_UNSUPPORTED | Dermal Filler | 900 /syringe | CLEAR_PRICE → NULL | 0 offers / 0 items | Radiesse treatments $900+ 不能证明 generic filler $900/syringe。 |
| A | 2801 | FAIL_MISMATCH | Morpheus8 | 1250  | CLEAR_PRICE → NULL | 1 offers / 1 items | 当前 Morpheus8 页面定价结构已变，未确认可替代的当前正常价。 |
| C | 2841 | FAIL_MISMATCH | Laser Hair Removal | 600 /session | CLEAR_PRICE → NULL | 2 offers / 2 items | 当前来源是 body-sculpting promo，不能支撑 Laser Hair Removal $600/session。 |
| C | 2846 | FAIL_MISMATCH | Laser Hair Removal | 395 /session | FIX_AREA_MAPPING → 395 (保留); area → Bikini | 1 offers / 1 items | $395 明确是 Bikini session regular price；补 service_area=Bikini，保留价格。 |
| C | 2847 | FAIL_MISMATCH | Laser Hair Removal | 220 /session | FIX_AREA_MAPPING → 220 (保留); area → Underarm | 1 offers / 1 items | $220 明确是 Underarm session regular price；补 service_area=Underarm，保留价格。 |
| A | 2854 | FAIL_UNSUPPORTED | Hydrafacial | 268 /session | CLEAR_PRICE → NULL | 1 offers / 1 items | exact source 当前没有价格证据。 |
| C | 2991 | FAIL_MISMATCH | Facial | 50 /session | CLEAR_PRICE → NULL | 0 offers / 0 items | $50 对应 add-on/dermaplaning，不是 generic Facial 正常价。 |
| A | 3024 | FAIL_UNSUPPORTED | Neurotoxin | 12 /unit | CLEAR_PRICE → NULL | 1 offers / 1 items | Belle Ame Bank 页面不能证明 $12 是 normal/non-member regular price。 |
| C | 3032 | FAIL_MISMATCH | Facial | 600 /session | CLEAR_PRICE → NULL | 2 offers / 2 items | $600 对应 Microneedling with PRF，不是 generic Facial。 |
| C | 3295 | FAIL_MISMATCH | Belotero | 359 /syringe | CLEAR_PRICE → NULL | 1 offers / 1 items | 来源支持 $359 treatment，但不支持 $359/syringe；在确认单位与正常价前清空旧的 syringe 解释。 |

### 按计划分批

| Batch | 候选范围 | 数量 | 当前状态 |
|---|---|---:|---|
| A | 仅清空无支持的 regular_price | 22 | 待确认；需先让 NULL 清理 migration live 并验证 |
| B | 有候选当前正常价的 regular_price 更新 | 2 | 758/2376 均因关联折扣价高于候选正价而阻塞 |
| C | service/variant/area/unit 映射或单位问题 | 27 | 按行处理；不做未经确认的单位推算 |


## Offer 下游影响

所有 linked Offer item 都列在下表。旧正价、折扣价、数量、active/package 状态与两侧更新时间来自只读 live 查询。

- **当前 live trigger 情景：** 清空服务价时函数因 `NEW.regular_price IS NULL` 直接返回；候选新价更新时，按当前函数规则投影结果，并保留 discount guard 的跳过情况。
- **尚未部署 migration 情景：** 下表基于本地待审 migration 的函数规则模拟；只在用户确认、migration 部署并验证后才成立。package 和多 item Offer 会被跳过；单 item 的旧正价若不等于旧服务派生值，会按该 migration 判为来源自有并保留。
- 该 migration 用“Offer 正价是否等于旧派生值”识别来源；显式来源价若恰好与派生值相同，也会被识别为派生值。表内条件结果遵循这一实现限制。
- 758 有两个 per-unit Offer 会因 $14 折扣高于候选 $13 而保留旧 $18；它另有 100-unit Offer 可推导为 $1,300，package Offers 不变。2376 的 per-unit Offer 会因 $14 高于候选 $5 而保留旧 $18；另一个 from Offer 缺少 delivered_units，按 migration 作为来源价/未知来源保留 $18。两条更新在逐 Offer 冲突解决前都不放行。

| Service ID | Offer / item | delivered_units | 当前 Offer regular | 当前 live trigger 结果 | migration 部署后条件预期 | discount_price | 状态 | 结构 | offer_updated_at | item_updated_at |
|---:|---|---:|---:|---|---|---:|---|---|---|---|
| 14 | 1373 / item 656 | — | 12.50 | 12.50 | 12.50 (old-derived 12.00 -> new-derived NULL) | 10.00 | inactive | non-package, 1 item(s) | 2026-09-20 15:21:33.867077+00 | 2026-09-15 06:29:27.674455+00 |
| 14 | 7808 / item 1868 | — | NULL | NULL | NULL | NULL | inactive | package, 2 item(s) | 2026-09-19 00:33:41.995466+00 | 2026-09-11 12:53:51.338587+00 |
| 14 | 10531 / item 5229 | — | NULL | NULL | NULL | NULL | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-19 00:31:32.31731+00 |
| 35 | 1503 / item 1006 | 1 | 499.00 | 499.00 | NULL (old-derived 499.00 -> new-derived NULL) | 399.00 | inactive | non-package, 1 item(s) | 2026-09-18 16:33:55.20007+00 | 2026-09-15 06:29:27.674455+00 |
| 35 | 8848 / item 3309 | — | 499.00 | 499.00 | NULL (old-derived 499.00 -> new-derived NULL) | 499.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-16 02:48:35.085427+00 |
| 35 | 8849 / item 3269 | — | NULL | NULL | NULL (old-derived 499.00 -> new-derived NULL) | 599.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-16 02:48:35.085427+00 |
| 236 | 6679 / item 770 | — | 15.00 | 15.00 | NULL (old-derived 15.00 -> new-derived NULL) | 11.50 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-15 06:29:27.674455+00 |
| 236 | 7770 / item 3428 | — | 15.00 | 15.00 | NULL (old-derived 15.00 -> new-derived NULL) | 10.50 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-16 02:48:35.085427+00 |
| 236 | 7820 / item 3432 | — | 15.00 | 15.00 | NULL (old-derived 15.00 -> new-derived NULL) | 11.50 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-16 02:48:35.085427+00 |
| 236 | 9791 / item 3430 | — | 15.00 | 15.00 | 15.00 | 11.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-17 04:47:18.049563+00 |
| 260 | 1403 / item 858 | — | NULL | NULL | NULL | 11.00 | inactive | package, 2 item(s) | 2026-09-19 00:33:41.995466+00 | 2026-09-11 14:04:29.013852+00 |
| 260 | 6692 / item 961 | — | NULL | NULL | NULL (old-derived 10.00 -> new-derived NULL) | 11.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-15 06:29:27.674455+00 |
| 260 | 6939 / item 5134 | — | NULL | NULL | NULL | NULL | inactive | non-package, 1 item(s) | 2026-09-21 05:05:42.825304+00 | 2026-09-18 16:18:13.549945+00 |
| 269 | 1705 / item 700 | 1 | 879.00 | 879.00 | NULL (old-derived 879.00 -> new-derived NULL) | NULL | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-15 06:29:27.674455+00 |
| 269 | 3921 / item 4141 | 2 | 3216.00 | 3216.00 | 3216.00 | 2300.00 | inactive | package, 2 item(s) | 2026-09-20 14:43:01.501655+00 | 2026-09-15 08:07:19.664943+00 |
| 269 | 8648 / item 2513 | 3 | 2637.00 | 2637.00 | 2637.00 | 1758.00 | active | package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-19 00:54:38.570329+00 |
| 269 | 8649 / item 2515 | 3 | 2637.00 | 2637.00 | 2637.00 | 1582.00 | active | package, 1 item(s) | 2026-09-22 07:25:58.296175+00 | 2026-09-19 00:54:38.570329+00 |
| 269 | 8650 / item 2514 | 3 | 2637.00 | 2637.00 | 2637.00 | 1670.00 | active | package, 1 item(s) | 2026-09-22 07:25:58.296175+00 | 2026-09-19 00:54:38.570329+00 |
| 292 | 7804 / item 63 | — | NULL | NULL | NULL | 213.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-15 06:29:27.674455+00 |
| 292 | 8733 / item 2624 | — | NULL | NULL | NULL | NULL | inactive | package, 6 item(s) | 2026-09-19 00:33:41.995466+00 | 2026-09-11 12:53:51.338587+00 |
| 292 | 8734 / item 2630 | — | 12.00 | 12.00 | NULL (old-derived 12.00 -> new-derived NULL) | 10.20 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-15 06:29:27.674455+00 |
| 292 | 9797 / item 4538 | 80 | NULL | NULL | NULL | NULL | active | package, 3 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-17 11:42:11.692366+00 |
| 292 | 10603 / item 5301 | — | NULL | NULL | NULL | NULL | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-19 00:31:32.31731+00 |
| 297 | 1543 / item 1014 | — | 800.00 | 800.00 | NULL (old-derived 800.00 -> new-derived NULL) | 600.00 | inactive | non-package, 1 item(s) | 2026-09-21 05:05:42.825304+00 | 2026-09-15 06:29:27.674455+00 |
| 322 | 1392 / item 667 | — | 13.00 | 13.00 | NULL (old-derived 13.00 -> new-derived NULL) | 11.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-15 06:29:27.674455+00 |
| 359 | 7758 / item 2860 | 1 | NULL | NULL | NULL (old-derived 550.00 -> new-derived NULL) | NULL | active | non-package, 1 item(s) | 2026-09-30 14:47:25.527617+00 | 2026-09-30 14:47:25.527617+00 |
| 359 | 10056 / item 4295 | — | NULL | NULL | NULL | NULL | active | non-package, 1 item(s) | 2026-09-30 14:47:25.527617+00 | 2026-09-30 14:47:25.527617+00 |
| 686 | 1347 / item 654 | 1 | 8.00 | 8.00 | NULL (old-derived 8.00 -> new-derived NULL) | 8.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:31:28.975722+00 | 2026-09-15 06:29:27.674455+00 |
| 687 | 1509 / item 1007 | 1 | 450.00 | 450.00 | NULL (old-derived 450.00 -> new-derived NULL) | 450.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:31:28.975722+00 | 2026-09-15 06:29:27.674455+00 |
| 758 | 4307 / item 750 | 100 | NULL | 1300.00 | 1300.00 (old-derived 1800.00 -> new-derived 1300.00) | 900.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-16 06:46:44.994714+00 |
| 758 | 7598 / item 84 | 100 | 1800.00 | 1800.00 | 1800.00 | 1400.00 | inactive | package, 1 item(s) | 2026-09-20 15:31:15.712626+00 | 2026-09-19 00:54:38.570329+00 |
| 758 | 7599 / item 202 | 10 | 180.00 | 180.00 | 180.00 | 150.00 | inactive | package, 1 item(s) | 2026-09-20 15:31:15.712626+00 | 2026-09-19 00:54:38.570329+00 |
| 758 | 7617 / item 618 | 30 | 540.00 | 540.00 | 540.00 | 450.00 | inactive | package, 1 item(s) | 2026-09-20 15:31:15.712626+00 | 2026-09-19 00:54:38.570329+00 |
| 758 | 7621 / item 129 | 24 | 432.00 | 432.00 | 432.00 | 360.00 | inactive | package, 1 item(s) | 2026-09-20 15:31:15.712626+00 | 2026-09-19 00:54:38.570329+00 |
| 758 | 7622 / item 651 | 20 | 360.00 | 360.00 | 360.00 | 300.00 | inactive | package, 1 item(s) | 2026-09-20 15:31:15.712626+00 | 2026-09-19 00:54:38.570329+00 |
| 758 | 7630 / item 403 | 50 | 900.00 | 900.00 | 900.00 | 550.00 | inactive | package, 1 item(s) | 2026-09-20 15:31:15.712626+00 | 2026-09-19 00:54:38.570329+00 |
| 758 | 7634 / item 644 | 64 | 1152.00 | 1152.00 | 1152.00 | 704.00 | inactive | package, 1 item(s) | 2026-09-20 15:31:15.712626+00 | 2026-09-19 00:54:38.570329+00 |
| 758 | 7781 / item 4221 | — | 18.00 | 18.00 | 18.00 | 14.00 | inactive | non-package, 1 item(s) | 2026-09-20 15:31:15.712626+00 | 2026-09-17 04:47:18.049563+00 |
| 758 | 7793 / item 4223 | — | 18.00 | 18.00 | 18.00 (old-derived 18.00 -> new-derived 13.00) | 14.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-16 02:48:35.085427+00 |
| 803 | 4949 / item 905 | 1 | 800.00 | 800.00 | NULL (old-derived 800.00 -> new-derived NULL) | 800.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-15 06:29:27.674455+00 |
| 1320 | 6661 / item 957 | — | NULL | NULL | NULL | 399.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-15 06:29:27.674455+00 |
| 1320 | 6668 / item 958 | — | 11.00 | 11.00 | NULL (old-derived 11.00 -> new-derived NULL) | 10.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-15 06:29:27.674455+00 |
| 1320 | 6670 / item 959 | — | NULL | NULL | NULL | 1500.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-15 06:29:27.674455+00 |
| 1898 | 7125 / item 778 | — | 9.00 | 9.00 | NULL (old-derived 9.00 -> new-derived NULL) | 9.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-15 06:29:27.674455+00 |
| 1950 | 8697 / item 2587 | — | NULL | NULL | NULL | NULL | inactive | package, 3 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-11 12:53:51.338587+00 |
| 1950 | 8989 / item 3303 | — | 12.50 | 12.50 | NULL (old-derived 12.50 -> new-derived NULL) | 10.50 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-16 02:48:35.085427+00 |
| 1950 | 10505 / item 5192 | 20 | NULL | NULL | NULL | 0.00 | active | package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-18 16:14:02.031596+00 |
| 1951 | 8695 / item 2581 | — | 4.75 | 4.75 | NULL (old-derived 4.75 -> new-derived NULL) | 3.75 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-15 06:29:27.674455+00 |
| 1951 | 8697 / item 2588 | — | NULL | NULL | NULL | NULL | inactive | package, 3 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-11 12:53:51.338587+00 |
| 1951 | 8990 / item 3312 | — | 4.75 | 4.75 | NULL (old-derived 4.75 -> new-derived NULL) | 3.75 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-16 02:48:35.085427+00 |
| 1951 | 10506 / item 5193 | 40 | NULL | NULL | NULL | 0.00 | active | package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-18 16:14:02.031596+00 |
| 1952 | 8697 / item 2589 | — | NULL | NULL | NULL | NULL | inactive | package, 3 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-11 12:53:51.338587+00 |
| 1952 | 8698 / item 2582 | — | 11.00 | 11.00 | NULL (old-derived 11.00 -> new-derived NULL) | 9.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-15 06:29:27.674455+00 |
| 1952 | 8991 / item 3277 | — | 11.00 | 11.00 | NULL (old-derived 11.00 -> new-derived NULL) | 9.00 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-16 02:48:35.085427+00 |
| 1953 | 7882 / item 994 | — | NULL | NULL | NULL | NULL | inactive | non-package, 1 item(s) | 2026-09-21 13:47:17.010135+00 | 2026-09-19 00:45:13.021806+00 |
| 1953 | 8273 / item 2190 | 1 | 2000.00 | 2000.00 | NULL (old-derived 2000.00 -> new-derived NULL) | 1800.00 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-15 06:29:27.674455+00 |
| 1953 | 8696 / item 2583 | — | NULL | NULL | NULL | NULL | inactive | package, 4 item(s) | 2026-09-18 13:54:11.690458+00 | 2026-09-16 02:48:35.085427+00 |
| 2253 | 7887 / item 1717 | — | 14.00 | 14.00 | 14.00 | 10.00 | inactive | package, 2 item(s) | 2026-09-19 00:33:41.995466+00 | 2026-09-11 14:04:29.013852+00 |
| 2253 | 7898 / item 1724 | — | 14.00 | 14.00 | 14.00 | 11.00 | inactive | package, 2 item(s) | 2026-09-19 00:33:41.995466+00 | 2026-09-11 14:04:29.013852+00 |
| 2253 | 10123 / item 4362 | — | NULL | NULL | NULL | NULL | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-19 00:45:13.021806+00 |
| 2253 | 10550 / item 5248 | — | 14.00 | 14.00 | NULL (old-derived 14.00 -> new-derived NULL) | 10.00 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-19 00:31:32.31731+00 |
| 2253 | 10552 / item 5250 | — | 14.00 | 14.00 | NULL (old-derived 14.00 -> new-derived NULL) | 11.00 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-19 00:31:32.31731+00 |
| 2306 | 1354 / item 1921 | — | 8.00 | 8.00 | NULL (old-derived 8.00 -> new-derived NULL) | 8.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-15 06:29:27.674455+00 |
| 2333 | 1580 / item 1956 | — | 850.00 | 850.00 | NULL (old-derived 850.00 -> new-derived NULL) | 800.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:31:28.975722+00 | 2026-09-15 06:29:27.674455+00 |
| 2333 | 1592 / item 1957 | — | 850.00 | 850.00 | NULL (old-derived 850.00 -> new-derived NULL) | 850.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:31:28.975722+00 | 2026-09-15 06:29:27.674455+00 |
| 2333 | 2158 / item 1974 | — | 850.00 | 850.00 | NULL (old-derived 850.00 -> new-derived NULL) | 600.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:31:28.975722+00 | 2026-09-15 06:29:27.674455+00 |
| 2345 | 1468 / item 1950 | 1 | 239.00 | 239.00 | NULL (old-derived 239.00 -> new-derived NULL) | 239.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:31:28.975722+00 | 2026-09-15 06:29:27.674455+00 |
| 2347 | 6680 / item 1992 | — | NULL | NULL | NULL (old-derived 11.50 -> new-derived NULL) | 11.50 | active | non-package, 1 item(s) | 2026-09-22 14:57:16.649975+00 | 2026-09-15 06:29:27.674455+00 |
| 2347 | 7767 / item 3429 | — | 11.50 | 11.50 | NULL (old-derived 11.50 -> new-derived NULL) | 10.50 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-16 02:48:35.085427+00 |
| 2347 | 7800 / item 2263 | — | 11.50 | 11.50 | NULL (old-derived 11.50 -> new-derived NULL) | 10.00 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-16 02:48:35.085427+00 |
| 2347 | 7813 / item 3433 | — | 11.50 | 11.50 | NULL (old-derived 11.50 -> new-derived NULL) | 11.50 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-16 02:48:35.085427+00 |
| 2347 | 9792 / item 3431 | — | 11.50 | 11.50 | 11.50 | 10.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-17 04:47:18.049563+00 |
| 2350 | 1407 / item 1938 | — | NULL | NULL | NULL | NULL | inactive | package, 2 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-11 12:53:51.338587+00 |
| 2350 | 10371 / item 4829 | — | NULL | NULL | NULL (old-derived 13.98 -> new-derived NULL) | 4.10 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-18 02:46:48.063758+00 |
| 2350 | 10372 / item 4830 | — | NULL | NULL | NULL (old-derived 13.98 -> new-derived NULL) | 3.72 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-18 02:46:50.732083+00 |
| 2376 | 10078 / item 4317 | — | 18.00 | 18.00 | 18.00 | 14.00 | inactive | non-package, 1 item(s) | 2026-09-20 15:31:15.712626+00 | 2026-09-17 06:57:57.753582+00 |
| 2376 | 10079 / item 4318 | — | 18.00 | 18.00 | 18.00 (old-derived 18.00 -> new-derived 5.00) | 14.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-17 06:57:57.753582+00 |
| 2498 | 1486 / item 2198 | 1 | 325.00 | 325.00 | NULL (old-derived 325.00 -> new-derived NULL) | 325.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:31:28.975722+00 | 2026-09-15 06:29:27.674455+00 |
| 2507 | 1675 / item 2219 | — | 700.00 | 700.00 | NULL (old-derived 700.00 -> new-derived NULL) | 650.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:31:28.975722+00 | 2026-09-15 06:29:27.674455+00 |
| 2507 | 2155 / item 2241 | — | 700.00 | 700.00 | NULL (old-derived 700.00 -> new-derived NULL) | 600.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:31:28.975722+00 | 2026-09-15 06:29:27.674455+00 |
| 2507 | 2156 / item 2243 | — | NULL | NULL | NULL (old-derived 700.00 -> new-derived NULL) | 800.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:31:28.975722+00 | 2026-09-16 01:48:12.854893+00 |
| 2530 | 8093 / item 2397 | 1 | 599.00 | 599.00 | NULL (old-derived 599.00 -> new-derived NULL) | 425.00 | inactive | non-package, 1 item(s) | 2026-09-17 13:47:06.962598+00 | 2026-09-17 13:47:06.962598+00 |
| 2530 | 8249 / item 2405 | 1 | 599.00 | 599.00 | NULL (old-derived 599.00 -> new-derived NULL) | 425.00 | inactive | non-package, 1 item(s) | 2026-09-17 13:47:08.646865+00 | 2026-09-17 13:47:08.646865+00 |
| 2544 | 7117 / item 2373 | — | NULL | NULL | NULL | NULL | inactive | non-package, 1 item(s) | 2026-09-22 15:35:01.715258+00 | 2026-09-22 15:35:01.715258+00 |
| 2544 | 7253 / item 986 | 1 | NULL | NULL | NULL (old-derived 650.00 -> new-derived NULL) | NULL | inactive | non-package, 1 item(s) | 2026-09-17 10:28:09.89395+00 | 2026-09-17 02:13:04.502806+00 |
| 2544 | 10669 / item 5382 | 1 | NULL | NULL | NULL (old-derived 650.00 -> new-derived NULL) | 299.00 | inactive | non-package, 1 item(s) | 2026-09-19 12:42:50.139461+00 | 2026-09-19 02:31:02.9229+00 |
| 2544 | 10671 / item 5384 | 1 | NULL | NULL | NULL (old-derived 650.00 -> new-derived NULL) | 1200.00 | inactive | non-package, 1 item(s) | 2026-09-19 12:42:50.139461+00 | 2026-09-19 02:31:02.9229+00 |
| 2656 | 8595 / item 2468 | 1 | 259.00 | 259.00 | NULL (old-derived 259.00 -> new-derived NULL) | NULL | inactive | non-package, 1 item(s) | 2026-09-17 10:57:37.243728+00 | 2026-09-15 06:29:27.674455+00 |
| 2656 | 8917 / item 3391 | 1 | 259.00 | 259.00 | NULL (old-derived 259.00 -> new-derived NULL) | 209.00 | inactive | non-package, 1 item(s) | 2026-09-17 12:27:31.370999+00 | 2026-09-15 06:29:27.674455+00 |
| 2656 | 10178 / item 4434 | 1 | NULL | NULL | NULL | NULL | inactive | package, 2 item(s) | 2026-09-20 14:30:33.03372+00 | 2026-09-17 10:57:37.243728+00 |
| 2656 | 10179 / item 4436 | 1 | NULL | NULL | NULL | NULL | inactive | package, 2 item(s) | 2026-09-20 14:30:33.03372+00 | 2026-09-17 10:57:37.243728+00 |
| 2656 | 10275 / item 4545 | 1 | 75.00 | 75.00 | 75.00 | 0.00 | active | package, 2 item(s) | 2026-09-19 12:25:07.84674+00 | 2026-09-17 13:24:31.677487+00 |
| 2656 | 10288 / item 4710 | 1 | NULL | NULL | NULL (old-derived 259.00 -> new-derived NULL) | 0.00 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-17 14:12:35.487107+00 |
| 2656 | 10290 / item 4712 | 1 | NULL | NULL | NULL (old-derived 259.00 -> new-derived NULL) | 0.00 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-17 14:12:38.578219+00 |
| 2801 | 9223 / item 3358 | 1 | 1250.00 | 1250.00 | NULL (old-derived 1250.00 -> new-derived NULL) | 495.00 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-17 13:47:29.594911+00 |
| 2841 | 9813 / item 3463 | 1 | 300.00 | 300.00 | 300.00 (old-derived 600.00 -> new-derived NULL) | 89.00 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-15 06:29:27.674455+00 |
| 2841 | 9814 / item 3464 | 1 | 600.00 | 600.00 | NULL (old-derived 600.00 -> new-derived NULL) | 199.00 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-15 06:29:27.674455+00 |
| 2846 | 9817 / item 3467 | 1 | 395.00 | 395.00 | 395.00 | 49.00 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-17 13:47:34.596546+00 |
| 2847 | 9816 / item 3466 | 1 | 220.00 | 220.00 | 220.00 | 19.00 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-17 13:47:34.026987+00 |
| 2854 | 9860 / item 3510 | 1 | 268.00 | 268.00 | NULL (old-derived 268.00 -> new-derived NULL) | 168.00 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-15 06:29:27.674455+00 |
| 3024 | 8813 / item 3701 | — | 12.00 | 12.00 | NULL (old-derived 12.00 -> new-derived NULL) | 10.20 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-15 06:29:27.674455+00 |
| 3032 | 9341 / item 3847 | — | 600.00 | 600.00 | 600.00 | 500.00 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-17 13:47:46.607454+00 |
| 3032 | 9349 / item 3848 | — | 600.00 | 600.00 | 600.00 | 450.00 | active | non-package, 1 item(s) | 2026-09-19 01:02:52.441479+00 | 2026-09-17 13:47:47.225018+00 |
| 3295 | 1496 / item 2199 | 1 | 359.00 | 359.00 | NULL (old-derived 359.00 -> new-derived NULL) | 359.00 | inactive | non-package, 1 item(s) | 2026-09-17 10:31:28.975722+00 | 2026-09-17 02:13:04.502806+00 |


## 写入 gate 与结论

1. 本文件是审批前 dry-run；数据库完全未改。
   Live migration 清单于 2026-10-01 复查：最新版本为 `20260930144725 repair_split_blankets_and_unstated_prices`；候选 `20261001022038 sync_offer_regular_price_from_service_inputs` 不在 live 清单中。
2. 对 `CLEAR_PRICE`，现有 trigger 会留下 Offer 正价不变；本地 migration 的条件投影见逐 item 明细。必须先部署并验证 migration，再考虑 service 写入。
3. 758、2376 各至少有一个 per-unit linked Offer 的折扣价高于候选服务正价；逐 Offer 条件结果与其余正常/套餐 item 分开列出。先解决冲突与来源语义，再确认是否更新。
4. 2846、2847 当前同一 business 下只有这两条 generic LHR 行，二者 area 均为空；Bikini/Underarm 补值不会与现有已标 area 行冲突。二者 Offer regular/discount 分别为 395/49 与 220/19，area-only 修改不会触发 regular-price sync。
5. 具体 variant、unit、service mapping 的后续复核见每行 `follow_up_reviews`。不做 `$14 ÷ 3`、`$975 ÷ 1.8`、half-filler ×2 等单位推算。
6. 79 条 `UNVERIFIABLE` 完全没有进入候选。

完整、逐 Offer 的可机读 diff 见 [20261001_clinic_service_regular_price_51_dryrun.json](20261001_clinic_service_regular_price_51_dryrun.json)。基线仍保留在 [20261001_clinic_service_regular_price_217_snapshot.json](20261001_clinic_service_regular_price_217_snapshot.json)。

## 逐行 JSON Ledger 字段

每行包含 current/proposed service fields、baseline updated_at、QA verdict、根因 follow-up、证据判断和所有 Offer item impact 字段（offer_id、delivered_units、旧正价、折扣价、is_active、is_package、item_count、两侧 updated_at），并分列 live trigger 结果与待部署 migration 的条件预期。
