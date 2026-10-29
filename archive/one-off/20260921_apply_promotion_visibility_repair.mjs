import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const PLAN = `${ROOT}/outputs/promotion_content_repair_dry_run_20260921.json`;
const OUTPUT = `${ROOT}/outputs/promotion_content_repair_apply_20260921.json`;

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) { const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/); if (match) values[match[1]] = match[2].replace(/^['"]|['"]$/g, ""); }
  return values;
}

const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
const base = String(env.SUPABASE_URL || "").replace(/\/$/, "");
const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
const apply = process.argv.includes("--apply");
const plan = JSON.parse(await readFile(PLAN, "utf8"));
const result = { generated_at: new Date().toISOString(), mode: apply ? "apply" : "dry_run", no_database_writes: !apply, changes: [], conflicts: [], already_applied: [] };

async function readRow(id) {
  const response = await fetch(`${base}/rest/v1/clinic_promotions?select=promotion_id,business_id,promotion_title,source_url,promotion_content,is_offer_extracted,updated_at&promotion_id=eq.${id}`, { headers: { apikey: key, Authorization: `Bearer ${key}` } });
  if (!response.ok) throw new Error(`read ${id}: ${response.status}`);
  const rows = await response.json(); if (rows.length !== 1) throw new Error(`read ${id} matched ${rows.length}`); return rows[0];
}

for (const item of plan.changes || []) {
  const current = await readRow(item.promotion_id);
  const identityMatches = current.business_id === item.business_id && current.promotion_title === item.promotion_title && current.source_url === item.source_url;
  const contentMatches = JSON.stringify(current.promotion_content || []) === JSON.stringify(item.old_content || []);
  const sameAfter = JSON.stringify(current.promotion_content || []) === JSON.stringify(item.proposed.promotion_content || []) && current.is_offer_extracted === false;
  const entry = { promotion_id: item.promotion_id, before: current, proposed: item.proposed };
  if (sameAfter) { result.already_applied.push({ ...entry, status: "already_applied" }); continue; }
  if (!identityMatches || current.updated_at !== item.old_updated_at || !contentMatches) { result.conflicts.push({ ...entry, status: "conflict", reasons: { identityMatches, updated_at_matches: current.updated_at === item.old_updated_at, content_matches: contentMatches } }); continue; }
  if (!apply) { result.changes.push({ ...entry, status: "ready" }); continue; }
  const params = new URLSearchParams({ promotion_id: `eq.${item.promotion_id}`, business_id: `eq.${item.business_id}`, updated_at: `eq.${item.old_updated_at}` });
  const response = await fetch(`${base}/rest/v1/clinic_promotions?${params}`, { method: "PATCH", headers: { apikey: key, Authorization: `Bearer ${key}`, "Content-Type": "application/json", Prefer: "return=representation" }, body: JSON.stringify({ promotion_content: item.proposed.promotion_content, is_offer_extracted: false, updated_at: new Date().toISOString() }) });
  if (!response.ok) { result.conflicts.push({ ...entry, status: "update_failed", error: await response.text() }); continue; }
  const rows = await response.json(); if (rows.length !== 1) { result.conflicts.push({ ...entry, status: "conflict_after_update", matched: rows.length }); continue; }
  result.changes.push({ ...entry, status: "updated", after: rows[0] });
}

await writeFile(OUTPUT, JSON.stringify(result, null, 2) + "\n");
console.log(JSON.stringify({ output: OUTPUT, mode: result.mode, ready_or_updated: result.changes.length, conflicts: result.conflicts.length, already_applied: result.already_applied.length, no_database_writes: result.no_database_writes }, null, 2));
