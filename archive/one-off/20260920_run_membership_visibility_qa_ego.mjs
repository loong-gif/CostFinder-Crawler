import { execFile } from "node:child_process";
import { mkdir, mkdtemp, readFile, rename, rm, writeFile } from "node:fs/promises";
import { promisify } from "node:util";
import { tmpdir } from "node:os";
import { join } from "node:path";

const execFileAsync = promisify(execFile);
const ROOT = "/Users/wyl/BrandRap/costfinder";
const SPACE_ID = 45;
const PAGE_LABEL = "p1";
const JSON_OUTPUT = `${ROOT}/outputs/membership_visibility_review_round2_20260921.json`;
const MARKDOWN_OUTPUT = `${ROOT}/outputs/membership_visibility_review_round2_20260921.md`;
const DRY_RUN_OUTPUT = `${ROOT}/outputs/membership_benefits_repair_dry_run_round2_20260921.json`;
const SCREENSHOT_DIR = `${ROOT}/outputs/membership_visibility_qa_round2_screenshots`;
const WORK_JSON_OUTPUT = `${JSON_OUTPUT}.tmp`;
const WORK_MARKDOWN_OUTPUT = `${MARKDOWN_OUTPUT}.tmp`;
const PAGE_SIZE = 500;
const PAGE_WAIT_MS = 1000;
const MAX_OCR_IMAGES_PER_PAGE = 10;

import {
  asClaims,
  buildManifest,
  buildRepairDryRun,
  collectPagedRows,
  isReviewableImage,
  normalize,
  summarize,
} from "/Users/wyl/BrandRap/costfinder/one-off/membership_visibility_qa_lib.mjs";
import {
  buildMembershipRecordAudits,
  buildMembershipRepairDryRun,
  markdownMembershipAudit,
  summarizeMembershipAudits,
} from "/Users/wyl/BrandRap/costfinder/one-off/membership_completeness_audit_lib.mjs";

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
  const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
  const baseUrl = String(env.SUPABASE_URL || "").replace(/\/$/, "");
  const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
  if (!baseUrl || !key) throw new Error("SUPABASE_URL or server key is missing");

  async function fetchAllRows(table, select, order) {
    return collectPagedRows(async (offset, limit) => {
      const params = new URLSearchParams({
        select,
        order,
        limit: String(limit),
        offset: String(offset),
      });
      const response = await fetch(`${baseUrl}/rest/v1/${table}?${params}`, {
        headers: { apikey: key, Authorization: `Bearer ${key}` },
      });
      if (!response.ok) throw new Error(`${table} returned HTTP ${response.status}`);
      return response.json();
    }, PAGE_SIZE);
  }

  const memberships = await fetchAllRows(
    "clinic_memberships",
    "plan_id,business_id,membership_name,membership_price,source_url,benefits,is_offer_extracted,is_ocr_required,need_browser,updated_at",
    "plan_id.asc",
  );
  return { memberships };
}

