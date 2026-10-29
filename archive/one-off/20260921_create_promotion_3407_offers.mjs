import { readFile, writeFile } from "node:fs/promises";
const ROOT = "/Users/wyl/BrandRap/costfinder";
function parseEnv(text) { const values = {}; for (const line of text.split(/\r?\n/)) { const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/); if (m) values[m[1]] = m[2].replace(/^['"]|['"]$/g, ""); } return values; }
const specs = [
  { raw: "BOTOX: $150 off; minimum 30 units; Brilliant Distinction & Inspire patients.", service_id: 2284, item_name: "Botox", delivered_units: null },
  { raw: "Dermal Filler: $150 off one syringe; Brilliant Distinction & Inspire patients.", service_id: 3251, item_name: "Dermal Filler", delivered_units: 1 },
];
const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8")); const base = String(env.SUPABASE_URL || "").replace(/\/$/, ""); const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY; if (!base || !key) throw new Error("Supabase credentials missing");
const headers = { apikey: key, Authorization: `Bearer ${key}`, "Content-Type": "application/json" };
async function get(url) { const r = await fetch(url, { headers }); if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`); return r.json(); }
async function post(table, body) { const r = await fetch(`${base}/rest/v1/${table}`, { method: "POST", headers: { ...headers, Prefer: "return=representation" }, body: JSON.stringify(body) }); if (!r.ok) throw new Error(`${table} ${r.status}: ${await r.text()}`); const rows = await r.json(); if (rows.length !== 1) throw new Error(`${table} inserted ${rows.length}`); return rows[0]; }
async function deactivateMaster(id) { const r = await fetch(`${base}/rest/v1/promo_offer_master?id=eq.${id}`, { method: "PATCH", headers: { ...headers, Prefer: "return=representation" }, body: JSON.stringify({ is_active: false, updated_at: new Date().toISOString() }) }); if (!r.ok) throw new Error(`rollback ${id}: ${await r.text()}`); }
const result = { mode: process.argv.includes("--apply") ? "apply" : "dry_run", specs, created_offers: [], created_items: [], errors: [] };
if (result.mode === "apply") {
  const promotion = (await get(`${base}/rest/v1/clinic_promotions?select=promotion_id,business_id,is_active&promotion_id=eq.3407`))[0];
  if (!promotion || promotion.business_id !== 3181 || promotion.is_active !== true) throw new Error("Promotion 3407 precondition failed");
  for (const spec of specs) {
    let master = null;
    try {
      master = await post("promo_offer_master", { business_id: 3181, promotion_id: 3407, offer_raw_text: spec.raw, regular_price: null, discount_price: null, discount_amount: 150, discount_percent: null, is_membership_required: false, is_new_customer_required: false, price_model: "total", conditions: ["Brilliant Distinction & Inspire patients", ...(spec.delivered_units === null ? ["Minimum 30 units purchased"] : ["One syringe"])], is_package: false, is_active: true });
      result.created_offers.push({ id: master.id, raw: master.offer_raw_text });
      const item = await post("promo_offer_items", { offer_id: master.id, service_id: spec.service_id, item_name: spec.item_name, delivered_units: spec.delivered_units, unit_price: null });
      result.created_items.push({ id: item.offer_item_id, offer_id: item.offer_id });
    } catch (error) {
      result.errors.push({ spec, error: String(error) });
      if (master?.id) await deactivateMaster(master.id);
      break;
    }
  }
}
const output = `${ROOT}/one-off/20260921_create_3407_offers_${result.mode}.json`; await writeFile(output, JSON.stringify(result, null, 2) + "\n"); console.log(JSON.stringify({ mode: result.mode, created_offers: result.created_offers.length, created_items: result.created_items.length, errors: result.errors.length, output }, null, 2));
