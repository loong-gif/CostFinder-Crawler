import { readFile, writeFile } from "node:fs/promises";
import { readFileSync } from "node:fs";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const OUTPUT_JSON = `${ROOT}/outputs/promotion_bidirectional_visibility_qa_20260921.json`;
const OUTPUT_MD = `${ROOT}/outputs/promotion_bidirectional_visibility_qa_20260921.md`;

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (match) values[match[1]] = match[2].replace(/^['"]|['"]$/g, "");
  }
  return values;
}

function canonicalUrl(value) {
  try {
    const url = new URL(String(value));
    url.hash = "";
    url.hostname = url.hostname.toLowerCase();
    url.pathname = url.pathname.replace(/\/+$/, "") || "/";
    return url.toString();
  } catch {
    return String(value || "").replace(/\/+$/, "");
  }
}

function normalizeText(value) {
  return String(value || "")
    .normalize("NFKC")
    .replace(/\\([*$]) /g, "$1")
    .replace(/\\([*$])/g, "$1")
    .replace(/\bunits?\b/gi, "u")
    .replace(/\b(\d+)\s+u\b/gi, "$1u")
    .replace(/\bsessions?\b/gi, "session")
    .replace(/\bper\s+month\b/gi, "monthly")
    .replace(/\b(one|1)\s+syringe\b/gi, "1 syringe")
    .replace(/\$\s*([\d,]+)(?:\.\d{1,2})?/g, (_match, amount) => `$${amount.replace(/,/g, "")}`)
    .replace(/[^a-z0-9$%]+/gi, " ")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

function tokens(value) {
  return normalizeText(value)
    .split(" ")
    .filter((token) => token && !new Set(["the", "a", "an", "and", "of", "for", "with", "only", "shop", "now"]).has(token));
}

function tokenSimilarity(left, right) {
  const a = new Set(tokens(left));
  const b = new Set(tokens(right));
  if (!a.size || !b.size) return 0;
  let overlap = 0;
  for (const token of a) if (b.has(token)) overlap += 1;
  return (2 * overlap) / (a.size + b.size);
}

function textMatch(claim, text) {
  const normalizedClaim = normalizeText(claim);
  const normalizedText = normalizeText(text);
  if (!normalizedClaim || !normalizedText) return { status: "not_found", score: 0 };
  if (normalizedText.includes(normalizedClaim)) return { status: "visible_text_match", score: 1 };

  const claimTokens = tokens(claim);
  const hasPriceOrQuantity = claimTokens.some((token) => /^\$?\d+(?:\.\d+)?$/.test(token));
  const meaningful = claimTokens.filter((token) => token.length > 2);
  const found = meaningful.filter((token) => normalizedText.includes(token));
  const score = meaningful.length ? found.length / meaningful.length : 0;
  if (hasPriceOrQuantity && score >= 0.8) return { status: "semantic_visible_text_match", score };
  if (!hasPriceOrQuantity && score >= 0.9) return { status: "semantic_visible_text_match", score };
  return { status: "not_found", score };
}

function loadJson(file) {
  return JSON.parse(readFileSync(file, "utf8"));
}

function addPageResult(map, sourceUrl, value, source) {
  const key = canonicalUrl(sourceUrl);
  if (!key) return;
  const existing = map.get(key) || {};
  map.set(key, { ...existing, ...value, evidenceSources: [...new Set([...(existing.evidenceSources || []), source])] });
}

function pageCandidatesFromText(text) {
  const lines = String(text || "")
    .split(/\r?\n/)
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter(Boolean);
  const candidates = [];
  const priceLine = /\$\s*\d[\d,]*(?:\.\d{1,2})?/;
  for (let index = 0; index < lines.length; index += 1) {
    if (!priceLine.test(lines[index])) continue;
    const price = lines[index];
    const previous = lines[index - 1] || "";
    const beforePrevious = lines[index - 2] || "";
    const hasSession = /\b(?:one|1|2|3|4|5|6|8|10)\s+sessions?\b/i.test(previous);
    const title = hasSession ? beforePrevious : previous;
    if (!title || /^(book now|shop now|add to cart|accept all|menu)$/i.test(title)) continue;
    if (title.length > 140) continue;
    candidates.push({ text: [title, hasSession ? previous : null, price].filter(Boolean).join(" "), line: index + 1 });
  }
  return candidates;
}

function claimEvidenceFromRaw(claim, rawClaims) {
  const normalized = normalizeText(claim);
  const exact = rawClaims.find((item) => normalizeText(item.claim) === normalized);
  if (!exact) return null;
  return {
    status: exact.status,
    rawStatus: exact.status,
    matchCount: exact.matchCount ?? null,
    visibleMatch: exact.visibleMatch ?? null,
    hiddenAncestors: exact.hiddenAncestors || [],
  };
}

function buildMarkdown(report) {
  const s = report.summary;
  const lines = [
    `# Promotion 双向可见性 QA（2026-09-21）`,
    "",
    "本轮只读重核，未写入 Supabase。原始 rendered QA 作为证据，但不采用 reconciliation 后把 `not_found` 晋级为 `visible_match` 的结果。",
    "",
    "## 汇总",
    "",
    `- 当前 Promotion：${s.currentPromotions} 条；active：${s.currentActivePromotions} 条。`,
    `- 有保存页面结果：${s.savedPageResultPromotions} 条；保存的可见页面文本：${s.savedVisibleTextPromotions} 条。`,
    `- 页面不可用：${s.pageUnavailablePromotions} 条；无法从保存结果完成反向候选提取：${s.reverseUnverifiedPromotions} 条。`,
    `- 现有 promotion_content 全部 claim 得到直接/语义页面支持：${s.forwardFullySupportedPromotions} 条。`,
    `- 现有 claim 部分未得到支持：${s.forwardPartialPromotions} 条。`,
    `- 页面候选发现数据库可能漏项的记录：${s.reverseCandidateReviewPromotions} 条；候选条目：${s.uncoveredCandidateCount} 条。`,
    `- OCR 仅作为辅助证据，不能替代 rendered DOM 可见性：${s.ocrReferencedPromotions} 条关联到 OCR 记录。`,
    "",
    "## `promotion_id=101`",
    "",
    `- 页面：${report.records.find((record) => record.promotion_id === 101)?.source_url || ""}`, 
    `- 页面可见卡片候选：${report.records.find((record) => record.promotion_id === 101)?.reverse.candidateCount || 0} 条。`,
    `- 当前数据库内容：${report.records.find((record) => record.promotion_id === 101)?.existingClaimCount || 0} 条。`,
    `- 页面候选未被当前数据库内容覆盖：${report.records.find((record) => record.promotion_id === 101)?.reverse.uncoveredCandidates.length || 0} 条。`,
    "",
  ];
  const target = report.records.find((record) => record.promotion_id === 101);
  for (const candidate of target?.reverse.uncoveredCandidates || []) lines.push(`- ${candidate.text}`);
  lines.push("", "## 记录级状态", "", "| Promotion | 状态 | 当前 claim | 支持 | 未决 | 页面候选未覆盖 | 证据 |", "|---:|---|---:|---:|---:|---:|---|");
  for (const record of report.records) {
    lines.push(`| ${record.promotion_id} | ${record.status} | ${record.existingClaimCount} | ${record.forward.supportedCount} | ${record.forward.unresolvedCount} | ${record.reverse.uncoveredCandidates.length} | ${record.evidenceTypes.join(", ")} |`);
  }
  return `${lines.join("\n")}\n`;
}

async function fetchRows(baseUrl, key, table, select) {
  const rows = [];
  for (let offset = 0; ; offset += 1000) {
    const url = `${baseUrl}/rest/v1/${table}?select=${encodeURIComponent(select)}&order=promotion_id.asc&limit=1000&offset=${offset}`;
    const response = await fetch(url, { headers: { apikey: key, Authorization: `Bearer ${key}` } });
    if (!response.ok) throw new Error(`${table} returned ${response.status}: ${await response.text()}`);
    const page = await response.json();
    rows.push(...page);
    if (page.length < 1000) return rows;
  }
}

const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
const baseUrl = String(env.SUPABASE_URL || "").replace(/\/$/, "");
const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
if (!baseUrl || !key) throw new Error("SUPABASE_URL or server key is missing");

const promotions = await fetchRows(baseUrl, key, "clinic_promotions", "promotion_id,business_id,promotion_title,source_url,promotion_content,is_active,is_offer_extracted,updated_at");
const raw = loadJson(`${ROOT}/outputs/visibility_qa_live_20260918.json`);
const current = loadJson(`${ROOT}/outputs/visibility_qa_current_20260918.json`);
const p0 = loadJson(`${ROOT}/outputs/promotion_p0_p1_browser_review_20260919.json`);
const missing = loadJson(`${ROOT}/outputs/promotion_missing_coverage_browser_review_20260919.json`);
const liveEvidence = loadJson(`${ROOT}/one-off/20260921_promotion_live_evidence.json`);
const ocrManifest = loadJson(`${ROOT}/outputs/visibility_review_20260918/visible_image_ocr_manifest.json`);
const ocrReview = loadJson(`${ROOT}/outputs/visibility_review_20260918/promotion_claim_ocr_review.json`);
const tesseract = loadJson(`${ROOT}/outputs/visibility_review_20260918/visible_image_ego_fetch_tesseract.json`);

const pages = new Map();
for (const [url, result] of Object.entries(raw.results || {})) addPageResult(pages, url, { status: result.status, finalUrl: result.url, title: result.title, rawClaims: result.claimResults || [] }, "raw_rendered_qa");
for (const [url, result] of Object.entries(current.results || {})) addPageResult(pages, url, { currentStatus: result.status, currentFinalUrl: result.url, currentClaims: result.claimResults || [] }, "current_rendered_qa");
for (const [url, result] of Object.entries(p0.results || {})) addPageResult(pages, url, { status: result.status, finalUrl: result.final_url || result.url, title: result.title, visibleText: result.body_text || null, p0Claims: result.records?.flatMap((record) => record.claims || []) || [] }, "promotion_browser_review");
for (const [id, result] of Object.entries(missing.results || {})) addPageResult(pages, result.source_url, { status: result.status, finalUrl: result.final_url || result.url, title: result.title, visibleText: result.body_text || null, missingRecordId: Number(id), missingMatchedClaims: result.matched_claims || [] }, "coverage_browser_review");
for (const evidence of liveEvidence.evidence || []) addPageResult(pages, evidence.source_url, { status: evidence.status, finalUrl: evidence.final_url || evidence.source_url, title: evidence.page_title || evidence.title, visibleText: evidence.visible_text || null, liveEvidence: evidence }, "saved_live_evidence");

const ocrByPage = new Map();
for (const [imageUrl, value] of Object.entries(ocrManifest)) for (const pageUrl of value.pages || []) {
  const keyUrl = canonicalUrl(pageUrl);
  const list = ocrByPage.get(keyUrl) || [];
  list.push({ imageUrl, status: value.status, checkedAt: value.checked_at, transcript: value.transcript || [], alt: value.alts || [] });
  ocrByPage.set(keyUrl, list);
}
for (const item of ocrReview.results || []) {
  const keyUrl = canonicalUrl(item.source_url);
  const list = ocrByPage.get(keyUrl) || [];
  list.push({ source: "promotion_claim_ocr_review", promotionId: item.promotion_id, claim: item.claim, status: item.status, transcript: item.transcript_lines || item.matching_transcript || [] });
  ocrByPage.set(keyUrl, list);
}
for (const [imageUrl, value] of Object.entries(tesseract.results || {})) for (const pageUrl of value.pages || []) {
  const keyUrl = canonicalUrl(pageUrl);
  const list = ocrByPage.get(keyUrl) || [];
  list.push({ imageUrl, status: value.status, transcript: value.transcript || [], source: "tesseract" });
  ocrByPage.set(keyUrl, list);
}

const records = promotions.map((promotion) => {
  const page = pages.get(canonicalUrl(promotion.source_url));
  const visibleText = page?.visibleText || "";
  const rawClaims = page?.rawClaims || [];
  const claims = Array.isArray(promotion.promotion_content) ? promotion.promotion_content : [];
  const claimResults = claims.map((claim) => {
    if (visibleText) {
      const match = textMatch(claim, visibleText);
      return { claim, status: match.status, score: match.score, evidenceType: page.liveEvidence ? "saved_live_visible_text" : "saved_page_text" };
    }
    const rawEvidence = claimEvidenceFromRaw(claim, rawClaims);
    if (rawEvidence) return { claim, status: rawEvidence.status === "visible_match" && rawEvidence.visibleMatch?.rects?.length ? "raw_rendered_visible_match" : rawEvidence.status, evidenceType: "raw_rendered_claim_result", matchCount: rawEvidence.matchCount, visibleMatch: rawEvidence.visibleMatch, hiddenAncestors: rawEvidence.hiddenAncestors };
    return { claim, status: page?.status === "page_unavailable" ? "page_unavailable" : "unverified_saved_evidence", evidenceType: page ? "saved_page_without_text" : "no_saved_page_result" };
  });
  const supported = claimResults.filter((claim) => ["visible_text_match", "semantic_visible_text_match", "raw_rendered_visible_match"].includes(claim.status));
  const unresolved = claimResults.filter((claim) => !["visible_text_match", "semantic_visible_text_match", "raw_rendered_visible_match"].includes(claim.status));
  const candidates = page?.visibleText ? pageCandidatesFromText(page.visibleText) : [];
  const uncoveredCandidates = candidates.filter((candidate) => !claims.some((claim) => textMatch(candidate.text, claim).status !== "not_found" || tokenSimilarity(candidate.text, claim) >= 0.75));
  const ocr = ocrByPage.get(canonicalUrl(promotion.source_url)) || [];
  let status = "reverse_unverified";
  if (page?.status === "page_unavailable" || page?.liveEvidence?.status === "error") status = "page_unavailable";
  else if (unresolved.length) status = "forward_unresolved";
  else if (uncoveredCandidates.length) status = "reverse_candidate_review";
  else if (page?.visibleText) status = "bidirectional_supported";
  return {
    promotion_id: promotion.promotion_id,
    business_id: promotion.business_id,
    promotion_title: promotion.promotion_title,
    source_url: promotion.source_url,
    is_active: promotion.is_active,
    is_offer_extracted: promotion.is_offer_extracted,
    existingClaimCount: claims.length,
    evidenceTypes: [...new Set([...(page?.evidenceSources || []), ...(ocr.length ? ["ocr_reference"] : [])])],
    page: page ? { status: page.status, finalUrl: page.finalUrl, title: page.title } : null,
    forward: { supportedCount: supported.length, unresolvedCount: unresolved.length, claims: claimResults },
    reverse: { candidateCount: candidates.length, uncoveredCandidates, extraction: page?.visibleText ? "saved_visible_text_price_card_heuristic" : "not_available_from_saved_artifact" },
    ocr: ocr.slice(0, 25),
    status,
  };
});

const summary = {
  currentPromotions: promotions.length,
  currentActivePromotions: promotions.filter((row) => row.is_active).length,
  savedPageResultPromotions: records.filter((record) => record.page).length,
  savedVisibleTextPromotions: records.filter((record) => record.reverse.extraction !== "not_available_from_saved_artifact").length,
  pageUnavailablePromotions: records.filter((record) => record.status === "page_unavailable").length,
  reverseUnverifiedPromotions: records.filter((record) => record.status === "reverse_unverified").length,
  forwardFullySupportedPromotions: records.filter((record) => record.forward.unresolvedCount === 0 && record.existingClaimCount > 0).length,
  forwardPartialPromotions: records.filter((record) => record.forward.unresolvedCount > 0).length,
  reverseCandidateReviewPromotions: records.filter((record) => record.reverse.uncoveredCandidates.length > 0).length,
  uncoveredCandidateCount: records.reduce((sum, record) => sum + record.reverse.uncoveredCandidates.length, 0),
  ocrReferencedPromotions: records.filter((record) => record.ocr.length > 0).length,
  noDatabaseWrites: true,
  sourceArtifacts: [
    "outputs/visibility_qa_live_20260918.json",
    "outputs/visibility_review_20260918/visible_image_ocr_manifest.json",
    "outputs/visibility_review_20260918/promotion_claim_ocr_review.json",
    "one-off/20260921_promotion_live_evidence.json",
  ],
};

const report = { generatedAt: new Date().toISOString(), method: "raw_saved_rendered_evidence_plus_saved_live_text_and_ocr_reference", summary, records };
await writeFile(OUTPUT_JSON, `${JSON.stringify(report, null, 2)}\n`);
await writeFile(OUTPUT_MD, buildMarkdown(report));
console.log(JSON.stringify({ outputJson: OUTPUT_JSON, outputMd: OUTPUT_MD, summary }, null, 2));
