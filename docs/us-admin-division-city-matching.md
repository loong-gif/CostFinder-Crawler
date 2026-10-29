# CostFinder：美国行政区划与城市展示名匹配规范（v2）

| 字段 | 值 |
| --- | --- |
| 状态 | 试点定稿（2026-10-27 金标 38 条：一致率 / 正确率 100%） |
| 适用范围 | 商户发现（`gmaps_discovery`）、目录入库 `master_business_info.city`、站点 `/[state]/[city]` |
| 相关材料 | 试点报告 `us_admin_city_pilot_20261027_v2.md`；原始方案草稿 `us_admin_division_city_plan_20261027.md` |
| 原则 | **坐标与面域定归属；多名字并存；展示名有规则；搜索用别名扩展，不把单个 city 字符串当成行政单位** |

---

## 1. 为什么需要这份规范

美国地名在产品里至少混用五层，**互不等价**：

| 层 | 含义 | CostFinder 相关例子 |
| --- | --- | --- |
| Incorporated place | 有市政府的 city / town | Irvine CA、Burbank CA、Lake Forest CA |
| Census CDP | 人口普查划定的统计地名，常无市政 | Ashburn VA、历史上的 Foothill Ranch |
| Unincorporated / neighborhood | 县辖社区，或大城市内社区名 | Corona Del Mar；Woodland Hills、Canoga Park（属 Los Angeles） |
| Postal city（USPS） | ZIP 投递名，可与政治边界不一致 | 同一坐标可能 postal=Ashburn，Census 却落在 Goose Creek Village CDP |
| County | 县；真无建制区域的法律归属常在县 | Loudoun County、Orange County |

Google Maps 的 `locality` / `sublocality` 与上表不对齐。  
若只搜 `"med spa in {city}"` 或把 scraper 返回的 city 原样入库，会出现：

- 把 Foothill Ranch 诊所改成 Lake Forest（市界吞并后 Census 面域变了，但市场仍用旧名）；
- 把 Ashburn 诊所改成 Goose Creek Village（微 CDP 切得过碎）；
- Corona Del Mar 被 geocode 错配到 Del Mar（圣地亚哥）；
- Newport Beach / Los Angeles 列表页看不到社区名诊所（站点按字符串 `ilike(city)`）。

本规范规定：**如何定 `display_city`（写入/展示用城市名）与 `search_aliases`（发现与后续别名查询用）**。

---

## 2. 字段契约（逻辑模型）

每条商户在匹配后应具备（可先 sidecar / 再 migration）：

| 字段 | 说明 |
| --- | --- |
| `google_place_id` | 商户身份主键（已有；城市标签可变，place_id 不变） |
| `lat` / `lng` | 点归属输入 |
| `postal_city` | ZIP → USPS preferred city |
| `census_place_name` + `geoid` + `place_class` | Census 面域：`incorporated` / `cdp` / `none` / `neighborhood_of:…` / `marketing_cdp_of:…` / `cdp_market` |
| `county_name` + `county_geoid` | 县 |
| **`display_city`** | **站点与 `cities.json` 使用的唯一展示名** |
| `search_aliases[]` | 父城、微 CDP、postal、县等别名 |
| `admin_match_confidence` | `high` / `medium` / `low` |
| `display_city_reason` | 规则标签，便于审计 |

**禁止**把 scraper 的 `complete_address.city` 直接当成 `display_city` 而不跑第 4 节规则。

---

## 3. 权威数据源（优先级）

| 优先级 | 源 | 用途 |
| --- | --- | --- |
| P0 | Google `place_id` + address components + 坐标 | 身份与初始组件 |
| P0 | Census TIGER / Gazetteer（Incorporated Places、CDPs、Counties） | 点-面归属、法定/统计名、GEOID |
| P0 | ZIP → city（USPS / HUD-USPS） | `postal_city` |
| P1 | Census Geocoder | 无坐标时补点 |
| P1 | GeoNames / GNIS | 未建制社区别名 |
| P2 | Google reverse geocode | 冲突对照，**不作唯一权威** |

**冲突裁决：** 面域以 TIGER 为准；展示名以第 4 节规则为准；postal 名一般不覆盖法定市名（规则 A/B/社区例外除外）。

---

## 4. `display_city` 规则（v2，按顺序命中）

### 规则 0 — 州 / 都会区校验（先于展示名）

若 geocode 命中结果与输入 ZIP 的都会区/州明显冲突（例如输入 `92625` Corona Del Mar，却匹配到 `92014` Del Mar），**拒绝该 Census 命中**，暂时保留运营/Google 城市名，并挂合理父城别名，`confidence=medium`。

**Med spa 示例**

| 诊所 | 地址 | 若无校验会发生什么 | v2 结果 |
| --- | --- | --- | --- |
| Robert Kessler MD a Medical Corp（business_id 3611） | 2121 East Coast Hwy Ste 200, Corona Del Mar, CA 92625 | Census 错配到 **Del Mar city**（圣地亚哥） | `display_city=Corona Del Mar`；aliases 含 `Newport Beach`（及审计用的 Del Mar）；`bad_match=zip_metro_mismatch` |

