import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const OUTPUT = `${ROOT}/outputs/final_visible_content_audit_20260918.json`;
const SOURCE_FILES = [
  `${ROOT}/outputs/biorevive_visible_product_sync_20260918.json`,
  `${ROOT}/outputs/remaining_visible_innertext_sync_20260918.json`,
];

function normalize(value) {
  let text = String(value ?? "").normalize("NFKC");
  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, " $1 ").replace(/\[([^\]]*)\]\([^)]*\)/g, " $1 ").replace(/<[^>]*>/g, " ").replace(/[–—]/g, "-").replace(/[*_~`#>\\]/g, " ");
  return text.replace(/([\p{N}])([\p{L}])/gu, "$1 $2").replace(/([\p{L}])([\p{N}])/gu, "$1 $2").replace(/[^\p{L}\p{N}$%]+/gu, " ").replace(/\s+/g, " ").trim().toLocaleLowerCase();
}

const targets = new Map();
for (const file of SOURCE_FILES) {
  const plan = JSON.parse(await readFile(file, "utf8"));
  const changes = plan.changes || (plan.promotion_id ? [{ promotion_id: plan.promotion_id, source_url: plan.source_url, new_content: plan.new_content }] : []);
  for (const change of changes) targets.set(Number(change.promotion_id), { promotion_id: Number(change.promotion_id), source_url: change.source_url, content: change.new_content });
}

const task = await taskSpace(6);
const page = task.page("p1");
const results = [];
for (const target of targets.values()) {
  const result = { promotion_id: target.promotion_id, source_url: target.source_url, claim_count: target.content.length, status: "failed", matched: [], unmatched: [], checked_at: new Date().toISOString() };
  try {
    await page.goto(target.source_url, { timeout: 15000, waitUntil: "domcontentloaded" });
    await page.waitForTimeout(2000);
    const bodyText = String(await page.evaluate(() => document.body?.innerText || ""));
    const snapshot = String(await page.snapshot({ scope: "full_page" }));
    const evidence = `${normalize(bodyText)} ${normalize(snapshot)}`;
    result.status = "verified";
    result.final_url = await page.url();
    result.title = await page.title();
    result.matched = target.content.filter(claim => normalize(claim).length >= 8 && evidence.includes(normalize(claim)));
    result.unmatched = target.content.filter(claim => !result.matched.includes(claim));
    if (result.unmatched.length) result.status = "incomplete";
  } catch (error) { result.error = String(error).slice(0, 1000); }
  results.push(result);
  console.log(JSON.stringify({ promotion_id: result.promotion_id, status: result.status, matched: result.matched.length, claims: result.claim_count }));
}
await writeFile(OUTPUT, JSON.stringify({ generated_at: new Date().toISOString(), all_verified: results.every(item => item.status === "verified"), results }, null, 2), "utf8");
console.log(JSON.stringify({ output: OUTPUT, all_verified: results.every(item => item.status === "verified"), promotion_count: results.length }, null, 2));
