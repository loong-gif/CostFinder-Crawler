import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const OUTPUT = `${ROOT}/outputs/final_visibility_gate_audit_20260918.json`;
function normalize(value) {
  let text = String(value ?? "").normalize("NFKC");
  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, " $1 ").replace(/\[([^\]]*)\]\([^)]*\)/g, " $1 ").replace(/<[^>]*>/g, " ");
  text = text.replace(/[–—]/g, "-").replace(/[*_~`#>\\]/g, " ");
  return text.replace(/([\p{N}])([\p{L}])/gu, "$1 $2").replace(/([\p{L}])([\p{N}])/gu, "$1 $2").replace(/[^\p{L}\p{N}$%]+/gu, " ").replace(/\s+/g, " ").trim().toLocaleLowerCase();
}
const env = {};
for (const line of (await readFile(`${ROOT}/.env`, "utf8")).split(/\r?\n/)) { const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/); if (m) env[m[1]] = m[2].replace(/^['"]|['"]$/g, ""); }
const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
const headers = { apikey: key, Authorization: `Bearer ${key}` };
const promotions = await fetch(`${env.SUPABASE_URL.replace(/\/$/, "")}/rest/v1/clinic_promotions?select=promotion_id,source_url,promotion_content,is_active&limit=1000`, { headers }).then(r => r.json());
const offers = [];
for (let offset = 0; ; offset += 1000) {
  const page = await fetch(`${env.SUPABASE_URL.replace(/\/$/, "")}/rest/v1/promo_offer_master?select=id,promotion_id,is_active&order=id.asc&limit=1000&offset=${offset}`, { headers }).then(r => r.json());
  offers.push(...page);
  if (page.length < 1000) break;
}
const files = await Promise.all([
  readFile(`${ROOT}/outputs/visibility_qa_current_20260918.json`, "utf8").then(JSON.parse),
  readFile(`${ROOT}/outputs/visibility_review_20260918/unavailable_retry_current_ego.json`, "utf8").then(JSON.parse),
  readFile(`${ROOT}/outputs/visibility_review_20260918/unavailable_commit_current_ego.json`, "utf8").then(JSON.parse),
  readFile(`${ROOT}/outputs/visibility_review_20260918/visible_image_ocr_manifest.json`, "utf8").then(JSON.parse),
  readFile(`${ROOT}/outputs/visibility_review_20260918/visible_image_tesseract_retry.json`, "utf8").then(JSON.parse),
  readFile(`${ROOT}/outputs/visibility_review_20260918/visible_image_ego_fetch_tesseract.json`, "utf8").then(JSON.parse),
  readFile(`${ROOT}/outputs/visibility_review_20260918/iframe_snapshot_review_v3.json`, "utf8").then(JSON.parse),
]);
const [report, retry, commit, imageOcr, tesseract, egoTesseract, iframe] = files;
const evidence = new Map();
const add = (url, values) => evidence.set(url, [...(evidence.get(url) || []), ...values]);
for (const [url, page] of Object.entries(report.results || {})) add(url, (page.claimResults || []).filter(item => item.status === "visible_match").map(item => item.claim));
for (const source of [retry.results || {}, commit.results || {}]) for (const [url, page] of Object.entries(source)) add(url, page.snapshot_matches || []);
for (const source of [imageOcr, tesseract.results || {}, egoTesseract.results || {}]) for (const item of Object.values(source)) for (const page of item.pages || []) add(page, [...(item.transcript || []), ...(item.alts || [])]);
for (const [url, page] of Object.entries(iframe.results || {})) add(url, page.snapshot_matches || []);

const records = [];
for (const row of promotions.filter(item => item.is_active === true)) {
  const claims = Array.isArray(row.promotion_content) ? row.promotion_content : [];
  const normalizedEvidence = (evidence.get(row.source_url) || []).map(normalize).filter(Boolean);
  const unmatched = claims.filter(claim => {
    const target = normalize(claim);
    return !target || !normalizedEvidence.some(value => value.includes(target));
  });
  records.push({ promotion_id: row.promotion_id, source_url: row.source_url, claim_count: claims.length, status: unmatched.length ? "unverified" : "verified", unmatched });
}
const inactivePromotionIds = new Set(promotions.filter(row => row.is_active !== true).map(row => Number(row.promotion_id)));
const emptyActivePromotionIds = new Set(promotions.filter(row => row.is_active === true && (!Array.isArray(row.promotion_content) || row.promotion_content.length === 0)).map(row => Number(row.promotion_id)));
const result = {
  generated_at: new Date().toISOString(),
  rule: "active promotion_content must have current rendered DOM, visible-image OCR, or iframe/accessibility evidence",
  summary: {
    active_promotions: records.length,
    verified_promotions: records.filter(row => row.status === "verified").length,
    unverified_promotions: records.filter(row => row.status === "unverified").length,
    active_claims: records.reduce((sum, row) => sum + row.claim_count, 0),
    unmatched_claims: records.reduce((sum, row) => sum + row.unmatched.length, 0),
    active_offers: offers.filter(row => row.is_active === true).length,
    active_offers_on_inactive_promotions: offers.filter(row => row.is_active === true && inactivePromotionIds.has(Number(row.promotion_id))).length,
    active_offers_on_empty_active_promotions: offers.filter(row => row.is_active === true && emptyActivePromotionIds.has(Number(row.promotion_id))).length,
  },
  records,
};
await writeFile(OUTPUT, JSON.stringify(result, null, 2), "utf8");
console.log(JSON.stringify({ output: OUTPUT, summary: result.summary }, null, 2));
