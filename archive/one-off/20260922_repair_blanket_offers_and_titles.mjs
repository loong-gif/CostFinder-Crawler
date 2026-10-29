/**
 * Purpose: repair seven confirmed blanket offers and restore only four
 * evidence-backed source titles (10475, 10441, 10183, 8716).
 * Inputs: the existing promo_offer_master, promo_offer_items, clinic_services,
 * clinic_promotions, clinic_memberships, saved_deals, claims, and listing_requests tables.
 * Database writes: no writes by default; pass --apply only after a dry run.
 * Completion status: in progress.
 */
import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const BLANKET_GROUPS = [
  { canonicalOfferId: 10541, duplicateOfferIds: [10542, 10543], title: "15% off all products and services" },
  { canonicalOfferId: 10559, duplicateOfferIds: [10560, 10561], title: "20% off all services" },
  { canonicalOfferId: 10562, duplicateOfferIds: [10563, 10564, 10565], title: "15% off all services" },
  { canonicalOfferId: 10570, duplicateOfferIds: [10571, 10572], title: "New-To-You Special — 10% OFF Any Service" },
  { canonicalOfferId: 10619, duplicateOfferIds: [10620, 10621, 10622, 10623, 10624, 10625, 10626, 10627, 10628], title: "10% off all services" },
  { canonicalOfferId: 10629, duplicateOfferIds: [10630, 10631], title: "50% off all services" },
  { canonicalOfferId: 10640, duplicateOfferIds: [10641, 10642, 10643, 10644, 10645, 10646, 10647, 10648], title: "15% off all products and services" },
];
const TITLE_RESTORATIONS = [
  { offerId: 10475, title: "Non-Surgical Brow Lift $79 (regular $135)", evidenceSource: "clinic_promotions.promotion_content", confidence: "exact" },
  { offerId: 10441, title: "Myers Shots $35", evidenceSource: "clinic_promotions.promotion_content", confidence: "exact" },
  { offerId: 10183, title: "New patient: 20% off first Skin Tightening treatment", evidenceSource: "promo_offer_master.offer_raw_text", confidence: "exact" },
  { offerId: 8716, title: "Chin Tox ($100 Value)", evidenceSource: "clinic_memberships.benefits", confidence: "exact" },
];
const TARGET_OFFER_IDS = BLANKET_GROUPS.flatMap(({ canonicalOfferId, duplicateOfferIds }) => [canonicalOfferId, ...duplicateOfferIds]);
const APPLY = process.argv.includes("--apply");

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (match) values[match[1]] = match[2].replace(/^[\"']|[\"']$/g, "");
  }
  return values;
}

async function request(baseUrl, key, path, options = {}) {
  const response = await fetch(`${baseUrl}/rest/v1/${path}`, {
    ...options,
    headers: { apikey: key, Authorization: `Bearer ${key}`, ...options.headers },
  });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}: ${await response.text()}`);
  return response.status === 204 ? [] : response.json();
}

async function fetchRows(baseUrl, key, table, select, filter = "") {
  const rows = [];
  for (let offset = 0; ; offset += 1000) {
    const page = await request(baseUrl, key, `${table}?select=${encodeURIComponent(select)}&limit=1000&offset=${offset}${filter}`);
    rows.push(...page);
    if (page.length < 1000) return rows;
  }
}

async function patchExactlyOne(baseUrl, key, table, filters, body) {
  const params = new URLSearchParams(filters);
  const rows = await request(baseUrl, key, `${table}?${params}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", Prefer: "return=representation" },
    body: JSON.stringify(body),
  });
  if (rows.length !== 1) throw new Error(`${table} expected one row but updated ${rows.length}`);
  return rows[0];
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function linkedRows(offers, items, services) {
  const offerById = new Map(offers.map((row) => [row.id, row]));
  const serviceById = new Map(services.map((row) => [row.service_id, row]));
  return items.map((item) => ({ ...item, offer: offerById.get(item.offer_id), service: serviceById.get(item.service_id) }));
}

