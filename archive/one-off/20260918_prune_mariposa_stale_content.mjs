import { readFile, writeFile } from "node:fs/promises";
const ROOT = "/Users/wyl/BrandRap/costfinder";
const OUTPUT = `${ROOT}/outputs/mariposa_visibility_prune_20260918.json`;
const APPLY = process.argv.includes("--apply");
const env = {};
for (const line of (await readFile(`${ROOT}/.env`, "utf8")).split(/\r?\n/)) { const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/); if (m) env[m[1]] = m[2].replace(/^['"]|['"]$/g, ""); }
const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
const base = env.SUPABASE_URL.replace(/\/$/, "");
const get = async (table, select, suffix = "") => { const r = await fetch(`${base}/rest/v1/${table}?select=${encodeURIComponent(select)}&limit=3000${suffix}`, { headers: { apikey: key, Authorization: `Bearer ${key}` }, signal: AbortSignal.timeout(20000) }); if (!r.ok) throw new Error(`${table} returned HTTP ${r.status}`); return r.json(); };
const patch = async (table, filters, body) => { const r = await fetch(`${base}/rest/v1/${table}?${new URLSearchParams(filters)}`, { method: "PATCH", headers: { apikey: key, Authorization: `Bearer ${key}`, "Content-Type": "application/json", Prefer: "return=representation" }, body: JSON.stringify(body), signal: AbortSignal.timeout(20000) }); const rows = await r.json(); if (!Array.isArray(rows) || rows.length !== 1) throw new Error(`${table} update matched ${rows.length}`); return rows[0]; };
const [promotions, offers] = await Promise.all([get("clinic_promotions", "promotion_id,source_url,promotion_content,is_active,updated_at", "&promotion_id=eq.3495"), get("promo_offer_master", "id,promotion_id,offer_raw_text,is_active,updated_at", "&is_active=eq.true&promotion_id=eq.3495")]);
const row = promotions.find(item => Number(item.promotion_id) === 3495);
const change = { promotion_id: 3495, source_url: row?.source_url, old_updated_at: row?.updated_at, old_content: row?.promotion_content || [], new_content: [], removed: row?.promotion_content || [] };
const plan = { mode: APPLY ? "apply" : "dry_run", generated_at: new Date().toISOString(), evidence_url: "https://mariposaskincare.com/collections/specials-gifts-with-purchase", evidence_summary: "current rendered page contains September skincare gifts, not the three prior medical-spa claims", promotion_changes: row?.is_active ? [change] : [], offer_changes: [] };
for (const offer of offers) if (Number(offer.promotion_id) === 3495) plan.offer_changes.push({ offer_id: offer.id, promotion_id: 3495, old_updated_at: offer.updated_at, offer_raw_text: offer.offer_raw_text });
if (APPLY && row?.is_active) { const updated = await patch("clinic_promotions", { promotion_id: "eq.3495", updated_at: `eq.${row.updated_at}` }, { promotion_content: [], is_offer_extracted: false, updated_at: new Date().toISOString() }); change.status = "updated"; change.new_updated_at = updated.updated_at; for (const offer of plan.offer_changes) { const updatedOffer = await patch("promo_offer_master", { id: `eq.${offer.offer_id}`, updated_at: `eq.${offer.old_updated_at}`, is_active: "eq.true" }, { is_active: false, updated_at: new Date().toISOString() }); offer.status = "deactivated"; offer.new_updated_at = updatedOffer.updated_at; } }
await writeFile(OUTPUT, JSON.stringify(plan, null, 2), "utf8");
console.log(JSON.stringify({ mode: plan.mode, promotion_count: plan.promotion_changes.length, removed: change.removed.length, offer_count: plan.offer_changes.length, output: OUTPUT }, null, 2));
