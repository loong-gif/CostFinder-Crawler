/**
 * Purpose: repair the five reviewed Offer fingerprint collisions from the
 * 2026-09-22 duplicate preflight.
 * Inputs: the ten named promo_offer_master rows, their promo_offer_items,
 * business 765 clinic_services, linked promotion/membership evidence, and
 * user-reference tables for the three duplicate candidates.
 * Database writes: no writes by default; pass --apply to update data values.
 * Completion status: in progress; remove after the apply result is accepted.
 */
import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";
const TARGET_OFFER_IDS = [6701, 9472, 8562, 8661, 10425, 10682, 10533, 10599, 10537, 10594];
const DUPLICATE_OFFER_IDS = [9472, 10599, 10594];
const TARGET_PROMOTION_IDS = [2, 697];
const TARGET_MEMBERSHIP_IDS = [330, 331, 502];
const OLD_GENERIC_LASER_SERVICE_ID = 2659;
const NEW_SERVICE_SPECS = [
  {
    offerId: 8562,
    serviceArea: "Full Chest and Abdomen",
    sourceExcerpt: "Free front of the neck with purchase of Full Chest and Abdomen for only $159 (regular $190).",
  },
  {
    offerId: 8661,
    serviceArea: "Full Back (including shoulders)",
    sourceExcerpt: "Free back of the neck with Full Back including shoulders for $159 (regular $190).",
  },
];
const REBUILT_10682 = {
  offer_raw_text: "Face laser treatment with neck included at no additional charge",
  discount_price: 0,
  discount_percent: 100,
  conditions: ["Face laser treatment required", "Valid through September 30, 2026"],
};

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (match) values[match[1]] = match[2].replace(/^['"]|['"]$/g, "");
  }
  return values;
}

async function apiClient() {
  const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
  const baseUrl = String(env.SUPABASE_URL || "").replace(/\/$/, "");
  const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
  if (!baseUrl || !key) throw new Error("SUPABASE_URL or server key is missing");
  return { baseUrl, key };
}

async function request(api, path, options = {}) {
  const response = await fetch(`${api.baseUrl}/rest/v1/${path}`, {
    ...options,
    signal: AbortSignal.timeout(30000),
    headers: { apikey: api.key, Authorization: `Bearer ${api.key}`, ...(options.headers || {}) },
  });
  if (!response.ok) throw new Error(`Supabase REST returned HTTP ${response.status} for ${path}: ${await response.text()}`);
  if (response.status === 204) return [];
  return response.json();
}

async function fetchRows(api, table, select, query = "") {
  const rows = [];
  for (let offset = 0; ; offset += 1000) {
    const page = await request(api, `${table}?select=${encodeURIComponent(select)}&limit=1000&offset=${offset}${query}`);
    rows.push(...page);
    if (page.length < 1000) return rows;
  }
}

