import { readFile, writeFile } from "node:fs/promises";
import { compareCandidateSnapshot } from "./membership_repair_apply_lib.mjs";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const OUTPUT = `${ROOT}/outputs/membership_benefits_second_batch_20260921.json`;
const PLAN_IDS = [342, 343, 344, 345, 392, 393, 529, 530, 531];

const CHOICES = {
  342: "Choose one monthly treatment: Dermaplane Facial, Go + Glow, Cold Plasma, Microderm, or Custom Medical Facial",
  343: "Choose one monthly treatment: Dermaplane Facial, Go + Glow, Cold Plasma, Microderm, or Custom Medical Facial",
  344: "Choose one monthly treatment: The Perfect Derma Peel, Skinwave, Target Cool Infusion Facial, Microneedling, 60 min Bespoke Facial, IPL, or LHR Lip, Chin & Underarm",
  345: "Choose one monthly treatment: The Perfect Derma Peel, Skinwave, Target Cool Infusion Facial, Microneedling, 60 min Bespoke Facial, IPL, or LHR Lip, Chin & Underarm",
  529: "Choose one monthly treatment: IV Nutrient Infusion Therapy, Laser Hair Removal Session of a Small Area (Bikini, Underarm or Chin & Upper Lip), or Jan Marini Facial of Your Choice with Dermaplaning",
  530: "Choose one monthly treatment: Chemical Peel, Facial Rejuvenation Treatment with the Lightpod Neo Elite, Microneedling Treatment, Deluxe Hydrafacial, Stellar M22 IPL Treatment, Jan Marini Starter Kit, Laser Hair Removal Session of a Medium Area, or NuEra Tight Face & Neck Maintenance Treatment",
  531: "Choose one monthly treatment: Four 40-Minute Fat and Cellulite Reduction Treatments with the Verju Laser, Radiofrequency Microneedling, Laser Hair Removal Session for Full Legs, or Microneedling Treatment with PRP + Deluxe Hydrafacial",
};

const REMOVE_GROUP_ITEMS = {
  342: ["Choose one monthly treatment: Dermaplane Facial", "Go + Glow", "Cold Plasma", "Microderm", "Custom Medical Facial"],
  343: ["Choose one monthly treatment: Dermaplane Facial", "Go + Glow", "Cold Plasma", "Microderm", "Custom Medical Facial"],
  344: ["Choose one monthly treatment: The Perfect Derma Peel", "Skinwave", "Target Cool Infusion Facial", "Microneedling", "60 min Bespoke Facial", "IPL", "LHR Lip, Chin & Underarm"],
  345: ["Choose one monthly treatment: The Perfect Derma Peel", "Skinwave", "Target Cool Infusion Facial", "Microneedling", "60 min Bespoke Facial", "IPL", "LHR Lip, Chin & Underarm"],
  529: ["1 IV Nutrient Infusion Therapy", "1 Laser Hair Removal Session of a Small Area: Bikini, Underarm or Chin & Upper Lip", "1 Jan Marini Facial of Your Choice with Dermaplaning"],
  530: ["1 Chemical Peel", "1 Facial Rejuvenation Treatment with the Lightpod Neo Elite", "1 Microneedling Treatment", "1 Deluxe Hydrafacial", "1 Stellar M22 IPL Treatment", "1 Jan Marini Starter Kit", "1 Laser Hair Removal Session of a Medium Area", "1 NuEra Tight Face & Neck Maintenance Treatment"],
  531: ["Four 40-Minute Fat and Cellulite Reduction Treatments with the Verju Laser", "1 Radiofrequency Microneedling", "1 Laser Hair Removal Session for Full Legs", "1 Microneedling Treatment with PRP + 1 Deluxe Hydrafacial"],
};

function key(value) {
  return String(value || "").replace(/\s+/g, " ").trim().toLocaleLowerCase();
}

function dedupe(values) {
  const seen = new Set();
  return values.filter(value => {
    const text = String(value || "").trim();
    const normalized = key(text);
    if (!normalized || seen.has(normalized)) return false;
    seen.add(normalized);
    return true;
  });
}

