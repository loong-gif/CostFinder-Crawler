import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const PROMOTION_IDS = [
  99, 101, 810, 841, 887, 888, 889, 892, 967, 3466, 3581, 3583, 3597,
  84, 107, 215, 217, 694, 815, 921, 3407, 3482, 3519, 3526, 3541,
  2, 3587, 149, 3588, 168, 3589, 201, 3590, 857, 3591, 8, 3584,
  626, 886, 3409, 147, 162, 172, 863,
];

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (match) values[match[1]] = match[2].replace(/^['"]|['"]$/g, "");
  }
  return values;
}

async function fetchRows(baseUrl, key, table, select, extra = "") {
  const rows = [];
  for (let offset = 0; ; offset += 1000) {
    const url = `${baseUrl}/rest/v1/${table}?select=${encodeURIComponent(select)}&limit=1000&offset=${offset}${extra}`;
    const response = await fetch(url, { headers: { apikey: key, Authorization: `Bearer ${key}` } });
    if (!response.ok) throw new Error(`${table} returned HTTP ${response.status}: ${await response.text()}`);
    const page = await response.json();
    rows.push(...page);
    if (page.length < 1000) return rows;
  }
}

const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
const baseUrl = String(env.SUPABASE_URL || "").replace(/\/$/, "");
const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
if (!baseUrl || !key) throw new Error("SUPABASE_URL or server key is missing");
const idFilter = `&promotion_id=in.(${PROMOTION_IDS.join(",")})`;
const promotions = await fetchRows(
  baseUrl,
  key,
  "clinic_promotions",
  "promotion_id,business_id,promotion_title,source_url,promotion_content,is_active,is_offer_extracted,is_ocr_required,requires_cf_verification,campaign_start_date,campaign_end_date,updated_at",
  idFilter,
);
const offers = await fetchRows(
  baseUrl,
  key,
  "promo_offer_master",
  "id,business_id,promotion_id,membership_plan_id,offer_raw_text,regular_price,discount_price,discount_amount,discount_percent,price_model,conditions,is_package,is_active,offer_fingerprint,updated_at",
  idFilter,
);
const offerIds = offers.map((row) => row.id).filter((id) => id != null);
const items = offerIds.length
  ? await fetchRows(baseUrl, key, "promo_offer_items", "offer_item_id,offer_id,service_id,item_name,delivered_units,unit_price,updated_at", `&offer_id=in.(${offerIds.join(",")})`)
  : [];
const capturedAt = new Date().toISOString();
const output = `${ROOT}/one-off/20260921_promotion_repair_snapshot_${capturedAt.replace(/[-:.TZ]/g, "").slice(0, 15)}.json`;
await writeFile(output, JSON.stringify({ snapshot_type: "promotion_repair_external_snapshot", captured_at: capturedAt, promotion_ids: PROMOTION_IDS, clinic_promotions: promotions, promo_offer_master: offers, promo_offer_items: items }, null, 2) + "\n");
console.log(JSON.stringify({ output, promotions: promotions.length, offers: offers.length, items: items.length }, null, 2));