---

### 规则 1 — 真在法定市内 → 默认用市名

点落在 **Incorporated place** 面内，且不适用规则 A（吞并运营名）或规则 4（大城市社区）时：

- `display_city` = 该 place 法定名（规范化，去掉 “city” 后缀）
- `place_class=incorporated`
- aliases 通常为空（或仅含县，按产品需要）

**Med spa 示例**

| 诊所 | 地址 | Census | display_city | aliases |
| --- | --- | --- | --- | --- |
| First OC Dermatology（3133） | 17305 Von Karman Ave #107, Irvine, CA 92614 | Irvine city | **Irvine** | — |
| Fix Me Up Med Spa（3045） | 2407 W Burbank Blvd, Burbank, CA 91506 | Burbank city | **Burbank** | — |
| AquaVita Wellness & Medspa（3152） | 23321 El Toro Rd Ste G, Lake Forest, CA 92630 | Lake Forest city | **Lake Forest** | — |

说明：Lake Forest **作为法定市本身**走规则 1；与「Foothill Ranch 落在 Lake Forest 市界」的规则 A 不同。

---

### 例外 A — 吞并 / 运营市场名优先

当满足：

1. 点的 Census **incorporated** 已是更大市（如 Lake Forest）；且  
2. 现有目录 city / Google 运营名 / seed 是产品已在用的社区或历史 CDP 名（如 **Foothill Ranch**）；

则：

- **`display_city` = 运营名（Foothill Ranch）**，**不要**改成大市名；
- 大市名进入 `search_aliases`；
- `place_class` 记为 `marketing_cdp_of:<parent_geoid>`（或等价标签）。

**若不做此例外：** 目录与 `cities.json` 种子会被批量改成 Lake Forest，破坏已有 SEO / 调度键。

**Med spa 示例**

| 诊所 | 地址 | Census 面域 | display_city | search_aliases |
| --- | --- | --- | --- | --- |
| OC Sports and Wellness（3128） | 26700 Towne Centre Dr Ste 100, Foothill Ranch, CA 92610 | **Lake Forest city**（已吞并原 CDP） | **Foothill Ranch** | Lake Forest, Orange（县） |

发现 query set 示例：

- `med spa in Foothill Ranch, CA`
- `med spa in Lake Forest, CA`（别名召回，结果再跑规则后仍标 Foothill Ranch）

---

### 例外 B — CDP 微面过切 → 常用市场 / postal 名

当满足：

1. 点落在某个 **很小的 CDP**；且  
2. matched address / postal / 现有 `city` 一致，且为更大的常用市场名；

则：

- **`display_city` = 常用市场名（postal / 现有 city）**；
- 微 CDP 名进入 `search_aliases`；
- `place_class=cdp_market`。

**Med spa 示例**

| 诊所 | 地址 | Census PIP | postal / 市场名 | display_city | aliases |
| --- | --- | --- | --- | --- | --- |
| Elle Aesthetic Arts and Plastic Surgery（3436） | 21021 Sycolin Rd Ste 001, Ashburn, VA 20147 | **Goose Creek Village CDP** | Ashburn | **Ashburn** | Goose Creek Village, Loudoun |
| Radiant Skin & Laser（3448） | 40 Tavernier Dr Suite D, Ponte Vedra Beach, FL 32081 | **Nocatee CDP** | Ponte Vedra Beach | **Ponte Vedra Beach** | Nocatee, St. Johns |

对比：真正落在 **Ashburn CDP** 面内的诊所（如 The Fix Clinic 3046）走普通 CDP 规则，`display_city=Ashburn`，aliases 含县即可。

---

### 规则 2 — 纯 CDP（无 incorporated）→ 用 CDP 名

点仅在 CDP 内、且不适用例外 B：

- `display_city` = CDP 名  
- aliases **必须**含所属 county 内主要 postal / 邻近大城（若有）

**Med spa 示例**

| 诊所 | 地址 | Census | display_city | aliases |
| --- | --- | --- | --- | --- |
| The Fix Clinic（3046） | 43490 Yukon Dr Ste 103, Ashburn, VA 20147 | Ashburn CDP | **Ashburn** | Loudoun |

---

### 规则 4 — 大城市内部社区名

点在 **大 incorporated 城市**（如 Los Angeles、Newport Beach）内，但 Maps / 目录使用知名社区名：

- **`display_city` = 社区名**（与当前目录、SEO、协作方习惯一致）；
- **`search_aliases` 必须含父城**；
- `place_class=neighborhood_of:<parent_geoid>`；
- 社区名需在白名单或 GNIS 居民点集合中（避免任意 sublocality 污染）。

**Med spa 示例**

