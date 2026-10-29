# one-off

**AI 与人工编写的一次性脚本统一放这里。** 不要放进 `scripts/`。

## 什么算 one-off

- 单域/单次数据修复、回填、恢复
- 迁移前置检查、临时诊断、实验性提取
- 预期跑完即弃、或合并进 `utils/`/`scripts/` 前的草稿

## 什么不算 one-off（应进 `scripts/` 或 `utils/`）

- 日常 cron、审计、monitor 轮询
- 可重复运行且已在 README 文档化的运维入口
- 会被多处 import 的共享逻辑（放 `utils/`/`crawler/`）

## 命名

`YYYYMMDD_<topic>.py` 或 `<topic>_<domain>.py`，文件名应能看出用途与范围。

## 文件头（必填）

每个脚本顶部用 docstring 写清：

- 目的
- 输入/前置条件（表、域名、business_id）
- 是否写库；默认 `--dry-run`
- 完成状态（进行中 / 已执行可删）

## 生命周期

1. 新建 → 只放 `one-off/`
2. 若逻辑稳定且需长期保留 → 提炼到 `utils/` 或升格为 `scripts/` 正式入口
3. 任务完成后 → 删除文件（历史见 `git log`）；不要堆在 `scripts/` 根目录

## 约束

- **禁止**在 `scripts/` 根目录新增一次性脚本
- **禁止**重建 `scripts/archive/`
- one-off 脚本**不纳入**常规 `pytest` 与生产调度
