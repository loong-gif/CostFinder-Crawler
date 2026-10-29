import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const REPORT = `${ROOT}/outputs/visibility_qa_current_20260918.json`;
const OUTPUT = `${ROOT}/outputs/current_visibility_prune_20260918.json`;
const APPLY = process.argv.includes("--apply");

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
  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, " $1 ").replace(/\[([^\]]*)\]\([^)]*\)/g, " $1 ");
  text = text.replace(/<[^>]*>/g, " ").replace(/[“”]/g, '"').replace(/[‘’]/g, "'");
  text = text.replace(/[–—]/g, "-").replace(/[*_~`#>\\]/g, " ");
  text = text.replace(/([\p{N}])([\p{L}])/gu, "$1 $2").replace(/([\p{L}])([\p{N}])/gu, "$1 $2");
  return text.replace(/[^\p{L}\p{N}$%]+/gu, " ").replace(/\s+/g, " ").trim().toLocaleLowerCase();
}

async function fetchRows(baseUrl, key, table, select, suffix = "") {
  const response = await fetch(`${baseUrl}/rest/v1/${table}?select=${encodeURIComponent(select)}&limit=3000${suffix}`, { headers: { apikey: key, Authorization: `Bearer ${key}` } });
  if (!response.ok) throw new Error(`${table} returned HTTP ${response.status}`);
  return response.json();
}

async function patchOne(baseUrl, key, table, filters, body) {
  const response = await fetch(`${baseUrl}/rest/v1/${table}?${new URLSearchParams(filters)}`, {
    method: "PATCH",
    headers: { apikey: key, Authorization: `Bearer ${key}`, "Content-Type": "application/json", Prefer: "return=representation" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`${table} returned HTTP ${response.status}: ${await response.text()}`);
  const rows = await response.json();
  if (!Array.isArray(rows) || rows.length !== 1) throw new Error(`${table} update matched ${rows.length} rows`);
  return rows[0];
}

const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
const baseUrl = String(env.SUPABASE_URL || "").replace(/\/$/, "");
const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
if (!baseUrl || !key) throw new Error("SUPABASE_URL or service key is missing");

const report = JSON.parse(await readFile(REPORT, "utf8"));
const [promotions, offers, imagePages, imageOcr, tesseractRetry, egoTesseract, iframeReview, unavailableRetry] = await Promise.all([
  fetchRows(baseUrl, key, "clinic_promotions", "promotion_id,source_url,promotion_content,is_active,is_offer_extracted,updated_at"),
  fetchRows(baseUrl, key, "promo_offer_master", "id,promotion_id,offer_raw_text,is_active,updated_at", "&is_active=eq.true"),
  readFile(`${ROOT}/outputs/visibility_review_20260918/visible_image_manifest.json`, "utf8").then(JSON.parse),
  readFile(`${ROOT}/outputs/visibility_review_20260918/visible_image_ocr_manifest.json`, "utf8").then(JSON.parse),
  readFile(`${ROOT}/outputs/visibility_review_20260918/visible_image_tesseract_retry.json`, "utf8").then(JSON.parse),
  readFile(`${ROOT}/outputs/visibility_review_20260918/visible_image_ego_fetch_tesseract.json`, "utf8").then(JSON.parse),
  readFile(`${ROOT}/outputs/visibility_review_20260918/iframe_snapshot_review_v3.json`, "utf8").then(JSON.parse),
  readFile(`${ROOT}/outputs/visibility_review_20260918/unavailable_retry_current_ego.json`, "utf8").then(JSON.parse),
]);

const ocrByPage = new Map();
for (const manifest of [imageOcr, tesseractRetry.results || {}, egoTesseract.results || {}]) {
  for (const item of Object.values(manifest)) {
    if (!Array.isArray(item.pages) || !Array.isArray(item.transcript) || item.status === "ocr_failed") continue;
    for (const page of item.pages) {
      const values = ocrByPage.get(page) || [];
      values.push(item.transcript.join("\n"), ...(item.alts || []));
      ocrByPage.set(page, values);
    }
  }
}
const iframeByPage = iframeReview.results || {};

const evidence = new Map();
for (const [sourceUrl, page] of Object.entries(report.results || {})) {
  const values = evidence.get(sourceUrl) || [];
  for (const claim of page.claimResults || []) if (claim.status === "visible_match") values.push(claim.claim);
  evidence.set(sourceUrl, values);
}
for (const [sourceUrl, values] of ocrByPage) evidence.set(sourceUrl, [...(evidence.get(sourceUrl) || []), ...values]);
for (const [sourceUrl, page] of Object.entries(iframeByPage)) evidence.set(sourceUrl, [...(evidence.get(sourceUrl) || []), ...(page.snapshot_matches || [])]);
for (const [sourceUrl, page] of Object.entries(unavailableRetry.results || {})) evidence.set(sourceUrl, [...(evidence.get(sourceUrl) || []), ...(page.snapshot_matches || [])]);

const plan = { mode: APPLY ? "apply" : "dry_run", generated_at: new Date().toISOString(), criteria: "visible DOM match, visible-image OCR, or iframe/accessibility snapshot match; page-unavailable records are held", promotion_changes: [], offer_changes: [], held_page_unavailable: [] };
for (const row of promotions.filter(item => item.is_active === true)) {
  const page = report.results?.[row.source_url];
  const oldContent = Array.isArray(row.promotion_content) ? row.promotion_content : [];
  const retryPage = unavailableRetry.results?.[row.source_url];
  const effectivePage = page?.status === "page_accessible" ? page : (retryPage?.status === "accessible" ? retryPage : page);
  if (!effectivePage || (effectivePage.status !== "page_accessible" && effectivePage.status !== "accessible")) {
    plan.held_page_unavailable.push({ promotion_id: row.promotion_id, source_url: row.source_url, claim_count: oldContent.length });
    continue;
  }
  const visible = new Set((evidence.get(row.source_url) || []).map(normalize).filter(value => value.length >= 8));
  const removed = oldContent.filter(value => !visible.has(normalize(value)));
  const visuallySupported = oldContent.filter(value => visible.has(normalize(value)) && !(page.claimResults || []).some(claim => claim.claim === value && claim.status === "visible_match"));
  if (!removed.length) continue;
  plan.promotion_changes.push({ promotion_id: row.promotion_id, source_url: row.source_url, old_updated_at: row.updated_at, old_content: oldContent, new_content: oldContent.filter(value => !removed.includes(value)), removed, visually_supported: visuallySupported });
}

const changedIds = new Set(plan.promotion_changes.map(change => Number(change.promotion_id)));
for (const offer of offers) {
  const change = plan.promotion_changes.find(item => Number(item.promotion_id) === Number(offer.promotion_id));
  if (!change) continue;
  const offerText = normalize(offer.offer_raw_text);
  if (change.removed.some(value => normalize(value).length >= 12 && offerText.includes(normalize(value)))) {
    plan.offer_changes.push({ offer_id: offer.id, promotion_id: offer.promotion_id, old_updated_at: offer.updated_at, offer_raw_text: offer.offer_raw_text });
  }
}

if (APPLY) {
  for (const change of plan.promotion_changes) {
    const updated = await patchOne(baseUrl, key, "clinic_promotions", { promotion_id: `eq.${change.promotion_id}`, updated_at: `eq.${change.old_updated_at}` }, { promotion_content: change.new_content, is_offer_extracted: false, updated_at: new Date().toISOString() });
    change.status = "updated";
    change.new_updated_at = updated.updated_at;
  }
  for (const change of plan.offer_changes) {
    const updated = await patchOne(baseUrl, key, "promo_offer_master", { id: `eq.${change.offer_id}`, updated_at: `eq.${change.old_updated_at}`, is_active: "eq.true" }, { is_active: false, updated_at: new Date().toISOString() });
    change.status = "deactivated";
    change.new_updated_at = updated.updated_at;
  }
}

await writeFile(OUTPUT, JSON.stringify(plan, null, 2), "utf8");
console.log(JSON.stringify({ mode: plan.mode, promotions: plan.promotion_changes.length, removed: plan.promotion_changes.reduce((sum, item) => sum + item.removed.length, 0), offers: plan.offer_changes.length, held_page_unavailable: plan.held_page_unavailable.length, ids: plan.promotion_changes.map(item => item.promotion_id), output: OUTPUT }, null, 2));
