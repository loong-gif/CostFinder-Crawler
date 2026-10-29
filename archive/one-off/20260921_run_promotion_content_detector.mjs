import { readFile, writeFile } from "node:fs/promises";
const ROOT = "/Users/wyl/BrandRap/costfinder";
const env = {};
for (const line of (await readFile(`${ROOT}/.env`, "utf8")).split(/\r?\n/)) { const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/); if (m) env[m[1]] = m[2].replace(/^['"]|['"]$/g, ""); }
const headers = { apikey: env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY, Authorization: `Bearer ${env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY}` };
const rows = await fetch(`${env.SUPABASE_URL.replace(/\/$/, "")}/rest/v1/clinic_promotions?select=promotion_id,is_active,promotion_content&limit=1000`, { headers }).then((r) => r.json());
const patterns = { data_image: /data:image/i, markdown_image: /!\[[^\]]*\]\(/, verification: /current verification|screenshot verification|\bQA\b/i, navigation: /schedule your self-care|related posts|book now|call to schedule/i, faq: /\?\s*$/m, isolated_price: /^\s*\$\d[\d,.]*\s*(for|only)?\s*$/i };
const findings = [];
for (const row of rows) { const values = Array.isArray(row.promotion_content) ? row.promotion_content.map(String) : []; const hit = {}; for (const [name, pattern] of Object.entries(patterns)) if (pattern.test(values.join("\n"))) hit[name] = values.filter((value) => pattern.test(value)).slice(0, 6); const normalized = values.map((value) => value.trim().toLowerCase()).filter(Boolean); const duplicates = normalized.filter((value, index, all) => all.indexOf(value) !== index); if (duplicates.length) hit.duplicate_entries = [...new Set(duplicates)].slice(0, 6); if (Object.keys(hit).length) findings.push({ promotion_id: row.promotion_id, is_active: row.is_active, findings: hit }); }
const result = { generated_at: new Date().toISOString(), promotion_count: rows.length, findings_count: findings.length, active_findings: findings.filter((row) => row.is_active).length, findings };
const output = `${ROOT}/one-off/20260921_full_promotion_content_detector_final.json`; await writeFile(output, JSON.stringify(result, null, 2) + "\n"); console.log(JSON.stringify({ output, promotion_count: rows.length, findings_count: findings.length, active_findings: result.active_findings }, null, 2));
