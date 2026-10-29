import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const OUTPUT = `${ROOT}/outputs/biorevive_visible_product_sync_20260918.json`;
const APPLY = process.argv.includes("--apply");
const PROMOTION_ID = 819;
const SOURCE_URL = "https://biorevivespa.com/collections/special-offers-best-deals";
const VISIBLE_CONTENT = [
  "Hydrafacial® + LED Light Therapy | First Treatment Promo — Regular price $250.00 USD; Sale price $149.00 USD",
  "HydraFacial® + CryoSkin™️ Facial Duo | First Treatment Promo — Regular price $600.00 USD; Sale price $199.00 USD",
  "Full Face & Neck HIFU Treatment | Non-Surgical Facelift — Regular price $1,000.00 USD; Sale price $395.00 USD",
  "Morpheus8 Burst | Full Face | 60% OFF | 1st Treatment Offer in Newport Beach — Regular price $1,250.00 USD; Sale price $495.00 USD",
  "Cool Peel Laser Treatment for Face | $499 Promo (Regular $1000) — Regular price $1,000.00 USD; Sale price $499.00 USD",
  "Microneedling + Exosome First Treatment Offer – $599 (Regular $1000) — Regular price $1,000.00 USD; Sale price $599.00 USD",
  "XERF™ FULL FACE SPECIAL — Regular price $2,500.00 USD; Sale price $1,800.00 USD",
];

function parseEnv(text) { const values = {}; for (const line of text.split(/\r?\n/)) { const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/); if (match) values[match[1]] = match[2].replace(/^['"]|['"]$/g, ""); } return values; }
async function fetchRow(baseUrl, key) { const response = await fetch(`${baseUrl}/rest/v1/clinic_promotions?select=promotion_id,source_url,promotion_content,is_active,is_offer_extracted,updated_at&promotion_id=eq.${PROMOTION_ID}`, { headers: { apikey: key, Authorization: `Bearer ${key}` } }); if (!response.ok) throw new Error(`clinic_promotions returned HTTP ${response.status}`); const rows = await response.json(); if (rows.length !== 1) throw new Error(`promotion ${PROMOTION_ID} returned ${rows.length} rows`); return rows[0]; }
async function patchOne(baseUrl, key, row) { const response = await fetch(`${baseUrl}/rest/v1/clinic_promotions?promotion_id=eq.${PROMOTION_ID}&updated_at=eq.${encodeURIComponent(row.updated_at)}`, { method: "PATCH", headers: { apikey: key, Authorization: `Bearer ${key}`, "Content-Type": "application/json", Prefer: "return=representation" }, body: JSON.stringify({ promotion_content: VISIBLE_CONTENT, is_offer_extracted: false, updated_at: new Date().toISOString() }) }); if (!response.ok) throw new Error(`clinic_promotions update failed: ${await response.text()}`); const rows = await response.json(); if (rows.length !== 1) throw new Error(`promotion update matched ${rows.length} rows`); return rows[0]; }

const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
const baseUrl = String(env.SUPABASE_URL || "").replace(/\/$/, "");
const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
const row = await fetchRow(baseUrl, key);
if (row.source_url !== SOURCE_URL || row.is_active !== true) throw new Error(`promotion ${PROMOTION_ID} source/status mismatch`);
const plan = { mode: APPLY ? "apply" : "dry_run", generated_at: new Date().toISOString(), promotion_id: PROMOTION_ID, source_url: SOURCE_URL, old_updated_at: row.updated_at, old_content: row.promotion_content, new_content: VISIBLE_CONTENT };
if (APPLY) { const updated = await patchOne(baseUrl, key, row); plan.status = "updated"; plan.new_updated_at = updated.updated_at; plan.readback_content = updated.promotion_content; }
await writeFile(OUTPUT, JSON.stringify(plan, null, 2), "utf8");
console.log(JSON.stringify({ mode: plan.mode, promotion_id: PROMOTION_ID, old_count: (row.promotion_content || []).length, new_count: VISIBLE_CONTENT.length, output: OUTPUT }, null, 2));
