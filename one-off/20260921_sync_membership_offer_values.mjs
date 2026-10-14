import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const OUTPUT = `${ROOT}/outputs/membership_offer_value_sync_20260921.json`;
const OFFER_ID = 8800;
const DESIRED_TEXT = "10% OFF SkinVive (regularly $450)";

export function desiredOfferText(offer) {
  return offer.offer_raw_text === "SkinVive (regularly $450)" ? DESIRED_TEXT : offer.offer_raw_text;
}

function parseEnv(text) {
  return Object.fromEntries(text.split(/\r?\n/).map(line => line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/)).filter(Boolean).map(match => [match[1], match[2].replace(/^['"]|['"]$/g, "")]));
}

async function loadClient() {
  const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
  const baseUrl = String(env.SUPABASE_URL || "").replace(/\/$/, "");
  const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
  if (!baseUrl || !key) throw new Error("SUPABASE_URL or server key is missing");
  return { baseUrl, key };
}

async function request(client, path, options = {}) {
  const response = await fetch(`${client.baseUrl}/rest/v1/${path}`, { ...options, signal: AbortSignal.timeout(15000), headers: { apikey: client.key, Authorization: `Bearer ${client.key}`, ...(options.headers || {}) } });
  if (!response.ok) throw new Error(`Supabase REST returned HTTP ${response.status} for ${path}`);
  return response.json();
}

export async function main({ apply = process.argv.includes("--apply") } = {}) {
  const client = await loadClient();
  const rows = await request(client, `promo_offer_master?select=id,membership_plan_id,is_active,offer_raw_text,regular_price,discount_percent,price_model,updated_at&id=eq.${OFFER_ID}`);
  if (rows.length !== 1) throw new Error(`expected one Offer ${OFFER_ID}, got ${rows.length}`);
  const offer = rows[0];
  const nextText = desiredOfferText(offer);
  const result = { generatedAt: new Date().toISOString(), mode: apply ? "apply" : "dry_run", databaseWrites: 0, offerOperations: 0, schemaOperations: 0, operation: null };
  if (nextText === offer.offer_raw_text) {
    result.operation = { offer_id: OFFER_ID, status: "already_applied" };
  } else if (!apply) {
    result.operation = { offer_id: OFFER_ID, status: "eligible", before: offer, after: { ...offer, offer_raw_text: nextText } };
  } else {
    const filter = new URLSearchParams({ id: `eq.${OFFER_ID}`, updated_at: `eq.${offer.updated_at}` });
    const updated = await request(client, `promo_offer_master?${filter}`, { method: "PATCH", headers: { "Content-Type": "application/json", Prefer: "return=representation" }, body: JSON.stringify({ offer_raw_text: nextText, updated_at: new Date().toISOString() }) });
    if (updated.length !== 1) throw new Error(`offer ${OFFER_ID} update affected ${updated.length} rows`);
    result.operation = { offer_id: OFFER_ID, status: "updated", before: offer, after: updated[0] };
    result.databaseWrites = 1;
    result.offerOperations = 1;
  }
  await writeFile(OUTPUT, JSON.stringify(result, null, 2), "utf8");
  console.log(JSON.stringify({ output: OUTPUT, mode: result.mode, databaseWrites: result.databaseWrites, offerOperations: result.offerOperations, schemaOperations: 0 }, null, 2));
  return result;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
