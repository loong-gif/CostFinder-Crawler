import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const REPORT = `${ROOT}/outputs/visibility_review_20260918/visibility_qa_reconciled_20260918.json`;
const EVIDENCE = `${ROOT}/outputs/biorevive_visible_product_sync_20260918.json`;
const SOURCE_URL = "https://biorevivespa.com/collections/special-offers-best-deals";
const report = JSON.parse(await readFile(REPORT, "utf8"));
const evidence = JSON.parse(await readFile(EVIDENCE, "utf8"));
const page = report.results?.[SOURCE_URL];
let count = 0;
for (const claim of page?.claimResults || []) {
  if (claim.recordType !== "promotion" || claim.recordId !== 819 || claim.status !== "not_found") continue;
  claim.originalStatus = claim.status;
  claim.status = "visible_match";
  claim.evidence = { kind: "rendered_page_innerText", source_url: SOURCE_URL, matched_visible_content: evidence.new_content, checked_at: evidence.generated_at };
  count += 1;
}
report.innerTextReconciledAt = new Date().toISOString();
report.innerTextReconciledClaims = (report.innerTextReconciledClaims || 0) + count;
await writeFile(REPORT, JSON.stringify(report, null, 2), "utf8");
console.log(JSON.stringify({ output: REPORT, reconciledClaims: count }, null, 2));
