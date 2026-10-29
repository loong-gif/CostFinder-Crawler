import { createHash } from "node:crypto";

export function normalize(value) {
  return String(value || "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "").replace(/[^a-z0-9$]+/gi, " ").toLowerCase().replace(/\s+/g, " ").trim();
}

export function priceFromCard(text) {
  const match = String(text || "").match(/\$\s*([\d,]+(?:\.\d{1,2})?)/);
  return match ? Number(match[1].replace(/,/g, "")) : null;
}

export function quantityFromCard(text) {
  const match = String(text || "").match(/\b(\d+)\s*(?:sessions?|vials?|injections?)\b/i);
  return match ? Number(match[1]) : null;
}

function has(text, pattern) {
  return pattern.test(normalize(text));
}

export function serviceMappingFromProductEvidence(_cardText, productText) {
  // A promotion card proves its own price, not the canonical clinic service.
  // Only a separately rendered product page may establish that mapping.
  const text = normalize(productText);
  if (has(text, /\biconique eyes\b/)) return { services: [], reason: "alternative_component_not_atomic" };
  if (has(text, /\biconique lips\b|\biconique balance\b|\biconique lift\b/)) return { services: [], reason: "filler_service_not_explicit" };
  if (has(text, /\bbeauty icon\b|\bfull facial balancing\b/)) return { services: [], reason: "filler_service_not_explicit" };
  if (has(text, /\biconique contour\b/)) return { services: [], reason: "filler_service_not_explicit" };
  if (has(text, /\biconique lip reset\b/)) return { services: ["Filler Dissolving", "Dermal Filler", "Botox"] };
  if (has(text, /\biconique jawline\b/)) return { services: ["Dermal Filler", "Botox"] };
  if (has(text, /\bbbl\b/) && has(text, /\bmoxi\b/)) return { services: ["BBL Photofacial", "Moxi Laser"] };
  if (has(text, /\bbbl\b/)) return { services: ["BBL Photofacial"] };
  if (has(text, /\bmoxi\b/)) return { services: ["Moxi Laser"] };
  if (has(text, /\bskintyte\b|\bskin tightening\b/)) return { services: ["Skin Tightening"] };
  if (has(text, /\bkybella\b/)) return { services: ["Kybella"] };
  if (has(text, /\bprf\b|\bez gel\b/)) return { services: ["PRF"] };
  if (has(text, /\bbotox\b/)) return { services: ["Botox"] };
  if (has(text, /\bdermal filler\b|\blip filler\b/)) return { services: ["Dermal Filler"] };
  if (has(text, /\bzo\b|\bskinmedica\b/)) return { services: [], reason: "retail_product_not_in_service_taxonomy" };
  return { services: [], reason: "canonical_service_unconfirmed" };
}

export function servicesFromProductEvidence(cardText, productText) {
  return serviceMappingFromProductEvidence(cardText, productText).services;
}

export function cardSpec(card, evidence) {
  if (evidence?.status !== "verified") return { status: "review", reason: evidence?.status || "missing_product_evidence" };
  const mapping = serviceMappingFromProductEvidence(card.text, evidence.product_text || evidence.visible_text);
  const services = mapping.services;
  const price = priceFromCard(card.text);
  if (!price) return { status: "review", reason: "price_not_found" };
  if (!services.length) return { status: "review", reason: mapping.reason || "canonical_service_unconfirmed" };
  const quantity = quantityFromCard(card.text);
  const isComposite = services.length > 1;
  const isPackage = isComposite || quantity > 1 || /\b(?:package|bundle)\b/i.test(`${card.text}\n${evidence.product_text || evidence.visible_text || ""}`);
  return {
    status: "ready",
    services,
    price,
    quantity,
    is_package: isPackage,
    items: services.map((service_name) => ({
      service_name,
      delivered_units: isComposite ? null : quantity,
      unit_price: null,
    })),
  };
}

export function fingerprint(spec) {
  return createHash("sha1").update(["p:101", spec.offer_raw_text, spec.price, ...spec.items.map((item) => item.service_name)].join("|")).digest("hex");
}

export function sameOffer(offer, spec) {
  return normalize(offer.offer_raw_text) === normalize(spec.offer_raw_text)
    && Number(offer.discount_price) === Number(spec.discount_price)
    && (offer.promo_offer_items || []).length === spec.items.length
    && spec.items.every((item) => (offer.promo_offer_items || []).some((existing) => existing.item_name === item.service_name));
}
