import { readFile, writeFile } from "node:fs/promises";
import { compareCandidateSnapshot } from "./membership_repair_apply_lib.mjs";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const OUTPUT = `${ROOT}/outputs/membership_benefits_third_batch_safe_20260921.json`;
const MEMBERSHIP_IDS = [247, 452, 454, 523];
const CLASSIC_CONTEXT = "Get 1 Classic service per month: IPL, Genesis or Sandro toning, Classic Diamond Glow or Oxygeneo, Dermaplane & Peel, Dermaneedling with serum, Nano facial with PDRN, or Factor 5 Peel";
const UPGRADE_CONTEXT = "Upgrade services: $250 member price; add $75 on the day of service and $125 for an additional area";
const COMBO_CONTEXT = "Combo services: $350–$550 member price; add $175–$375 on the day of service and $200 for an additional area";

function unique(values) {
  const seen = new Set();
  return values.filter(value => {
    const normalized = String(value || "").replace(/\s+/g, " ").trim();
    if (!normalized || seen.has(normalized)) return false;
    seen.add(normalized);
    return true;
  });
}

export function buildBenefits(planId, benefits) {
  const current = Array.isArray(benefits) ? [...benefits] : [];
  if (planId === 247) {
    return unique(current.map(value => value === "10% off skin-quality treatments including SkinVive"
      ? "10% off Chemical Peels, Microneedling, PRF Treatments, and SkinVive"
      : value));
  }
  if (planId === 452) {
    return unique(current.map(value => value === "$35 IV member pricing" ? "$35 off IV Therapy" : value));
  }
  if (planId === 454) {
    return unique(current.filter(value => ![
      "Injectable Membership — 10% off regular-priced single units of injectable: filler, Sculptra, Radiesse, and fat lipolysis.",
      "Unuse dues may be BANKED and applied toward other skin laser services (Ulthera, Mixto, Neogen, Sylfirm/Morpheus, etc) OR filler packages 15% OFF Retail & 10% OFF other regular priced skin/laser services Exclude neurotoxins",
    ].includes(value)));
  }
  if (planId === 523) {
    const classic = "Get 1 Classic service per month";
    if (current.includes(CLASSIC_CONTEXT)) return unique(current);
    const remaining = current.filter(value => value !== classic);
    return [
      CLASSIC_CONTEXT,
      UPGRADE_CONTEXT,
      COMBO_CONTEXT,
      ...remaining,
    ];
  }
  throw new Error(`unsupported safe third-batch plan ${planId}`);
}

function parseEnv(text) {
  return Object.fromEntries(text.split(/\r?\n/).map(line => line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/)).filter(Boolean).map(match => [match[1], match[2].replace(/^['"]|['"]$/g, "")]));
}

async function client() {
  const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
  const baseUrl = String(env.SUPABASE_URL || "").replace(/\/$/, "");
  const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
  if (!baseUrl || !key) throw new Error("SUPABASE_URL or server key is missing");
  return { baseUrl, key };
}

async function request(api, path, options = {}) {
  const response = await fetch(`${api.baseUrl}/rest/v1/${path}`, { ...options, signal: AbortSignal.timeout(15000), headers: { apikey: api.key, Authorization: `Bearer ${api.key}`, ...(options.headers || {}) } });
  if (!response.ok) throw new Error(`Supabase REST returned HTTP ${response.status} for ${path}`);
  return response.json();
}

async function main({ apply = process.argv.includes("--apply") } = {}) {
  const api = await client();
  const q = new URLSearchParams({ select: "plan_id,business_id,membership_name,benefits,source_url,updated_at", plan_id: `in.(${MEMBERSHIP_IDS.join(",")})`, order: "plan_id.asc" });
  const memberships = await request(api, `clinic_memberships?${q}`);
  const candidates = memberships.map(row => ({ ...row, suggested_benefits: buildBenefits(Number(row.plan_id), row.benefits), evidence: "current rendered source verified in ego-browser on 2026-09-21" }));
  const result = { generatedAt: new Date().toISOString(), mode: apply ? "apply" : "dry_run", databaseWrites: 0, offerOperations: 0, schemaOperations: 0, operations: [] };

  if (!apply) {
    result.operations = candidates.map(row => ({ plan_id: row.plan_id, status: "eligible", before: row.benefits, after: row.suggested_benefits, evidence: row.evidence }));
  } else {
    for (const candidate of candidates) {
      const current = (await request(api, `clinic_memberships?select=plan_id,business_id,membership_name,benefits,source_url,updated_at&plan_id=eq.${candidate.plan_id}`))[0];
      const comparison = compareCandidateSnapshot({ ...candidate, current_benefits: candidate.benefits }, current);
      if (comparison.status !== "ready") {
        result.operations.push({ plan_id: candidate.plan_id, ...comparison });
        continue;
      }
      const now = new Date().toISOString();
      const filter = new URLSearchParams({ plan_id: `eq.${candidate.plan_id}`, updated_at: `eq.${current.updated_at}` });
      const updated = await request(api, `clinic_memberships?${filter}`, { method: "PATCH", headers: { "Content-Type": "application/json", Prefer: "return=representation" }, body: JSON.stringify({ benefits: candidate.suggested_benefits, updated_at: now }) });
      if (updated.length !== 1) throw new Error(`plan ${candidate.plan_id} update affected ${updated.length} rows`);
      result.operations.push({ plan_id: candidate.plan_id, status: "updated", before: current, after: updated[0] });
      result.databaseWrites += 1;
    }

    const offer = (await request(api, "promo_offer_master?select=id,is_active,offer_raw_text,updated_at&id=eq.7882"))[0];
    if (!offer) throw new Error("offer 7882 is missing");
    if (offer.is_active) {
      const offerFilter = new URLSearchParams({ id: "eq.7882", updated_at: `eq.${offer.updated_at}` });
      const updatedOffer = await request(api, `promo_offer_master?${offerFilter}`, { method: "PATCH", headers: { "Content-Type": "application/json", Prefer: "return=representation" }, body: JSON.stringify({ is_active: false, updated_at: new Date().toISOString() }) });
      if (updatedOffer.length !== 1) throw new Error(`offer 7882 update affected ${updatedOffer.length} rows`);
      result.operations.push({ offer_id: 7882, status: "deactivated", before: offer, after: updatedOffer[0] });
      result.offerOperations = 1;
    } else {
      result.operations.push({ offer_id: 7882, status: "already_inactive" });
    }
  }

  await writeFile(OUTPUT, JSON.stringify(result, null, 2), "utf8");
  console.log(JSON.stringify({ output: OUTPUT, mode: result.mode, databaseWrites: result.databaseWrites, offerOperations: result.offerOperations, schemaOperations: 0 }, null, 2));
  return result;
}

export { main };
if (import.meta.url === `file://${process.argv[1]}`) await main();
