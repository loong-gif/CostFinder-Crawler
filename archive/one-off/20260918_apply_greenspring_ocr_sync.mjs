import { readFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const PROMOTION_ID = 64;
const OFFER_ID = 7597;
const EXPECTED_PROMOTION_UPDATED_AT = "2026-09-18T07:51:55.537+00:00";
const EXPECTED_OFFER_UPDATED_AT = "2026-09-17T10:28:09.89395+00:00";
const CONTENT = [
  "September Specials",
  "ALL LASERS",
  "10% OFF Non-Members | 15% OFF Insiders",
  "Excludes CO2 lasers",
  "MICRONEEDLING SERIES",
  "Purchase a 3-Treatment Series",
  "Receive Complimentary Dermaplane Treatments",
  "+ 15% OFF AnteAGE® Stem Cells Add-On",
  "DIAMONDGLOW®",
  "FACIAL BALANCING",
  "Buy 3 Syringes Get the 4th FREE",
];

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (match) values[match[1]] = match[2].replace(/^['"]|['"]$/g, "");
  }
  return values;
}

async function patchRow(baseUrl, key, table, filters, body) {
  const params = new URLSearchParams(filters);
  const response = await fetch(`${baseUrl}/rest/v1/${table}?${params}`, {
    method: "PATCH",
    headers: {
      apikey: key,
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/json",
      Prefer: "return=representation",
    },
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
if (!baseUrl || !key) throw new Error("SUPABASE_URL or service key is missing");

const now = new Date().toISOString();
const promotion = await patchRow(
  baseUrl,
  key,
  "clinic_promotions",
  { promotion_id: `eq.${PROMOTION_ID}`, updated_at: `eq.${EXPECTED_PROMOTION_UPDATED_AT}` },
  { promotion_content: CONTENT, is_offer_extracted: false, is_ocr_required: false, updated_at: now },
);
const offer = await patchRow(
  baseUrl,
  key,
  "promo_offer_master",
  { id: `eq.${OFFER_ID}`, updated_at: `eq.${EXPECTED_OFFER_UPDATED_AT}`, is_active: "eq.true" },
  { is_active: false, updated_at: now },
);
console.log(JSON.stringify({ promotion_id: promotion.promotion_id, promotion_updated_at: promotion.updated_at, content_count: promotion.promotion_content.length, offer_id: offer.id, offer_active: offer.is_active }, null, 2));
