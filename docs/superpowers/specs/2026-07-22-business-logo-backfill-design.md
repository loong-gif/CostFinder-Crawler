# 商家 Logo 回填设计

## 目标

为 `master_business_info` 补充可展示的商家 logo URL，供 C 端列表/详情使用。

本期只做：

1. 表上新增可空列 `logo_url`
2. 对 `logo_url IS NULL` 的行，从官网首页规则提取 logo 外链并写回
3. 写库策略：**只填空，不覆盖已有值**（保护后续商家自上传）

本期不做：

- 下载镜像到 Supabase Storage（Phase 2）
- Google Places Photo 兜底（Phase 2b；主档已有 `google_place_id`，但仓库尚无 Places API 客户端）
- 把 logo 抓取并入促销/Search/Scrape 主链路
- `logo_source` / `cover_image_url`（cover 不在本期范围）

## 约束（已确认）

| 项 | 选择 |
| --- | --- |
| 存储形态 | 先存外链 URL；后续再迁 Storage |
| 数据源 | 官网优先；Google 为后续兜底 |
| 写入策略 | 仅当 `logo_url` 为空时写入 |

## Schema

```sql
ALTER TABLE master_business_info
  ADD COLUMN IF NOT EXISTS logo_url text;
```

- 列语义：可公开访问的图片 URL（绝对 URL）
- 与前端 PRD（`02-business-auth.md` / `05-business-management.md`）中的 `logo_url` 对齐
- 不改现有 RLS / 业务外键；主档仍是业务根表，logo 只是主档属性

同步更新文档：`docs/data-model-pipeline.md` §3.1 列说明。

## 抓取与提取

### 输入

- `master_business_info`：`business_id`, `website`, `logo_url`
- 过滤：`logo_url IS NULL AND website IS NOT NULL AND btrim(website) <> ''`

### 流程

```text
website → 规范化首页 URL
  → Firecrawl scrape（要能拿到 HTML/metadata，不只 markdown）
  → 规则候选集
  → 选一条绝对 URL
  → PATCH master_business_info.logo_url（仅空行）
```

不写 `firecrawl_search_raw` / `firecrawl_scrape_raw`：logo 不是促销证据链的一部分；避免污染 raw 表用途。

### 选图优先级（高 → 低）

1. `meta[property="og:image"]` / `meta[name="twitter:image"]`（排除明显 banner/hero 文件名，如含 `banner`/`hero`/`slider`）
2. `link[rel="apple-touch-icon"]` 或较大尺寸 favicon（优先 png/webp；svg 可接受）
3. 首页 header/nav 区域 `img`：`alt`/`class`/`id`/`src` 含 `logo`（大小写不敏感），且非 data-URI 占位图
4. 以上皆无 → 跳过（`logo_url` 保持 NULL），记入审计，留给 Phase 2b

相对路径用页面最终 URL 做 `urljoin` 成绝对 URL。明显无效候选（空、javascript:、过小的 1×1 追踪图若能从 URL/属性判断）丢弃。

### 幂等与安全

- 默认 `--dry-run`：只打印将写入的 `business_id` / URL，不 PATCH
- 正式写入前再读一次该行：`logo_url` 已非空则跳过（防并发/人工回填）
- 单站 scrape 失败：记录错误，继续下一家；不中断整批
- 不删除、不覆盖已有 `logo_url`

## 代码落点

| 产物 | 位置 | 说明 |
| --- | --- | --- |
| 迁移 SQL | `config/sql/m017_master_business_logo_url.sql` | 仅 `ADD COLUMN` |
| 选图纯函数 | `utils/business_logo.py` | `pick_logo_url(html, base_url) -> str \| None`；可单测 |
| 回填入口 | `scripts/backfill_business_logos.py` | 可重复运维入口（新店入库后可再跑）；默认 dry-run |

说明：回填可重复且会文档化，因此进 `scripts/` 而非 `one-off/`。选图逻辑放 `utils/`，脚本只做 IO 与批处理。

## 验证

1. 迁移后：`information_schema` 可见 `logo_url`
2. 自检：`utils/business_logo.py` 对 2–3 段合成 HTML fixture 断言优先级（og > apple-touch > img[logo]）
3. dry-run：对少量 `business_id` 打印候选 URL，人工 spot-check
4. 实写后：`logo_url IS NOT NULL` 比例与失败原因分布可查；已有非空行不被改写

## Phase 2 / 2b（非本期）

- **Phase 2**：下载外链 → Supabase Storage → 回写自家 URL；仍遵守只填空（或仅替换 crawl 来源，若届时加 `logo_source`）
- **Phase 2b**：官网失败时用 `google_place_id` 调 Places Photo；需新增 API 客户端与密钥

## 边界

- 不保证每家都有 logo；宁可空也不填错图
- 外链可能失效或防盗链；这是本期已知上限，升级路径见 Phase 2
- 不处理 cover / 社媒头像
