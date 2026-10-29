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

- `visible_match`: the claim has a matching text node and passes the layout/ancestor gate.
- `dom_only_hidden`: the claim exists in DOM but fails the gate; it must not support a write or activation.
- `not_found`: the claim is absent from the current DOM.
- `page_unavailable`: the browser could not establish a usable page state.
- `image_only`: the claim is supported by a rendered image and has persistent screenshot/OCR evidence; DOM text alone is insufficient.
- `image_review_pending`: a rendered image or Canvas is present, but OCR/visual review is incomplete or failed; this is not a negative content finding.
- `empty_benefits_pending`: the Membership has no stored benefits, so no claim was generated; the plan requires separate image and plan-identity review.

Existing browser QA reports that lack computed-style, ancestor and layout-rect data do not satisfy this gate retroactively. They remain historical evidence and must be rechecked before being used as current source support.

## Manual and visual evidence path

When a claim is not found in the main document, the review proceeds in this order:

1. Retry navigation with `domcontentloaded`/`commit` semantics and inspect `document.body.innerText` plus the accessibility snapshot. A navigation timeout is not treated as a visibility decision.
2. Inspect visible iframe content and merge only exact snapshot matches.
3. Inspect visible images/canvas elements. The image itself must have a layout box and visible ancestors; OCR is used only to match the exact claim text, with the image URL, page URL, screenshot, transcript and timestamp retained. Do not discard a rendered image only because its natural bitmap is smaller than an arbitrary pixel threshold.
4. If OCR completes without an exact match, keep the claim as `not_found` for manual attribution; if the review cannot complete, keep it as `image_review_pending`. Neither state alone authorizes a Membership write.
5. If no rendered evidence supports a confirmed source claim, remove it from active `promotion_content` or `benefits` only through a separately reviewed repair. Do not invent replacement wording, and do not automatically deactivate dependent Offers in the Membership visibility pass.

The final gate is evaluated against the current database, not the original scrape report. The 2026-09-18 run produced `outputs/final_visibility_gate_audit_20260918.json`: 160 active Promotions, 1,444 active claims, 160 verified records, 0 unmatched claims, and 0 active Offers attached to inactive Promotions. Active Offers attached to empty active Promotions are also rejected; the final run deactivated 51 such Offers.

## 爬虫优先复用渲染（可选，默认关闭）

`MONITOR_RENDER_MODE=crawler_first` 时，监控只在满足以下**全部**条件时复用上一次真实浏览器渲染，而不重新渲染：

1. 本次爬虫 Markdown 的摘要与上次真实渲染时的 `content_digest` 相同；
2. 爬虫 `rawHtml` 经 `utils/crawler_visibility.py` 过滤隐藏元素后，`assess_page` 判定无需浏览器：
   正文不为空、可见文本不少于 200 字符、没有 `d-none`/`hidden`/`collapse` 等依赖 CSS 的隐藏类、
   没有疑似促销图片、每条价格事实的数字都出现在过滤后的可见文本中；
3. 上一次真实渲染距今不超过 `MONITOR_RENDER_MAX_AGE_DAYS`（默认 14 天），且当时可访问并有视觉摘要。

新页面、内容变化、空内容、可疑内容、超过期限的页面仍然进浏览器。复用的证据保留原始渲染的时间戳和摘要，
并标记 `visibility_source: crawler_html_filtered`；不会改写 `browser.json`。

**注意：** 过滤只依据 HTML 与内联样式，不等于真实布局；依赖 JS 注入或外部 CSS 的页面靠"可疑即进浏览器"兜底。
该模式把"当前可见性证据"放宽为"内容未变 + 过滤干净 + 近期真实渲染"，与 `AGENTS.md` 中
"之后的抓取需要当前的可见性证据"的字面要求有张力，启用前需要先确认并同步修改该条规则。

另外，`run_data_quality_audit.py --recheck` 现在有运行时间预算（`--max-seconds`，默认 1800，
环境变量 `DATA_QUALITY_RECHECK_MAX_SECONDS`，`0` 为不限）；超出后停止开始新的渲染，未处理的条目
保持 `pending`/`review`，由下一次运行继续。
