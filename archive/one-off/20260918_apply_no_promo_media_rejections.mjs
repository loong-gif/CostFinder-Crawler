import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const OUTPUT = `${ROOT}/outputs/no_promo_media_rejections_20260918.json`;
const APPLY = process.argv.includes("--apply");
const TARGETS = new Map([
  [215, { source_url: "https://www.vivadayspa.com/specials", reason: "remaining_claims_not_found; visible_images_are_transparent_svg_placeholders_or_brand_assets; cookiebot_iframe_only" }],
  [850, { source_url: "https://pchmedspa.com/special-offer", reason: "no_visible_images_or_canvas; visible_iframe_is_recaptcha_only" }],
  [3587, { source_url: "https://newlookskincenter.com/monthly-specials/", reason: "visible_images_are_logos_or_app_badges; visible_iframe_is_contact_form_only" }],
]);
const DEACTIVATE_ALL_OFFERS = new Set([850, 3587]);

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) { const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/); if (match) values[match[1]] = match[2].replace(/^['"]|['"]$/g, ""); }
  return values;
}
function normalize(value) {
  let text = String(value ?? "").normalize("NFKC");
  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, " $1 ").replace(/\[([^\]]*)\]\([^)]*\)/g, " $1 ").replace(/<[^>]*>/g, " ").replace(/[–—]/g, "-").replace(/[*_~`#>\\]/g, " ");
  return text.replace(/([\p{N}])([\p{L}])/gu, "$1 $2").replace(/([\p{L}])([\p{N}])/gu, "$1 $2").replace(/[^\p{L}\p{N}$%]+/gu, " ").replace(/\s+/g, " ").trim().toLocaleLowerCase();
}
async function fetchRows(baseUrl, key, table, select, suffix = "") { const response = await fetch(`${baseUrl}/rest/v1/${table}?select=${encodeURIComponent(select)}&limit=3000${suffix}`, { headers: { apikey: key, Authorization: `Bearer ${key}` } }); if (!response.ok) throw new Error(`${table} returned HTTP ${response.status}`); return response.json(); }
async function patchOne(baseUrl, key, table, filters, body) { const response = await fetch(`${baseUrl}/rest/v1/${table}?${new URLSearchParams(filters)}`, { method: "PATCH", headers: { apikey: key, Authorization: `Bearer ${key}`, "Content-Type": "application/json", Prefer: "return=representation" }, body: JSON.stringify(body) }); if (!response.ok) throw new Error(`${table} returned HTTP ${response.status}: ${await response.text()}`); const rows = await response.json(); if (!Array.isArray(rows) || rows.length !== 1) throw new Error(`${table} update matched ${rows.length} rows`); return rows[0]; }

const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
const baseUrl = String(env.SUPABASE_URL || "").replace(/\/$/, "");
const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
const report = JSON.parse(await readFile(`${ROOT}/outputs/visibility_review_20260918/visibility_qa_reconciled_20260918.json`, "utf8"));
const [promotions, offers] = await Promise.all([
  fetchRows(baseUrl, key, "clinic_promotions", "promotion_id,source_url,promotion_content,is_active,is_offer_extracted,updated_at"),
  fetchRows(baseUrl, key, "promo_offer_master", "id,promotion_id,offer_raw_text,is_active,updated_at", "&is_active=eq.true"),
]);
const plan = { mode: APPLY ? "apply" : "dry_run", generated_at: new Date().toISOString(), evidence: Object.fromEntries(TARGETS), promotion_changes: [], offer_changes: [] };
for (const [promotionId, target] of TARGETS) {
  const row = promotions.find(item => Number(item.promotion_id) === promotionId);
  if (!row || row.is_active !== true || row.source_url !== target.source_url) continue;
  const page = report.results?.[row.source_url];
  const notFound = new Set((page?.claimResults || []).filter(claim => claim.recordType === "promotion" && claim.status === "not_found").map(claim => normalize(claim.claim)));
  const oldContent = Array.isArray(row.promotion_content) ? row.promotion_content : [];
  const removed = oldContent.filter(value => notFound.has(normalize(value)));
  if (removed.length) plan.promotion_changes.push({ promotion_id: promotionId, source_url: row.source_url, old_updated_at: row.updated_at, removed, old_content: oldContent, new_content: oldContent.filter(value => !removed.some(item => normalize(item) === normalize(value))) });
}
const changedIds = new Set(plan.promotion_changes.map(item => Number(item.promotion_id)));
for (const offer of offers) {
  const promotionId = Number(offer.promotion_id);
  if (!changedIds.has(promotionId)) continue;
  const change = plan.promotion_changes.find(item => Number(item.promotion_id) === promotionId);
  const offerText = normalize(offer.offer_raw_text);
  if (DEACTIVATE_ALL_OFFERS.has(promotionId) || change.removed.some(value => normalize(value).length >= 12 && offerText.includes(normalize(value)))) plan.offer_changes.push({ offer_id: offer.id, promotion_id: promotionId, old_updated_at: offer.updated_at, offer_raw_text: offer.offer_raw_text });
}
if (APPLY) {
  for (const change of plan.promotion_changes) { const updated = await patchOne(baseUrl, key, "clinic_promotions", { promotion_id: `eq.${change.promotion_id}`, updated_at: `eq.${change.old_updated_at}` }, { promotion_content: change.new_content, is_offer_extracted: false, updated_at: new Date().toISOString() }); change.status = "updated"; change.new_updated_at = updated.updated_at; }
  for (const change of plan.offer_changes) { const updated = await patchOne(baseUrl, key, "promo_offer_master", { id: `eq.${change.offer_id}`, updated_at: `eq.${change.old_updated_at}`, is_active: "eq.true" }, { is_active: false, updated_at: new Date().toISOString() }); change.status = "deactivated"; change.new_updated_at = updated.updated_at; }
}
await writeFile(OUTPUT, JSON.stringify(plan, null, 2), "utf8");
console.log(JSON.stringify({ mode: plan.mode, promotion_count: plan.promotion_changes.length, removed_claims: plan.promotion_changes.reduce((sum, item) => sum + item.removed.length, 0), offer_count: plan.offer_changes.length, promotion_ids: plan.promotion_changes.map(item => item.promotion_id), output: OUTPUT }, null, 2));