export function buildDesiredBenefits(planId, currentBenefits) {
  const current = Array.isArray(currentBenefits) ? currentBenefits : [];
  if (CHOICES[planId]) {
    const removals = new Set((REMOVE_GROUP_ITEMS[planId] || []).map(key));
    const firstRemovedIndex = current.findIndex(value => removals.has(key(value)));
    const remaining = current.filter(value => !removals.has(key(value)));
    const choice = planId === 530
      ? `${CHOICES[planId]} (only after completing 4 weekly sessions)`
      : CHOICES[planId];
    const insertAt = firstRemovedIndex < 0
      ? remaining.length
      : current.slice(0, firstRemovedIndex).filter(value => !removals.has(key(value))).length;
    remaining.splice(insertAt, 0, choice);
    return dedupe(remaining);
  }
  if ([392, 393].includes(planId)) {
    return dedupe([...current, "Monthly membership payment is placed into your account to use toward any service"]);
  }
  throw new Error(`unsupported second-batch plan ${planId}`);
}

function parseEnv(text) {
  return Object.fromEntries(text.split(/\r?\n/).map(line => line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/)).filter(Boolean).map(match => [match[1], match[2].replace(/^['"]|['"]$/g, "")]));
}

async function loadClient() {
  const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
  const baseUrl = String(env.SUPABASE_URL || "").replace(/\/$/, "");
  const keyValue = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
  if (!baseUrl || !keyValue) throw new Error("SUPABASE_URL or server key is missing");
  return { baseUrl, key: keyValue };
}

async function fetchRows(client) {
  const query = new URLSearchParams({
    select: "plan_id,business_id,membership_name,benefits,source_url,updated_at",
    plan_id: `in.(${PLAN_IDS.join(",")})`,
    order: "plan_id.asc",
  });
  const response = await fetch(`${client.baseUrl}/rest/v1/clinic_memberships?${query}`, { headers: { apikey: client.key, Authorization: `Bearer ${client.key}` } });
  if (!response.ok) throw new Error(`clinic_memberships read returned HTTP ${response.status}`);
  const rows = await response.json();
  if (rows.length !== PLAN_IDS.length) throw new Error(`expected ${PLAN_IDS.length} Membership rows, got ${rows.length}`);
  return rows;
}

async function updateBenefits(client, candidate, current) {
  const comparison = compareCandidateSnapshot(candidate, current);
  if (comparison.status !== "ready") return { plan_id: candidate.plan_id, ...comparison };
  const now = new Date().toISOString();
  const query = new URLSearchParams({ plan_id: `eq.${candidate.plan_id}`, updated_at: `eq.${current.updated_at}` });
  const response = await fetch(`${client.baseUrl}/rest/v1/clinic_memberships?${query}`, {
    method: "PATCH",
    headers: { apikey: client.key, Authorization: `Bearer ${client.key}`, "Content-Type": "application/json", Prefer: "return=representation" },
    body: JSON.stringify({ benefits: candidate.suggested_benefits, updated_at: now }),
  });
  if (!response.ok) throw new Error(`plan ${candidate.plan_id} update returned HTTP ${response.status}`);
  const rows = await response.json();
  if (rows.length !== 1) throw new Error(`plan ${candidate.plan_id} update affected ${rows.length} rows`);
  return { plan_id: candidate.plan_id, status: "updated", before: current, after: rows[0], comparison };
}

export async function main({ apply = process.argv.includes("--apply") } = {}) {
  const result = { generatedAt: new Date().toISOString(), mode: apply ? "apply" : "dry_run", databaseWrites: 0, offerOperations: 0, clinicServiceOperations: 0, schemaOperations: 0, operations: [] };
  const client = await loadClient();
  const currentRows = await fetchRows(client);
  const candidates = currentRows.map(row => ({
    plan_id: Number(row.plan_id),
    business_id: row.business_id,
    membership_name: row.membership_name,
    source_url: row.source_url,
    updated_at: row.updated_at,
    current_benefits: row.benefits,
    suggested_benefits: buildDesiredBenefits(Number(row.plan_id), row.benefits),
    evidence: "current rendered source verified in ego-browser on 2026-09-21",
  }));

  if (!apply) {
    result.operations = candidates.map(candidate => ({ plan_id: candidate.plan_id, status: "eligible", before: candidate.current_benefits, after: candidate.suggested_benefits, evidence: candidate.evidence }));
  } else {
    for (const candidate of candidates) {
      const current = currentRows.find(row => Number(row.plan_id) === candidate.plan_id);
      const operation = await updateBenefits(client, candidate, current);
      result.operations.push(operation);
      if (operation.status === "updated") result.databaseWrites += 1;
    }
  }

  await writeFile(OUTPUT, JSON.stringify(result, null, 2), "utf8");
  console.log(JSON.stringify({ output: OUTPUT, mode: result.mode, databaseWrites: result.databaseWrites, offerOperations: 0, schemaOperations: 0 }, null, 2));
  return result;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
