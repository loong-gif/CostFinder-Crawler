import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const URL = "https://nobhillaesthetics.com/pages/specials";
const CLAIM = "-17%";
const OUTPUT = `${ROOT}/outputs/visibility_review_20260918/iframe_snapshot_review_v3.json`;

function normalize(value) {
  let text = String(value ?? "").normalize("NFKC");
  text = text.replace(/[–—]/g, "-").replace(/[*_~`#>\\]/g, " ");
  return text.replace(/([\p{N}])([\p{L}])/gu, "$1 $2").replace(/([\p{L}])([\p{N}])/gu, "$1 $2").replace(/[^\p{L}\p{N}$%]+/gu, " ").replace(/\s+/g, " ").trim().toLocaleLowerCase();
}

const task = await taskSpace(6);
const page = task.page("p1");
await page.goto(URL, { timeout: 15000, waitUntil: "domcontentloaded" });
await page.waitForTimeout(2000);
const bodyText = String(await page.evaluate(() => document.body?.innerText || ""));
const matched = normalize(bodyText).includes(normalize(CLAIM));
const review = JSON.parse(await readFile(OUTPUT, "utf8"));
const item = review.results[URL] || { source_url: URL, claims: [CLAIM], snapshot_matches: [] };
item.claims = [...new Set([...(item.claims || []), CLAIM])];
if (matched) item.snapshot_matches = [...new Set([...(item.snapshot_matches || []), CLAIM])];
item.unmatched_claims = item.claims.filter(value => !(item.snapshot_matches || []).includes(value));
item.status = "accessible";
item.checked_at = new Date().toISOString();
item.manual_body_text_match = matched;
review.results[URL] = item;
await writeFile(OUTPUT, JSON.stringify(review, null, 2), "utf8");
console.log(JSON.stringify({ url: URL, claim: CLAIM, matched, output: OUTPUT }, null, 2));
