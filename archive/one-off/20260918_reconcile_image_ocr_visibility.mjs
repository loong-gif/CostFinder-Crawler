import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const OUTPUT = `${ROOT}/outputs/visibility_review_20260918/visibility_qa_reconciled_20260918.json`;
const QUEUE = `${ROOT}/outputs/visibility_review_20260918/current_active_review_queue.json`;
const EGO_OCR = `${ROOT}/outputs/visibility_review_20260918/visible_image_ego_fetch_tesseract.json`;

function normalize(value) {
  let text = String(value ?? "").normalize("NFKC");
  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, " $1 ").replace(/\[([^\]]*)\]\([^)]*\)/g, " $1 ").replace(/<[^>]*>/g, " ");
  text = text.replace(/[–—]/g, "-").replace(/[*_~`#>\\]/g, " ");
  return text.replace(/([\p{N}])([\p{L}])/gu, "$1 $2").replace(/([\p{L}])([\p{N}])/gu, "$1 $2").replace(/[^\p{L}\p{N}$%]+/gu, " ").replace(/\s+/g, " ").trim().toLocaleLowerCase();
}

const report = JSON.parse(await readFile(OUTPUT, "utf8"));
const queue = JSON.parse(await readFile(QUEUE, "utf8")).queue;
const ego = JSON.parse(await readFile(EGO_OCR, "utf8"));
const transcriptByPage = new Map();
for (const [imageUrl, item] of Object.entries(ego.results || {})) {
  if (item.status !== "tesseract_complete") continue;
  for (const pageUrl of item.pages || []) {
    const current = transcriptByPage.get(pageUrl) || { image_urls: [], lines: [] };
    current.image_urls.push(imageUrl);
    current.lines.push(...(item.transcript || []));
    transcriptByPage.set(pageUrl, current);
  }
}

const queueKeys = new Set(queue.map(item => `${item.promotion_id}\t${normalize(item.claim)}`));
const manualImageMatches = new Map([
  ["https://maxwell-aesthetics.com/specials-promotions/", new Set([
    "Annual Fall Event: A Night in White",
    "October 15, 2026",
    "5:00–7:00 PM",
    "More details to come",
  ])],
]);
let reconciledClaims = 0;
for (const [sourceUrl, result] of Object.entries(report.results || {})) {
  const evidence = transcriptByPage.get(sourceUrl);
  const transcriptText = normalize((evidence?.lines || []).join("\n"));
  const manual = manualImageMatches.get(sourceUrl) || new Set();
  for (const claim of result.claimResults || []) {
    if (claim.recordType !== "promotion" || claim.status !== "not_found") continue;
    const key = `${claim.recordId}\t${normalize(claim.claim)}`;
    const exact = queueKeys.has(key) && normalize(claim.claim).length >= 8 && transcriptText.includes(normalize(claim.claim));
    const manualMatch = queueKeys.has(key) && manual.has(claim.claim);
    if (!exact && !manualMatch) continue;
    claim.originalStatus = claim.status;
    claim.status = "visible_match";
    claim.evidence = {
      kind: "visible_image_tesseract",
      image_urls: evidence?.image_urls || [],
      transcript_lines: evidence?.lines || [],
      manual_visual_confirmation: manualMatch,
      checked_at: new Date().toISOString(),
    };
    reconciledClaims += 1;
  }
}
report.imageOcrReconciledAt = new Date().toISOString();
report.imageOcrReconciledClaims = (report.imageOcrReconciledClaims || 0) + reconciledClaims;
await writeFile(OUTPUT, JSON.stringify(report, null, 2), "utf8");
console.log(JSON.stringify({ output: OUTPUT, reconciledClaims }, null, 2));
