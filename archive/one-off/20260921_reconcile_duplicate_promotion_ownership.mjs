import { readFile, writeFile } from "node:fs/promises";
const ROOT = "/Users/wyl/BrandRap/costfinder";
function parseEnv(text) { const values = {}; for (const line of text.split(/\r?\n/)) { const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/); if (m) values[m[1]] = m[2].replace(/^['"]|['"]$/g, ""); } return values; }
async function rows(base, key, table, select, extra) { const r = await fetch(`${base}/rest/v1/${table}?select=${encodeURIComponent(select)}&limit=2000${extra}`, { headers: { apikey: key, Authorization: `Bearer ${key}` } }); if (!r.ok) throw new Error(`${table}: ${await r.text()}`); return r.json(); }
async function patchRow(base, key, table, filters, body) { const r = await fetch(`${base}/rest/v1/${table}?${new URLSearchParams(filters)}`, { method: "PATCH", headers: { apikey: key, Authorization: `Bearer ${key}`, "Content-Type": "application/json", Prefer: "return=representation" }, body: JSON.stringify({ ...body, updated_at: new Date().toISOString() }) }); if (!r.ok) throw new Error(`${table}: ${await r.text()}`); const result = await r.json(); if (result.length !== 1) throw new Error(`${table} matched ${result.length}`); return result[0]; }
const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8")); const base = String(env.SUPABASE_URL || "").replace(/\/$/, ""); const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY; if (!base || !key) throw new Error("Supabase credentials missing");
const targetOffers = [8556, 8559, 8560, 8562, 8563, 8566, 8567, 8568, 8575, 8576, 8587, 8589, 8590, 8591, 8594, 8596, 8599, 8600, 8602, 10175, 10176, 10177, 10180];
const offers = await rows(base, key, "promo_offer_master", "id,promotion_id,offer_raw_text,is_active,updated_at", `&id=in.(${targetOffers.join(",")})`);
const moveToTwo = new Set([8562, 8568, 8576, 8587, 8590, 8591, 8594, 8600]);
const deactivateOfferIds = new Set([8556, 8559, 8560, 8563, 8566, 8567, 8575, 8589, 8596, 8599, 8602, 10175, 10176, 10177, 10180]);
const result = { mode: process.argv.includes("--apply") ? "apply" : "dry_run", moved: [], deactivated_offers: [], deactivated_promotions: [], conflicts: [] };
for (const offer of offers) {
  if (!offer.is_active) continue;
  const action = moveToTwo.has(Number(offer.id)) ? "move" : deactivateOfferIds.has(Number(offer.id)) ? "deactivate" : null;
  if (!action) continue;
  const item = { offer_id: offer.id, from_promotion_id: offer.promotion_id, offer_raw_text: offer.offer_raw_text, action };
  if (result.mode === "apply") {
    try {
      const updated = await patchRow(base, key, "promo_offer_master", { id: `eq.${offer.id}`, promotion_id: `eq.${offer.promotion_id}`, is_active: "eq.true", updated_at: `eq.${offer.updated_at}` }, action === "move" ? { promotion_id: 2 } : { is_active: false });
      item.new_promotion_id = updated.promotion_id; item.new_is_active = updated.is_active; item.new_updated_at = updated.updated_at;
    } catch (error) { item.status = "conflict_or_failure"; item.error = String(error); result.conflicts.push(item); continue; }
  }
  (action === "move" ? result.moved : result.deactivated_offers).push(item);
}
const duplicatePromotionIds = [3587, 3588, 3589, 3590, 3591, 3584];
const promotionRows = await rows(base, key, "clinic_promotions", "promotion_id,promotion_title,promotion_content,is_active,updated_at", `&promotion_id=in.(${[2,149,168,201,857,8,...duplicatePromotionIds].join(",")})`);
const complete149 = [
  "Birthday Month monthly specials: September 1–30, 2026.",
  "Vault members: Botox $9.50/unit, Jeuveau $9.00/unit, Daxxify $6.00/unit, and Dysport $5.00/unit.",
  "Non-members: Botox $10.00/unit, Jeuveau $9.50/unit, Daxxify $6.50/unit, and Dysport $5.50/unit.",
  "New Dysport clients who purchase 50 or more units receive two treatment areas free.",
  "Filler: buy one Juvederm filler, get one free.",
  "Sculptra: buy 2 vials, get the 3rd 50% off; buy 3 vials, get the 4th free.",
  "Laser hair removal: underarms, 6 treatments $540; chin, 6 treatments $315; legs, 6 treatments $1,800; Brazilian, 6 treatments $1,200.",
  "HALO: face or chest plus Alastin Regenerating Skin Nectar for $1,570; savings of $245.",
];
if (result.mode === "apply") {
  const canonical149 = promotionRows.find((row) => Number(row.promotion_id) === 149);
  if (!canonical149) throw new Error("canonical 149 missing");
  const updated = await patchRow(base, key, "clinic_promotions", { promotion_id: "eq.149", updated_at: `eq.${canonical149.updated_at}` }, { promotion_title: "September Specials | Skinjectables", promotion_content: complete149, is_offer_extracted: false });
  result.canonical_149_updated_at = updated.updated_at;
  for (const id of duplicatePromotionIds) {
    const row = promotionRows.find((candidate) => Number(candidate.promotion_id) === id);
    if (!row || row.is_active !== true) continue;
    const updatedDuplicate = await patchRow(base, key, "clinic_promotions", { promotion_id: `eq.${id}`, is_active: "eq.true", updated_at: `eq.${row.updated_at}` }, { is_active: false, is_offer_extracted: false });
    result.deactivated_promotions.push({ promotion_id: id, updated_at: updatedDuplicate.updated_at });
  }
} else {
  result.canonical_149_proposed = { promotion_id: 149, promotion_title: "September Specials | Skinjectables", promotion_content: complete149 };
  result.deactivated_promotions = duplicatePromotionIds.map((id) => ({ promotion_id: id, status: "ready_if_active" }));
}
const output = `${ROOT}/one-off/20260921_duplicate_promotion_ownership_${result.mode}.json`; await writeFile(output, JSON.stringify(result, null, 2) + "\n"); console.log(JSON.stringify({ mode: result.mode, moved: result.moved.length, deactivated_offers: result.deactivated_offers.length, deactivated_promotions: result.deactivated_promotions.length, conflicts: result.conflicts.length, output }, null, 2));
