import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const QUEUE = `${ROOT}/outputs/visibility_review_20260918/current_active_review_queue.json`;
const OUTPUT = `${ROOT}/outputs/visibility_review_20260918/unavailable_retry_ego.json`;

function normalize(value) {
  let text = String(value ?? "").normalize("NFKC");
  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, " $1 ").replace(/\[([^\]]*)\]\([^)]*\)/g, " $1 ").replace(/<[^>]*>/g, " ").replace(/[–—]/g, "-").replace(/[*_~`#>\\]/g, " ");
  return text.replace(/([\p{N}])([\p{L}])/gu, "$1 $2").replace(/([\p{L}])([\p{N}])/gu, "$1 $2").replace(/[^\p{L}\p{N}$%]+/gu, " ").replace(/\s+/g, " ").trim().toLocaleLowerCase();
}

const queue = JSON.parse(await readFile(QUEUE, "utf8")).queue.filter(item => item.reason === "page_unavailable");
const grouped = new Map();
for (const item of queue) { const current = grouped.get(item.source_url) || { source_url: item.source_url, promotion_ids: new Set(), claims: [] }; current.promotion_ids.add(Number(item.promotion_id)); current.claims.push(item.claim); grouped.set(item.source_url, current); }
const task = await taskSpace(6);
const page = task.page("p1");
const results = {};
for (const target of grouped.values()) {
  const claims = [...new Set(target.claims)];
  const result = { source_url: target.source_url, promotion_ids: [...target.promotion_ids], claims, status: "page_unavailable", snapshot_matches: [], unmatched_claims: [], checked_at: new Date().toISOString() };
  try {
    await page.goto(target.source_url, { timeout: 15000, waitUntil: "domcontentloaded" });
    await page.waitForTimeout(2000);
    const bodyText = String(await page.evaluate(() => document.body?.innerText || ""));
    const snapshot = String(await page.snapshot({ scope: "full_page" }));
    const evidence = `${normalize(bodyText)} ${normalize(snapshot)}`;
    result.status = "accessible";
    result.final_url = await page.url();
    result.title = await page.title();
    result.body_text_length = bodyText.length;
    result.snapshot_length = snapshot.length;
    result.snapshot_matches = claims.filter(claim => normalize(claim).length >= 8 && evidence.includes(normalize(claim)));
    result.unmatched_claims = claims.filter(claim => !result.snapshot_matches.includes(claim));
  } catch (error) { result.error = String(error).slice(0, 1000); }
  results[target.source_url] = result;
  console.log(JSON.stringify({ url: target.source_url, status: result.status, matches: result.snapshot_matches.length, claims: claims.length }));
}
await writeFile(OUTPUT, JSON.stringify({ generated_at: new Date().toISOString(), results }, null, 2), "utf8");
console.log(JSON.stringify({ output: OUTPUT, urls: Object.keys(results).length, accessible: Object.values(results).filter(item => item.status === "accessible").length, matched: Object.values(results).reduce((sum, item) => sum + item.snapshot_matches.length, 0) }, null, 2));
