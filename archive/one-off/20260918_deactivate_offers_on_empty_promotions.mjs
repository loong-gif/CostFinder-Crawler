import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const OUTPUT = `${ROOT}/outputs/empty_promotion_offer_cleanup_20260918.json`;
const APPLY = process.argv.includes("--apply");

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) { const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/); if (match) values[match[1]] = match[2].replace(/^['"]|['"]$/g, ""); }
  return values;
}
const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
const base = env.SUPABASE_URL.replace(/\/$/, "");
const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
const headers = { apikey: key, Authorization: `Bearer ${key}` };
async function fetchAll(table, select, suffix = "", order = "id.asc") {
  const rows = [];
  for (let offset = 0; ; offset += 1000) {
    const response = await fetch(`${base}/rest/v1/${table}?select=${encodeURIComponent(select)}&order=${order}&limit=1000&offset=${offset}${suffix}`, { headers });
    if (!response.ok) throw new Error(`${table} returned HTTP ${response.status}`);
    const page = await response.json(); rows.push(...page); if (page.length < 1000) return rows;
  }
}
async function patchOne(item) {
  const response = await fetch(`${base}/rest/v1/promo_offer_master?${new URLSearchParams({ id: `eq.${item.id}`, updated_at: `eq.${item.old_updated_at}`, is_active: "eq.true" })}`, { method: "PATCH", headers: { ...headers, "Content-Type": "application/json", Prefer: "return=representation" }, body: JSON.stringify({ is_active: false, updated_at: new Date().toISOString() }) });
  if (!response.ok) throw new Error(`offer ${item.id} returned HTTP ${response.status}: ${await response.text()}`);
  const rows = await response.json(); if (!Array.isArray(rows) || rows.length !== 1) throw new Error(`offer ${item.id} update matched ${rows.length}`); return rows[0];
}

const [promotions, offers] = await Promise.all([
  fetchAll("clinic_promotions", "promotion_id,promotion_content,is_active,updated_at", "", "promotion_id.asc"),
  fetchAll("promo_offer_master", "id,promotion_id,offer_raw_text,is_active,updated_at", "&is_active=eq.true"),
]);
const emptyPromotionIds = new Set(promotions.filter(row => row.is_active === true && (!Array.isArray(row.promotion_content) || row.promotion_content.length === 0)).map(row => Number(row.promotion_id)));
const changes = offers.filter(offer => emptyPromotionIds.has(Number(offer.promotion_id))).map(offer => ({ id: offer.id, promotion_id: offer.promotion_id, offer_raw_text: offer.offer_raw_text, old_updated_at: offer.updated_at, status: "ready" }));
const result = { mode: APPLY ? "apply" : "dry_run", generated_at: new Date().toISOString(), empty_promotion_ids: [...emptyPromotionIds].sort((a, b) => a - b), changes };
if (APPLY) for (const change of changes) { try { const updated = await patchOne(change); change.status = "deactivated"; change.new_updated_at = updated.updated_at; } catch (error) { change.status = "conflict_or_failed"; change.error = String(error); } }
result.deactivated_count = result.changes.filter(item => item.status === "deactivated").length;
result.failed_count = result.changes.filter(item => item.status === "conflict_or_failed").length;
await writeFile(OUTPUT, JSON.stringify(result, null, 2), "utf8");
console.log(JSON.stringify({ mode: result.mode, empty_promotions: result.empty_promotion_ids.length, offers: changes.length, deactivated: result.deactivated_count, failed: result.failed_count, output: OUTPUT }, null, 2));
