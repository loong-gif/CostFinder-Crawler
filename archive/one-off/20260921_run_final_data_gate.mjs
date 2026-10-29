import { readFile, writeFile } from "node:fs/promises";
const ROOT = "/Users/wyl/BrandRap/costfinder";
const envText = await readFile(`${ROOT}/.env`, "utf8");
const env = {};
for (const line of envText.split(/\r?\n/)) { const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/); if (match) env[match[1]] = match[2].replace(/^['"]|['"]$/g, ""); }
const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
const headers = { apikey: key, Authorization: `Bearer ${key}` };
async function get(table, select = "*") {
  const rows = [];
  for (let offset = 0; ; offset += 1000) {
    const response = await fetch(`${env.SUPABASE_URL}/rest/v1/${table}?select=${encodeURIComponent(select)}&limit=1000&offset=${offset}`, { headers });
    if (!response.ok) throw new Error(`${table}: ${response.status} ${await response.text()}`);
    const page = await response.json(); rows.push(...page); if (page.length < 1000) return rows;
  }
}
const [promotions, offers, items, services] = await Promise.all([
  get("clinic_promotions", "promotion_id,business_id,is_active,is_offer_extracted"),
  get("promo_offer_master", "id,business_id,promotion_id,is_active,price_model"),
  get("promo_offer_items", "offer_item_id,offer_id,service_id,item_name,unit_price"),
  get("clinic_services", "service_id,business_id,service_name")
]);
const pById = new Map(promotions.map(row => [row.promotion_id, row]));
const sById = new Map(services.map(row => [row.service_id, row]));
const itemsByOffer = new Map(); for (const item of items) { if (!itemsByOffer.has(item.offer_id)) itemsByOffer.set(item.offer_id, []); itemsByOffer.get(item.offer_id).push(item); }
const activeOffers = offers.filter(row => row.is_active);
const checks = {
  active_offer_without_item: activeOffers.filter(row => !(itemsByOffer.get(row.id) || []).length).length,
  active_null_service_items: activeOffers.flatMap(row => itemsByOffer.get(row.id) || []).filter(item => item.service_id == null).length,
  cross_business_mapping: activeOffers.flatMap(row => (itemsByOffer.get(row.id) || []).map(item => ({ row, item }))).filter(({ row, item }) => { const service = sById.get(item.service_id); return service && service.business_id !== row.business_id; }).length,
  item_service_name_mismatch: activeOffers.flatMap(row => (itemsByOffer.get(row.id) || []).map(item => ({ row, item }))).filter(({ item }) => { const service = sById.get(item.service_id); return service && item.item_name && service.service_name && item.item_name.trim().toLowerCase() !== service.service_name.trim().toLowerCase(); }).length,
  duplicate_offer_service_pairs: [...new Set(activeOffers.flatMap(row => (itemsByOffer.get(row.id) || []).map(item => `${row.promotion_id ?? "null"}:${item.service_id}`)))].length - new Set(activeOffers.flatMap(row => (itemsByOffer.get(row.id) || []).map(item => `${row.promotion_id ?? "null"}:${item.service_id}`))).size,
  active_offer_on_inactive_promotion: activeOffers.filter(row => row.promotion_id != null && pById.get(row.promotion_id)?.is_active === false).length,
  active_offer_on_empty_active_promotion: activeOffers.filter(row => row.promotion_id != null && pById.get(row.promotion_id)?.is_active === true && !(itemsByOffer.get(row.id) || []).length).length,
  active_per_unit_missing_unit_price: activeOffers.filter(row => row.price_model === "per_unit" && (itemsByOffer.get(row.id) || []).some(item => item.unit_price == null)).length
};
const pairCounts = new Map(); for (const row of activeOffers) for (const item of itemsByOffer.get(row.id) || []) { const key = `${row.id}:${item.service_id}`; pairCounts.set(key, (pairCounts.get(key) || 0) + 1); }
checks.duplicate_offer_service_pairs = [...pairCounts.values()].filter(count => count > 1).reduce((sum, count) => sum + count - 1, 0);
const duplicateSourceGroups = [[2,3587],[149,3588],[168,3589],[201,3590],[857,3591],[8,3584]].map(([canonical, duplicate]) => ({ canonical, duplicate, canonical_active: pById.get(canonical)?.is_active === true, duplicate_active: pById.get(duplicate)?.is_active === true }));
const unresolved = activeOffers.filter(row => row.promotion_id != null && pById.get(row.promotion_id)?.is_active !== true).map(row => row.id);
const result = { generated_at: new Date().toISOString(), counts: { promotions: promotions.length, offers: offers.length, items: items.length, services: services.length }, checks, duplicate_source_groups: duplicateSourceGroups, unresolved_active_offer_ids: unresolved };
const output = `${ROOT}/one-off/20260921_final_data_gate.json`; await writeFile(output, JSON.stringify(result, null, 2) + "\n"); console.log(JSON.stringify({ output, counts: result.counts, checks, unresolved_active_offer_ids: unresolved }, null, 2));