const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
const baseUrl = String(env.SUPABASE_URL || "").replace(/\/$/, "");
const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
if (!baseUrl || !key) throw new Error("SUPABASE_URL and a server key are required");

const titleOfferIds = TITLE_RESTORATIONS.map(({ offerId }) => offerId);
const allOfferIds = [...new Set([...TARGET_OFFER_IDS, ...titleOfferIds])];
const offerFilter = `&id=in.(${allOfferIds.join(",")})&order=id.asc`;
const offers = await fetchRows(baseUrl, key, "promo_offer_master", "id,business_id,promotion_id,membership_plan_id,offer_raw_text,discount_percent,discount_amount,discount_price,price_model,is_active,is_package,conditions,offer_fingerprint,updated_at", offerFilter);
const items = await fetchRows(baseUrl, key, "promo_offer_items", "offer_item_id,offer_id,service_id,item_name,delivered_units,unit_price,updated_at", `&offer_id=in.(${allOfferIds.join(",")})&order=offer_item_id.asc`);
const serviceIds = [...new Set(items.map(({ service_id }) => service_id).filter((value) => value != null))];
const services = serviceIds.length ? await fetchRows(baseUrl, key, "clinic_services", "service_id,business_id,service_name", `&service_id=in.(${serviceIds.join(",")})`) : [];
const promotionIds = [...new Set(offers.map(({ promotion_id }) => promotion_id).filter((value) => value != null))];
const membershipPlanIds = [...new Set(offers.map(({ membership_plan_id }) => membership_plan_id).filter((value) => value != null))];
const promotions = promotionIds.length ? await fetchRows(baseUrl, key, "clinic_promotions", "promotion_id,business_id,promotion_title,promotion_content", `&promotion_id=in.(${promotionIds.join(",")})`) : [];
const memberships = membershipPlanIds.length ? await fetchRows(baseUrl, key, "clinic_memberships", "plan_id,business_id,membership_name,benefits", `&plan_id=in.(${membershipPlanIds.join(",")})`) : [];
const savedDeals = await fetchRows(baseUrl, key, "saved_deals", "deal_id", `&deal_id=in.(${TARGET_OFFER_IDS.join(",")})`);
const claims = await fetchRows(baseUrl, key, "claims", "deal_id", `&deal_id=in.(${TARGET_OFFER_IDS.join(",")})`);
const listingRequests = await fetchRows(baseUrl, key, "listing_requests", "offer_id", `&offer_id=in.(${TARGET_OFFER_IDS.join(",")})`);

// This staging covers every currently linked item, not only the four rows we
// can safely restore automatically in this run. All other rows stay review.
const allOffers = await fetchRows(baseUrl, key, "promo_offer_master", "id,promotion_id,membership_plan_id,offer_raw_text,is_package", "&order=id.asc");
const allLinkedItems = await fetchRows(baseUrl, key, "promo_offer_items", "offer_item_id,offer_id,service_id,item_name,delivered_units,unit_price,updated_at", "&service_id=not.is.null&order=offer_item_id.asc");
const allServices = await fetchRows(baseUrl, key, "clinic_services", "service_id,business_id,service_name", "&order=service_id.asc");

