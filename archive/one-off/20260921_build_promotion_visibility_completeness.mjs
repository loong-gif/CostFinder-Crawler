import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const REPORT_JSON = `${ROOT}/outputs/promotion_visibility_completeness_20260921.json`;
const REPORT_MD = `${ROOT}/outputs/promotion_visibility_completeness_20260921.md`;
const DRY_RUN = `${ROOT}/outputs/promotion_content_repair_dry_run_20260921.json`;

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (match) values[match[1]] = match[2].replace(/^['"]|['"]$/g, "");
  }
  return values;
}

function canonicalUrl(value) {
  try { const url = new URL(String(value)); url.hash = ""; url.hostname = url.hostname.toLowerCase(); url.pathname = url.pathname.replace(/\/+$/, "") || "/"; return url.toString(); }
  catch { return String(value || "").replace(/\/+$/, ""); }
}

function normalize(value) {
  return String(value || "").normalize("NFKC").replace(/\s+/g, " ").replace(/\bunits?\b/gi, "u").replace(/\b(\d+)\s+u\b/gi, "$1u").replace(/\$\s*([\d,]+)(?:\.\d{1,2})?/g, "$$$1").replace(/[^a-z0-9$%]+/gi, " ").toLowerCase().trim();
}

function supported(claim, text) {
  const a = normalize(claim); const b = normalize(text);
  return Boolean(a && b && (b.includes(a) || a.includes(b) || a.split(" ").filter((token) => token.length > 2).every((token) => b.includes(token))));
}

function candidatesFromText(text) {
  const lines = String(text || "").split(/\r?\n/).map((line) => line.replace(/\s+/g, " ").trim()).filter(Boolean);
  const result = [];
  for (let index = 0; index < lines.length; index += 1) {
    if (!/\$\s*\d[\d,]*(?:\.\d{1,2})?/.test(lines[index])) continue;
    const previous = lines[index - 1] || "";
    const beforePrevious = lines[index - 2] || "";
    const hasSession = /\b(?:one|1|2|3|4|5|6|8|10)\s+sessions?\b/i.test(previous);
    const title = hasSession ? beforePrevious : previous;
    if (!title || /^(shop now|book now|add to cart|accept all|menu)$/i.test(title)) continue;
    result.push({ text: [title, hasSession ? previous : null, lines[index]].filter(Boolean).join(" "), line: index + 1 });
  }
  return result;
}

async function fetchRows(base, key) {
  const rows = [];
  for (let offset = 0; ; offset += 1000) {
    const response = await fetch(`${base}/rest/v1/clinic_promotions?select=promotion_id,business_id,promotion_title,source_url,promotion_content,is_active,is_offer_extracted,updated_at&order=promotion_id.asc&limit=1000&offset=${offset}`, { headers: { apikey: key, Authorization: `Bearer ${key}` } });
    if (!response.ok) throw new Error(`clinic_promotions returned ${response.status}`);
    const page = await response.json(); rows.push(...page); if (page.length < 1000) return rows;
  }
}

const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
const base = String(env.SUPABASE_URL || "").replace(/\/$/, "");
const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
if (!base || !key) throw new Error("Supabase configuration missing");
const promotions = await fetchRows(base, key);
const bidirectional = JSON.parse(await readFile(`${ROOT}/outputs/promotion_bidirectional_visibility_qa_20260921.json`, "utf8"));
const liveEvidence = JSON.parse(await readFile(`${ROOT}/outputs/promotion_visibility_completeness_live_20260921.json`, "utf8"));
const priorLive = JSON.parse(await readFile(`${ROOT}/one-off/20260921_promotion_live_evidence.json`, "utf8"));
const priorByUrl = new Map((priorLive.evidence || []).map((item) => [canonicalUrl(item.source_url), item]));
const oldById = new Map((bidirectional.records || []).map((item) => [Number(item.promotion_id), item]));

