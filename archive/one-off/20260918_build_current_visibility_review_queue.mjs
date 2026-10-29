import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const OUTPUT = `${ROOT}/outputs/visibility_review_20260918/current_active_review_queue.json`;

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
  const response = await fetch(`${baseUrl}/rest/v1/clinic_promotions?select=promotion_id,source_url,promotion_content,is_active,updated_at&is_active=eq.true&order=promotion_id.asc&limit=3000`, {
    headers: { apikey: key, Authorization: `Bearer ${key}` },
  });
  if (!response.ok) throw new Error(`clinic_promotions returned HTTP ${response.status}`);
  return response.json();
}

const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
const baseUrl = String(env.SUPABASE_URL || "").replace(/\/$/, "");
const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
const rows = await fetchRows(baseUrl, key);
const report = JSON.parse(await readFile(`${ROOT}/outputs/visibility_review_20260918/visibility_qa_reconciled_20260918.json`, "utf8"));
const media = JSON.parse(await readFile(`${ROOT}/outputs/visibility_review_20260918/visible_image_manifest.json`, "utf8"));
const ocr = JSON.parse(await readFile(`${ROOT}/outputs/visibility_review_20260918/visible_image_ocr_manifest.json`, "utf8"));
const tesseract = JSON.parse(await readFile(`${ROOT}/outputs/visibility_review_20260918/visible_image_tesseract_retry.json`, "utf8"));
const egoTesseract = JSON.parse(await readFile(`${ROOT}/outputs/visibility_review_20260918/visible_image_ego_fetch_tesseract.json`, "utf8"));

const queue = [];
for (const row of rows) {
  const page = report.results?.[row.source_url];
  if (!page) continue;
  const content = new Set((row.promotion_content || []).map(normalize));
  const claims = (page.claimResults || []).filter(claim =>
    claim.recordType === "promotion" &&
    claim.status === "not_found" &&
    content.has(normalize(claim.claim))
  );
  if (!claims.length) continue;
  const mediaPage = media[row.source_url] || {};
  const candidates = (mediaPage.images || []).filter(image =>
    Number(image.naturalWidth || 0) >= 250 && Number(image.naturalHeight || 0) >= 100
  );
  const ocrComplete = image =>
    ocr[image.src]?.status === "ocr_complete" ||
    tesseract.results?.[image.src]?.status === "tesseract_complete" ||
    egoTesseract.results?.[image.src]?.status === "tesseract_complete";
  let reason = "visual_unmatched";
  if (page.status !== "page_accessible") reason = "page_unavailable";
  else if ((mediaPage.iframes || []).length) reason = "iframe_or_dynamic";
  else if (candidates.some(image => !ocrComplete(image))) reason = "visual_ocr_incomplete";
  else if (candidates.length) reason = "visual_unmatched_after_ocr";
  else if (!mediaPage.images && !mediaPage.canvases) reason = "visual_evidence_missing";
  for (const claim of claims) {
    queue.push({
      promotion_id: Number(row.promotion_id),
      source_url: row.source_url,
      claim: claim.claim,
      reason,
      updated_at: row.updated_at,
    });
  }
}

const summary = {};
for (const item of queue) summary[item.reason] = (summary[item.reason] || 0) + 1;
await writeFile(OUTPUT, JSON.stringify({ generated_at: new Date().toISOString(), summary, queue }, null, 2), "utf8");
console.log(JSON.stringify({ output: OUTPUT, summary, claim_count: queue.length, promotion_count: new Set(queue.map(item => item.promotion_id)).size }, null, 2));
