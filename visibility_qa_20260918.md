# Rendered Visibility QA

- 检查时间：2026-09-18（Asia/Shanghai）
- 模式：先只读 QA，随后按证据门禁执行受控 Supabase 同步；所有写入均使用 `updated_at` 乐观并发条件并完成回读
- 新门禁：网页证据必须同时满足“文本存在于 DOM”与“目标节点及祖先在当前渲染状态有实际 layout box”

## 结论

本轮已完成全量 visibility QA：228 条 Promotion、245 条 Membership，合计 311 个唯一 source URL。每个 URL 均被标记为 `page_accessible` 或 `page_unavailable`；可访问页逐条检查 DOM 文本、Range layout box 和祖先 computed style。

汇总结果：

- 234 个 URL 可访问，77 个 URL 在本轮不可用；不可用页面没有被当作可见或隐藏。
- 3478 条声明中，1428 条通过可见性门禁，426 条为 `dom_only_hidden`，1624 条 `not_found`。
- 81 条记录含有至少一条隐藏声明：42 条 Promotion、39 条 Membership。

Greenspring 的批量导航第一次遇到临时 `ERR_CONNECTION_CLOSED`，随后在同一个 ego-lite 任务空间中完成直接复核并写回报告；因此最终报告不再把 Greenspring 计为不可用。

没有用 Firecrawl 或普通文本抓取替代 computed-style / layout-box 检查。

可见性 evaluator 已在同一 ego 任务空间的受控 data URL 上验证：包含四个 Elementor hidden class 的文本被判定为 `visible=false`，并准确返回 `SECTION.display=none` 及对应 class；普通 `example.com` 文本被判定为 `visible=true`。

## 已确认的 Greenspring 反例

- Business：`241`，Greenspring Aesthetics
- Active Promotion：`64`，`First Tox`
- Source URL：`https://greenspringaesthetics.com/specials`
- 初始 `promotion_content` 曾包含 `Botox/Jeuveau - Doctor`、`$12.00 per unit` 和 `Save $2.00 per unit off regular price`
- 直接 ego-lite 复核页面标题为 `Exclusive MedSpa Specials | Limited-Time Beauty Deals`，页面状态为可访问
- 上述三条文本均存在于 DOM，但 `Range.getClientRects()` 为空；共同祖先为 `DIV.elementor-element-3989c4ca.elementor-hidden-widescreen.elementor-hidden-desktop.elementor-hidden-tablet.elementor-hidden-mobile`，computed `display:none`
- 对应 Doctor Offer：`7764`（Botox）和 `10077`（Jeuveau），当前均为 inactive
- 对应 Staff Offer：`7597`，已停用
- 既有视觉证据 `greenspringaesthetics_specials.png` 显示当前用户可见的是 September Specials 图片：All Lasers、Microneedling Series、DiamondGlow、Facial Balancing；没有显示上述 Doctor 文本

这说明 Promotion 上游证据已经混入了当前不可见/过时内容；后续价格、服务映射和 Offer 数学自洽不能证明该证据对用户可见。

## 当前数据库影响面

- `clinic_promotions`：160 条 active Promotion，153 个 active source URL
- `clinic_memberships`：245 条记录，99 个 source URL；243 条有 benefits，2 条没有 benefits
- active Promotion 下的 active Offer：889 条
- active Offer 挂在 inactive Promotion 上：0 条

## 旧 QA 报告的限制

旧报告覆盖了 244 条 Promotion / 229 个 URL，以及 235 条 Membership / 94 个 URL；但其 browser evidence 没有保存目标节点的 computed style、祖先可见性或 layout box。因此这些记录只能说明“当时浏览器/抓取流程拿到了内容”，不能作为新门禁下的通过证据。

## 结果文件

