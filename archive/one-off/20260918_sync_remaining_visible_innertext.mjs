import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const OUTPUT = `${ROOT}/outputs/remaining_visible_innertext_sync_20260918.json`;
const APPLY = process.argv.includes("--apply");
const TARGETS = new Map([
  [815, [
    "Laser Hair Removal | Unlimited for One Year Promo",
    "Regular price $1,800.00 USD; Sale price $649.00 USD",
    "One Year Unlimited $649",
    "Why Choose Laser Hair Removal at BioRevive Spa?",
    "Ready to Ditch Waxing and Shaving?",
    "Benefits of Laser Hair Removal",
    "Long-Lasting Smoothness",
    "Safe and Comfortable",
    "Fast Treatments, Fewer Sessions",
    "Confidence Boost",
    "What Areas Can Be Treated?",
    "Frequently Asked Questions - Laser Hair Removal Newport Beach",
    "How many sessions will I need for permanent results?",
    "Is Optimas Max safe?",
    "Is Optimas Max safe for all skin types?",
    "Does laser hair removal hurt?",
    "Can I return to normal activities after treatment?",
    "Why choose BioRevive Spa in Newport Beach?",
  ]],
  [3516, [
    "Transform Your Skin: Exclusive Halo Tribrid & BBL Packages",
    "Unveil your most radiant complexion yet with our advanced laser resurfacing packages. For a limited time, book the Halo Tribrid for $1,999 or maximize your results with the BBL + Halo Tribrid combo for $2,450. Both packages include a complimentary, medical-grade post-procedure kit to protect your investment and accelerate healing.",
    "Need a skin refresh but can't afford the downtime? Revitalize your complexion without pressing pause on your busy schedule. Choose our deeply rejuvenating ClearSilk Laser for just $240, or opt for our comprehensive Laser Facial for $375 to achieve an instant, luminous glow.",
    "Hit the reset button on your skin's texture and tone by Monday. Take advantage of our introductory pricing on the Nano Peel Laser-your perfect \"weekend peel.\" At just $399 (a $500 value), this treatment precisely polishes away dullness and imperfections with minimal downtime, leaving you with a fresh, youthful radiance.",
    "Consistency is the secret to a flawless complexion. Secure your glow year-round with our exclusive monthly facial memberships. Choose the House Foundation ($229/month) or the House Essential ($349/month) to receive premium, curated revitalization treatments designed to give you that coveted \"glass skin\" aesthetic every single month.",
    "Glow on the Go: Zero-Downtime Laser Facials",
    "The Ultimate \"Weekend Peel\": Nano Laser Peel Intro Offer",
    "Maintain the Glow: \"Glass Skin\" Facial Memberships",
  ]],
  [3543, [
    "New patients only: Neurotoxin treatments for $8/unit with 50+ units.",
    "exclusive aesthetic specials every Tuesday at our Broadway location and every Wednesday at our Sunrise location.",
    "Walk-ins are welcome.",
  ]],
  [3549, [
    "Botox $14.00 per unit",
    "Botox $13.00 – cost per unit if getting 50+ units",
    "$1050 – cost of 100 units (1 bottle) of Botox. Savings of $350.",
    "Save $ on Filler when doing multiple syringes.",
  ]],
  [3584, [
    "Buy 2 syringes of filler - Get in 1 syringe FREE!",
    "The Ultimate Skin Rewind RF Microneedling + Moxi Laser Treatment NOT available in RiNo currently (subject to change)",
    "Non-members: $1,125 (save $374)",
    "Members: $999 (save $499)",
  ]],
]);

function parseEnv(text) { const values = {}; for (const line of text.split(/\r?\n/)) { const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/); if (match) values[match[1]] = match[2].replace(/^['"]|['"]$/g, ""); } return values; }
async function fetchRows(baseUrl, key) { const response = await fetch(`${baseUrl}/rest/v1/clinic_promotions?select=promotion_id,source_url,promotion_content,is_active,updated_at&promotion_id=in.(${[...TARGETS.keys()].join(",")})`, { headers: { apikey: key, Authorization: `Bearer ${key}` } }); if (!response.ok) throw new Error(await response.text()); return response.json(); }
async function patchOne(baseUrl, key, row, content) { const response = await fetch(`${baseUrl}/rest/v1/clinic_promotions?promotion_id=eq.${row.promotion_id}&updated_at=eq.${encodeURIComponent(row.updated_at)}`, { method: "PATCH", headers: { apikey: key, Authorization: `Bearer ${key}`, "Content-Type": "application/json", Prefer: "return=representation" }, body: JSON.stringify({ promotion_content: content, is_offer_extracted: false, updated_at: new Date().toISOString() }) }); if (!response.ok) throw new Error(await response.text()); const rows = await response.json(); if (rows.length !== 1) throw new Error(`update matched ${rows.length}`); return rows[0]; }

function buildContent(row) {
  const oldContent = Array.isArray(row.promotion_content) ? row.promotion_content : [];
  const id = Number(row.promotion_id);
  if (id === 815) {
    const removed = new Set(["**Regular price $649.00 USD**", "*Regular price $649.00 USD**", "**Regular price ~$1,800.00 USD~ Sale price $649.00 USD**", "*Regular price ~$1,800.00 USD~ Sale price $649.00 USD**"]);
    return [...oldContent.filter(value => !removed.has(value)), "Regular price $1,800.00 USD; Sale price $649.00 USD"];
  }
  if (id === 3516) return oldContent.filter(value => value !== "[](tel:+1 714 596 1053)");
  if (id === 3543) return oldContent.map(value => value === "Tuesday at Broadway location; Wednesday at Sunrise location." ? "exclusive aesthetic specials every Tuesday at our Broadway location and every Wednesday at our Sunrise location." : value);
  if (id === 3549 || id === 3584) return TARGETS.get(id);
  return oldContent;
}

const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8")); const baseUrl = String(env.SUPABASE_URL || "").replace(/\/$/, ""); const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY; const rows = await fetchRows(baseUrl, key); const plan = { mode: APPLY ? "apply" : "dry_run", generated_at: new Date().toISOString(), changes: [] };
for (const row of rows) { if (!TARGETS.has(Number(row.promotion_id)) || row.is_active !== true) continue; const content = buildContent(row); const change = { promotion_id: row.promotion_id, source_url: row.source_url, old_updated_at: row.updated_at, old_content: row.promotion_content, new_content: content }; if (APPLY) { const updated = await patchOne(baseUrl, key, row, content); change.status = "updated"; change.new_updated_at = updated.updated_at; } plan.changes.push(change); }
await writeFile(OUTPUT, JSON.stringify(plan, null, 2), "utf8"); console.log(JSON.stringify({ mode: plan.mode, promotion_count: plan.changes.length, output: OUTPUT }, null, 2));