assert(offers.length === allOfferIds.length, `expected ${allOfferIds.length} target offers, found ${offers.length}`);
const rows = linkedRows(offers, items, services);
const countBy = (values, key) => values.reduce((counts, value) => counts.set(value[key], (counts.get(value[key]) || 0) + 1), new Map());
const savedDealCounts = countBy(savedDeals, "deal_id");
const claimCounts = countBy(claims, "deal_id");
const listingRequestCounts = countBy(listingRequests, "offer_id");
for (const group of BLANKET_GROUPS) {
  const canonicalRows = rows.filter((row) => row.offer_id === group.canonicalOfferId);
  assert(canonicalRows.length === 1, `canonical offer ${group.canonicalOfferId} must have exactly one item`);
  assert(canonicalRows[0].offer.is_active === true && canonicalRows[0].offer.is_package === false, `canonical offer ${group.canonicalOfferId} is not an active non-package offer`);
  for (const duplicateOfferId of group.duplicateOfferIds) {
    const duplicate = offers.find((row) => row.id === duplicateOfferId);
    assert(duplicate?.is_active === true, `duplicate offer ${duplicateOfferId} is no longer active`);
    assert(!savedDealCounts.get(duplicateOfferId) && !claimCounts.get(duplicateOfferId) && !listingRequestCounts.get(duplicateOfferId), `duplicate offer ${duplicateOfferId} has a user reference`);
  }
}
for (const restoration of TITLE_RESTORATIONS) {
  const targetRows = rows.filter((row) => row.offer_id === restoration.offerId);
  assert(targetRows.length === 1, `title restoration offer ${restoration.offerId} must have exactly one item`);
  assert(targetRows[0].offer.is_package === false, `title restoration offer ${restoration.offerId} is a package`);
}

const capturedAt = new Date().toISOString();
const snapshotRows = rows.map((row) => ({
  offer_id: row.offer_id,
  offer_item_id: row.offer_item_id,
  business_id: row.offer.business_id,
  promotion_id: row.offer.promotion_id,
  membership_plan_id: row.offer.membership_plan_id,
  offer_raw_text: row.offer.offer_raw_text,
  discount_percent: row.offer.discount_percent,
  discount_amount: row.offer.discount_amount,
  discount_price: row.offer.discount_price,
  price_model: row.offer.price_model,
  is_active: row.offer.is_active,
  is_package: row.offer.is_package,
  conditions: row.offer.conditions,
  item_name: row.item_name,
  service_id: row.service_id,
  service_name: row.service?.service_name ?? null,
  delivered_units: row.delivered_units,
  unit_price: row.unit_price,
  offer_fingerprint: row.offer.offer_fingerprint,
  updated_at: row.updated_at,
}));
const blanketPlan = BLANKET_GROUPS.map((group) => ({
  ...group,
  action: "deactivate_duplicates_and_convert_canonical_to_blanket",
  canonical_item: rows.find((row) => row.offer_id === group.canonicalOfferId)?.offer_item_id,
}));
const restorationByOfferId = new Map(TITLE_RESTORATIONS.map((row) => [row.offerId, row]));
const allRows = linkedRows(allOffers, allLinkedItems, allServices);
assert(allRows.every((row) => row.offer), "a linked promo_offer_item has no promo_offer_master row");
const promotionById = new Map(promotions.map((row) => [row.promotion_id, row]));
const membershipByPlanId = new Map(memberships.map((row) => [row.plan_id, row]));
const titleStaging = allRows.map((row) => {
  const restoration = restorationByOfferId.get(row.offer_id);
  const evidenceText = restoration?.evidenceSource === "clinic_promotions.promotion_content"
    ? promotionById.get(row.offer.promotion_id)?.promotion_content ?? null
    : restoration?.evidenceSource === "clinic_memberships.benefits"
      ? membershipByPlanId.get(row.offer.membership_plan_id)?.benefits ?? null
      : restoration ? row.offer.offer_raw_text : null;
  return {
    offer_item_id: row.offer_item_id,
    offer_id: row.offer_id,
    current_item_name: row.item_name,
    proposed_item_name: restoration?.title ?? row.item_name,
    service_id: row.service_id,
    service_name: row.service?.service_name ?? null,
    evidence_source: restoration?.evidenceSource ?? null,
    evidence_parent_id: row.offer.promotion_id ?? row.offer.membership_plan_id ?? null,
    evidence_text: evidenceText,
    confidence: restoration?.confidence ?? "review",
    action: restoration ? "restore" : "review",
  };
});
const expectedAfter = snapshotRows.map((row) => {
  const group = BLANKET_GROUPS.find((entry) => entry.canonicalOfferId === row.offer_id);
  if (group) return { ...row, service_id: null, service_name: null, item_name: group.title, delivered_units: null, unit_price: null };
  if (BLANKET_GROUPS.some((entry) => entry.duplicateOfferIds.includes(row.offer_id))) return { ...row, is_active: false };
  const restoration = restorationByOfferId.get(row.offer_id);
  return restoration ? { ...row, item_name: restoration.title } : row;
});

