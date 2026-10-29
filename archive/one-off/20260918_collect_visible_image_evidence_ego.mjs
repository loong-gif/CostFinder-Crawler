import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const SPACE_ID = 6;
const OUTPUT_PATH = `${ROOT}/outputs/visibility_review_20260918/visible_image_manifest.json`;

const report = JSON.parse(await readFile(`${ROOT}/outputs/visibility_qa_live_20260918.json`, "utf8"));
const screenshotManifest = JSON.parse(await readFile(`${ROOT}/outputs/visibility_review_20260918/screenshot_manifest.json`, "utf8"));
const targetUrls = Object.keys(screenshotManifest).sort();

let manifest = {};
try { manifest = JSON.parse(await readFile(OUTPUT_PATH, "utf8")); } catch { manifest = {}; }

const task = await taskSpace(SPACE_ID);
const page = await task.newPage();

for (const sourceUrl of targetUrls) {
  if (manifest[sourceUrl]) continue;
  const result = { source_url: sourceUrl, checked_at: new Date().toISOString(), status: "page_unavailable", images: [], iframes: [], canvases: [] };
  process.stdout.write(`inspect ${sourceUrl}\n`);
  try {
    try { await page.goto(sourceUrl); } catch (error) { result.navigation_error = String(error).slice(0, 800); }
    try { await page.waitForLoadState("load"); } catch {}
    await page.waitForTimeout(500);
    const observed = await page.evaluate(() => {
      const visible = element => {
        const rect = element.getBoundingClientRect();
        let current = element;
        while (current && current.nodeType === Node.ELEMENT_NODE) {
          const style = getComputedStyle(current);
          if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse" || Number(style.opacity) === 0) return false;
          current = current.parentElement;
        }
        return rect.width > 0 && rect.height > 0;
      };
      const rectData = element => { const rect = element.getBoundingClientRect(); return { x: rect.x, y: rect.y, width: rect.width, height: rect.height }; };
      return {
        url: location.href,
        title: document.title,
        readyState: document.readyState,
        images: [...document.images].filter(visible).map((image, index) => ({ index, src: image.currentSrc || image.src, alt: image.alt || "", rect: rectData(image), naturalWidth: image.naturalWidth, naturalHeight: image.naturalHeight })),
        iframes: [...document.querySelectorAll("iframe")].filter(visible).map(frame => ({ src: frame.src || "", rect: rectData(frame) })),
        canvases: [...document.querySelectorAll("canvas")].filter(visible).map(canvas => ({ rect: rectData(canvas), width: canvas.width, height: canvas.height })),
      };
    });
    result.status = observed.url.startsWith("http") ? "accessible" : "page_unavailable";
    result.final_url = observed.url;
    result.title = observed.title;
    result.ready_state = observed.readyState;
    result.images = observed.images;
    result.iframes = observed.iframes;
    result.canvases = observed.canvases;
  } catch (error) {
    result.error = String(error).slice(0, 1000);
    try { await page.goto("about:blank"); } catch {}
  }
  manifest[sourceUrl] = result;
  await writeFile(OUTPUT_PATH, JSON.stringify(manifest, null, 2), "utf8");
}

const values = Object.values(manifest);
console.log(JSON.stringify({ total: values.length, accessible: values.filter(item => item.status === "accessible").length, unavailable: values.filter(item => item.status !== "accessible").length, withImages: values.filter(item => item.images.length).length, withIframes: values.filter(item => item.iframes.length).length, output: OUTPUT_PATH }, null, 2));
