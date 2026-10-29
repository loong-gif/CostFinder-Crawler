import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const OUTPUT = `${ROOT}/outputs/tesseract_eligible_prune_apply_20260918.json`;
const APPLY = process.argv.includes("--apply");
const HINT = /(special|promo|offer|sale|deal|laser|botox|filler|membership|tox|skin|facial|package|discount|gift|summer|fall|winter|spring|sept|oct|nov|dec)/i;
const SKIP = /(logo|icon|avatar|favicon|facebook|instagram|youtube|google|pixel|tracking)/i;
const DEACTIVATE_ALL_ACTIVE_OFFERS = new Set([68, 149, 3509, 920, 982]);

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (match) values[match[1]] = match[2].replace(/^['"]|['"]$/g, "");
  }
  return values;
}

function normalize(value) {
  let text = String(value || "").normalize("NFKC");
  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");
  return text.replace(/[*_`#>\\]/g, "").replace(/\s+/g, " ").trim().toLocaleLowerCase();
}

async function fetchRows(baseUrl, key, table, select, suffix = "") {
  const response = await fetch(`${baseUrl}/rest/v1/${table}?select=${encodeURIComponent(select)}&limit=3000${suffix}`, {
    headers: { apikey: key, Authorization: `Bearer ${key}` },
  });
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
const report = JSON.parse(await readFile(`${ROOT}/outputs/visibility_review_20260918/visibility_qa_reconciled_20260918.json`, "utf8"));
const media = JSON.parse(await readFile(`${ROOT}/outputs/visibility_review_20260918/visible_image_manifest.json`, "utf8"));
const ocr = JSON.parse(await readFile(`${ROOT}/outputs/visibility_review_20260918/visible_image_ocr_manifest.json`, "utf8"));
const tesseract = JSON.parse(await readFile(`${ROOT}/outputs/visibility_review_20260918/visible_image_tesseract_retry.json`, "utf8"));
const egoTesseract = JSON.parse(await readFile(`${ROOT}/outputs/visibility_review_20260918/visible_image_ego_fetch_tesseract.json`, "utf8"));
const eligibleUrls = new Set();
const imageText = new Map();
for (const [url, page] of Object.entries(media)) {
  if (page.status !== "accessible" || (page.iframes || []).length || (page.canvases || []).length) continue;
  const candidates = (page.images || []).filter(image => {
    const text = `${image.src || ""} ${image.alt || ""}`;
    return Number(image.naturalWidth || 0) >= 250 && Number(image.naturalHeight || 0) >= 100 && HINT.test(text) && !SKIP.test(text);
  });
  const complete = candidates.every(image =>
    ocr[image.src]?.status === "ocr_complete" ||
    tesseract.results?.[image.src]?.status === "tesseract_complete" ||
    egoTesseract.results?.[image.src]?.status === "tesseract_complete"
  );
  if (!complete) continue;
  eligibleUrls.add(url);
  const lines = [];
  for (const image of candidates) {
    if (ocr[image.src]?.status === "ocr_complete") lines.push(...(ocr[image.src].transcript || []));
    if (tesseract.results?.[image.src]?.status === "tesseract_complete") lines.push(...(tesseract.results[image.src].transcript || []));
    if (egoTesseract.results?.[image.src]?.status === "tesseract_complete") lines.push(...(egoTesseract.results[image.src].transcript || []));
  }
  imageText.set(url, normalize(lines.join("\n")));
}

const [promotions, offers] = await Promise.all([
  fetchRows(baseUrl, key, "clinic_promotions", "promotion_id,source_url,promotion_content,is_active,is_offer_extracted,updated_at"),
  fetchRows(baseUrl, key, "promo_offer_master", "id,promotion_id,offer_raw_text,is_active,updated_at", "&is_active=eq.true"),
]);
const plan = { mode: APPLY ? "apply" : "dry_run", generated_at: new Date().toISOString(), eligible_url_count: eligibleUrls.size, promotion_changes: [], offer_changes: [] };

for (const row of promotions) {
  if (row.is_active !== true || !eligibleUrls.has(row.source_url)) continue;
  const page = report.results?.[row.source_url];
  const rejected = new Set((page?.claimResults || [])
    .filter(claim => claim.recordType === "promotion" && claim.status === "not_found")
    .map(claim => normalize(claim.claim)));
  const visibleImageText = imageText.get(row.source_url) || "";
  const oldContent = Array.isArray(row.promotion_content) ? row.promotion_content : [];
  const removed = oldContent.filter(value => rejected.has(normalize(value)) && !visibleImageText.includes(normalize(value)));
  if (!removed.length) continue;
  plan.promotion_changes.push({ promotion_id: row.promotion_id, source_url: row.source_url, old_updated_at: row.updated_at, old_content: oldContent, new_content: oldContent.filter(value => !removed.some(item => normalize(item) === normalize(value))), removed });
}

const changedIds = new Set(plan.promotion_changes.map(change => Number(change.promotion_id)));
for (const offer of offers) {
  if (!changedIds.has(Number(offer.promotion_id))) continue;
  const change = plan.promotion_changes.find(item => Number(item.promotion_id) === Number(offer.promotion_id));
  const offerText = normalize(offer.offer_raw_text);
  if (DEACTIVATE_ALL_ACTIVE_OFFERS.has(Number(offer.promotion_id)) || change.removed.some(value => normalize(value).length >= 12 && offerText.includes(normalize(value)))) {
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
console.log(JSON.stringify({ mode: plan.mode, eligible_url_count: plan.eligible_url_count, promotion_count: plan.promotion_changes.length, offer_count: plan.offer_changes.length, promotion_ids: plan.promotion_changes.map(item => item.promotion_id), output: OUTPUT }, null, 2));