await writeFile(`${ROOT}/one-off/20260922_blanket_offer_before.json`, JSON.stringify({ captured_at: capturedAt, rows: snapshotRows }, null, 2) + "\n");
await writeFile(`${ROOT}/one-off/20260922_blanket_offer_plan.json`, JSON.stringify({ captured_at: capturedAt, blanket_groups: blanketPlan, title_restorations: TITLE_RESTORATIONS }, null, 2) + "\n");
await writeFile(`${ROOT}/one-off/20260922_blanket_offer_after_expected.json`, JSON.stringify({ captured_at: capturedAt, rows: expectedAfter }, null, 2) + "\n");
await writeFile(`${ROOT}/one-off/20260922_offer_title_restore.json`, JSON.stringify({ captured_at: capturedAt, rows: titleStaging }, null, 2) + "\n");

const result = { mode: APPLY ? "apply" : "dry_run", captured_at: capturedAt, deactivated_offers: [], blanket_items_updated: [], titles_restored: [], conflicts: [] };
if (APPLY) {
  for (const group of BLANKET_GROUPS) {
    for (const offerId of group.duplicateOfferIds) {
      const offer = offers.find((row) => row.id === offerId);
      const updated = await patchExactlyOne(baseUrl, key, "promo_offer_master", { id: `eq.${offerId}`, is_active: "eq.true", updated_at: `eq.${offer.updated_at}` }, { is_active: false });
      assert(updated.is_active === false, `duplicate offer ${offerId} did not deactivate`);
      result.deactivated_offers.push({ offer_id: offerId, updated_at: updated.updated_at });
    }
  }
  for (const group of BLANKET_GROUPS) {
    const item = rows.find((row) => row.offer_id === group.canonicalOfferId);
    const updated = await patchExactlyOne(baseUrl, key, "promo_offer_items", { offer_item_id: `eq.${item.offer_item_id}`, updated_at: `eq.${item.updated_at}` }, { service_id: null, item_name: group.title, delivered_units: null, unit_price: null });
    assert(updated.service_id === null && updated.item_name === group.title, `canonical offer ${group.canonicalOfferId} did not become the planned blanket item`);
    result.blanket_items_updated.push({ offer_id: group.canonicalOfferId, offer_item_id: item.offer_item_id, item_name: updated.item_name, service_id: updated.service_id });
  }
  for (const restoration of TITLE_RESTORATIONS) {
    const item = rows.find((row) => row.offer_id === restoration.offerId);
    const updated = await patchExactlyOne(baseUrl, key, "promo_offer_items", { offer_item_id: `eq.${item.offer_item_id}`, updated_at: `eq.${item.updated_at}` }, { item_name: restoration.title });
    assert(updated.item_name === restoration.title && updated.service_id === item.service_id, `title restoration ${restoration.offerId} did not preserve its source title and service mapping`);
    result.titles_restored.push({ offer_id: restoration.offerId, offer_item_id: item.offer_item_id, item_name: updated.item_name, service_id: updated.service_id });
  }
}
await writeFile(`${ROOT}/one-off/20260922_blanket_offer_${result.mode}.json`, JSON.stringify(result, null, 2) + "\n");
console.log(JSON.stringify({ ...result, artifacts: ["one-off/20260922_blanket_offer_before.json", "one-off/20260922_blanket_offer_plan.json", "one-off/20260922_blanket_offer_after_expected.json", "one-off/20260922_offer_title_restore.json"] }, null, 2));
