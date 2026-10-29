const fs = await import("node:fs/promises");
const base = "/Users/wyl/BrandRap/costfinder/one-off/outputs/candidates_20261020";
const task = await taskSpace("CostFinder remaining candidates 20261023");
const page = task.page("p1");
const normalize = (value) => String(value || "").replace(/\s+/g, " ").trim().toLowerCase();
const rows = JSON.parse(await fs.readFile(`${base}/business_status.json`, "utf8")).filter((row) => row.master_verified_at).map((row) => row.input_row);
for (const row of rows) {
  const directory = `${base}/extraction/${row}`;
  const exists = await fs.stat(directory).then(() => true, (error) => {
    if (error.code === "ENOENT") return false;
    throw error;
  });
  if (!exists) continue;
  for (const filename of (await fs.readdir(directory)).filter((name) => name.endsWith(".json")).sort()) {
    const extraction = JSON.parse(await fs.readFile(`${directory}/${filename}`, "utf8"));
    if (Object.keys(extraction.extractions).length !== 4 || extraction.extraction_contracts?.offers !== "source_item_name_v1") continue;
    const outputDirectory = `${base}/visibility/${row}`;
    await fs.mkdir(outputDirectory, { recursive: true });
    const target = `${outputDirectory}/${filename}`;
    const previous = await fs.readFile(target, "utf8").then(JSON.parse, (error) => {
      if (error.code === "ENOENT") return null;
      throw error;
    });
    if (previous?.source_captured_at === extraction.captured_at && previous.business_id === extraction.business_id && previous.source_url === extraction.source_url && previous.status === "page_accessible" && previous.review_mode === "scrolled_dom") continue;
    const receipt = {
      business_id: extraction.business_id,
      input_row: row,
      source_url: extraction.source_url,
      source_captured_at: extraction.captured_at,
      extraction_updated_at: extraction.updated_at,
      offer_contract: extraction.extraction_contracts.offers,
      task_space_id: task.spaceId,
      status: "page_unavailable",
      checked_at: new Date().toISOString(),
      review_mode: "scrolled_dom",
    };
    const persistReceipt = async () => {
      if (previous?.captured_at) {
        const version = previous.captured_at.replace(/[:.]/g, "_");
        const archive = `${base}/visibility_history/${row}/${version}`;
        await fs.mkdir(archive, { recursive: true });
        await fs.writeFile(`${archive}/${filename}`, JSON.stringify(previous, null, 2));
      }
      await fs.writeFile(`${target}.pending`, JSON.stringify(receipt, null, 2));
      await fs.rename(`${target}.pending`, target);
    };
    try {
      await page.goto(extraction.source_url, { waitUntil: "domcontentloaded", timeout: 60000 });
      await page.waitForFunction(() => Boolean(document.body?.innerText?.trim()), undefined, { timeout: 30000 });
    } catch (error) {
      receipt.reason = String(error);
      await persistReceipt();
      console.log(JSON.stringify({ row, url: extraction.source_url, status: "page_unavailable", reason: receipt.reason }));
      continue;
    }
    let y = 0;
    for (let batch = 0; batch < 80; batch += 5) {
      const state = await page.evaluate(async ({ y, steps }) => {
        for (let step = 0; step < steps && y < document.documentElement.scrollHeight; step++, y += Math.max(400, window.innerHeight - 100)) {
          window.scrollTo(0, y);
          await new Promise((resolve) => setTimeout(resolve, 200));
        }
        return { y, height: document.documentElement.scrollHeight };
      }, { y, steps: Math.min(5, 80 - batch) });
      y = state.y;
      if (y >= state.height) break;
    }
    await page.evaluate(() => window.scrollTo(0, 0));
    const captured = await page.evaluate(() => {
      const nodes = [];
      const root = document.body || document.documentElement;
      if (!root) return { url: location.href, title: document.title, nodes, controls: [] };
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        const text = String(node.nodeValue || "").replace(/\s+/g, " ").trim();
        if (!text || ["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE"].includes(node.parentElement?.tagName)) continue;
        const range = document.createRange();
        range.selectNodeContents(node);
        const rects = [...range.getClientRects()].map(({ x, y, width, height }) => ({ x, y, width, height }));
        const ancestors = [];
        for (let element = node.parentElement; element; element = element.parentElement) {
          const style = getComputedStyle(element);
          ancestors.push({ tag: element.tagName, display: style.display, visibility: style.visibility, opacity: style.opacity });
        }
        const visible = rects.some((rect) => rect.width > 0 && rect.height > 0) && ancestors.every((item) => item.display !== "none" && !["hidden", "collapse"].includes(item.visibility) && Number(item.opacity) !== 0);
        nodes.push({ text, visible, rects, ancestors });
      }
      return { url: location.href, title: document.title, nodes, controls: [...document.querySelectorAll('details:not([open]), [aria-expanded="false"]')].map((element) => ({ tag: element.tagName, text: element.innerText, id: element.id })) };
    });
    receipt.status = "page_accessible";
    receipt.url = captured.url;
    receipt.captured_at = new Date().toISOString();
    receipt.capture = captured;
    const visibleText = normalize(captured.nodes.filter((node) => node.visible).map((node) => node.text).join(" "));
    const claims = [];
    for (const item of extraction.extractions.promotions.promotions) {
      for (const claim of item.promotion_content) claims.push({ recordType: "promotion", claim });
    }
    for (const item of extraction.extractions.memberships.memberships) {
      for (const claim of [...(item.benefits || []), item.membership_description].filter(Boolean)) claims.push({ recordType: "membership", claim });
    }
    for (const item of extraction.extractions.offers.offers) claims.push({ recordType: "offer", claim: item.offer_raw_text });
    receipt.claimResults = claims.map((item) => ({ ...item, status: normalize(item.claim) && visibleText.includes(normalize(item.claim)) ? "visible_match" : "not_found" }));
    await persistReceipt();
    console.log(JSON.stringify({ row, url: receipt.url, claims: receipt.claimResults.length, visible: receipt.claimResults.filter((item) => item.status === "visible_match").length, collapsed_controls: captured.controls.length }));
  }
}
