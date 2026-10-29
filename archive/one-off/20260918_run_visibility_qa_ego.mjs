import { readFile, writeFile } from "node:fs/promises";

const SPACE_ID = 6;
const PAGE_LABEL = "p1";
const OUTPUT_PATH = "/Users/wyl/BrandRap/costfinder/outputs/visibility_qa_current_20260918.json";
const PAGE_WAIT_MS = 1500;
const KNOWN_EVALUATION_TIMEOUT_URLS = new Set([
  "https://viomedspa.com/clubvio/",
]);

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (!match || match[1].startsWith("#")) continue;
    values[match[1]] = match[2].replace(/^['"]|['"]$/g, "");
  }
  return values;
}

async function loadSupabaseRows() {
  const env = parseEnv(await readFile("/Users/wyl/BrandRap/costfinder/.env", "utf8"));
  const baseUrl = String(env.SUPABASE_URL || "").replace(/\/$/, "");
  const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
  if (!baseUrl || !key) throw new Error("SUPABASE_URL or writer key is missing");

  async function fetchRows(table, select, order) {
    const requestUrl = `${baseUrl}/rest/v1/${table}?select=${encodeURIComponent(select)}&order=${encodeURIComponent(order)}&limit=1000`;
    const response = await fetch(requestUrl, {
      headers: { apikey: key, Authorization: `Bearer ${key}` },
    });
    if (!response.ok) throw new Error(`${table} returned HTTP ${response.status}`);
    return response.json();
  }

  const [promotions, memberships] = await Promise.all([
    fetchRows("clinic_promotions", "promotion_id,business_id,source_url,promotion_content,is_active", "promotion_id.asc"),
    fetchRows("clinic_memberships", "plan_id,business_id,source_url,benefits", "plan_id.asc"),
  ]);
  return { promotions, memberships };
}

function asClaims(value) {
  if (Array.isArray(value)) return value.filter(item => typeof item === "string" && item.trim());
  if (typeof value === "string" && value.trim()) return [value];
  return [];
}

function buildManifest({ promotions, memberships }) {
  const manifest = new Map();
  function add(type, recordId, row) {
    const sourceUrl = String(row.source_url || "").trim();
    if (!sourceUrl) return;
    if (!manifest.has(sourceUrl)) manifest.set(sourceUrl, { sourceUrl, records: [] });
    const entry = manifest.get(sourceUrl);
    const claims = asClaims(type === "promotion" ? row.promotion_content : row.benefits);
    entry.records.push({
      recordType: type,
      recordId: Number(recordId),
      isActive: type === "promotion" ? Boolean(row.is_active) : null,
      claims,
    });
  }
  for (const row of promotions) add("promotion", row.promotion_id, row);
  for (const row of memberships) add("membership", row.plan_id, row);
  return [...manifest.values()].sort((left, right) => left.sourceUrl.localeCompare(right.sourceUrl));
}

async function loadPreviousResults() {
  try {
    return JSON.parse(await readFile(OUTPUT_PATH, "utf8"));
  } catch {
    return {
      reportType: "rendered_visibility_qa",
      checkedAt: new Date().toISOString(),
      criteria: "DOM text plus text Range layout box plus visible ancestors",
      results: {},
    };
  }
}

