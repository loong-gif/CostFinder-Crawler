# Rendered Visibility QA

## Purpose

Firecrawl Markdown/HTML 只能证明文本进入了抓取结果，不能证明用户在当前浏览器渲染状态能看到它。`onlyMainContent` 也不是 visible-content-only 过滤器；隐藏的正文 section 仍可能被抽取。

对价格、活动日期、优惠条件和 membership benefit，网页证据必须满足：

```text
source_claim ⊆ DOM_text ∩ rendered_visible_text
```

## Browser gate

对每条进入 `promotion_content`、`benefits` 或 Offer 的关键文本，在 ego-lite 中：

1. 在当前页面 DOM 中定位包含该文本的 text node。
2. 对 text node 建立 `Range`，检查 `range.getClientRects()` 至少有一个宽高均大于 0 的矩形。
3. 从 text node 的元素向上检查祖先链，记录每一层的 `display`、`visibility`、`opacity` 和 `getClientRects()`。
4. 任一祖先为 `display:none`、`visibility:hidden/collapse`、`opacity:0`，或节点没有有效 layout rect，即判定为 `not_rendered_visible`。
5. 对可见的 `Show More`、`Read More`、`View More`、`Expand` 控件执行正常点击，再重新检查 DOM；不能通过注入 CSS 强行显示隐藏内容。
6. 保存最终 URL、检查时间、匹配文本、祖先隐藏原因和 rect 摘要；不能只保存“文本找到”。

页面被阻断、超时或无法加载时，状态只能是 `page_unavailable`，不能当作可见或不可见。

## ego-lite evaluator

下面的页面内检查用于一次 URL 的可见性核验；调用方应将结果与当前 Promotion/Membership/Offer 的声明逐条关联：

```js
({ claims }) => {
  const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
  const textNodes = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) textNodes.push(node);
  const results = [];

  for (const claim of claims) {
    const matches = [];
    for (const node of textNodes) {
      if (!normalize(node.nodeValue).includes(normalize(claim))) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      const rects = [...range.getClientRects()].map(rect => ({
        x: rect.x, y: rect.y, width: rect.width, height: rect.height,
      }));
      const ancestors = [];
      let current = node.parentElement;
      while (current && ancestors.length < 20) {
        const style = getComputedStyle(current);
        ancestors.push({
          tag: current.tagName,
          id: current.id,
          className: String(current.className || ''),
          display: style.display,
          visibility: style.visibility,
          opacity: style.opacity,
          rects: [...current.getClientRects()].map(rect => ({
            width: rect.width, height: rect.height,
          })),
        });
        current = current.parentElement;
      }
      const hiddenAncestor = ancestors.find(item =>
        item.display === 'none' ||
        item.visibility === 'hidden' ||
        item.visibility === 'collapse' ||
        Number(item.opacity) === 0 ||
        (item.rects.length > 0 && item.rects.every(rect => rect.width <= 0 || rect.height <= 0))
      ) || null;
      const hasTextLayoutBox = rects.some(rect => rect.width > 0 && rect.height > 0);
      matches.push({
        text: normalize(node.nodeValue),
        visible: hasTextLayoutBox && !hiddenAncestor,
        rects,
        hiddenAncestor,
        visibilityFailure: hiddenAncestor || (!hasTextLayoutBox ? {reason: 'text_node_has_no_layout_box'} : null),
        ancestors,
      });
    }
    results.push({claim, matches});
  }

  return {url: location.href, title: document.title, results};
}
```

## Acceptance states

`visibility-manifest --plan` (`utils/visibility_manifest.py`) records per page and per claim:

