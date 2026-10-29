// Render-review runner for ego-browser. The ego runtime does not pass environment variables, so the
// paths are injected as a first line by scripts/run_render_review.sh:
//   globalThis.RR = {"shard": "...", "out": "...", "shots": "..."};
// For every item it opens the URL in ego-lite and checks each evidence claim against the rendered page.
const fs = await import("node:fs/promises");
const RR = globalThis.RR;
const shard = JSON.parse(await fs.readFile(RR.shard, "utf8"));
const outPath = RR.out;
const shotDir = RR.shots || "";

const task = await taskSpace("costfinder render review " + (shard.name || ""));
const page = task.page("p1");
console.log("taskSpaceId", task.spaceId);

const results = [];
for (const item of shard.items) {
  const rec = { id: item.id, url: item.url, business_id: item.business_id, checked_at: new Date().toISOString() };
  try {
    await page.goto(item.url, { waitUntil: "domcontentloaded", timeout: 45000 });
    try { await page.waitForLoadState("load", { timeout: 15000 }); } catch (_) {}
    await page.waitForTimeout(1500);
    rec.final_url = await page.url();
    rec.title = await page.title();
    rec.result = await page.evaluate(({ claims }) => {
      const norm = (s) => String(s || "").toLowerCase().replace(/\s+/g, " ")
        .replace(/(\d),(\d{3})/g, "$1$2").replace(/\$\s+/g, "$").replace(/\s+%/g, "%")
        .replace(/(\d)\.00(?!\d)/g, "$1").trim();
      const has = (hay, a) => {
        let i = hay.indexOf(a);
        while (i !== -1) {
          const next = hay[i + a.length];
          if (!next || !/\d/.test(next)) return true;
          i = hay.indexOf(a, i + 1);
        }
        return false;
      };
      const nodes = [];
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let n;
      while ((n = walker.nextNode())) { const t = norm(n.nodeValue); if (t) nodes.push({ node: n, t }); }
      const domText = norm(document.body.textContent);
      const rendered = norm(document.body.innerText);
      const visibleNode = (node) => {
        const range = document.createRange();
        range.selectNodeContents(node);
        const rects = [...range.getClientRects()].filter((r) => r.width > 0 && r.height > 0);
        if (!rects.length) return false;
        for (let c = node.parentElement; c; c = c.parentElement) {
          const s = getComputedStyle(c);
          if (s.display === "none" || s.visibility === "hidden" || s.visibility === "collapse" || Number(s.opacity) === 0) return false;
        }
        return true;
      };
      const out = claims.map((claim) => {
        if (!claim.anchors.length) return { id: claim.id, status: "no_anchor", anchors: [] };
        const anchors = claim.anchors.map((a) => {
          const inRendered = has(rendered, a);
          const inVisibleNode = nodes.some((x) => has(x.t, a) && visibleNode(x.node));
          return { anchor: a, visible: inRendered || inVisibleNode, in_dom: has(domText, a) };
        });
        const status = anchors.every((x) => x.visible) ? "visible_match"
          : anchors.every((x) => x.in_dom) ? "dom_only_hidden" : "not_found";
        return { id: claim.id, status, anchors };
      });
      return { url: location.href, title: document.title, rendered_chars: rendered.length, claims: out };
    }, { claims: item.claims });
    if (shotDir) rec.screenshot = await page.screenshot({ path: `${shotDir}/${item.id}.png` });
  } catch (e) {
    rec.error = String(e && e.message ? e.message : e).slice(0, 300);
  }
  results.push(rec);
  await fs.writeFile(outPath, JSON.stringify({ space: task.spaceId, shard: shard.name, results }, null, 1));
}
await task.finish({ keep: [] });
console.log("DONE", results.length);
