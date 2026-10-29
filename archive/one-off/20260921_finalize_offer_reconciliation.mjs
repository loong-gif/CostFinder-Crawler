import { readFile, writeFile } from "node:fs/promises";
const ROOT = "/Users/wyl/BrandRap/costfinder";
function parseEnv(text) { const values = {}; for (const line of text.split(/\r?\n/)) { const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/); if (m) values[m[1]] = m[2].replace(/^['"]|['"]$/g, ""); } return values; }
async function readRows(base, key, table, select, extra) { const r = await fetch(`${base}/rest/v1/${table}?select=${encodeURIComponent(select)}&limit=2000${extra}`, { headers: { apikey: key, Authorization: `Bearer ${key}` } }); if (!r.ok) throw new Error(await r.text()); return r.json(); }
async function patchRow(base, key, table, filters, body) { const r = await fetch(`${base}/rest/v1/${table}?${new URLSearchParams(filters)}`, { method: "PATCH", headers: { apikey: key, Authorization: `Bearer ${key}`, "Content-Type": "application/json", Prefer: "return=representation" }, body: JSON.stringify({ ...body, updated_at: new Date().toISOString() }) }); if (!r.ok) throw new Error(await r.text()); const rows = await r.json(); if (rows.length !== 1) throw new Error(`${table} matched ${rows.length}`); return rows[0]; }
const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8")); const base = String(env.SUPABASE_URL || "").replace(/\/$/, ""); const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY; if (!base || !key) throw new Error("Supabase credentials missing");
const offerIds = [10524, 10525, 10370, 10578, 7523];
const offers = await readRows(base, key, "promo_offer_master", "id,promotion_id,is_active,offer_raw_text,updated_at", `&id=in.(${offerIds.join(",")})`);
const completePromotions = new Set([2, 8, 99, 107, 149, 168, 201, 215, 217, 3482, 3519, 694, 815, 857]);
const result = { mode: process.argv.includes("--apply") ? "apply" : "dry_run", deactivated_offers: [], promotion_flags: [], conflicts: [] };
for (const offer of offers) {
  if (offer.is_active !== true) continue;
  const item = { offer_id: offer.id, promotion_id: offer.promotion_id, offer_raw_text: offer.offer_raw_text };
  if (result.mode === "apply") {
    try { const updated = await patchRow(base, key, "promo_offer_master", { id: `eq.${offer.id}`, is_active: "eq.true", updated_at: `eq.${offer.updated_at}` }, { is_active: false }); item.updated_at = updated.updated_at; item.is_active = updated.is_active; }
    catch (error) { item.status = "conflict_or_failure"; item.error = String(error); result.conflicts.push(item); continue; }
  }
  result.deactivated_offers.push(item);
}
const promotions = await readRows(base, key, "clinic_promotions", "promotion_id,is_active,is_offer_extracted,updated_at", `&promotion_id=in.(${[...completePromotions].join(",")})`);
for (const row of promotions) {
  if (row.is_active !== true || row.is_offer_extracted === true) continue;
  const item = { promotion_id: row.promotion_id, old_updated_at: row.updated_at, is_offer_extracted: true };
  if (result.mode === "apply") {
    try { const updated = await patchRow(base, key, "clinic_promotions", { promotion_id: `eq.${row.promotion_id}`, is_active: "eq.true", updated_at: `eq.${row.updated_at}` }, { is_offer_extracted: true }); item.updated_at = updated.updated_at; }
    catch (error) { item.status = "conflict_or_failure"; item.error = String(error); result.conflicts.push(item); continue; }
  }
  result.promotion_flags.push(item);
}
const output = `${ROOT}/one-off/20260921_offer_reconciliation_${result.mode}.json`; await writeFile(output, JSON.stringify(result, null, 2) + "\n"); console.log(JSON.stringify({ mode: result.mode, deactivated_offers: result.deactivated_offers.length, promotion_flags: result.promotion_flags.length, conflicts: result.conflicts.length, output }, null, 2));
