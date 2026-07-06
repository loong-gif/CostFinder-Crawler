# Change-Driven SQL Audit 设计

- 日期：2026-07-02
- 状态：已通过设计审阅，待实现
- 主题：在 Firecrawl change-driven offer 抽取管线中输出 `promo_offer_master` 数据更新 SQL 作为审计留痕

## 背景与动机

`utils/change_driven_extractor.py` 已实现完整的 change-driven 管线：基于 Firecrawl monitor check 的 meaningful 页 diff，由 LLM 决策每条 offer 的 `action`（`insert` / `update` / `mark_ended`），并通过 Supabase PostgREST（`update_row` / `insert_rows` / `update status='ended'`）直接写入 `promo_offer_master`。

当前问题：写库路径是 PostgREST API 调用，**不产出 SQL 文本**。下游缺少可审计、可回放的 SQL 留痕，无法在 report 中直接看到"这次 check 触发了哪些 SQL"。

## 目标

- 保留现有 PostgREST 直写行为，零回归。
- 在 monitor report JSON 中追加每页的 SQL 语句列表，作为审计留痕。
- SQL 与实际写库操作一一对应，反映最终决策（含 service_name 归一化、价格回填后的状态）。

## 非目标

- 不替换 PostgREST 写入路径为执行 SQL。
- 不引入新文件 / 新模块 / 新依赖。
- 不做 dry-run-only SQL 文件产物（SQL 始终随 report 留痕，无论是否 dry_run）。
- 不改变"删除"语义：仍为软删除 `status='ended'`，不是 `DELETE FROM`。

## 改动范围

仅 `utils/change_driven_extractor.py`：

1. 新增 `sql_quote(value) -> str`：数字/None/bool/字符串处理 + 单引号转义。
2. 新增 `build_offer_sql_statements(offers, *, source_url, source_name, now_iso) -> List[str]`。
3. 在 `extract_and_upsert_check_pages` 的 `page_result` 中追加 `sql_statements` 字段（无论 dry_run 都生成）。

`scripts/firecrawl_monitor_poll.py` **不改**：它已把整个 `change_driven` dict 写入 `monitor_report["checks_processed"][i]["change_driven"]`，SQL 自动随 report 落到 `output/monitor_results/firecrawl_monitor_poll_*.json`。

## SQL 语义

三类 action，与现有 PostgREST 写库操作严格一一对应：

### insert

```sql
INSERT INTO promo_offer_master (col1, col2, ...) VALUES (val1, val2, ...);
```

- 列集 = `build_offer_insert_payload` 的非空 key + 固定列（`channel` / `status` / `source_url` / `source_name`）。
- 与 `client.insert_rows("promo_offer_master", [payload])` 写入的 payload 完全一致。

### update

```sql
UPDATE promo_offer_master SET field=val, ..., updated_at='now' WHERE id=<matched_id>;
```

- 只含 `build_offer_update_payload` 的非空字段 + `updated_at`。
- `matched_id` 来自 `offer["matched_id"]`（已由 `validate_offer_actions` 解析为真实数据库 id）。
- 与 `_update_master_row` 写入的 payload 一致（含 `updated_at` 同源 timestamp）。

### mark_ended

```sql
UPDATE promo_offer_master SET status='ended', updated_at='now' WHERE id=<matched_id>;
```

- 与 `apply_offer_actions` 中 mark_ended 分支写入的内容一致。

## 值格式化规则

- 数值字段（`_MASTER_NUMERIC_FIELDS`）：`_parse_price` 成功 → 裸数字（如 `99` / `12.5`）；失败/None → `NULL`。
- 文本字段：单引号包裹，内部 `'` → `''` 转义；空字符串/None → `NULL`。
- `updated_at` / 固定列：字符串走文本转义规则。
- `matched_id`：裸字符串（UUID），SQL 里用单引号包裹（Postgres uuid 字面量接受带引号）。

## 数据流

```
meaningful_pages
  → extract_diff_payload
  → fetch_candidate_offers
  → LLM → validate_offer_actions
  → standardize_offer_service_names
  → enrich_update_actions_with_diff_prices
  → apply_offer_actions            (PostgREST 直写，不变)
  → build_offer_sql_statements     (新增，纯生成 SQL 文本)
  → page_result["sql_statements"]
```

SQL 生成在 `apply_offer_actions` **之后**调用，输入是同一批已 enriched 的 offers，确保 SQL 反映最终决策。

## 错误处理

- SQL 生成失败**不影响写库**：`build_offer_sql_statements` 调用包 try/except，失败时 `sql_statements=[]` 并在 page_result 记 `sql_error` 字段，写库已成功的不回滚。
- LLM/candidate 失败的已有处理不变；这些路径下根本不会走到 SQL 生成（`extracted_offers` 为空或 page_result 已是 `llm_error` / `invalid_llm_payload` / `no_diff_data`）。

## 测试

新增 `tests/test_change_driven_sql.py`（无框架，纯 assert，可直接 `python` 运行），覆盖：

1. `insert`：所有字段齐全 → SQL 含全部列、数值无引号、文本转义单引号。
2. `update`：部分字段空 → SQL 只含非空字段 + `updated_at` + `WHERE id=`。
3. `mark_ended`：SQL 固定 `SET status='ended', updated_at=... WHERE id=`。
4. 空列表 → 返回 `[]`。
5. 单引号 / 分号 / 反斜杠注入文本 → 被正确转义，不破坏 SQL 结构。
6. `None` / 空字符串 → `NULL`。

## 验收标准

- 配置 `LLM_API_URL/LLM_MODEL/LLM_API_KEY` 跑 `python scripts/firecrawl_monitor_poll.py --dry-run`，report JSON 中每条 `extracted` page 都含非空 `sql_statements` 数组。
- 跑 `--skip-apify-on-success` 正式模式后，report JSON 中的 `sql_statements` 与实际写库结果（updated/inserted/ended 计数）一一对应。
- `python tests/test_change_driven_sql.py` 全部 assert 通过。
- 不改 PostgREST 写入路径，原有 `apply_offer_actions` 行为零变化。