async function inspectPage(page, entry) {
  const claims = entry.records.flatMap(record =>
    record.claims.map(claim => ({
      recordType: record.recordType,
      recordId: record.recordId,
      claim,
    }))
  );
  await page.goto(entry.sourceUrl);
  await page.waitForLoadState("load");
  await page.waitForTimeout(PAGE_WAIT_MS);
  return page.evaluate(({ claims }) => {
    const normalize = value => {
      let text = String(value || "").normalize("NFKC");
      text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, " $1 ");
      text = text.replace(/\[([^\]]*)\]\([^)]*\)/g, " $1 ");
      text = text.replace(/<[^>]*>/g, " ").replace(/[–—]/g, "-").replace(/[*_~`#>\\]/g, " ");
      return text.replace(/([\p{N}])([\p{L}])/gu, "$1 $2").replace(/([\p{L}])([\p{N}])/gu, "$1 $2").replace(/[^\p{L}\p{N}$%]+/gu, " ").replace(/\s+/g, " ").trim().toLocaleLowerCase();
    };
    const textNodes = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) textNodes.push(node);

    const normalizedBodyText = normalize(document.body?.innerText || "");
    const claimResults = claims.map(input => {
      const normalizedClaim = normalize(input.claim);
      const matches = [];
      for (const textNode of textNodes) {
        if (!normalizedClaim || !normalize(textNode.nodeValue).includes(normalizedClaim)) continue;
        const range = document.createRange();
        range.selectNodeContents(textNode);
        const rects = [...range.getClientRects()].map(rect => ({
          x: rect.x, y: rect.y, width: rect.width, height: rect.height,
        }));
        const ancestors = [];
        let current = textNode.parentElement;
        while (current && ancestors.length < 20) {
          const style = getComputedStyle(current);
          ancestors.push({
            tag: current.tagName,
            id: current.id,
            className: String(current.className || ""),
            display: style.display,
            visibility: style.visibility,
            opacity: style.opacity,
            rects: [...current.getClientRects()].map(rect => ({ width: rect.width, height: rect.height })),
          });
          current = current.parentElement;
        }
        const hiddenAncestor = ancestors.find(item =>
          item.display === "none" ||
          item.visibility === "hidden" ||
          item.visibility === "collapse" ||
          Number(item.opacity) === 0 ||
          (item.rects.length > 0 && item.rects.every(rect => rect.width <= 0 || rect.height <= 0))
        ) || null;
        const hasTextLayoutBox = rects.some(rect => rect.width > 0 && rect.height > 0);
        matches.push({
          visible: hasTextLayoutBox && !hiddenAncestor,
          rects,
          hiddenAncestor,
        });
      }
      const visibleMatch = matches.find(match => match.visible);
      const visibleInnerTextMatch = normalizedClaim.length >= 8 && normalizedBodyText.includes(normalizedClaim);
      return {
        ...input,
        status: visibleMatch || visibleInnerTextMatch ? "visible_match" : (matches.length ? "dom_only_hidden" : "not_found"),
        matchCount: matches.length,
        visibleMatch: visibleMatch || null,
        visibleEvidence: visibleInnerTextMatch ? { kind: "rendered_innerText" } : null,
        hiddenAncestors: matches.filter(match => match.hiddenAncestor).map(match => match.hiddenAncestor),
      };
    });
    return { url: location.href, title: document.title, claimResults };
  }, { claims });
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
      if (!recordStatuses[key]) recordStatuses[key] = [];
      recordStatuses[key].push(claim.status);
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

const task = await taskSpace(SPACE_ID);
const page = task.page(PAGE_LABEL);
const { promotions } = await loadSupabaseRows();
const activePromotions = promotions.filter(row => row.is_active === true);
const manifest = buildManifest({ promotions: activePromotions, memberships: [] });
const report = { reportType: "rendered_visibility_qa_current_promotions", checkedAt: new Date().toISOString(), criteria: "DOM text plus text Range layout box plus visible ancestors plus rendered innerText", results: {} };
report.scope = {
  promotionRecords: activePromotions.length,
  membershipRecords: 0,
  uniqueSourceUrls: manifest.length,
};
report.results ||= {};

for (const entry of manifest) {
  if (report.results[entry.sourceUrl]) continue;
  process.stdout.write(`checking ${entry.sourceUrl}\n`);
  if (KNOWN_EVALUATION_TIMEOUT_URLS.has(entry.sourceUrl)) {
    report.results[entry.sourceUrl] = {
      status: "page_unavailable",
      error: "page_evaluation_timeout_observed",
      finalUrl: entry.sourceUrl,
      claimResults: [],
    };
    report.summary = summarize(report.results);
    await writeFile(OUTPUT_PATH, JSON.stringify(report, null, 2), "utf8");
    continue;
  }
  try {
    report.results[entry.sourceUrl] = {
      status: "page_accessible",
      ...(await inspectPage(page, entry)),
    };
  } catch (error) {
    report.results[entry.sourceUrl] = {
      status: "page_unavailable",
      error: String(error),
      finalUrl: await page.url(),
      claimResults: [],
    };
  }
  report.summary = summarize(report.results);
  await writeFile(OUTPUT_PATH, JSON.stringify(report, null, 2), "utf8");
  process.stdout.write(`${JSON.stringify(report.summary)}\n`);
}

report.summary = summarize(report.results);
report.completedAt = new Date().toISOString();
await writeFile(OUTPUT_PATH, JSON.stringify(report, null, 2), "utf8");
console.log(JSON.stringify({ outputPath: OUTPUT_PATH, summary: report.summary }));
