#!/usr/bin/env node
import { mkdir, readFile, writeFile } from "node:fs/promises";

const apply = process.argv.includes("--apply");
const env = Object.fromEntries((await readFile(".env", "utf8")).split(/\r?\n/)
  .filter((line) => line && !line.startsWith("#")).map((line) => {
    const index = line.indexOf("=");
    return [line.slice(0, index), line.slice(index + 1).replace(/^"|"$/g, "")];
  }));
const baseUrl = String(env.SUPABASE_URL || "").replace(/\/$/, "");
const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
if (!baseUrl || !key) throw new Error("SUPABASE_URL and a server credential are required");
const headers = { apikey: key, Authorization: `Bearer ${key}` };

const allowed = new Set([
  "face", "neck", "chin", "lips", "forehead", "brow", "glabella", "crow's feet", "bunny lines", "gummy smile",
  "jawline", "nose", "under eyes", "underarms", "trapezius", "scalp", "hands", "body", "full brazilian", "full legs",
  "lower legs", "upper legs", "lower arms", "chest", "abdomen",
  "sideburns", "beard", "cheeks", "earlobes", "nipple", "submental",
]);
const aliases = new Map([
  ["Lip", "lips"], ["Upper Lip", "lips"], ["Chin", "chin"], ["Neck", "neck"], ["BBL Neck", "neck"],
  ["BBL Hands", "hands"], ["Deep CO2 Face", "face"], ["Face", "face"], ["full face", "face"], ["Lower Arms", "lower arms"],
  ["Lower Legs (knees included)", "lower legs"], ["Upper Legs (knees not included)", "upper legs"],
  ["shoulder/trap", "trapezius"], ["under-eye", "under eyes"], ["underarm", "underarms"], ["Underarms", "underarms"],
  ["Neck Skin Tightening", "neck"], ["XERF Face", "face"], ["Laser Hair Removal — Full Brazilian", "full brazilian"],
  ["Laser Hair Removal — Full Face", "full face"], ["Sideburns", "sideburns"], ["Beard", "beard"],
  ["Chest", "chest"], ["Abdomen", "abdomen"],
]);

const response = await fetch(`${baseUrl}/rest/v1/clinic_services?select=service_id,service_area&service_area=not.is.null&order=service_id.asc`, { headers });
if (!response.ok) throw new Error(`Could not read clinic_services: ${response.status} ${await response.text()}`);
const rows = await response.json();
const audit = rows.map((row) => {
  const target = aliases.get(row.service_area) ?? (allowed.has(row.service_area) ? row.service_area : null);
  return { ...row, target_service_area: target, reason: target === row.service_area ? "already_canonical" : target ? "canonical_alias" : "invalid_or_unresolved" };
});
const changes = audit.filter((row) => row.target_service_area !== row.service_area);
if (!(audit.length === 94 && audit.filter((row) => row.target_service_area).length === 86)) {
  throw new Error(`Unexpected live inventory: ${audit.length} non-null rows, ${audit.filter((row) => row.target_service_area).length} retained`);
}

if (apply) for (const row of changes) {
  const filter = new URLSearchParams({ service_id: `eq.${row.service_id}`, service_area: `eq.${row.service_area}` });
  const update = await fetch(`${baseUrl}/rest/v1/clinic_services?${filter}`, {
    method: "PATCH", headers: { ...headers, "Content-Type": "application/json", Prefer: "return=representation" },
    body: JSON.stringify({ service_area: row.target_service_area }),
  });
  if (!update.ok) throw new Error(`Could not update ${row.service_id}: ${update.status} ${await update.text()}`);
  if ((await update.json()).length !== 1) throw new Error(`Concurrent change detected for ${row.service_id}`);
}

await mkdir("outputs", { recursive: true });
await writeFile("outputs/service_area_normalization_20261013.json", JSON.stringify({
  applied: apply, summary: { reviewed: audit.length, retained: audit.filter((row) => row.target_service_area).length, cleared: audit.filter((row) => !row.target_service_area).length, changed: changes.length }, audit,
}, null, 2));
console.log(JSON.stringify({ applied: apply, reviewed: audit.length, retained: audit.filter((row) => row.target_service_area).length, cleared: audit.filter((row) => !row.target_service_area).length, changed: changes.length }));
