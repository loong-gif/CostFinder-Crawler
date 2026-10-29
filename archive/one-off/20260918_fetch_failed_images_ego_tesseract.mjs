import { mkdir, readFile, writeFile } from "node:fs/promises";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const ROOT = "/Users/wyl/BrandRap/costfinder";
const OCR_PATH = `${ROOT}/outputs/visibility_review_20260918/visible_image_ocr_manifest.json`;
const RETRY_PATH = `${ROOT}/outputs/visibility_review_20260918/visible_image_tesseract_retry.json`;
const OUTPUT_PATH = `${ROOT}/outputs/visibility_review_20260918/visible_image_ego_fetch_tesseract.json`;
const IMAGE_DIR = `${ROOT}/outputs/visibility_review_20260918/ego_fetched_images`;

function normalizeLines(text) {
  const seen = new Set();
  return String(text || "").split(/\r?\n/).map(line => line.replace(/\s+/g, " ").trim()).filter(line => line && !seen.has(line.toLocaleLowerCase()) && seen.add(line.toLocaleLowerCase()));
}

const ocr = JSON.parse(await readFile(OCR_PATH, "utf8"));
const retry = JSON.parse(await readFile(RETRY_PATH, "utf8"));
const failed = Object.entries(retry.results || {}).filter(([, item]) => item.status !== "tesseract_complete");
await mkdir(IMAGE_DIR, { recursive: true });
const task = await taskSpace(6);
const page = task.page("p1");
let currentPage = "";
const results = {};

for (const [src, item] of failed) {
  const pageUrl = (item.pages || [])[0];
  const result = { image_url: src, pages: item.pages || [], alts: item.alts || [], status: "failed" };
  try {
    if (pageUrl !== currentPage) {
      await page.goto(pageUrl, { timeout: 15000, waitUntil: "domcontentloaded" });
      await page.waitForTimeout(500);
      currentPage = pageUrl;
    }
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
      } catch (error) {
        return { error: String(error) };
      }
    }, src);
    if (!dataUrl?.data) throw new Error(dataUrl?.error || "browser_image_fetch_failed");
    const match = String(dataUrl.data).match(/^data:([^;]+);base64,(.+)$/);
    if (!match) throw new Error("invalid_browser_image_data");
    const tempPath = `${IMAGE_DIR}/ego_ocr_${process.pid}_${Object.keys(results).length}.img`;
    await writeFile(tempPath, Buffer.from(match[2], "base64"));
    const { stdout } = await execFileAsync("/opt/homebrew/bin/tesseract", [tempPath, "stdout", "--psm", "6"], { maxBuffer: 2 * 1024 * 1024 });
    result.status = "tesseract_complete";
    result.transcript = normalizeLines(stdout);
    result.bytes = Buffer.byteLength(match[2], "base64");
  } catch (error) {
    result.error = String(error).slice(0, 1000);
  }
  results[src] = result;
  console.log(JSON.stringify({ url: src, status: result.status, lines: (result.transcript || []).length }));
}

const summary = {
  target_count: failed.length,
  complete: Object.values(results).filter(item => item.status === "tesseract_complete").length,
  failed: Object.values(results).filter(item => item.status !== "tesseract_complete").length,
};
await writeFile(OUTPUT_PATH, JSON.stringify({ generated_at: new Date().toISOString(), summary, results }, null, 2), "utf8");
console.log(JSON.stringify({ output: OUTPUT_PATH, ...summary }, null, 2));
