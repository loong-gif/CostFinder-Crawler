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
