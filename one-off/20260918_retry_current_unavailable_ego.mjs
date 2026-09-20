import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const REPORT = `${ROOT}/outputs/visibility_qa_current_20260918.json`;
const OUTPUT = `${ROOT}/outputs/visibility_review_20260918/unavailable_retry_current_ego.json`;

function normalize(value) {
  let text = String(value ?? "").normalize("NFKC");
  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, " $1 ").replace(/\[([^\]]*)\]\([^)]*\)/g, " $1 ").replace(/<[^>]*>/g, " ");
  text = text.replace(/[–—]/g, "-").replace(/[*_~`#>\\]/g, " ");
  return text.replace(/([\p{N}])([\p{L}])/gu, "$1 $2").replace(/([\p{L}])([\p{N}])/gu, "$1 $2").replace(/[^\p{L}\p{N}$%]+/gu, " ").replace(/\s+/g, " ").trim().toLocaleLowerCase();
}

const report = JSON.parse(await readFile(REPORT, "utf8"));
const active = await (async () => {
  const env = {};
  for (const line of (await readFile(`${ROOT}/.env`, "utf8")).split(/\r?\n/)) { const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/); if (m) env[m[1]] = m[2].replace(/^['"]|['"]$/g, ""); }
  const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
  const response = await fetch(`${env.SUPABASE_URL.replace(/\/$/, "")}/rest/v1/clinic_promotions?select=promotion_id,source_url,promotion_content,is_active&is_active=eq.true&limit=1000`, { headers: { apikey: key, Authorization: `Bearer ${key}` } });
  return response.json();
})();

const targets = active.filter(row => report.results?.[row.source_url]?.status !== "page_accessible");
const task = await taskSpace(6);
const page = task.page("p1");
const results = {};
for (const row of targets) {
  const claims = Array.isArray(row.promotion_content) ? row.promotion_content : [];
  const result = { source_url: row.source_url, promotion_ids: [Number(row.promotion_id)], claims, status: "page_unavailable", snapshot_matches: [], unmatched_claims: [], checked_at: new Date().toISOString() };
  try {
    await page.goto(row.source_url, { timeout: 15000, waitUntil: "domcontentloaded" });
    await page.waitForTimeout(2500);
    const bodyText = String(await page.evaluate(() => document.body?.innerText || ""));
    const snapshot = String(await page.snapshot({ scope: "full_page" }));
    const evidence = normalize(`${bodyText}\n${snapshot}`);
    result.status = "accessible";
    result.final_url = await page.url();
    result.title = await page.title();
    result.body_text_length = bodyText.length;
    result.snapshot_length = snapshot.length;
    result.snapshot_matches = claims.filter(claim => normalize(claim).length >= 8 && evidence.includes(normalize(claim)));
    result.unmatched_claims = claims.filter(claim => !result.snapshot_matches.includes(claim));
  } catch (error) { result.error = String(error).slice(0, 1200); }
  results[row.source_url] = result;
  console.log(JSON.stringify({ url: row.source_url, status: result.status, matches: result.snapshot_matches.length, claims: claims.length }));
}
await writeFile(OUTPUT, JSON.stringify({ generated_at: new Date().toISOString(), results }, null, 2), "utf8");
console.log(JSON.stringify({ output: OUTPUT, urls: targets.length, accessible: Object.values(results).filter(item => item.status === "accessible").length, matched: Object.values(results).reduce((sum, item) => sum + item.snapshot_matches.length, 0) }, null, 2));