async function inspectPage(page, entry) {
  const claims = entry.records.flatMap(record => record.claims.map(claim => ({
    recordType: record.recordType,
    recordId: record.recordId,
    membershipName: record.membershipName,
    claim: claim.claim,
    claimKind: claim.claimKind,
    benefitIndex: claim.benefitIndex,
  })));
  const scopes = entry.records.map(record => ({
    recordId: record.recordId,
    membershipName: record.membershipName,
    membershipPrice: record.membershipPrice,
    storedBenefits: record.storedBenefits,
  }));
  await page.goto(entry.sourceUrl, { timeout: 12000, waitUntil: "domcontentloaded" });
  try { await page.waitForLoadState("load", { timeout: 5000 }); } catch {}
  await page.waitForTimeout(PAGE_WAIT_MS);
  return page.evaluate(async ({ claims, scopes }) => {
    const normalizeInPage = value => {
      let text = String(value || "").normalize("NFKC");
      text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, " $1 ");
      text = text.replace(/\[([^\]]*)\]\([^)]*\)/g, " $1 ");
      text = text.replace(/<[^>]*>/g, " ").replace(/[–—]/g, "-");
      text = text.replace(/[*_~`#>\\]/g, " ");
      return text.replace(/([\p{N}])([\p{L}])/gu, "$1 $2")
        .replace(/([\p{L}])([\p{N}])/gu, "$1 $2")
        .replace(/[^\p{L}\p{N}$%]+/gu, " ")
        .replace(/\s+/g, " ")
        .trim()
        .toLocaleLowerCase();
    };
    const rectDataInPage = element => {
      const rect = element.getBoundingClientRect();
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
    };
    const visibilityData = element => {
      if (!element) return { visible: false, ancestors: [] };
      const rect = element.getBoundingClientRect();
      let current = element;
      const ancestors = [];
      while (current && current.nodeType === Node.ELEMENT_NODE && ancestors.length < 20) {
        const style = getComputedStyle(current);
        const rects = [...current.getClientRects()].map(item => ({ width: item.width, height: item.height }));
        ancestors.push({ tag: current.tagName, id: current.id, className: String(current.className || ""), display: style.display, visibility: style.visibility, opacity: style.opacity, rects });
        if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse" || Number(style.opacity) === 0 || (rects.length > 0 && rects.every(item => item.width <= 0 || item.height <= 0))) return { visible: false, ancestors };
        current = current.parentElement;
      }
      return { visible: rect.width > 0 && rect.height > 0, ancestors };
    };
    const inspectClaims = () => {
      const textNodes = [];
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) textNodes.push(node);
      const normalizedBodyText = normalizeInPage(document.body?.innerText || "");
      return claims.map(input => {
        const normalizedClaim = normalizeInPage(input.claim);
        const matches = [];
        for (const textNode of textNodes) {
          if (!normalizedClaim || !normalizeInPage(textNode.nodeValue).includes(normalizedClaim)) continue;
          const range = document.createRange();
          range.selectNodeContents(textNode);
          const rects = [...range.getClientRects()].map(rect => ({ x: rect.x, y: rect.y, width: rect.width, height: rect.height }));
          const ancestorData = visibilityData(textNode.parentElement);
          const hasTextLayoutBox = rects.some(rect => rect.width > 0 && rect.height > 0);
          const hiddenAncestor = ancestorData.ancestors.find(item => item.display === "none" || item.visibility === "hidden" || item.visibility === "collapse" || Number(item.opacity) === 0 || (item.rects.length > 0 && item.rects.every(rect => rect.width <= 0 || rect.height <= 0))) || null;
          matches.push({ visible: hasTextLayoutBox && !hiddenAncestor, rects, hiddenAncestor, ancestors: ancestorData.ancestors });
        }
        const visibleMatch = matches.find(match => match.visible);
        const visibleInnerTextMatch = normalizedClaim.length >= 8 && normalizedBodyText.includes(normalizedClaim);
        return { ...input, status: visibleMatch || visibleInnerTextMatch ? "visible_match" : (matches.length ? "dom_only_hidden" : "not_found"), matchCount: matches.length, visibleMatch: visibleMatch || null, visibleEvidence: visibleInnerTextMatch ? { kind: "rendered_innerText" } : null, hiddenAncestors: matches.filter(match => match.hiddenAncestor).map(match => match.hiddenAncestor) };
      });
    };
    const controlLabel = element => String(element.innerText || element.getAttribute("aria-label") || element.title || "").replace(/\s+/g, " ").trim();
    const interactionPattern = /\b(show|read|view|see|load)\s+more\b|\bexpand\b/i;
    const interactionCandidates = () => [...document.querySelectorAll("button,[role='button'],summary,[aria-expanded]")].filter(element => {
      const label = controlLabel(element);
      const href = String(element.getAttribute("href") || "");
      const safeLink = element.tagName !== "A" || !href || href.startsWith("#");
      return safeLink && interactionPattern.test(label) && visibilityData(element).visible;
    });
    const controlState = element => ({ text: controlLabel(element), tag: element.tagName, id: element.id || null, className: String(element.className || ""), ariaExpanded: element.getAttribute("aria-expanded"), display: getComputedStyle(element).display, visibility: getComputedStyle(element).visibility, rect: rectDataInPage(element) });
    const initialClaimResults = inspectClaims();
    const interactionEvidence = [];
    for (const control of interactionCandidates()) {
      const before = controlState(control);
      try {
        control.click();
        await new Promise(resolve => setTimeout(resolve, 250));
        interactionEvidence.push({ action: "click", before, after: controlState(control), success: true });
      } catch (error) {
        interactionEvidence.push({ action: "click", before, success: false, error: String(error).slice(0, 500) });
      }
    }
    const claimResults = inspectClaims();
    const initialByKey = new Map(initialClaimResults.map(item => [`${item.recordType}:${item.recordId}:${item.claimKind}:${item.claim}`, item]));
    for (const claim of claimResults) {
      const initial = initialByKey.get(`${claim.recordType}:${claim.recordId}:${claim.claimKind}:${claim.claim}`);
      if (initial && initial.status !== "visible_match" && claim.status === "visible_match" && interactionEvidence.length) claim.visibleEvidence = { kind: "rendered_dom_after_interaction", interactions: interactionEvidence };
    }
    const visibleMedia = [];
    for (const image of [...document.images]) {
      const mediaVisibility = visibilityData(image);
      if (!mediaVisibility.visible) continue;
      visibleMedia.push({ kind: "image", src: image.currentSrc || image.src, alt: image.alt || "", rect: rectDataInPage(image), naturalWidth: image.naturalWidth, naturalHeight: image.naturalHeight });
    }
    for (const canvas of [...document.querySelectorAll("canvas")]) {
      const mediaVisibility = visibilityData(canvas);
      if (mediaVisibility.visible) visibleMedia.push({ kind: "canvas", rect: rectDataInPage(canvas), width: canvas.width, height: canvas.height });
    }
    const genericScopeText = /^(home|about|contact|services|menu|book now|buy now|pay now|learn more|read more|show more|view more|expand|accept|privacy policy|terms|copyright|follow us|instagram|facebook)$/i;
    const scopeKeyword = /choose|service|treatment|monthly|membership|member|off|save|unit|session|commitment|laser|botox|dysport|filler|skin|facial|microneed|price|visit|credit|benefit|option|select|included|per month/i;
    const candidateLines = (text, storedBenefits, membershipName, membershipPrice) => {
      const stored = (storedBenefits || []).map(normalizeInPage);
      const name = normalizeInPage(membershipName);
      const price = normalizeInPage(membershipPrice);
      const seen = new Set();
      return String(text || '').split(/\r?\n/).map(line => line.replace(/\s+/g, ' ').trim()).filter(line => {
        const normalized = normalizeInPage(line);
        if (!normalized || normalized.length < 3 || normalized.length > 240 || seen.has(normalized)) return false;
        seen.add(normalized);
        if (genericScopeText.test(line) || normalized === name || (price && normalized === price)) return false;
        if (stored.some(value => value && (normalized === value || normalized.includes(value) || value.includes(normalized)))) return false;
        return scopeKeyword.test(line);
      });
    };
    const membershipScopes = (scopes || []).map(input => {
      const targetName = normalizeInPage(input.membershipName);
      const targetPrice = normalizeInPage(input.membershipPrice);
      const elements = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"],article,section,li,div')]
        .filter(element => visibilityData(element).visible && targetName && normalizeInPage(element.innerText || '').includes(targetName));
      const anchors = elements.flatMap(element => {
        const candidates = [];
        let current = element;
        while (current && current !== document.body && candidates.length < 8) {
          const text = String(current.innerText || '').trim();
          const classes = `${current.id || ''} ${String(current.className || '')}`;
          const rect = current.getBoundingClientRect();
          if (text.length >= 40 && text.length <= 6000 && rect.width > 0 && rect.height > 0) {
            let score = 0;
            if (/ARTICLE|SECTION|LI/.test(current.tagName)) score += 1;
            if (/membership|member|plan|tier|card|club|package/i.test(classes)) score += 4;
            if (targetPrice && normalizeInPage(text).includes(targetPrice)) score += 3;
            if (/^H[1-6]$/.test(current.tagName) || current.getAttribute('role') === 'heading') score += 2;
            score += Math.max(0, 5 - text.length / 350);
            candidates.push({ element: current, score, text });
          }
          current = current.parentElement;
        }
        return candidates;
      }).sort((left, right) => right.score - left.score || left.text.length - right.text.length);
      const selected = anchors[0];
      if (!selected) return { recordId: input.recordId, status: 'unverified', reason: 'membership_heading_or_container_not_found', candidateLines: [] };
      const container = selected.element;
      const containerData = { tag: container.tagName, id: container.id || null, className: String(container.className || ''), rect: rectDataInPage(container) };
      const lines = candidateLines(container.innerText || '', input.storedBenefits || [], input.membershipName, input.membershipPrice);
      return {
        recordId: input.recordId,
        status: 'verified',
        headingText: input.membershipName,
        container: containerData,
        scopeTextLines: String(container.innerText || '').split(/\r?\n/).map(line => line.replace(/\s+/g, ' ').trim()).filter(Boolean).slice(0, 200),
        candidateLines: lines.map(text => ({ text, evidence: { kind: 'rendered_membership_scope', container: containerData } })),
      };
    });
    return { url: location.href, finalUrl: location.href, title: document.title, initialClaimResults, claimResults, visibleMedia, interactionEvidence, membershipScopes };
  }, { claims, scopes });
}

function uniqueLines(text) {
  const seen = new Set();
  return String(text || "").split(/\r?\n/)
    .map(line => line.replace(/\s+/g, " ").trim())
    .filter(line => line && !seen.has(line.toLocaleLowerCase()) && seen.add(line.toLocaleLowerCase()));
}

async function resolveTesseract() {
  for (const candidate of ["/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract", "tesseract"]) {
    try {
      if (candidate === "tesseract") await execFileAsync("which", [candidate]);
      else await execFileAsync(candidate, ["--version"]);
      return candidate;
    } catch {}
  }
  return null;
}

async function fetchImageDataUrl(page, imageUrl) {
  return page.evaluate(async url => {
    try {
      const response = await fetch(url);
      if (!response.ok) return { error: `HTTP ${response.status}` };
      const blob = await response.blob();
      const data = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = reject;
        reader.readAsDataURL(blob);
      });
      return { data };
    } catch (error) {
      return { error: String(error) };
    }
  }, imageUrl);
}

async function applyImageOcr(page, pageResult, tesseractPath, tempDir) {
  const pending = pageResult.claimResults.filter(item => item.status === "not_found");
  if (!pending.length) return pageResult;
  const images = (pageResult.visibleMedia || [])
    .filter(item => isReviewableImage(item) && String(item.src || "").startsWith("http"))
    .slice(0, MAX_OCR_IMAGES_PER_PAGE);
  if (!images.length) return pageResult;
  if (!tesseractPath) {
    for (const claim of pending) claim.status = "image_review_pending";
    pageResult.imageReviewStatus = "pending_tesseract_unavailable";
    pageResult.imageReview = images.map(image => ({ imageUrl: image.src, alt: image.alt, rect: image.rect, transcript: [], error: "tesseract_unavailable", checkedAt: new Date().toISOString() }));
    return pageResult;
  }
  const imageTranscripts = [];
  for (const [index, image] of images.entries()) {
    try {
      const payload = await fetchImageDataUrl(page, image.src);
      const match = String(payload?.data || "").match(/^data:([^;]+);base64,(.+)$/);
      if (!match) {
        imageTranscripts.push({ image, transcript: [], error: payload?.error || "image_fetch_failed" });
        continue;
      }
      const imagePath = join(tempDir, `membership_${process.pid}_${index}.img`);
      await writeFile(imagePath, Buffer.from(match[2], "base64"));
      const { stdout } = await execFileAsync(tesseractPath, [imagePath, "stdout", "--psm", "6"], { maxBuffer: 2 * 1024 * 1024 });
      imageTranscripts.push({ image, transcript: uniqueLines(stdout) });
    } catch (error) {
      imageTranscripts.push({ image, transcript: [], error: String(error).slice(0, 500) });
    }
  }
  const incompleteReview = imageTranscripts.some(item => item.error);
  for (const claim of pending) {
    const match = imageTranscripts.find(item => normalize(item.transcript.join(" ")).includes(normalize(claim.claim)));
    if (match) {
      claim.status = "image_only";
      claim.visibleEvidence = { kind: "rendered_image_ocr", imageUrl: match.image.src, imageRect: match.image.rect, checkedAt: new Date().toISOString() };
      claim.imageOcrTranscript = match.transcript;
    } else if (incompleteReview) {
      claim.status = "image_review_pending";
    }
  }
  pageResult.imageReviewStatus = incompleteReview ? "partial" : "completed";
  pageResult.imageReview = imageTranscripts.map(item => ({
    imageUrl: item.image.src,
    alt: item.image.alt,
    rect: item.image.rect,
    transcript: item.transcript,
    error: item.error || null,
    checkedAt: new Date().toISOString(),
  }));
  return pageResult;
}

async function captureReviewScreenshot(page, pageResult, sequence) {
  const reviewableMedia = (pageResult.visibleMedia || []).filter(isReviewableImage);
  if (!reviewableMedia.length || !(pageResult.claimResults || []).some(item => ["not_found", "image_review_pending"].includes(item.status))) return null;
  const path = `${SCREENSHOT_DIR}/page_${String(sequence).padStart(3, "0")}.png`;
  try {
    await page.screenshot({ path });
    pageResult.screenshotPath = path;
    return path;
  } catch (error) {
    pageResult.screenshotError = String(error).slice(0, 500);
    return null;
  }
}

function markdownReport(report) {
  const s = report.summary;
  const lines = [
    "# Membership Benefits Rendered-Visibility QA",
    "",
    `- Checked at: \`${report.checkedAt}\``,
    `- Database writes: \`0\``,
    `- Membership records: \`${s.membershipRecords}\``,
    `- Records with benefits: \`${s.membershipsWithBenefits}\``,
    `- Unique source URLs: \`${s.uniqueSourceUrls}\``,
    `- Benefit claims: \`${s.benefitClaims}\``,
    `- Accessible pages: \`${s.accessibleUrls}\``,
    `- Unavailable pages: \`${s.unavailableUrls}\``,
    `- Visible DOM/innerText: \`${s.visibleMatch}\``,
    `- Visible image OCR: \`${s.imageOnly}\``,
    `- Image review pending: \`${s.imageReviewPending}\``,
    `- DOM-only hidden: \`${s.domOnlyHidden}\``,
    `- Not found: \`${s.notFound}\``,
    `- Page unavailable: \`${s.pageUnavailableClaims}\``,
    `- Empty benefits pending: \`${s.recordsEmptyBenefitsPending}\``,
    `- Record gate: \`${s.recordsVisible} visible + ${s.recordsReview} review + ${s.recordsEmptyBenefitsPending} empty-benefits-pending\``,
    "",
    "## Review Queue",
    "",
  ];
  const reviewRows = [];
  for (const page of Object.values(report.results)) {
    for (const claim of page.claimResults || []) {
      if (!["visible_match", "image_only"].includes(claim.status) && claim.claimKind === "stored_benefit") {
        reviewRows.push(`- plan \`${claim.recordId}\` — \`${claim.status}\` — ${claim.claim} — ${page.url || page.finalUrl || ""}`);
      }
    }
  }
  lines.push(...(reviewRows.length ? reviewRows : ["- None"]));
  lines.push("", "## Empty Benefits", "");
  for (const page of Object.values(report.results)) {
    for (const record of page.emptyBenefitRecords || []) lines.push(`- plan \`${record.recordId}\` — \`empty_benefits_pending\` — ${record.membershipName} — ${page.url || page.finalUrl || ""}`);
  }
  lines.push("", "## Scope", "", "This report validates `benefits ⊆ rendered-visible-content`. It does not infer missing benefits from the page and does not update Supabase.", "");
  return lines.join("\n");
}

const { memberships } = await loadSupabaseRows();
const manifest = buildManifest(memberships);
const task = await taskSpace(SPACE_ID);
const page = task.page(PAGE_LABEL);
const tesseractPath = await resolveTesseract();
const tempDir = await mkdtemp(join(tmpdir(), "membership-visibility-"));
const report = {
  reportType: "rendered_visibility_qa_memberships",
  checkedAt: new Date().toISOString(),
  criteria: "DOM text plus text Range layout box plus computed-style ancestors plus rendered innerText plus normal expansion controls plus exact OCR for rendered media",
  writeback: "none",
  scope: { membershipRecords: memberships.length, membershipsWithBenefits: memberships.filter(row => asClaims(row.benefits).length > 0).length, uniqueSourceUrls: manifest.length },
  results: {},
};

function refreshAuditReport() {
  const visibilitySummary = summarize(report.results, memberships, manifest);
  report.recordAudits = buildMembershipRecordAudits(memberships, report);
  report.summary = summarizeMembershipAudits(report.recordAudits, visibilitySummary);
  report.repairDryRun = buildMembershipRepairDryRun(report.recordAudits, report);
}

try {
  await mkdir(SCREENSHOT_DIR, { recursive: true });
  for (const [sequence, entry] of manifest.entries()) {
    process.stdout.write(`checking ${entry.sourceUrl}\n`);
    try {
      const result = await inspectPage(page, entry);
      result.status = "page_accessible";
      result.emptyBenefitRecords = entry.records.filter(record => record.emptyBenefits).map(record => ({
        recordType: record.recordType,
        recordId: record.recordId,
        membershipName: record.membershipName,
        status: "empty_benefits_pending",
      }));
      report.results[entry.sourceUrl] = await applyImageOcr(page, result, tesseractPath, tempDir);
      await captureReviewScreenshot(page, report.results[entry.sourceUrl], sequence + 1);
    } catch (error) {
      report.results[entry.sourceUrl] = {
        status: "page_unavailable",
        finalUrl: await page.url(),
        error: String(error).slice(0, 1000),
        emptyBenefitRecords: entry.records.filter(record => record.emptyBenefits).map(record => ({
          recordType: record.recordType,
          recordId: record.recordId,
          membershipName: record.membershipName,
          status: "empty_benefits_pending",
        })),
        claimResults: entry.records.flatMap(record => record.claims.map(claim => ({
          recordType: record.recordType,
          recordId: record.recordId,
          membershipName: record.membershipName,
          claim: claim.claim,
          claimKind: claim.claimKind,
          benefitIndex: claim.benefitIndex,
          status: "page_unavailable",
        }))),
      };
    }
    refreshAuditReport();
    await writeFile(WORK_JSON_OUTPUT, JSON.stringify(report, null, 2), "utf8");
    await writeFile(WORK_MARKDOWN_OUTPUT, markdownMembershipAudit(report), "utf8");
    process.stdout.write(`${JSON.stringify(report.summary)}\n`);
  }
} finally {
  await rm(tempDir, { recursive: true, force: true });
}

report.completedAt = new Date().toISOString();
refreshAuditReport();
await writeFile(WORK_JSON_OUTPUT, JSON.stringify(report, null, 2), "utf8");
await writeFile(WORK_MARKDOWN_OUTPUT, markdownMembershipAudit(report), "utf8");
await rename(WORK_JSON_OUTPUT, JSON_OUTPUT);
await rename(WORK_MARKDOWN_OUTPUT, MARKDOWN_OUTPUT);
await writeFile(DRY_RUN_OUTPUT, JSON.stringify(report.repairDryRun, null, 2), "utf8");
console.log(JSON.stringify({ json: JSON_OUTPUT, markdown: MARKDOWN_OUTPUT, dryRun: DRY_RUN_OUTPUT, summary: report.summary }, null, 2));
