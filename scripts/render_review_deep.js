// Deep render review for ego-browser: expands collapsed panels and tabs, then collects the visible text and
// the visible images of each page. Paths are injected as the first line by scripts/run_render_review_deep.sh:
//   globalThis.RR = {"shard": "...", "out": "...", "timeout": 90000};
const fs = await import("node:fs/promises");
const RR = globalThis.RR;
const shard = JSON.parse(await fs.readFile(RR.shard, "utf8"));
const gotoTimeout = RR.timeout || 60000;

const task = await taskSpace("costfinder deep render review " + (shard.name || ""));
const page = task.page("p1");
console.log("taskSpaceId", task.spaceId);

const results = [];
for (const item of shard.items) {
  const rec = { url: item.url, business_id: item.business_id, checked_at: new Date().toISOString() };
  try {
    // 只按 PC 屏幕宽度渲染核验（与 monitor 的 desktop 断点相同）
    await page.cdp("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1600, deviceScaleFactor: 1, mobile: false });
    try {
      await page.goto(item.url, { waitUntil: "domcontentloaded", timeout: gotoTimeout });
    } catch (error) {
      // 连接被断开、重置这类网络瞬时故障重试一次；其余导航失败是该页面的结果
      if (!/net::ERR_CONNECTION_(CLOSED|RESET)/.test(String(error))) throw error;
      await page.waitForTimeout(5000);
      await page.goto(item.url, { waitUntil: "domcontentloaded", timeout: gotoTimeout });
    }
    try { await page.waitForLoadState("load", { timeout: 20000 }); } catch (_) {}
    await page.waitForTimeout(2000);
    rec.final_url = await page.url();
    rec.title = await page.title();
    rec.result = await page.evaluate(async () => {
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      // 文字区域中心点上最上层的元素就是这段文字时，用户确实看得见（高度为 0 但溢出可见的容器不会误判为隐藏）
      const pointVisible = (node, rect) => {
        const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
        if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) return false;
        const top = document.elementFromPoint(x, y);
        return !!top && (node.parentElement.contains(top) || top.contains(node.parentElement));
      };
      const clipped = (el) => {
        for (let c = el; c && c !== document.body; c = c.parentElement) {
          const s = getComputedStyle(c);
          if (s.display === "none" || s.visibility === "hidden" || s.visibility === "collapse" || Number(s.opacity) === 0) return true;
          if (s.overflow !== "visible" || s.overflowY !== "visible" || s.overflowX !== "visible") {
            const r = c.getBoundingClientRect();
            if (r.height < 2 || r.width < 2) return true;
          }
          if (s.maxHeight === "0px" || s.height === "0px") return true;
        }
        return false;
      };
      const collectText = () => {
        const out = [];
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let n;
        while ((n = walker.nextNode())) {
          const t = n.nodeValue.replace(/\s+/g, " ").trim();
          if (!t) continue;
          const p = n.parentElement;
          if (!p || ["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE"].includes(p.tagName)) continue;
          const range = document.createRange();
          range.selectNodeContents(n);
          const rects = [...range.getClientRects()].filter((r) => r.width > 0 && r.height > 0);
          if (!rects.length || (clipped(p) && !pointVisible(n, rects[0]))) continue;
          out.push(t);
        }
        return out;
      };
      window.__rrCollectText = collectText;
      const collectImages = () => {
        const seen = new Set();
        const out = [];
        const push = (url, alt, w, h) => {
          if (!url || url.startsWith("data:") || seen.has(url)) return;
          seen.add(url);
          out.push({ url, alt: alt || "", w: Math.round(w), h: Math.round(h) });
        };
        for (const img of document.querySelectorAll("img")) {
          const r = img.getBoundingClientRect();
          if (r.width < 120 || r.height < 60 || clipped(img)) continue;
          push(img.currentSrc || img.src, img.alt, r.width, r.height);
        }
        for (const el of document.querySelectorAll("*")) {
          const bg = getComputedStyle(el).backgroundImage;
          const m = bg && bg.match(/url\(["']?([^"')]+)["']?\)/);
          if (!m) continue;
          const r = el.getBoundingClientRect();
          if (r.width < 200 || r.height < 80 || clipped(el)) continue;
          push(new URL(m[1], location.href).href, "", r.width, r.height);
        }
        return out;
      };
      const texts = new Set(collectText());
      const images = new Map();
      const addImages = () => collectImages().forEach((i) => images.set(i.url, i));
      // lazy-loaded content
      for (let y = 0; y < document.body.scrollHeight; y += 700) { window.scrollTo(0, y); await sleep(120); }
      window.scrollTo(0, 0);
      await sleep(500);
      collectText().forEach((t) => texts.add(t));
      addImages();
      // expand collapsed controls, one at a time, collecting what each click reveals
      document.querySelectorAll("details:not([open])").forEach((d) => { if (!d.closest("nav, header")) d.open = true; });
      const selector = [
        '[aria-expanded="false"]', "details > summary", '[data-toggle="collapse"]', '[data-bs-toggle="collapse"]',
        ".accordion-button.collapsed", ".elementor-tab-title", ".et_pb_toggle_closed .et_pb_toggle_title",
        '[role="tab"]', ".accordion-header", ".accordion-title", ".faq-question", ".toggle-title", ".collapsible",
        ".wp-block-ub-content-toggle-accordion-title-wrap", ".fusion-toggle-heading", ".tab-title", ".nav-tabs a",
      ].join(",");
      const controls = [...document.querySelectorAll(selector)].filter((el) => {
        if (el.closest("nav, header, footer, [role=menu], .menu, .navbar")) return false;
        const r = el.getBoundingClientRect();
        if (r.width < 4 || r.height < 4) return false;
        const a = el.tagName === "A" ? el : el.querySelector("a[href]");
        if (a) {
          const href = a.getAttribute("href") || "";
          if (href && !href.startsWith("#") && !href.startsWith("javascript")) return false;
        }
        return true;
      }).slice(0, 80);
      let clicked = 0;
      for (const el of controls) {
        try { el.scrollIntoView({ block: "center" }); el.click(); clicked++; } catch (_) { continue; }
        await sleep(250);
        collectText().forEach((t) => texts.add(t));
      }
      await sleep(600);
      collectText().forEach((t) => texts.add(t));
      addImages();
      return { controls: controls.length, clicked, texts: [...texts], images: [...images.values()].slice(0, 40) };
    });
    // 页头的下拉菜单与通知卡片：悬停菜单项、点开通知开关后可见的文字同样计入（全站横幅优惠常只放在这里）
    const reveal = await page.evaluate(() => {
      const targets = [];
      for (const li of document.querySelectorAll("header li")) {
        if (!li.querySelector('ul, [class*="submenu"], [class*="mega"], [class*="dropdown"]')) continue;
        const handle = li.firstElementChild;
        const r = handle && handle.getBoundingClientRect();
        if (r && r.width >= 4 && r.height >= 4) targets.push({ el: handle, click: false });
      }
      for (const el of document.querySelectorAll("header button, header [role=button]")) {
        const label = `${el.getAttribute("aria-label") || ""} ${el.className}`;
        const r = el.getBoundingClientRect();
        if (/notif|bell|announce/i.test(label) && r.width >= 4 && r.height >= 4) targets.push({ el, click: true });
      }
      return targets.slice(0, 12).map((t, i) => { t.el.setAttribute("data-rr-reveal", String(i)); return { i, click: t.click }; });
    });
    const revealed = new Set();
    for (const target of reveal) {
      const selector = `[data-rr-reveal="${target.i}"]`;
      try {
        if (target.click) await page.click(selector, { timeout: 3000 });
        else await page.hover(selector, { timeout: 3000 });
      } catch (_) { continue; }
      await page.waitForTimeout(800);
      (await page.evaluate(() => window.__rrCollectText())).forEach((t) => revealed.add(t));
      if (target.click) { try { await page.click(selector, { timeout: 3000 }); } catch (_) {} }
    }
    rec.result.revealed_controls = reveal.length;
    rec.result.texts = [...new Set([...rec.result.texts, ...revealed])];
  } catch (e) {
    rec.error = String(e && e.message ? e.message : e).slice(0, 300);
  }
  results.push(rec);
  await fs.writeFile(RR.out, JSON.stringify({ space: task.spaceId, shard: shard.name, results }, null, 1));
}
await task.finish({ keep: [] });
console.log("DONE", results.length);
