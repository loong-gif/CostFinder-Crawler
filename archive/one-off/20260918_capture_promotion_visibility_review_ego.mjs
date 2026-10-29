import { readFile, mkdir, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const SPACE_ID = 6;
const PAGE_LABEL = "p1";
const REPORT_PATH = `${ROOT}/outputs/visibility_qa_live_20260918.json`;
const OUTPUT_DIR = `${ROOT}/outputs/visibility_review_20260918`;
const MANIFEST_PATH = `${OUTPUT_DIR}/screenshot_manifest.json`;

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (match) values[match[1]] = match[2].replace(/^['"]|['"]$/g, "");
  }
  return values;
}

async function fetchActivePromotionIds() {
  const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
  const baseUrl = String(env.SUPABASE_URL || "").replace(/\/$/, "");
  const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
  const response = await fetch(`${baseUrl}/rest/v1/clinic_promotions?select=promotion_id,is_active&limit=1000`, {
    headers: { apikey: key, Authorization: `Bearer ${key}` },
  });
  if (!response.ok) throw new Error(`clinic_promotions returned HTTP ${response.status}`);
  return new Set((await response.json()).filter(row => row.is_active === true).map(row => Number(row.promotion_id)));
}

const report = JSON.parse(await readFile(REPORT_PATH, "utf8"));
const activeIds = await fetchActivePromotionIds();
const targets = new Map();
for (const [sourceUrl, result] of Object.entries(report.results || {})) {
  for (const claim of result.claimResults || []) {
    if (claim.recordType !== "promotion" || claim.status !== "not_found") continue;
    if (!activeIds.has(Number(claim.recordId))) continue;
    const entry = targets.get(sourceUrl) || { sourceUrl, promotionIds: new Set(), claims: [] };
    entry.promotionIds.add(Number(claim.recordId));
    entry.claims.push({ promotion_id: Number(claim.recordId), claim: claim.claim });
    targets.set(sourceUrl, entry);
  }
}

await mkdir(OUTPUT_DIR, { recursive: true });
let manifest = {};
try { manifest = JSON.parse(await readFile(MANIFEST_PATH, "utf8")); } catch { manifest = {}; }

const task = await taskSpace(SPACE_ID);
const page = task.page(PAGE_LABEL);
for (const entry of [...targets.values()].sort((a, b) => a.sourceUrl.localeCompare(b.sourceUrl))) {
  if (manifest[entry.sourceUrl]) continue;
  const safeName = entry.sourceUrl.replace(/^https?:\/\//, "").replace(/[^a-zA-Z0-9._-]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 150);
  const screenshotPath = `${OUTPUT_DIR}/${safeName}.png`;
  const result = {
    source_url: entry.sourceUrl,
    promotion_ids: [...entry.promotionIds].sort((a, b) => a - b),
    claims: entry.claims,
    screenshot_path: screenshotPath,
    status: "page_unavailable",
    checked_at: new Date().toISOString(),
  };
  process.stdout.write(`capture ${entry.sourceUrl}\n`);
  try {
    await page.goto(entry.sourceUrl);
    try { await page.waitForLoadState("load"); } catch {}
    await page.waitForTimeout(700);
    const currentUrl = await page.url();
    const title = await page.title();
    if (!currentUrl.startsWith("http") || currentUrl.includes("chrome-error")) {
      result.error = `unusable_page:${currentUrl}`;
    } else {
      await page.screenshot({ path: screenshotPath, fullPage: true });
      result.status = "captured";
      result.final_url = currentUrl;
      result.title = title;
    }
  } catch (error) {
    result.error = String(error).slice(0, 1000);
    try { await page.goto("about:blank"); } catch {}
  }
  manifest[entry.sourceUrl] = result;
  await writeFile(MANIFEST_PATH, JSON.stringify(manifest, null, 2), "utf8");
}

const values = Object.values(manifest);
console.log(JSON.stringify({ total: values.length, captured: values.filter(item => item.status === "captured").length, unavailable: values.filter(item => item.status !== "captured").length, manifest: MANIFEST_PATH }, null, 2));
