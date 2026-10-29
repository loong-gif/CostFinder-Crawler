import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const REPORT_PATH = `${ROOT}/outputs/visibility_qa_live_20260918.json`;
const PLAN_PATH = `${ROOT}/outputs/promotion_visibility_cleanup_apply_20260918.json`;
const APPLY = process.argv.includes("--apply");

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (match) values[match[1]] = match[2].replace(/^['"]|['"]$/g, "");
  }
  return values;
}

function normalize(value) {
  let text = String(value || "").normalize("NFKC");
  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1");
  text = text.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");
  return text.replace(/[*_`#>]/g, "").replace(/\s+/g, " ").trim().toLocaleLowerCase();
}

async function fetchRows(baseUrl, key, table, select) {
  const url = `${baseUrl}/rest/v1/${table}?select=${encodeURIComponent(select)}&limit=2000`;
  const response = await fetch(url, { headers: { apikey: key, Authorization: `Bearer ${key}` } });
  if (!response.ok) throw new Error(`${table} returned HTTP ${response.status}`);
  return response.json();
}

async function patchRow(baseUrl, key, entry) {
  const params = new URLSearchParams({
    promotion_id: `eq.${entry.promotion_id}`,
    updated_at: `eq.${entry.old_updated_at}`,
  });
  const response = await fetch(`${baseUrl}/rest/v1/clinic_promotions?${params}`, {
    method: "PATCH",
    headers: {
      apikey: key,
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/json",
      Prefer: "return=representation",
    },
    body: JSON.stringify({ promotion_content: entry.new_content, updated_at: new Date().toISOString() }),
  });
  if (!response.ok) throw new Error(`promotion ${entry.promotion_id} returned HTTP ${response.status}`);
  const rows = await response.json();
  if (!Array.isArray(rows) || rows.length !== 1) throw new Error(`promotion ${entry.promotion_id} update matched ${rows.length} rows`);
  return rows[0];
}

const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
const baseUrl = String(env.SUPABASE_URL || "").replace(/\/$/, "");
const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
if (!baseUrl || !key) throw new Error("SUPABASE_URL or service key is missing");

const report = JSON.parse(await readFile(REPORT_PATH, "utf8"));
const promotions = await fetchRows(baseUrl, key, "clinic_promotions", "promotion_id,business_id,source_url,promotion_content,is_active,updated_at");
const byId = new Map(promotions.map(row => [Number(row.promotion_id), row]));
const hiddenById = new Map();
for (const result of Object.values(report.results || {})) {
  for (const claim of result.claimResults || []) {
    if (claim.recordType !== "promotion" || claim.status !== "dom_only_hidden") continue;
    const list = hiddenById.get(Number(claim.recordId)) || [];
    list.push(claim.claim);
    hiddenById.set(Number(claim.recordId), list);
  }
}

const plan = [];
for (const [promotionId, hiddenClaims] of hiddenById) {
  const row = byId.get(promotionId);
  if (!row || row.is_active !== true) continue;
  const hiddenNormalized = new Set(hiddenClaims.map(normalize));
  const oldContent = Array.isArray(row.promotion_content) ? row.promotion_content : [];
  const removed = [];
  const kept = [];
  for (const value of oldContent) {
    if (hiddenNormalized.has(normalize(value))) removed.push(value);
    else kept.push(value);
  }
  if (!removed.length) continue;
  plan.push({
    promotion_id: promotionId,
    business_id: row.business_id,
    source_url: row.source_url,
    old_updated_at: row.updated_at,
    old_content: oldContent,
    new_content: kept,
    removed_hidden_claims: removed,
    status: "ready",
    applied: false,
  });
}

const result = {
  mode: APPLY ? "apply" : "dry_run",
  generated_at: new Date().toISOString(),
  gate: "remove only current DOM claims classified as dom_only_hidden; preserve not_found claims",
  target_count: plan.length,
  plan,
};

if (APPLY) {
  for (const entry of plan) {
    try {
      const updated = await patchRow(baseUrl, key, entry);
      entry.applied = true;
      entry.status = "updated";
      entry.new_updated_at = updated.updated_at;
    } catch (error) {
      entry.status = "conflict_or_failed";
      entry.error = String(error);
    }
  }
  result.applied_count = plan.filter(entry => entry.applied).length;
  result.skipped_count = plan.filter(entry => !entry.applied).length;
}

await writeFile(PLAN_PATH, JSON.stringify(result, null, 2), "utf8");
console.log(JSON.stringify({ mode: result.mode, target_count: result.target_count, applied_count: result.applied_count ?? 0, skipped_count: result.skipped_count ?? 0, ids: plan.map(entry => entry.promotion_id) }, null, 2));
