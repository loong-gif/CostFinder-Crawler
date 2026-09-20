import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const QUEUE_PATH = `${ROOT}/outputs/visibility_review_20260918/current_active_review_queue.json`;
const OUTPUT_PATH = `${ROOT}/outputs/visibility_review_20260918/iframe_snapshot_review_v3.json`;

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

const queue = JSON.parse(await readFile(QUEUE_PATH, "utf8")).queue.filter(item => item.reason === "iframe_or_dynamic");
const grouped = new Map();
for (const item of queue) {
  const current = grouped.get(item.source_url) || { source_url: item.source_url, promotion_ids: new Set(), claims: [] };
  current.promotion_ids.add(Number(item.promotion_id));
  current.claims.push(item.claim);
  grouped.set(item.source_url, current);
}

const task = await taskSpace(6);
const page = task.page("p1");
const results = {};
for (const target of grouped.values()) {
  const claims = [...new Set(target.claims)];
  const result = {
    source_url: target.source_url,
    promotion_ids: [...target.promotion_ids],
    claims,
    status: "page_unavailable",
    snapshot_matches: [],
    unmatched_claims: [],
    iframe_nodes: [],
    checked_at: new Date().toISOString(),
  };
  try {
    await page.goto(target.source_url, { timeout: 15000, waitUntil: "domcontentloaded" });
    await page.waitForTimeout(2000);
    const snapshot = String(await page.snapshot({ scope: "full_page" }));
    const bodyText = String(await page.evaluate(() => document.body?.innerText || ""));
    const normalizedEvidence = `${normalize(bodyText)} ${normalize(snapshot)}`;
    result.status = "accessible";
    result.final_url = await page.url();
    result.title = await page.title();
    result.snapshot_length = snapshot.length;
    result.body_text_length = bodyText.length;
    result.iframe_nodes = await page.evaluate(() => [...document.querySelectorAll("iframe")].map(frame => {
      const rect = frame.getBoundingClientRect();
      const style = getComputedStyle(frame);
      return {
        src: frame.src || frame.getAttribute("src") || "",
        title: frame.title || "",
        visible: style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0,
        rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      };
    }));
    result.snapshot_matches = claims.filter(claim => {
      const normalizedClaim = normalize(claim);
      return normalizedClaim.length >= 8 && normalizedEvidence.includes(normalizedClaim);
    });
    result.unmatched_claims = claims.filter(claim => !result.snapshot_matches.includes(claim));
  } catch (error) {
    result.error = String(error).slice(0, 1000);
  }
  results[target.source_url] = result;
  console.log(JSON.stringify({ url: target.source_url, status: result.status, matches: result.snapshot_matches.length, claims: claims.length, iframe_nodes: result.iframe_nodes.length }));
}

const values = Object.values(results);
await writeFile(OUTPUT_PATH, JSON.stringify({ generated_at: new Date().toISOString(), results }, null, 2), "utf8");
console.log(JSON.stringify({ output: OUTPUT_PATH, urls: values.length, accessible: values.filter(item => item.status === "accessible").length, unavailable: values.filter(item => item.status !== "accessible").length, snapshotMatchedClaims: values.reduce((sum, item) => sum + item.snapshot_matches.length, 0) }, null, 2));