async function patchExactlyOne(api, table, filters, body) {
  const params = new URLSearchParams(filters);
  const updated = await request(api, `${table}?${params}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", Prefer: "return=representation" },
    body: JSON.stringify({ ...body, updated_at: new Date().toISOString() }),
  });
  if (updated.length !== 1) throw new Error(`${table} expected one row but updated ${updated.length}`);
  return updated[0];
}

async function insertExactlyOne(api, table, body) {
  const inserted = await request(api, table, {
    method: "POST",
    headers: { "Content-Type": "application/json", Prefer: "return=representation" },
    body: JSON.stringify([body]),
  });
  if (inserted.length !== 1) throw new Error(`${table} expected one inserted row but received ${inserted.length}`);
  return inserted[0];
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function oneItem(items, offerId) {
  const matches = items.filter((item) => item.offer_id === offerId);
  assert(matches.length === 1, `offer ${offerId} must have exactly one item; found ${matches.length}`);
  return matches[0];
}

function byId(rows, field) {
  return new Map(rows.map((row) => [Number(row[field]), row]));
}

async function loadSnapshot(api) {
  const offers = await fetchRows(
    api,
    "promo_offer_master",
    "id,business_id,promotion_id,membership_plan_id,is_active,is_package,price_model,regular_price,discount_price,discount_percent,discount_amount,is_membership_required,is_new_customer_required,offer_fingerprint,offer_raw_text,conditions,updated_at",
    `&id=in.(${TARGET_OFFER_IDS.join(",")})&order=id.asc`,
  );
  const items = await fetchRows(
    api,
    "promo_offer_items",
    "offer_item_id,offer_id,item_name,service_id,delivered_units,unit_price,updated_at",
    `&offer_id=in.(${TARGET_OFFER_IDS.join(",")})&order=offer_item_id.asc`,
  );
  const services = await fetchRows(
    api,
    "clinic_services",
    "service_id,business_id,service_name,service_area,unit_type,regular_price,source_url,updated_at",
    "&business_id=eq.765&order=service_id.asc",
  );
  const promotions = await fetchRows(
    api,
    "clinic_promotions",
    "promotion_id,business_id,is_active,promotion_content,source_url,updated_at",
    `&promotion_id=in.(${TARGET_PROMOTION_IDS.join(",")})&order=promotion_id.asc`,
  );
  const memberships = await fetchRows(
    api,
    "clinic_memberships",
    "plan_id,business_id,membership_name,membership_price,billing_period,minimum_commitment_months,benefits,source_url,updated_at",
    `&plan_id=in.(${TARGET_MEMBERSHIP_IDS.join(",")})&order=plan_id.asc`,
  );
  const refs = {};
  for (const [table, column, select] of [
    ["saved_deals", "deal_id", "deal_id"],
    ["claims", "deal_id", "id,deal_id,status"],
    ["listing_requests", "offer_id", "id,offer_id,status"],
  ]) {
    refs[table] = await fetchRows(api, table, select, `&${column}=in.(${DUPLICATE_OFFER_IDS.join(",")})`);
  }
  return { offers, items, services, promotions, memberships, refs, captured_at: new Date().toISOString() };
}

function exactService(services, spec) {
  return services.filter(
    (service) => service.business_id === 765
      && service.service_name === "Laser Hair Removal"
      && service.service_area === spec.serviceArea,
  );
}

function verifyEvidence(snapshot) {
  const offers = byId(snapshot.offers, "id");
  const items = snapshot.items;
  const promotions = byId(snapshot.promotions, "promotion_id");
  const memberships = byId(snapshot.memberships, "plan_id");
  assert(snapshot.offers.length === TARGET_OFFER_IDS.length, `expected ${TARGET_OFFER_IDS.length} target offers, found ${snapshot.offers.length}`);
  assert(snapshot.promotions.length === TARGET_PROMOTION_IDS.length, "target promotions are incomplete");
  assert(snapshot.memberships.length === TARGET_MEMBERSHIP_IDS.length, "target memberships are incomplete");
  assert(promotions.get(2).business_id === 765 && promotions.get(697).business_id === 1766, "target promotion/business ownership changed");
  assert(memberships.get(502).business_id === 1952 && memberships.get(331).business_id === 930 && memberships.get(330).business_id === 930, "target membership/business ownership changed");
  const promotion2Evidence = String(promotions.get(2).promotion_content).toLowerCase();
  const promotion697Evidence = String(promotions.get(697).promotion_content).toLowerCase();
  const membership502Evidence = String(memberships.get(502).benefits).toLowerCase();
  const membership331Evidence = String(memberships.get(331).benefits).toLowerCase();
  const membership330Evidence = String(memberships.get(330).benefits).toLowerCase();
  assert(promotion2Evidence.includes("full chest and abdomen") && promotion2Evidence.includes("full back including shoulders"), "promotion 2 source evidence changed");
  assert(promotion697Evidence.includes("neck is included at no additional charge") && promotion697Evidence.includes("save 20%"), "promotion 697 source evidence changed");
  assert(membership502Evidence.includes("$10/unit neurotoxin"), "membership 502 source evidence changed");
  assert(membership331Evidence.includes("15% off injectables") && membership330Evidence.includes("10% off injectables"), "membership 330/331 source evidence changed");
  for (const offerId of TARGET_OFFER_IDS) {
    const offer = offers.get(offerId);
    assert(offer.business_id === (offerId === 6701 || offerId === 9472 ? 1952 : offerId === 8562 || offerId === 8661 ? 765 : offerId === 10425 || offerId === 10682 ? 1766 : 930), `offer ${offerId} business changed`);
    assert(oneItem(items, offerId).offer_id === offerId, `offer ${offerId} item relation changed`);
  }
  for (const table of Object.keys(snapshot.refs)) {
    assert(snapshot.refs[table].length === 0, `duplicate candidates have ${table} references`);
  }
}

function buildPlan(snapshot) {
  verifyEvidence(snapshot);
  const offers = byId(snapshot.offers, "id");
  const plan = {
    captured_at: snapshot.captured_at,
    source: "user-confirmed-2026-09-22-collision-review",
    schema_changes: 0,
    service_inserts: [],
    item_updates: [],
    master_updates: [],
    already_correct: [],
    review: [],
  };

  for (const offerId of DUPLICATE_OFFER_IDS) {
    const offer = offers.get(offerId);
    if (offer.is_active === true) {
      plan.master_updates.push({
        kind: "deactivate_duplicate",
        offer_id: offerId,
        expected: { is_active: true, updated_at: offer.updated_at, offer_raw_text: offer.offer_raw_text },
        reason: `duplicate of ${offerId === 9472 ? 6701 : offerId === 10599 ? 10533 : 10537}`,
      });
    } else {
      plan.already_correct.push({ kind: "already_inactive", offer_id: offerId });
    }
  }

  for (const spec of NEW_SERVICE_SPECS) {
    const offer = offers.get(spec.offerId);
    const item = oneItem(snapshot.items, spec.offerId);
    const matches = exactService(snapshot.services, spec);
    assert(matches.length <= 1, `multiple exact service rows found for ${spec.serviceArea}`);
    const existingService = matches[0] || null;
    const targetServiceId = existingService?.service_id ?? null;
    const oldMappingIsExpected = item.service_id === OLD_GENERIC_LASER_SERVICE_ID;
    const alreadyMapped = targetServiceId !== null && item.service_id === targetServiceId;
    if (!oldMappingIsExpected && !alreadyMapped) {
      plan.review.push({ offer_id: spec.offerId, offer_item_id: item.offer_item_id, reason: "service_mapping_changed_since_review", current_service_id: item.service_id, expected_old_service_id: OLD_GENERIC_LASER_SERVICE_ID, target_service_area: spec.serviceArea });
      continue;
    }
    if (!existingService) {
      plan.service_inserts.push({
        offer_id: spec.offerId,
        business_id: 765,
        service_name: "Laser Hair Removal",
        service_area: spec.serviceArea,
        unit_type: "session",
        regular_price: 190,
        source_url: "https://newlookskincenter.com/laser-hair-removal-qa",
        source_excerpt: spec.sourceExcerpt,
      });
    }
    if (alreadyMapped) {
      plan.already_correct.push({ kind: "already_mapped", offer_id: spec.offerId, offer_item_id: item.offer_item_id, service_id: targetServiceId });
    } else {
      plan.item_updates.push({
        kind: "remap_service",
        offer_id: spec.offerId,
        offer_item_id: item.offer_item_id,
        expected: { service_id: OLD_GENERIC_LASER_SERVICE_ID, updated_at: item.updated_at },
        target_service_area: spec.serviceArea,
        target_service_id: targetServiceId,
      });
    }
    assert(offer.is_active === true, `offer ${spec.offerId} is not active; refusing to repair its mapping automatically`);
  }

  const offer10682 = offers.get(10682);
  if (
    offer10682.offer_raw_text === REBUILT_10682.offer_raw_text
    && offer10682.discount_price === REBUILT_10682.discount_price
    && offer10682.discount_percent === REBUILT_10682.discount_percent
    && JSON.stringify(offer10682.conditions || null) === JSON.stringify(REBUILT_10682.conditions)
  ) {
    plan.already_correct.push({ kind: "already_rebuilt", offer_id: 10682 });
  } else if (
    offer10682.offer_raw_text === "Face laser treatment includes neck free (stack two lasers save 20%)"
    && offer10682.discount_price === null
    && offer10682.discount_percent === 20
    && offer10682.conditions === null
    && offer10682.is_active === true
  ) {
    plan.master_updates.push({
      kind: "rebuild_offer",
      offer_id: 10682,
      expected: { offer_raw_text: offer10682.offer_raw_text, discount_price: offer10682.discount_price, discount_percent: offer10682.discount_percent, conditions: offer10682.conditions, updated_at: offer10682.updated_at },
      values: REBUILT_10682,
      reason: "split promotion 697 face-plus-free-neck term from stack-two-lasers term",
    });
  } else {
    plan.review.push({ kind: "offer_10682_changed_since_review", offer_id: 10682, current: { offer_raw_text: offer10682.offer_raw_text, discount_price: offer10682.discount_price, discount_percent: offer10682.discount_percent, conditions: offer10682.conditions, is_active: offer10682.is_active } });
  }
  return plan;
}

function compactSnapshot(snapshot) {
  return {
    captured_at: snapshot.captured_at,
    offers: snapshot.offers,
    items: snapshot.items,
    services: snapshot.services.filter((row) => row.service_name === "Laser Hair Removal"),
    promotions: snapshot.promotions,
    memberships: snapshot.memberships,
    refs: snapshot.refs,
  };
}

function normalizeNumber(value) {
  return value === null || value === undefined ? "null" : String(Number(value));
}

function sourceKey(offer) {
  if (offer.membership_plan_id !== null && offer.membership_plan_id !== undefined) return `m:${offer.membership_plan_id}`;
  if (offer.promotion_id !== null && offer.promotion_id !== undefined) return `p:${offer.promotion_id}`;
  return `b:${offer.business_id ?? ""}`;
}

function newFingerprintKey(offer, items) {
  const price = [offer.regular_price, offer.discount_price, offer.discount_percent, offer.discount_amount]
    .map(normalizeNumber).join(",");
  const itemKey = items
    .sort((a, b) => String(a.service_id).localeCompare(String(b.service_id)))
    .map((item) => `${item.service_id ?? "null"}:${item.delivered_units ?? "null"}:${normalizeNumber(item.unit_price)}`)
    .join(",");
  return `${sourceKey(offer)}|${itemKey}|price:${price}`;
}

async function runTargetPreflight(api) {
  const offers = await fetchRows(api, "promo_offer_master", "id,business_id,promotion_id,membership_plan_id,is_active,regular_price,discount_price,discount_percent,discount_amount,offer_raw_text,updated_at", `&id=in.(${TARGET_OFFER_IDS.join(",")})&order=id.asc`);
  const items = await fetchRows(api, "promo_offer_items", "offer_id,service_id,delivered_units,unit_price", `&offer_id=in.(${TARGET_OFFER_IDS.join(",")})&order=offer_id.asc`);
  const offerGroups = new Map();
  for (const offer of offers.filter((row) => row.is_active === true)) {
    const key = newFingerprintKey(offer, items.filter((item) => item.offer_id === offer.id));
    if (!offerGroups.has(key)) offerGroups.set(key, []);
    offerGroups.get(key).push(offer.id);
  }
  return [...offerGroups.entries()]
    .filter(([, ids]) => ids.length > 1)
    .map(([fingerprint_key, ids]) => ({ fingerprint_key, offer_ids: ids }));
}

async function applyPlan(api, snapshot, plan) {
  const result = { database_writes: 0, service_inserts: [], item_updates: [], master_updates: [], conflicts: [] };
  const serviceIds = new Map();
  for (const spec of NEW_SERVICE_SPECS) {
    const matches = exactService(snapshot.services, spec);
    if (matches.length === 1) {
      serviceIds.set(spec.offerId, matches[0].service_id);
      continue;
    }
    const inserted = await insertExactlyOne(api, "clinic_services", {
      business_id: 765,
      service_name: "Laser Hair Removal",
      service_area: spec.serviceArea,
      unit_type: "session",
      regular_price: 190,
      source_url: "https://newlookskincenter.com/laser-hair-removal-qa",
    });
    serviceIds.set(spec.offerId, inserted.service_id);
    result.service_inserts.push({ offer_id: spec.offerId, service_id: inserted.service_id, service_area: spec.serviceArea });
    result.database_writes += 1;
  }
  for (const operation of plan.item_updates) {
    const serviceId = serviceIds.get(operation.offer_id);
    assert(serviceId !== undefined, `missing target service for offer ${operation.offer_id}`);
    const updated = await patchExactlyOne(api, "promo_offer_items", {
      offer_item_id: `eq.${operation.offer_item_id}`,
      offer_id: `eq.${operation.offer_id}`,
      service_id: `eq.${OLD_GENERIC_LASER_SERVICE_ID}`,
      updated_at: `eq.${operation.expected.updated_at}`,
    }, { service_id: serviceId });
    assert(updated.service_id === serviceId, `offer item ${operation.offer_item_id} did not receive target service ${serviceId}`);
    result.item_updates.push({ offer_id: operation.offer_id, offer_item_id: operation.offer_item_id, service_id: serviceId, updated_at: updated.updated_at });
    result.database_writes += 1;
  }
  for (const operation of plan.master_updates.filter((entry) => entry.kind === "deactivate_duplicate")) {
    const updated = await patchExactlyOne(api, "promo_offer_master", {
      id: `eq.${operation.offer_id}`,
      is_active: "eq.true",
      updated_at: `eq.${operation.expected.updated_at}`,
    }, { is_active: false });
    assert(updated.is_active === false, `duplicate offer ${operation.offer_id} did not deactivate`);
    result.master_updates.push({ kind: operation.kind, offer_id: operation.offer_id, updated_at: updated.updated_at });
    result.database_writes += 1;
  }
  for (const operation of plan.master_updates.filter((entry) => entry.kind === "rebuild_offer")) {
    const updated = await patchExactlyOne(api, "promo_offer_master", {
      id: `eq.${operation.offer_id}`,
      promotion_id: "eq.697",
      business_id: "eq.1766",
      is_active: "eq.true",
      updated_at: `eq.${operation.expected.updated_at}`,
    }, operation.values);
    assert(updated.offer_raw_text === REBUILT_10682.offer_raw_text && updated.discount_percent === 100 && updated.discount_price === 0, "offer 10682 did not rebuild to the standalone free-neck term");
    result.master_updates.push({ kind: operation.kind, offer_id: operation.offer_id, updated_at: updated.updated_at, values: operation.values });
    result.database_writes += 1;
  }
  return result;
}

async function main() {
  const api = await apiClient();
  const snapshot = await loadSnapshot(api);
  const plan = buildPlan(snapshot);
  const mode = process.argv.includes("--apply") ? "apply" : "dry_run";
  const planPath = `${ROOT}/one-off/20260922_fingerprint_collision_repair_plan.json`;
  await writeFile(planPath, JSON.stringify({ snapshot: compactSnapshot(snapshot), plan }, null, 2) + "\n");
  if (plan.review.length > 0) throw new Error(`review required for ${plan.review.length} operation(s); see ${planPath}`);
  const result = { mode, captured_at: snapshot.captured_at, plan, database_writes: 0, service_inserts: [], item_updates: [], master_updates: [], conflicts: [], target_preflight_after: null, artifacts: [planPath] };
  if (mode === "apply") {
    const applied = await applyPlan(api, snapshot, plan);
    Object.assign(result, applied);
    result.target_preflight_after = await runTargetPreflight(api);
  }
  const outputPath = `${ROOT}/one-off/20260922_fingerprint_collision_repair_${mode}.json`;
  await writeFile(outputPath, JSON.stringify(result, null, 2) + "\n");
  result.artifacts.push(outputPath);
  console.log(JSON.stringify({ mode, database_writes: result.database_writes, service_inserts: result.service_inserts.length, item_updates: result.item_updates.length, master_updates: result.master_updates.length, target_preflight_after: result.target_preflight_after, output: outputPath }, null, 2));
}

await main();
