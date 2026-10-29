import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const SPACE_ID = 50;
const PAGE_LABEL = "p1";
const OUTPUT = `${ROOT}/outputs/promotion_visibility_completeness_live_20260921.json`;

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (match) values[match[1]] = match[2].replace(/^['"]|['"]$/g, "");
  }
  return values;
}

async function fetchPromotion(id) {
  const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
  const base = String(env.SUPABASE_URL || "").replace(/\/$/, "");
  const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
  const response = await fetch(`${base}/rest/v1/clinic_promotions?select=promotion_id,business_id,promotion_title,source_url,updated_at&promotion_id=eq.${id}`, {
    headers: { apikey: key, Authorization: `Bearer ${key}` },
  });
  if (!response.ok) throw new Error(`promotion ${id} read failed: ${response.status}`);
  const rows = await response.json();
  if (rows.length !== 1) throw new Error(`promotion ${id} matched ${rows.length}`);
  return rows[0];
}

const id = Number(process.argv.find((arg) => /^--id=/.test(arg))?.split("=")[1] || 101);
const promotion = await fetchPromotion(id);
const task = await taskSpace(SPACE_ID);
const page = task.page(PAGE_LABEL);
const evidence = {
  captured_at: new Date().toISOString(),
  task_space_id: task.spaceId,
  promotion_id: id,
  business_id: promotion.business_id,
  source_url: promotion.source_url,
  status: "page_unavailable",
};

try {
  try { await page.goto(promotion.source_url); } catch (error) {
    const currentUrl = await page.url();
    if (!currentUrl.startsWith("http") || currentUrl.includes("chrome-error")) throw error;
  }
  try { await page.waitForLoadState("domcontentloaded"); } catch {}
  const captured = await page.evaluate(() => {
    const visible = (element) => {
      if (!(element instanceof Element)) return false;
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && style.visibility !== "collapse" && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
    };
    const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const cards = [...document.querySelectorAll("a.card-feature-card")]
      .filter(visible)
      .map((card) => {
        const rect = card.getBoundingClientRect();
        const text = clean((card.innerText || "").replace(/\bShop Now\b/gi, ""));
        const section = card.closest("section");
        return {
          text,
          href: card.href,
          rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
          selector: "a.card-feature-card",
          section_class: section?.className || null,
          section_text: clean(section?.querySelector("h1,h2,h3,h4,h5,h6")?.innerText || ""),
        };
      })
      .filter((card) => card.text && /\$\s*[\d,]+(?:\.\d{1,2})?/i.test(card.text));
    return { url: location.href, title: document.title, body_text: document.body.innerText || "", cards };
  });
  evidence.status = captured.cards.length ? "page_accessible" : "page_accessible_no_cards";
  evidence.final_url = captured.url;
  evidence.page_title = captured.title;
  evidence.body_text = captured.body_text;
  evidence.cards = captured.cards;
} catch (error) {
  evidence.error = String(error).slice(0, 1000);
  try { evidence.final_url = await page.url(); } catch {}
}

await writeFile(OUTPUT, JSON.stringify(evidence, null, 2) + "\n", "utf8");
console.log(JSON.stringify({ output: OUTPUT, promotion_id: id, status: evidence.status, cards: evidence.cards?.length || 0 }, null, 2));
