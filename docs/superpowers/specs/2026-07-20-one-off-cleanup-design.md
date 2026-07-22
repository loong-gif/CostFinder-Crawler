# 一次性产物清理设计

**日期:** 2026-07-20  
**状态:** 已批准并执行

## 目标

硬删除仓库内已完成使命的一次性脚本、过时规划文档与本地残留，保留可重复运行的生产/审计入口；将 `service_name_dict.json` 权威路径统一到仓库根目录。

## 约束

- 硬删除，不新建 `docs/archive/`；历史靠 `git log` 找回
- 不删 `config/sql/` 迁移（含 `config/sql/archive/`）
- 不改业务提取/抓取逻辑（除词典路径）

## 删除清单

### 脚本

- 整目录 `scripts/archive/`
- `scripts/restore_loulou_raw_from_bundle.py`
- `scripts/check_monitor_results.py`
- `scripts/firecrawl_monitor_report.py`

### 文档

- 根目录 `PLAN.md`、`DESIGN.md`、`PLAN-REVIEW-LOG.md`
- 全部 `docs/superpowers/plans/`
- Specs：`2026-07-02-change-driven-sql-audit-design.md`、`2026-07-09-promo-offer-master-quality-design.md`、`2026-07-15-clinic-services-botox-crawl-design.md`

### 本地残留

- `.env.backup_*`
- 空目录 `.agents/`、`.codex/`、`CF_Extrator_Agent/`
- 本地 `reports/` 产物（gitignore）

## 保留

### 脚本（生产/审计/管道）

monitor/poll、月度/detect staging、社媒 daily、hermes worker、`audit_*`、`apply_sql_migration.py`、`seed_clinic_services_{search,botox}.py`、`backfill_clinic_services_from_offers.py`、`run_domain_architecture_pipeline.py`、`apply_pipeline_bundle.py`、`discover_staging_price_page_gaps.py`

### 文档

- `docs/data-model-pipeline.md`
- Specs：`2026-07-18-promo-service-matching-design.md`、`2026-07-18-membership-extraction-schema-design.md`、`2026-07-15-promo-offer-items-migration-design.md`、`2026-07-19-costfinder-architecture-skill-design.md`
- `.cursor/skills/costfinder-architecture/`

### 数据

- 根目录 `service_name_dict.json`（权威路径）

## 代码与配置变更

- `utils/offer_extraction_llm.py`、`utils/change_driven_extractor.py`：`PROJECT_ROOT / "service_name_dict.json"`
- `.gitignore`：`!/service_name_dict.json`
- `README.md`：去掉 `scripts/archive/`、死链 Firecrawl 引导、`CF_Extrator_Agent/` 目录树
- `scripts/apply_sql_migration.py`：drop gate 提示改为「确认库内 membership FK 已迁移 / 见 git 历史」

## 验证

1. `rg` 无 `scripts/archive`、`CF_Extrator_Agent`、已删 spec/plan 残留引用
2. `pytest -q` 冒烟

## 后续约束（2026-07-20 追加）

- 一次性脚本统一放 [`one-off/`](../../../one-off/)，见 [`one-off/README.md`](../../../one-off/README.md)
- AI 不得在 `scripts/` 根目录新增 one-off；任务完成后从 `one-off/` 删除