const records = promotions.map((row) => {
  const claims = Array.isArray(row.promotion_content) ? row.promotion_content.map(String) : [];
  const old = oldById.get(Number(row.promotion_id));
  const direct = Number(row.promotion_id) === Number(liveEvidence.promotion_id) ? liveEvidence : null;
  const pageEvidence = direct || priorByUrl.get(canonicalUrl(row.source_url));
  const directCards = direct?.status === "page_accessible" ? direct.cards.map((card, index) => ({ ...card, index: index + 1, evidence_type: "rendered_dom_card" })) : [];
  const candidates = directCards.length ? directCards : (old?.reverse?.uncoveredCandidates || []).map((candidate) => ({ ...candidate, evidence_type: "saved_text_candidate" }));
  const existingSupported = claims.filter((claim) => directCards.length ? direct.body_text && supported(claim, direct.body_text) : old?.forward?.claims?.find((item) => item.claim === claim && ["visible_text_match", "semantic_visible_text_match", "raw_rendered_visible_match"].includes(item.status)));
  const unresolvedItems = claims.filter((claim) => !existingSupported.includes(claim));
  const additions = candidates.filter((candidate) => !claims.some((claim) => supported(claim, candidate.text))).map((candidate) => candidate.text);
  const stable = directCards.length > 0 && direct.final_url && canonicalUrl(direct.final_url) === canonicalUrl(row.source_url);
  const pageUnavailable = pageEvidence?.status === "error" || old?.status === "page_unavailable";
  const pageCompleteness = stable ? "verified" : pageUnavailable ? "unavailable" : "unverified";
  const proposed = stable ? [...claims, ...additions] : [...claims];
  const writeEligible = stable && additions.length > 0 && unresolvedItems.length === 0;
  let recordClass = "no_visible_support";
  if (pageUnavailable) recordClass = "page_unavailable";
  else if (writeEligible) recordClass = "supported_with_explicit_change";
  else if (existingSupported.length === claims.length && claims.length > 0) recordClass = additions.length ? "supported_existing_only" : "supported_existing_only";
  else if (existingSupported.length > 0) recordClass = "partial_support";
  return {
    promotion_id: row.promotion_id,
    business_id: row.business_id,
    promotion_title: row.promotion_title,
    source_url: row.source_url,
    updated_at: row.updated_at,
    existing_benefits: claims,
    existing_supported: existingSupported,
    proposed_additions: additions,
    proposed_modifications: [],
    proposed_removals: [],
    unresolved_items: unresolvedItems,
    unresolved_reasons: unresolvedItems.length ? [pageCompleteness === "unverified" ? "page_block_not_reliably_located" : "existing_claim_not_supported"] : [],
    record_class: recordClass,
    page_completeness: pageCompleteness,
    page_candidates: candidates,
    page_final_url: direct?.final_url || pageEvidence?.final_url || old?.page?.finalUrl || null,
    evidence_type: stable ? ["rendered_dom_card", "rendered_inner_text"] : pageEvidence ? [pageEvidence.status === "visible_text" ? "saved_live_visible_text" : "saved_rendered_qa"] : [],
    before_after_diff: { before: claims, after: proposed, added: additions, modified: [], removed: [] },
    write_eligible: writeEligible,
    is_offer_extracted: row.is_offer_extracted,
  };
});

const eligible = records.filter((record) => record.write_eligible);
const report = {
  generated_at: new Date().toISOString(),
  no_database_writes: true,
  summary: {
    promotion_count: records.length,
    write_eligible_count: eligible.length,
    supported_existing_only: records.filter((record) => record.record_class === "supported_existing_only").length,
    supported_with_explicit_change: records.filter((record) => record.record_class === "supported_with_explicit_change").length,
    partial_support: records.filter((record) => record.record_class === "partial_support").length,
    page_unavailable: records.filter((record) => record.record_class === "page_unavailable").length,
    no_visible_support: records.filter((record) => record.record_class === "no_visible_support").length,
    page_completeness_verified: records.filter((record) => record.page_completeness === "verified").length,
  },
  records,
};
const dryRun = { generated_at: report.generated_at, mode: "dry_run", no_database_writes: true, changes: eligible.map((record) => ({ promotion_id: record.promotion_id, business_id: record.business_id, promotion_title: record.promotion_title, source_url: record.source_url, old_updated_at: record.updated_at, old_content: record.existing_benefits, proposed: { promotion_content: record.before_after_diff.after, is_offer_extracted: false }, page_final_url: record.page_final_url, evidence_type: record.evidence_type, evidence: record.page_candidates, write_eligible: true })) };
await writeFile(REPORT_JSON, JSON.stringify(report, null, 2) + "\n");
await writeFile(DRY_RUN, JSON.stringify(dryRun, null, 2) + "\n");
const md = [`# Promotion 页面可见内容完整性报告（2026-09-21）`, "", "本报告使用 rendered DOM / layout box 证据；本阶段不修改 Supabase。", "", `- Promotion 总数：${records.length}`, `- 可安全回写：${eligible.length}`, `- 页面完整性已验证：${report.summary.page_completeness_verified}`, `- 页面不可用：${report.summary.page_unavailable}`, "", "## 可回写记录", "", ...eligible.map((record) => `- ${record.promotion_id}: ${record.existing_benefits.length} → ${record.before_after_diff.after.length} 条；新增 ${record.proposed_additions.length} 条。`), "", "## 101 核心证据", "", ...(records.find((record) => record.promotion_id === 101)?.page_candidates || []).map((candidate) => `- ${candidate.text} [${candidate.rect ? `layout ${candidate.rect.width}x${candidate.rect.height}` : candidate.evidence_type}]`)];
await writeFile(REPORT_MD, `${md.join("\n")}\n`);
console.log(JSON.stringify({ report: REPORT_JSON, dry_run: DRY_RUN, write_eligible: eligible.map((record) => record.promotion_id), promotion_count: records.length }, null, 2));
