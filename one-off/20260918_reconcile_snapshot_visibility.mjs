import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const RAW_REPORT = `${ROOT}/outputs/visibility_qa_live_20260918.json`;
const SNAPSHOT_REVIEW = `${ROOT}/outputs/visibility_review_20260918/iframe_snapshot_review.json`;
const SNAPSHOT_REVIEW_V2 = `${ROOT}/outputs/visibility_review_20260918/iframe_snapshot_review_v2.json`;
const SNAPSHOT_REVIEW_V3 = `${ROOT}/outputs/visibility_review_20260918/iframe_snapshot_review_v3.json`;
const UNAVAILABLE_RETRY = `${ROOT}/outputs/visibility_review_20260918/unavailable_retry_ego.json`;
const OUTPUT = `${ROOT}/outputs/visibility_review_20260918/visibility_qa_reconciled_20260918.json`;

function normalize(value) {
  let text = String(value ?? "").normalize("NFKC");
  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, " $1 ");
  text = text.replace(/\[([^\]]*)\]\([^)]*\)/g, " $1 ");
  text = text.replace(/<[^>]*>/g, " ");
  text = text.replace(/(^|\n)\s*[-+*•]\s+/g, "$1");
  text = text.replace(/[“”]/g, '"').replace(/[‘’]/g, "'");
  text = text.replace(/[–—]/g, "-").replace(/[*_~`#>\\]/g, " ");
  return text.replace(/([\p{N}])([\p{L}])/gu, "$1 $2").replace(/([\p{L}])([\p{N}])/gu, "$1 $2").replace(/[^\p{L}\p{N}$%]+/gu, " ").replace(/\s+/g, " ").trim().toLocaleLowerCase();
}

function summarize(results) {
  const summary = { urls: 0, accessibleUrls: 0, unavailableUrls: 0, claims: 0, visibleMatch: 0, domOnlyHidden: 0, notFound: 0 };
  const recordStatuses = {};
  for (const result of Object.values(results)) {
    summary.urls += 1;
    if (result.status === "page_accessible") summary.accessibleUrls += 1;
    else summary.unavailableUrls += 1;
    for (const claim of result.claimResults || []) {
      summary.claims += 1;
      const key = `${claim.recordType}:${claim.recordId}`;
      (recordStatuses[key] ||= []).push(claim.status);
      if (claim.status === "visible_match") summary.visibleMatch += 1;
      if (claim.status === "dom_only_hidden") summary.domOnlyHidden += 1;
      if (claim.status === "not_found") summary.notFound += 1;
    }
  }
  const recordGateCounts = { visible: 0, hidden: 0, notFound: 0 };
  for (const statuses of Object.values(recordStatuses)) {
    if (statuses.includes("dom_only_hidden")) recordGateCounts.hidden += 1;
    else if (statuses.includes("not_found")) recordGateCounts.notFound += 1;
    else recordGateCounts.visible += 1;
  }
  return { ...summary, recordGateCounts };
}

const rawReport = JSON.parse(await readFile(RAW_REPORT, "utf8"));
let report = rawReport;
try {
  const previous = JSON.parse(await readFile(OUTPUT, "utf8"));
  if (previous.results && previous.snapshotReconciledAt) report = previous;
} catch {}
const snapshotReview = JSON.parse(await readFile(SNAPSHOT_REVIEW, "utf8"));
const snapshotReviewV2 = JSON.parse(await readFile(SNAPSHOT_REVIEW_V2, "utf8"));
const snapshotReviewV3 = JSON.parse(await readFile(SNAPSHOT_REVIEW_V3, "utf8"));
let unavailableRetry = {};
try { unavailableRetry = JSON.parse(await readFile(UNAVAILABLE_RETRY, "utf8")); } catch {}
for (const review of [snapshotReviewV2.results || {}, snapshotReviewV3.results || {}, unavailableRetry.results || {}]) {
  for (const [sourceUrl, evidence] of Object.entries(review)) {
    const previous = snapshotReview[sourceUrl] || {};
    snapshotReview[sourceUrl] = {
      ...previous,
      ...evidence,
      snapshot_matches: [...new Set([...(previous.snapshot_matches || []), ...(evidence.snapshot_matches || [])])],
      claims: [...new Set([...(previous.claims || []), ...(evidence.claims || [])])],
      has_iframe_snapshot: Boolean(previous.has_iframe_snapshot || (evidence.iframe_nodes || []).length),
    };
  }
}
const reconciled = structuredClone(report);
let reconciledClaims = 0;

for (const [sourceUrl, evidence] of Object.entries(snapshotReview)) {
  const result = reconciled.results?.[sourceUrl];
  if (!result || evidence.status !== "accessible") continue;
  result.status = "page_accessible";
  if (evidence.title) result.title = evidence.title;
  if (evidence.final_url) result.finalUrl = evidence.final_url;
  const visibleClaims = new Set((evidence.snapshot_matches || []).map(normalize));
  for (const claim of result.claimResults || []) {
    if (claim.recordType !== "promotion" || claim.status !== "not_found") continue;
    if (!visibleClaims.has(normalize(claim.claim))) continue;
    claim.originalStatus = claim.status;
    claim.status = "visible_match";
    claim.evidence = {
      kind: "rendered_accessibility_snapshot",
      has_iframe_snapshot: Boolean(evidence.has_iframe_snapshot),
      checked_at: evidence.checked_at,
    };
    reconciledClaims += 1;
  }
}

reconciled.criteria = `${rawReport.criteria}; rendered accessibility snapshot, including visible iframe content, may reconcile main-document not_found claims`;
reconciled.snapshotReconciledAt = new Date().toISOString();
reconciled.snapshotReconciledClaims = reconciledClaims;
reconciled.summary = summarize(reconciled.results || {});
await writeFile(OUTPUT, JSON.stringify(reconciled, null, 2), "utf8");
console.log(JSON.stringify({ output: OUTPUT, reconciledClaims, summary: reconciled.summary }, null, 2));
