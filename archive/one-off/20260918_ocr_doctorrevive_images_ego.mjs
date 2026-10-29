import { mkdir, readFile, writeFile } from "node:fs/promises";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const ROOT = "/Users/wyl/BrandRap/costfinder";
const PAGE_URL = "https://www.doctorrevive.com/deals.html";
const MEDIA_PATH = `${ROOT}/outputs/visibility_review_20260918/visible_image_manifest.json`;
const OCR_PATH = `${ROOT}/outputs/visibility_review_20260918/visible_image_ocr_manifest.json`;
const RETRY_PATH = `${ROOT}/outputs/visibility_review_20260918/visible_image_tesseract_retry.json`;
const EGO_PATH = `${ROOT}/outputs/visibility_review_20260918/visible_image_ego_fetch_tesseract.json`;
const IMAGE_DIR = `${ROOT}/outputs/visibility_review_20260918/ego_fetched_images`;

function lines(text) {
  const seen = new Set();
  return String(text || "").split(/\r?\n/).map(value => value.replace(/\s+/g, " ").trim()).filter(value => value && !seen.has(value.toLocaleLowerCase()) && seen.add(value.toLocaleLowerCase()));
}

const media = JSON.parse(await readFile(MEDIA_PATH, "utf8"))[PAGE_URL];
const ocr = JSON.parse(await readFile(OCR_PATH, "utf8"));
const retry = JSON.parse(await readFile(RETRY_PATH, "utf8"));
const existing = JSON.parse(await readFile(EGO_PATH, "utf8"));
const done = new Set([
  ...Object.entries(ocr).filter(([, item]) => item.status === "ocr_complete").map(([src]) => src),
  ...Object.entries(retry.results || {}).filter(([, item]) => item.status === "tesseract_complete").map(([src]) => src),
  ...Object.entries(existing.results || {}).filter(([, item]) => item.status === "tesseract_complete").map(([src]) => src),
]);
const targets = (media.images || []).filter(image => Number(image.naturalWidth || 0) >= 250 && Number(image.naturalHeight || 0) >= 100 && !done.has(image.src));
await mkdir(IMAGE_DIR, { recursive: true });
const task = await taskSpace(6);
const page = task.page("p1");
await page.goto(PAGE_URL, { timeout: 15000, waitUntil: "domcontentloaded" });
await page.waitForTimeout(500);

for (const image of targets) {
  const result = { image_url: image.src, pages: [PAGE_URL], alts: [image.alt || ""], status: "failed" };
  try {
    const dataUrl = await page.evaluate(async imageUrl => {
      try {
        const response = await fetch(imageUrl);
        if (!response.ok) return { error: `HTTP ${response.status}` };
        const blob = await response.blob();
        const data = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result));
          reader.onerror = reject;
          reader.readAsDataURL(blob);
        });
        return { data };
      } catch (error) { return { error: String(error) }; }
    }, image.src);
    if (!dataUrl?.data) throw new Error(dataUrl?.error || "browser_image_fetch_failed");
    const match = String(dataUrl.data).match(/^data:([^;]+);base64,(.+)$/);
    if (!match) throw new Error("invalid_browser_image_data");
    const tempPath = `${IMAGE_DIR}/doctorrevive_${process.pid}_${targets.indexOf(image)}.img`;
    await writeFile(tempPath, Buffer.from(match[2], "base64"));
    const { stdout } = await execFileAsync("/opt/homebrew/bin/tesseract", [tempPath, "stdout", "--psm", "6"], { maxBuffer: 2 * 1024 * 1024 });
    result.status = "tesseract_complete";
    result.transcript = lines(stdout);
    result.bytes = Buffer.byteLength(match[2], "base64");
  } catch (error) {
    result.error = String(error).slice(0, 1000);
  }
  existing.results[image.src] = result;
  console.log(JSON.stringify({ image: image.src, status: result.status, lines: (result.transcript || []).length }));
}

existing.summary = {
  ...(existing.summary || {}),
  doctorrevive_target_count: targets.length,
  doctorrevive_complete: targets.filter(image => existing.results[image.src]?.status === "tesseract_complete").length,
  doctorrevive_failed: targets.filter(image => existing.results[image.src]?.status !== "tesseract_complete").length,
};
await writeFile(EGO_PATH, JSON.stringify(existing, null, 2), "utf8");
console.log(JSON.stringify({ output: EGO_PATH, ...existing.summary }, null, 2));
