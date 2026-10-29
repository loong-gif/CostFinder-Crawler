// Current evidence for the approved manual batch; no monitor state is advanced.
const fs = await import("node:fs/promises");
const base = "/Users/wyl/BrandRap/costfinder/output/monitor_price_sync/verified_sync_20261015";
const rows = JSON.parse(await fs.readFile(base + "/pages.json", "utf8"));
const task = await taskSpace(11);
const managed = await task.pages();
const first = task.page("p1");
const second = managed.find(p => p.label === "p2") || await task.newPage();
let cursor = 0;
async function worker(page) {
  await page.cdp("Emulation.setDeviceMetricsOverride", {width: 1440, height: 1600, deviceScaleFactor: 1, mobile: false});
  while (cursor < rows.length) {
    const row = rows[cursor++];
    const key = row.sample_ids[0].split("_")[0];
    const dir = base + "/current_evidence/" + key;
    await fs.mkdir(dir, {recursive: true});
    let prior;
    try { prior = JSON.parse(await fs.readFile(dir + "/capture.json", "utf8")); } catch {}
    if (prior && Date.now() - Date.parse(prior.captured_at) < 24 * 3600 * 1000) continue;
    const capture = {url: row.url, sample_ids: row.sample_ids, captured_at: new Date().toISOString(), navigation_error: null};
    try {
      try { await page.goto(row.url, {waitUntil: "domcontentloaded", timeout: 12000}); }
      catch (e) { capture.navigation_error = String(e).slice(0, 350); }
      capture.final_url = await page.url();
      if (!capture.final_url.startsWith("http")) throw new Error("no_committed_document");
      capture.initial = await page.evaluate(() => ({ready_state: document.readyState, height: document.documentElement.scrollHeight}));
      // Scrolling triggers lazy images; a bounded capture is never removal proof.
      await page.evaluate(async () => {
        for (let y = 0; y < Math.min(document.documentElement.scrollHeight, 24000); y += 1000) {
          window.scrollTo(0, y); await new Promise(resolve => setTimeout(resolve, 90));
        }
        window.scrollTo(0, 0);
      });
      capture.page = await page.evaluate(() => ({
        title: document.title, text: document.body.innerText, height: document.documentElement.scrollHeight,
        images: [...document.images].map(i => {const r = i.getBoundingClientRect(); return {
          src: i.currentSrc || i.src, alt: i.alt, complete: i.complete, natural_width: i.naturalWidth,
          x: r.x + scrollX, y: r.y + scrollY, width: r.width, height: r.height
        };}).filter(i => i.width >= 200 && i.height >= 120)
      }));
      capture.screenshot = dir + "/full_page.png";
      await page.screenshot({path: capture.screenshot, fullPage: true});
      capture.snapshot = await page.snapshot({scope: "full_page"});
      capture.removal_proof = false;
      capture.capture_status = capture.page.text.length ? "captured" : "empty";
    } catch (e) { capture.capture_status = "failed"; capture.error = String(e).slice(0, 500); }
    await fs.writeFile(dir + "/capture.json", JSON.stringify(capture, null, 2));
    console.log(JSON.stringify({key, status: capture.capture_status, text_chars: capture.page?.text?.length, images: capture.page?.images?.length}));
  }
}
await Promise.all([worker(first), worker(second)]);
console.log("CAPTURE_COMPLETE");
