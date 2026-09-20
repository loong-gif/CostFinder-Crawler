import { readFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const SOURCE_URLS = new Set([
  "https://advancedaesthetics.com/fall-specials",
  "https://bbspamed.com/special-offers",
  "https://relivehendersonville.com/portfolio-category/specials",
]);

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (match) values[match[1]] = match[2].replace(/^['"]|['"]$/g, "");
  }
  return values;
}

async function fetchRows(baseUrl, key, table, select, extra = "") {
  const response = await fetch(`${baseUrl}/rest/v1/${table}?select=${encodeURIComponent(select)}&limit=2000${extra}`, { headers: { apikey: key, Authorization: `Bearer ${key}` } });
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
const report = JSON.parse(await readFile(`${ROOT}/outputs/visibility_qa_live_20260918.json`, "utf8"));
const promotions = await fetchRows(baseUrl, key, "clinic_promotions", "promotion_id,source_url,promotion_content,is_active,is_offer_extracted,updated_at");
const offers = await fetchRows(baseUrl, key, "promo_offer_master", "id,promotion_id,is_active,updated_at", "&is_active=eq.true");
const promotionChanges = [];
for (const row of promotions) {
  if (!SOURCE_URLS.has(row.source_url) || row.is_active !== true) continue;
  const page = report.results[row.source_url];
  const rejected = new Set((page?.claimResults || []).filter(claim => claim.recordType === "promotion" && ["not_found", "dom_only_hidden"].includes(claim.status)).map(claim => String(claim.claim)));
  const oldContent = Array.isArray(row.promotion_content) ? row.promotion_content : [];
  const newContent = oldContent.filter(value => !rejected.has(String(value)));
  const removed = oldContent.filter(value => rejected.has(String(value)));
  if (!removed.length) continue;
  const updated = await patchOne(baseUrl, key, "clinic_promotions", { promotion_id: `eq.${row.promotion_id}`, updated_at: `eq.${row.updated_at}` }, { promotion_content: newContent, is_offer_extracted: false, updated_at: new Date().toISOString() });
  promotionChanges.push({ promotion_id: row.promotion_id, source_url: row.source_url, removed, remaining: updated.promotion_content });
}

const offerChanges = [];
for (const offer of offers) {
  const promotion = promotions.find(row => Number(row.promotion_id) === Number(offer.promotion_id));
  if (!promotion || !SOURCE_URLS.has(promotion.source_url)) continue;
  const updated = await patchOne(baseUrl, key, "promo_offer_master", { id: `eq.${offer.id}`, updated_at: `eq.${offer.updated_at}`, is_active: "eq.true" }, { is_active: false, updated_at: new Date().toISOString() });
  offerChanges.push({ offer_id: offer.id, promotion_id: offer.promotion_id, is_active: updated.is_active });
}

console.log(JSON.stringify({ promotionChanges, offerChanges }, null, 2));