- 机器可读结果：[visibility_qa_live_20260918.json](outputs/visibility_qa_live_20260918.json)
- 合并 iframe/可见快照证据后的结果：[visibility_qa_reconciled_20260918.json](outputs/visibility_review_20260918/visibility_qa_reconciled_20260918.json)
- iframe/可见快照复核清单：[iframe_snapshot_review.json](outputs/visibility_review_20260918/iframe_snapshot_review.json)
- 当前 active 人工复核队列：[current_active_review_queue.json](outputs/visibility_review_20260918/current_active_review_queue.json)
- 本地 OCR 重试结果：[visible_image_tesseract_retry.json](outputs/visibility_review_20260918/visible_image_tesseract_retry.json)
- 写入门禁实现：[rendered_visibility_gate.py](utils/rendered_visibility_gate.py)
- 可复用协议：[rendered_visibility_qa.md](docs/rendered_visibility_qa.md)
- 可复跑脚本：[20260918_run_visibility_qa_ego.mjs](one-off/20260918_run_visibility_qa_ego.mjs)

## 已执行的同步

- 28 条 active Promotion 的 `dom_only_hidden` claims 已按旧值/`updated_at` 乐观并发条件清理，28/28 回读成功。
- Greenspring `promotion_id=64` 已替换为当前可见 September Specials 图片的 11 条 OCR 原文；`is_offer_extracted=false`，因为新的图片促销尚未完成 Offer 原子化。
- Greenspring Staff Offer `7597` 已停用；Doctor Offers `7764`、`10077` 原本已 inactive。
- 3 个确认没有可见图片、iframe、canvas 或可见 CSS 背景促销内容的页面（Promotion `216`、`693`、`811`）已删除其无 DOM/无视觉证据 claims，并停用其 12 个 active Offers。
- 另有 52 条满足“页面可访问、无 iframe/canvas、候选图片 OCR 全部完成”的 active Promotion 已清理 `not_found` claims，并停用 17 个直接依赖这些文本的 active Offers；52/52 Promotion、17/17 Offer 回读成功。
- 对剩余 URL 追加了浏览器可见性树复核：43 个 URL 中 42 个可访问，32 条原先主文档 `not_found` 的 Promotion claim 在用户可见快照（包括可见 iframe 内容）中被确认；这些 claim 保留在 `promotion_content`，不作删除。
- 本地 Tesseract 补充证据后，又清理 3 条 Promotion 的 20 条无证据 claim，并停用 4 个直接依赖 Offer；3/3 Promotion、4/4 Offer 回读成功。
- 对 3 个页面已确认只有无促销意义的可见图片或透明 SVG 占位图的 Promotion（`62`、`109`、`3539`），又清理 9 条无证据 claim，并停用 9 个 active Offers；3/3 Promotion、9/9 Offer 回读成功。
- Doctor Revive `promotion_id=3547` 的 16 张候选可见图片已完成浏览器上下文抓取和 Tesseract 复核；3 条 claim 均无精确视觉匹配，已清理并停用 1 个依赖 Offer，1/1 Promotion、1/1 Offer 回读成功。
- 持久化入口已加硬门禁：缺少 rendered visibility manifest 时拒绝 Promotion 写入；有 manifest 时只写入 `visible_match` claim。相关测试 38 项通过。

视觉复核补充结果：97 个含 `not_found` 的 active source URL 中，94 个可访问；ego 确认 90 个页面存在可见图片，原 OCR 完成 92 张，另以本地 Tesseract 补充完成 49 张。按当前数据库回读，结合可见快照和 OCR 清理后仍有 29 条 active Promotion 记录含 365 条尚未确认的 `not_found` claims，其中 351 条涉及 iframe/dynamic，14 条页面不可用；这些仍保持 review，未被自动覆盖。

## 后续动作

1. 只有通过可见性门禁的文本，才允许进入 `promotion_content` / `benefits` 和下游 Offer 证据链。
2. Greenspring `promotion_id=64` 已切换到当前可见 September Specials 图片 OCR 内容；由于图片促销尚未完成 Offer 原子化，`is_offer_extracted=false`，不自动创建或激活 Offer。
3. 当前 active review 队列见 [current_active_review_queue.json](outputs/visibility_review_20260918/current_active_review_queue.json)：351 条 iframe/dynamic、14 条页面不可用。页面不可用项需要在后续链路可用时重跑；在此之前不应把其声明当作当前可见证据。