- `page_accessible`: the render produced a usable page; its claims are judged below.
- `page_not_accessible`: navigation failed, or the render stayed blank or showed a block page after `MAX_ATTEMPTS` renders in new task spaces (`utils/ego_render.py`). Its claims are not judged; this is not a negative content finding.
- `visible_match`: the claim is traceable in the visible text after expanding panels and tabs (`claim_matches`).
- `image_only`: the claim is traceable only in the OCR text of a visible image; the image URL, page URL, transcript and capture time are kept as `visibleEvidence`.
- `not_visible`: the page rendered and neither its visible text nor its readable images carry the claim.

`render-pages` (`utils/render_pages.py`) records per discovered page `verified`, `not_visible` or `page_unavailable` with the same meaning; a `page_unavailable` page keeps `add_with_review` and is rendered again on the next run.

Existing browser QA reports that lack computed-style, ancestor and layout-rect data do not satisfy this gate retroactively. They remain historical evidence and must be rechecked before being used as current source support.

## Image evidence path

When a claim is not in the visible text, the command proceeds automatically:

1. Visible promotion images of the rendered page are downloaded and read by `utils.paddle_ocr_markdown.ocr_lines`. The command opens the SSH tunnel to the OCR service when it is unreachable and closes it afterwards.
2. An image visitors cannot load (HTTP error status) or that is not a raster image gives no text. A failing OCR service is retried; when it keeps failing the command stops, so an OCR failure never becomes `not_visible`.
3. A claim matched in the OCR text becomes `image_only`; otherwise it is `not_visible` and is excluded from the write by `utils.rendered_visibility_gate.filter_visible_source_content`. When the page content changes, the next monitor run verifies it again.

The final gate is evaluated against the current database, not the original scrape report. The 2026-09-18 run produced `outputs/final_visibility_gate_audit_20260918.json`: 160 active Promotions, 1,444 active claims, 160 verified records, 0 unmatched claims, and 0 active Offers attached to inactive Promotions. Active Offers attached to empty active Promotions are also rejected; the final run deactivated 51 such Offers.

## 爬虫优先复用渲染（可选，默认关闭）

`MONITOR_RENDER_MODE=crawler_first` 时，监控只在满足以下**全部**条件时复用上一次真实浏览器渲染，而不重新渲染：

1. 本次爬虫 Markdown 的摘要与上次真实渲染时的 `content_digest` 相同；
2. 爬虫 `rawHtml` 连同页面样式表（`utils/crawler_visibility_fetch.fetch_stylesheets` 抓取 `<link>`、`<style>`
   与递归 `@import`）经 `utils/crawler_visibility.assess_page` 按 1440 宽桌面屏幕的层叠规则计算可见性后，
   判定无需浏览器：正文不为空、可见文本不少于 200 字符、样式表都已取得、选择器与条件规则都能判定、
   没有轮播与滚动动画、内嵌框架或疑似促销图片、每条价格事实的数字都出现在可见文本中；
3. 上一次真实渲染距今不超过 `MONITOR_RENDER_MAX_AGE_DAYS`（默认 14 天），且当时可访问并有视觉摘要。

新页面、内容变化、空内容、可疑内容、超过期限的页面仍然进浏览器。复用的证据保留原始渲染的时间戳和摘要，
并标记 `visibility_source: crawler_html_filtered`；不会改写 `browser.json`。

**注意：** 可见性按 HTML 与 CSS 的层叠计算，不计算布局；依赖 JavaScript 的内容（轮播、滚动动画、脚本注入）
以及样式表取不到、选择器或条件规则无法判定的页面进浏览器。
该模式把"当前可见性证据"放宽为"内容未变 + 过滤干净 + 近期真实渲染"，与 `AGENTS.md` 中
"之后的抓取需要当前的可见性证据"的字面要求有张力，启用前需要先确认并同步修改该条规则。

另外，`run_data_quality_audit.py --recheck` 现在有运行时间预算（`--max-seconds`，默认 1800，
环境变量 `DATA_QUALITY_RECHECK_MAX_SECONDS`，`0` 为不限）；超出后停止开始新的渲染，未处理的条目
保持 `pending`/`review`，由下一次运行继续。