| 诊所 | 地址 | Census 父城 | display_city | aliases |
| --- | --- | --- | --- | --- |
| Newport Beach Salon & Suites（2992） | 2711 East Coast Hwy #206, Corona Del Mar, CA 92625 | Newport Beach city | **Corona Del Mar** | Newport Beach |
| Beauty & Body by Flora（3303） | 22801 Ventura Blvd Ste 101, Woodland Hills, CA 91364 | Los Angeles city | **Woodland Hills** | Los Angeles |
| Rejuven8 Med Spa（3609） | 22339 Sherman Way, Canoga Park, CA 91303 | Los Angeles city | **Canoga Park** | Los Angeles |

发现：社区名 + 父城都要搜；入库仍写社区 `display_city`。  
站点短期仍 `ilike(city, display_city)`；中期应用别名查询，否则 Newport Beach / LA 页看不到这些诊所。

---

### 规则 3 — 真无 place 面域（unincorporated）

`place_class=none`：

- `display_city` 优先 **USPS postal city**；
- aliases 含社区名（若有）；
- **county 必填**。

（当前金标集中偏少；农村 / 县辖点按此条执行，低置信进人工队列。）

---

### 低置信 / 人工队列

入队条件（任一）：无可靠坐标且 ZIP 无法解析；Census 与 postal **州**不一致；同 place_id 两次 `display_city` 不一致；跨州同名且有可见 deals；`confidence=low`（如 geocode 未命中仍暂留 Google city）。

人工 **只改** `display_city` 与 aliases，不改 `google_place_id`。

---

## 5. 发现（Discovery）如何接规则

过滤线不变：`reviews ≥ 50`、`rating ≥ 4.0`、必须有 website；去重仍以 **`google_place_id`** 为先。

对每个 market seed：

1. 解析 seed 的 `place_class`；  
2. 构建 **query set** = `display_city` + 全部 `search_aliases`（可选 county / viewport）；  
3. 每个关键词模板（med spa / plastic surgery / …）对 query set 展开；  
4. 结果合并后，**对每条商户再跑第 4 节规则** 决定入库 `city`，而不是沿用搜索用的字符串；  
5. 试点门控按 **market（seed∪aliases）** 计数，避免 CDP 被拆碎后试点失败。

**示例：Corona Del Mar\|CA**

- `med spa in Corona Del Mar, CA`  
- `med spa in Newport Beach, CA`  
→ 在 Newport Beach 搜到的 CDM 诊所，入库仍为 `Corona Del Mar`。

**示例：Foothill Ranch\|CA**

- `med spa in Foothill Ranch, CA`  
- `med spa in Lake Forest, CA`  
→ 在 Lake Forest 市界内的 Foothill Ranch 运营点，入库仍为 `Foothill Ranch`。

---

## 6. 去重键

1. `google_place_id`（第一键）  
2. 唯一 website host  
3. `normalize(name) + round(lat,5)+round(lng,5)` 或 name + ZIP + street number  
4. **不要**单独用 `name|city` 做唯一键（`display_city` 会随规则调整）

---

## 7. 验收标准

1. 金标集（Irvine / Ashburn / Foothill Ranch / Corona Del Mar / Woodland Hills / Ponte Vedra 等）`display_city` 与人工期望一致；v2 试点 **38/38**。  
2. 同一 market，含 aliases 的 query set 召回 ⊇ 只搜 display_city。  
3. 同一 place_id 连续两次 discovery，`display_city` 一致率 ≥ 99%。  
4. 无静默跨州错配（规则 0）。  
5. 不因城市字符串从社区名改为父城名而重复建商户。  
6. 过滤线不回归。

---

## 8. 落地顺序

1. ~~规则定稿 + 金标试点~~（已完成，keep_v2）  
2. 将规则函数接入 discovery 解析路径（Mac 仓 `gmaps_discovery` / scraper 行映射）  
3. `cities.json` seed 旁挂 aliases / geoid  
4. 站点：`display_city OR aliases` 查询（中期）  
5. Schema：`lat/lng/zip/county/aliases` 列 migration（另开）

---

## 9. English summary（for collaborators）

We do **not** treat a single city string as an administrative unit.

- Resolve geography by **point-in-polygon** (Census TIGER) + ZIP postal names; Google components are inputs only.  
- **Display rules (v2):** incorporated place name by default; keep **operated market names** when annexed into a larger city (Foothill Ranch ⊂ Lake Forest); prefer **market/postal names** over micro-CDPs (Ashburn over Goose Creek Village; Ponte Vedra Beach over Nocatee); keep **neighborhood names** inside big cities (Corona Del Mar, Woodland Hills) with **parent city aliases**; truly unincorporated → postal + county; reject zip/metro geocode mismatches (CDM ↛ Del Mar).  
- **Discovery** expands each market seed to a query set (display + aliases); re-label every result with the rules above. Quality bar unchanged (reviews ≥ 50, rating ≥ 4, website). Dedup by `google_place_id`.  
- Pilot (38 live CostFinder med spa / derm rows): **100%** display agreement after v2 tweaks.

---

## 10. 修订记录

| 日期 | 变更 |
| --- | --- |
| 2026-10-27 | 初版方案（分层 + Census + 展示规则） |
| 2026-10-27 | v2：例外 A/B + 州/都会区校验；金标试点 100%；本文档定稿 |
