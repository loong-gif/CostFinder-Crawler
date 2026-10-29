import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const INPUT = `${ROOT}/outputs/promotion_visibility_completeness_live_20260921.json`;
const OUTPUT = `${ROOT}/outputs/iconique_101_service_evidence_20260921.json`;
const SPACE_ID = 50;
const PAGE_LABEL = "p1";

const parent = JSON.parse(await readFile(INPUT, "utf8"));
const cards = (parent.cards || []).filter((card) => card.href);
const task = await taskSpace(SPACE_ID);
const page = task.page(PAGE_LABEL);
const results = [];
for (const card of cards) {
  const result = { card_text: card.text, product_url: card.href, status: "page_unavailable", checked_at: new Date().toISOString() };
  const expectedToken = new URL(card.href).pathname.split("/").filter(Boolean).at(-2)?.split("-")[0] || "";
  try {
    try { await page.goto(card.href); } catch (error) {
      const currentUrl = await page.url();
      if (!currentUrl.startsWith("http") || currentUrl.includes("chrome-error")) throw error;
    }
    try { await page.waitForLoadState("domcontentloaded"); } catch {}
    const productLoaded = await page.waitForFunction((token) => {
      const visible = (element) => {
        if (!(element instanceof Element)) return false;
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden" && style.visibility !== "collapse" && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
      };
      const normalize = (value) => String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
      const title = normalize(document.title);
      return !title.includes("cart") && [...document.querySelectorAll("h1,h2,h3")].some((element) => visible(element) && normalize(element.innerText).includes(normalize(token)));
    }, expectedToken, { timeout: 10000 }).then(() => true).catch(() => false);
    if (!productLoaded) {
      result.status = "page_incomplete";
      result.final_url = await page.url();
      result.page_title = await page.title();
      results.push(result);
      continue;
    }
    const captured = await page.evaluate((token) => {
      const visible = (element) => {
        if (!(element instanceof Element)) return false;
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden" && style.visibility !== "collapse" && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
      };
      const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
      const body = clean(document.body?.innerText || "");
      const cf = /just a moment|security verification|verify you are human|cloudflare/i.test(body) || /just a moment/i.test(document.title);
      const headings = [...document.querySelectorAll("h1,h2,h3")].filter(visible).map((element) => clean(element.innerText)).filter(Boolean);
      const detail = [...document.querySelectorAll("main p, main li, [class*='product'] p, [class*='product'] li")].filter(visible).map((element) => clean(element.innerText)).filter(Boolean);
      const normalizedToken = clean(token).toLowerCase();
      const heading = [...document.querySelectorAll("h1,h2,h3")].find((element) => visible(element) && clean(element.innerText).toLowerCase().includes(normalizedToken));
      const productText = clean(heading?.closest("form")?.innerText || heading?.parentElement?.parentElement?.innerText || "");
      return {
        final_url: location.href,
        page_title: document.title,
        cf,
        headings,
        detail: [...new Set(detail)].slice(0, 30),
        product_text: productText.slice(0, 4000),
        visible_text: productText.slice(0, 4000),
      };
    }, expectedToken);
    Object.assign(result, captured, {
      status: captured.cf ? "cf_verification_required" : (captured.product_text.length > 15 ? "verified" : "page_incomplete"),
    });
    results.push(result);
    if (result.status === "cf_verification_required") break;
  } catch (error) {
    result.error = String(error).slice(0, 1000);
    results.push(result);
    break;
  }
}
const output = { captured_at: new Date().toISOString(), task_space_id: task.spaceId, parent_url: parent.final_url || parent.source_url, results };
await writeFile(OUTPUT, JSON.stringify(output, null, 2) + "\n", "utf8");
console.log(JSON.stringify({ output: OUTPUT, captured: results.filter((row) => row.status === "verified").length, blocked: results.filter((row) => row.status === "cf_verification_required").length, total: cards.length }, null, 2));
