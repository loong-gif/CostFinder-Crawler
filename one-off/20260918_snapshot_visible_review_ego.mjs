import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const SPACE_ID = 6;
const PAGE_LABEL = "p1";
const REPORT_PATH = `${ROOT}/outputs/visibility_qa_live_20260918.json`;
const OUTPUT_PATH = `${ROOT}/outputs/visibility_review_20260918/iframe_snapshot_review.json`;

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (match) values[match[1]] = match[2].replace(/^['"]|['"]$/g, "");
  }
  return values;
}

function normalize(value) {
  let text = String(value ?? "").normalize("NFKC");
  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1");
  text = text.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");
  return text.replace(/[*_`#>\\]/g, "").replace(/\s+/g, " ").trim().toLocaleLowerCase();
}

async function fetchRows(baseUrl, key) {
  const response = await fetch(`${baseUrl}/rest/v1/clinic_promotions?select=promotion_id,source_url,promotion_content,is_active&is_active=eq.true&order=promotion_id.asc&limit=3000`, {
    headers: { apikey: key, Authorization: `Bearer ${key}` },
  });
  if (!response.ok) throw new Error(`clinic_promotions returned HTTP ${response.status}`);
  return response.json();
}

const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
const baseUrl = String(env.SUPABASE_URL || "").replace(/\/$/, "");
const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
const report = JSON.parse(await readFile(REPORT_PATH, "utf8"));
const rows = await fetchRows(baseUrl, key);
const targets = new Map();

for (const row of rows) {
  const pageReport = report.results?.[row.source_url];
  if (!pageReport) continue;
  const currentClaims = new Set((row.promotion_content || []).map(normalize));
  const claims = (pageReport.claimResults || [])
    .filter(claim => claim.recordType === "promotion" && claim.status === "not_found" && currentClaims.has(normalize(claim.claim)))
    .map(claim => claim.claim);
  if (!claims.length) continue;
  const entry = targets.get(row.source_url) || { source_url: row.source_url, promotion_ids: [], claims: [] };
  entry.promotion_ids.push(Number(row.promotion_id));
  entry.claims.push(...claims);
  targets.set(row.source_url, entry);
}

let manifest = {};
try { manifest = JSON.parse(await readFile(OUTPUT_PATH, "utf8")); } catch {}

const task = await taskSpace(SPACE_ID);
const page = task.page(PAGE_LABEL);
for (const target of targets.values()) {
  if (manifest[target.source_url]?.status === "accessible") continue;
  const claims = [...new Set(target.claims)];
  const result = {
    source_url: target.source_url,
    promotion_ids: [...new Set(target.promotion_ids)],
    claims,
    status: "page_unavailable",
    snapshot_matches: [],
    checked_at: new Date().toISOString(),
  };
  try {
    await page.goto(target.source_url, { timeout: 10000, waitUntil: "domcontentloaded" });
    await page.waitForTimeout(500);
    const snapshot = String(await page.snapshot({ scope: "full_page" }));
    const normalizedSnapshot = normalize(snapshot);
    result.status = "accessible";
    result.final_url = await page.url();
    result.title = await page.title();
    result.has_iframe_snapshot = /(^|\n)\s*iframe\b/i.test(snapshot);
    result.snapshot_matches = claims.filter(claim => normalizedSnapshot.includes(normalize(claim)));
    result.unmatched_claims = claims.filter(claim => !result.snapshot_matches.some(match => normalize(match) === normalize(claim)));
  } catch (error) {
    result.error = String(error).slice(0, 1000);
  }
  manifest[target.source_url] = result;
  await writeFile(OUTPUT_PATH, JSON.stringify(manifest, null, 2), "utf8");
  console.log(JSON.stringify({ url: target.source_url, status: result.status, matches: result.snapshot_matches.length, claims: claims.length }));
}

const values = Object.values(manifest);
console.log(JSON.stringify({
  output: OUTPUT_PATH,
  urls: values.length,
  accessible: values.filter(item => item.status === "accessible").length,
  unavailable: values.filter(item => item.status !== "accessible").length,
  snapshotMatchedClaims: values.reduce((sum, item) => sum + (item.snapshot_matches || []).length, 0),
  urlsWithSnapshotMatches: values.filter(item => (item.snapshot_matches || []).length).length,
}, null, 2));
