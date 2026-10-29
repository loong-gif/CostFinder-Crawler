import { readFile, writeFile } from "node:fs/promises";
import { cardSpec, fingerprint, sameOffer } from "./iconique_101_offer_reconciliation_lib.mjs";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const PARENT = `${ROOT}/outputs/promotion_visibility_completeness_live_20260921.json`;
const EVIDENCE = `${ROOT}/outputs/iconique_101_service_evidence_20260921.json`;
const DRY_RUN = `${ROOT}/outputs/iconique_101_offer_reconciliation_dry_run_20260921.json`;
const APPLY = `${ROOT}/outputs/iconique_101_offer_reconciliation_apply_20260921.json`;

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) { const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/); if (match) values[match[1]] = match[2].replace(/^['"]|['"]$/g, ""); }
  return values;
}

const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
const base = String(env.SUPABASE_URL || "").replace(/\/$/, "");
const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
const headers = { apikey: key, Authorization: `Bearer ${key}`, "Content-Type": "application/json" };
const apply = process.argv.includes("--apply");

async function request(path, options = {}) {
  const response = await fetch(`${base}/rest/v1/${path}`, { ...options, headers: { ...headers, ...(options.headers || {}) } });
  if (!response.ok) throw new Error(`${path}: ${response.status} ${await response.text()}`);
  return response.json();
}

async function state() {
  const promotion = (await request("clinic_promotions?select=promotion_id,business_id,promotion_title,source_url,promotion_content,is_active,is_offer_extracted,updated_at&promotion_id=eq.101"))[0];
  const offers = await request("promo_offer_master?select=id,business_id,promotion_id,membership_plan_id,is_active,is_package,price_model,regular_price,discount_price,discount_percent,discount_amount,is_membership_required,is_new_customer_required,offer_fingerprint,offer_raw_text,conditions,updated_at,promo_offer_items(offer_item_id,item_name,service_id,delivered_units,unit_price,updated_at)&promotion_id=eq.101&order=id.asc");
  const services = await request("clinic_services?select=service_id,business_id,service_name,regular_price,unit_type,service_area,source_url,updated_at&business_id=eq.2567&order=service_id.asc");
  if (!promotion || promotion.business_id !== 2567 || promotion.is_active !== true) throw new Error("promotion 101 precondition failed");
  return { promotion, offers, services };
}

function buildPlan(current) {
  const parent = JSON.parse(readFileSync(PARENT, "utf8"));
  const evidence = JSON.parse(readFileSync(EVIDENCE, "utf8"));
  const evidenceByUrl = new Map((evidence.results || []).map((row) => [row.product_url, row]));
  const serviceByName = new Map(current.services.map((row) => [row.service_name, row]));
  const cards = parent.cards || [];
  const review = [];
  const ready = [];
  for (const card of cards) {
    const parsed = cardSpec(card, evidenceByUrl.get(card.href));
    if (parsed.status !== "ready") { review.push({ card_text: card.text, product_url: card.href, reason: parsed.reason }); continue; }
    const spec = { ...parsed, offer_raw_text: card.text, discount_price: parsed.price, regular_price: null, discount_percent: null, discount_amount: null, price_model: "total", is_membership_required: false, is_new_customer_required: false, conditions: [] };
    spec.items = spec.items.map((item) => ({ ...item, service: serviceByName.get(item.service_name) || null }));
    const existing = current.offers.find((offer) => sameOffer(offer, spec));
    if (existing) {
      ready.push({ card_text: card.text, action: existing.is_active ? "existing" : "activate", offer_id: existing.id, offer_updated_at: existing.updated_at, spec });
      continue;
    }
    ready.push({ card_text: card.text, action: "create", spec: { ...spec, offer_fingerprint: fingerprint(spec) } });
  }
  return { current, cards, ready, review, complete: review.length === 0 && ready.length === cards.length };
}

function samePromotionSnapshot(before, after) {
  return before.promotion_id === after.promotion_id
    && before.business_id === after.business_id
    && before.promotion_title === after.promotion_title
    && before.source_url === after.source_url
    && before.updated_at === after.updated_at
    && JSON.stringify(before.promotion_content) === JSON.stringify(after.promotion_content);
}

const { readFileSync } = await import("node:fs");
const current = await state();
const plan = buildPlan(current);
const output = {
  generated_at: new Date().toISOString(),
  mode: apply ? "apply" : "dry_run",
  no_database_writes: !apply,
  promotion: { promotion_id: current.promotion.promotion_id, business_id: current.promotion.business_id, promotion_title: current.promotion.promotion_title, source_url: current.promotion.source_url, updated_at: current.promotion.updated_at, is_offer_extracted: current.promotion.is_offer_extracted },
  ready: plan.ready,
  review: plan.review,
  complete: plan.complete,
  existing_offers: plan.ready.filter((entry) => entry.action === "existing").map((entry) => ({ card_text: entry.card_text, offer_id: entry.offer_id, status: "already_applied" })),
  created_services: [],
  created_offers: [],
  conflicts: [],
};

async function activateOffer(entry, master, fresh) {
  const currentOffer = fresh.offers.find((offer) => offer.id === master.id);
  if (!currentOffer || !sameOffer(currentOffer, entry.spec) || currentOffer.is_active || currentOffer.updated_at !== master.updated_at) {
    output.conflicts.push({ card_text: entry.card_text, reason: "offer_changed_before_activation", offer_id: master.id });
    return;
  }
  const activated = await request(`promo_offer_master?id=eq.${master.id}&is_active=eq.false`, { method: "PATCH", headers: { Prefer: "return=representation" }, body: JSON.stringify({ is_active: true, updated_at: new Date().toISOString() }) });
  if (activated.length !== 1) throw new Error("offer_activation_conflict");
  output.created_offers.push({ card_text: entry.card_text, status: "created", offer_id: master.id });
}

if (apply) {
  for (const entry of plan.ready.filter((item) => item.action !== "existing")) {
    const fresh = await state();
    if (!samePromotionSnapshot(current.promotion, fresh.promotion) || fresh.promotion.is_active !== true) { output.conflicts.push({ card_text: entry.card_text, reason: "promotion_changed" }); continue; }
    if (entry.action === "activate") {
      const existing = fresh.offers.find((offer) => offer.id === entry.offer_id);
      if (!existing || existing.updated_at !== entry.offer_updated_at) { output.conflicts.push({ card_text: entry.card_text, reason: "offer_changed_before_activation", offer_id: entry.offer_id }); continue; }
      try { await activateOffer(entry, existing, fresh); } catch (error) { output.conflicts.push({ card_text: entry.card_text, reason: "activation_failed", offer_id: entry.offer_id, error: String(error) }); }
      continue;
    }
    const items = [];
    let blocked = false;
    for (const item of entry.spec.items) {
      let service = fresh.services.find((row) => row.service_name === item.service_name);
      if (!service) {
        const evidenceRow = JSON.parse(readFileSync(EVIDENCE, "utf8")).results.find((row) => row.product_url === (JSON.parse(readFileSync(PARENT, "utf8")).cards.find((card) => card.text === entry.card_text)?.href));
        if (!evidenceRow || evidenceRow.status !== "verified") { output.conflicts.push({ card_text: entry.card_text, reason: "service_evidence_changed" }); blocked = true; break; }
        const inserted = await request("clinic_services", { method: "POST", headers: { Prefer: "return=representation" }, body: JSON.stringify([{ business_id: 2567, service_name: item.service_name, regular_price: null, unit_type: "others", source_url: evidenceRow.final_url }]) });
        service = inserted[0]; output.created_services.push({ service_id: service.service_id, service_name: service.service_name, source_url: service.source_url });
      }
      items.push({ ...item, service });
    }
    if (blocked) continue;
    const duplicate = fresh.offers.find((offer) => sameOffer(offer, entry.spec));
    if (duplicate) { output.created_offers.push({ card_text: entry.card_text, status: "already_exists", offer_id: duplicate.id }); continue; }
    const master = await request("promo_offer_master", { method: "POST", headers: { Prefer: "return=representation" }, body: JSON.stringify([{ business_id: 2567, promotion_id: 101, membership_plan_id: null, is_active: false, is_package: entry.spec.is_package, price_model: "total", regular_price: null, discount_price: entry.spec.discount_price, discount_percent: null, discount_amount: null, is_membership_required: false, is_new_customer_required: false, conditions: [], offer_raw_text: entry.spec.offer_raw_text, offer_fingerprint: entry.spec.offer_fingerprint }]) });
    const masterRow = master[0];
    try {
      await request("promo_offer_items", { method: "POST", headers: { Prefer: "return=representation" }, body: JSON.stringify(items.map((item) => ({ offer_id: masterRow.id, service_id: item.service.service_id, item_name: item.service.service_name, delivered_units: item.delivered_units, unit_price: null }))) });
      await activateOffer(entry, masterRow, await state());
    } catch (error) {
      output.conflicts.push({ card_text: entry.card_text, reason: "item_or_activation_failed", offer_id: masterRow.id, error: String(error) });
    }
  }
  const finalState = await state();
  const finalPlan = buildPlan(finalState);
  if (finalPlan.complete && finalPlan.review.length === 0 && finalState.promotion.is_offer_extracted !== true) {
    const updated = await request(`clinic_promotions?promotion_id=eq.101&updated_at=eq.${encodeURIComponent(finalState.promotion.updated_at)}&is_active=eq.true`, { method: "PATCH", headers: { Prefer: "return=representation" }, body: JSON.stringify({ is_offer_extracted: true, updated_at: new Date().toISOString() }) });
    if (updated.length !== 1) output.conflicts.push({ promotion_id: 101, reason: "promotion_flag_conflict" }); else output.promotion.is_offer_extracted = true;
  }
  output.final_promotion = { updated_at: finalState.promotion.updated_at, is_offer_extracted: finalState.promotion.is_offer_extracted };
}

output.no_database_writes = !apply || (output.created_services.length === 0 && output.created_offers.length === 0);
await writeFile(apply ? APPLY : DRY_RUN, JSON.stringify(output, null, 2) + "\n", "utf8");
console.log(JSON.stringify({ output: apply ? APPLY : DRY_RUN, mode: output.mode, cards: plan.cards.length, ready: plan.ready.length, review: plan.review.length, complete: plan.complete, created_offers: output.created_offers.length, no_database_writes: output.no_database_writes }, null, 2));
