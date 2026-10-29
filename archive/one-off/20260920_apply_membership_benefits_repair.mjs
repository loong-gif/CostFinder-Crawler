import { readFile, writeFile } from "node:fs/promises";
import { compareCandidateSnapshot } from "./membership_repair_apply_lib.mjs";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const DRY_RUN = `${ROOT}/outputs/membership_benefits_repair_dry_run_round2_20260921.json`;
const APPLY_OUTPUT = `${ROOT}/outputs/membership_benefits_repair_apply_20260921.json`;
const ALLOWED_PLAN_IDS = new Set([247, 644, 645, 646, 647]);
const REVIEW_ONLY_PLAN_IDS = new Set([
  342, 343, 344, 345, 392, 393, 451, 452, 453, 454, 461, 462,
  515, 523, 529, 530, 531, 676, 677, 696, 697,
]);

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (!match || match[1].startsWith("#")) continue;
    values[match[1]] = match[2].replace(/^['"]|['"]$/g, "");
  }
  return values;
}

async function loadClient() {
  const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
  const baseUrl = String(env.SUPABASE_URL || "").replace(/\/$/, "");
  const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
  if (!baseUrl || !key) throw new Error("SUPABASE_URL or server key is missing");
  return { baseUrl, key };
}

async function fetchCurrent(client, planId) {
  const query = new URLSearchParams({
    select: "plan_id,business_id,membership_name,benefits,source_url,updated_at,is_offer_extracted,is_ocr_required,need_browser",
    plan_id: `eq.${planId}`,
  });
  const response = await fetch(`${client.baseUrl}/rest/v1/clinic_memberships?${query}`, {
    headers: { apikey: client.key, Authorization: `Bearer ${client.key}` },
  });
  if (!response.ok) throw new Error(`clinic_memberships read returned HTTP ${response.status}`);
  const rows = await response.json();
  if (rows.length !== 1) throw new Error(`expected one Membership row for ${planId}, got ${rows.length}`);
  return rows[0];
}

async function updateBenefits(client, candidate, current) {
  if (!ALLOWED_PLAN_IDS.has(Number(candidate.plan_id))) throw new Error(`plan ${candidate.plan_id} is outside the allowlist`);
  if (!candidate.write_eligible) return { plan_id: candidate.plan_id, status: "skipped", reason: candidate.reason || "not_evidence_closed" };
  const comparison = compareCandidateSnapshot(candidate, current);
  if (comparison.status !== "ready") return { plan_id: candidate.plan_id, ...comparison };
  const now = new Date().toISOString();
  const query = new URLSearchParams({ plan_id: `eq.${candidate.plan_id}`, updated_at: `eq.${current.updated_at}` });
  const response = await fetch(`${client.baseUrl}/rest/v1/clinic_memberships?${query}`, {
    method: "PATCH",
    headers: {
      apikey: client.key,
      Authorization: `Bearer ${client.key}`,
      "Content-Type": "application/json",
      Prefer: "return=representation",
    },
    body: JSON.stringify({ benefits: candidate.suggested_benefits, updated_at: now }),
  });
  if (!response.ok) throw new Error(`plan ${candidate.plan_id} update returned HTTP ${response.status}`);
  const rows = await response.json();
  if (rows.length !== 1) throw new Error(`plan ${candidate.plan_id} update affected ${rows.length} rows`);
  return { plan_id: candidate.plan_id, status: "updated", before: current, after: rows[0], comparison };
}

const dryRun = JSON.parse(await readFile(DRY_RUN, "utf8"));
const apply = process.argv.includes("--apply");
const result = {
  generatedAt: new Date().toISOString(),
  mode: apply ? "apply" : "dry_run",
  databaseWrites: 0,
  offerOperations: 0,
  clinicServiceOperations: 0,
  schemaOperations: 0,
  reviewOnlyPlanIds: [...REVIEW_ONLY_PLAN_IDS].sort((a, b) => a - b),
  operations: [],
};

if (!apply) {
    result.operations = dryRun.candidates.map(candidate => ({
      plan_id: candidate.plan_id,
      status: ALLOWED_PLAN_IDS.has(Number(candidate.plan_id)) && candidate.write_eligible ? "eligible" : "skipped",
      reason: ALLOWED_PLAN_IDS.has(Number(candidate.plan_id)) ? candidate.reason : "outside_evidence_closed_allowlist",
      before: candidate.current_benefits,
      after: candidate.suggested_benefits,
    }));
} else {
  const client = await loadClient();
  for (const candidate of dryRun.candidates) {
    if (!ALLOWED_PLAN_IDS.has(Number(candidate.plan_id)) || !candidate.write_eligible) {
      result.operations.push({ plan_id: candidate.plan_id, status: "skipped", reason: candidate.reason || "not_allowlisted_or_not_evidence_closed" });
      continue;
    }
    const current = await fetchCurrent(client, candidate.plan_id);
    const operation = await updateBenefits(client, candidate, current);
    result.operations.push(operation);
    if (operation.status === "updated") result.databaseWrites += 1;
  }
}

await writeFile(APPLY_OUTPUT, JSON.stringify(result, null, 2), "utf8");
console.log(JSON.stringify({ output: APPLY_OUTPUT, mode: result.mode, databaseWrites: result.databaseWrites, offerOperations: 0, clinicServiceOperations: 0, schemaOperations: 0 }, null, 2));
