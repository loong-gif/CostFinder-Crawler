import { readFile } from "node:fs/promises";
const ROOT = "/Users/wyl/BrandRap/costfinder";
const text = await readFile(`${ROOT}/.env`, "utf8"); const env = {};
for (const line of text.split(/\r?\n/)) { const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/); if (match) env[match[1]] = match[2].replace(/^['"]|['"]$/g, ""); }
const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
const headers = { apikey: key, Authorization: `Bearer ${key}`, "Content-Type": "application/json" };
async function get(url) { const response = await fetch(url, { headers }); if (!response.ok) throw new Error(await response.text()); const rows = await response.json(); if (rows.length !== 1) throw new Error(`expected one row, got ${rows.length}`); return rows[0]; }
async function patch(url, body) { const response = await fetch(url, { method: "PATCH", headers: { ...headers, Prefer: "return=representation" }, body: JSON.stringify(body) }); if (!response.ok) throw new Error(await response.text()); const rows = await response.json(); if (rows.length !== 1) throw new Error(`expected one updated row, got ${rows.length}`); return rows[0]; }
const base = String(env.SUPABASE_URL).replace(/\/$/, "");
const promotion = await get(`${base}/rest/v1/clinic_promotions?promotion_id=eq.3407&select=promotion_id,is_active,is_offer_extracted,updated_at`);
if (promotion.is_active !== true || promotion.is_offer_extracted !== false) throw new Error("3407 precondition failed");
const updatedPromotion = await patch(`${base}/rest/v1/clinic_promotions?promotion_id=eq.3407&updated_at=eq.${encodeURIComponent(promotion.updated_at)}`, { is_offer_extracted: true, updated_at: new Date().toISOString() });
const result = { promotion_id: 3407, promotion_updated_at: updatedPromotion.updated_at, offers: [] };
for (const id of [10702, 10703]) {
  const offer = await get(`${base}/rest/v1/promo_offer_master?id=eq.${id}&select=id,promotion_id,is_active,updated_at`);
  if (offer.promotion_id !== 3407 || offer.is_active !== false) throw new Error(`offer ${id} precondition failed`);
  const updated = await patch(`${base}/rest/v1/promo_offer_master?id=eq.${id}&is_active=eq.false&updated_at=eq.${encodeURIComponent(offer.updated_at)}`, { is_active: true, updated_at: new Date().toISOString() });
  result.offers.push({ id: updated.id, is_active: updated.is_active, updated_at: updated.updated_at });
}
console.log(JSON.stringify(result, null, 2));
