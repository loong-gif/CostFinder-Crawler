const fs = await import("node:fs/promises");

const base = "/Users/wyl/BrandRap/costfinder/one-off/outputs/candidates_20261020";
const task = await taskSpace("CostFinder candidates 20261020 Firecrawl fallback");
const page = task.page("p1");
const readJson = async (path) => JSON.parse(await fs.readFile(path, "utf8"));
const rows = [];
for (const batch of [1, 2, 3]) {
  const path = `${base}/batches/batch_${batch}/status.json`;
  const status = await readJson(path);
  for (const row of status) {
    const pages = (row.discovery?.pages || []).filter((item) => item.status === "scrape_failed");
    if (pages.length) rows.push(...pages.map((item) => ({ row, url: item.url, error: item.error || "scrape_failed" })));
    else if (row.status === "crawl_failed" && row.source?.官网) rows.push({ row, url: row.source.官网, error: row.reason || "crawl_failed" });
  }
}
const seen = new Set();
for (const item of rows) {
  const key = `${item.row.input_row}|${item.url}`;
  if (seen.has(key)) continue;
  seen.add(key);
  const directory = `${base}/ego_fallback/${item.row.input_row}`;
  await fs.mkdir(directory, { recursive: true });
  const safe = Buffer.from(item.url).toString("base64url").slice(0, 80);
  const output = `${directory}/${safe}.json`;
  try {
    await page.goto(item.url, { waitUntil: "domcontentloaded", timeout: 45000 });
    await page.waitForFunction(() => Boolean(document.body?.innerText?.trim()), undefined, { timeout: 15000 });
    const capture = await page.evaluate(() => ({
      url: location.href,
      title: document.title,
      text: document.body.innerText,
      captured_at: new Date().toISOString(),
    }));
    await fs.writeFile(output, JSON.stringify({ input_row: item.row.input_row, business_id: item.row.business_id, requested_url: item.url, firecrawl_error: item.error, status: "ego_accessible", capture }, null, 2));
    console.log(JSON.stringify({ input_row: item.row.input_row, url: item.url, status: "ego_accessible" }));
  } catch (error) {
    await fs.writeFile(output, JSON.stringify({ input_row: item.row.input_row, business_id: item.row.business_id, requested_url: item.url, firecrawl_error: item.error, status: "ego_failed", error: String(error) }, null, 2));
    console.log(JSON.stringify({ input_row: item.row.input_row, url: item.url, status: "ego_failed" }));
  }
}
await task.finish({ keep: [] });
console.log(JSON.stringify({ firecrawl_failed_targets: rows.length, unique_targets: seen.size, database_writes: 0 }));
