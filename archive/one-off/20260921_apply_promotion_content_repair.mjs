import { readFile, writeFile } from "node:fs/promises";

const ROOT = "/Users/wyl/BrandRap/costfinder";

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (match) values[match[1]] = match[2].replace(/^['"]|['"]$/g, "");
  }
  return values;
}

async function getOne(baseUrl, key, id) {
  const response = await fetch(`${baseUrl}/rest/v1/clinic_promotions?select=promotion_id,promotion_title,promotion_content,is_active,is_offer_extracted,requires_cf_verification,updated_at&promotion_id=eq.${id}`, { headers: { apikey: key, Authorization: `Bearer ${key}` } });
  if (!response.ok) throw new Error(`read ${id} failed: ${response.status} ${await response.text()}`);
  const rows = await response.json();
  if (rows.length !== 1) throw new Error(`read ${id} matched ${rows.length}`);
  return rows[0];
}

async function patchOne(baseUrl, key, row, body) {
  const params = new URLSearchParams({ promotion_id: `eq.${row.promotion_id}`, updated_at: `eq.${row.updated_at}` });
  const response = await fetch(`${baseUrl}/rest/v1/clinic_promotions?${params}`, {
    method: "PATCH",
    headers: { apikey: key, Authorization: `Bearer ${key}`, "Content-Type": "application/json", Prefer: "return=representation" },
    body: JSON.stringify({ ...body, updated_at: new Date().toISOString() }),
  });
  if (!response.ok) throw new Error(`update ${row.promotion_id} failed: ${response.status} ${await response.text()}`);
  const rows = await response.json();
  if (rows.length !== 1) throw new Error(`update ${row.promotion_id} matched ${rows.length}`);
  return rows[0];
}

const changes = new Map([
  [84, { promotion_content: [
    "QUADESSY + CoolPeel: 1 treatment $995; save $705.",
    "QUADESSY + CoolPeel: 3 treatments $2,875; save $1,475.",
    "Laser Hair Removal packages: 15% off; 6 treatments per package.",
    "Jeuveau or Letybo: purchase a 100-unit vial and receive 10% off.",
    "Newsletter special: Brazilian or Manzilian Laser Hair Removal, 6 treatments; regular $1,625, now $1,137.50 (30% off).",
  ], is_offer_extracted: false }],
  [99, { promotion_content: [
    "$150 off a full syringe of lip filler plus a free gift.",
    "Half syringe lip filler: $399.",
    "Russian Lip: $599.",
    "$150 off each full syringe of facial balancing.",
    "Botox: 40 units for $450.",
  ], is_offer_extracted: false }],
  [101, { promotion_content: [
    "BOTOX 64 units: $768.",
  ], is_offer_extracted: false }],
  [107, { promotion_content: [
    "Membership Program: Botox, Xeomin, and Dysport.",
    "Members receive 26% off neurotoxin treatments.",
    "Membership benefits include priority scheduling, special pricing on other treatments, exclusive member-only events, personalized treatment plans, and a free monthly B12 injection.",
  ], is_offer_extracted: false }],
  [215, { promotion_content: [
    "Palo Santo Massage Package — 1 hour 45 minutes — $325.",
    "Moor Mud Body Polishing Treatment — 1 hour — $150.",
    "Deep Renewal Hydrafacial Package — 1 hour — $280.",
    "Grounding Retreat Massage + Hydrafacial Package — 2 hours 15 minutes — $395.",
    "New guest special: 15% off first visit, valid Monday–Thursday through September 30, 2026; code WELCOME15.",
    "First-time Botox: buy 40 units and receive 10 free units; 50 units for $540; Allē membership required or join Allē.",
    "First-time Dysport: buy 120 units and receive 30 free units.",
    "Lip filler: 15% off $700 treatment; pay $595; code 15OFFLIPS.",
    "Juvederm or Restylane filler: 15% off; code 15OFFFILLER.",
    "Facial balancing: 15% off; code FACIALBALANCING15.",
    "Sculptra: save $100 per vial.",
    "Kybella: pre-purchase 3 vials and receive a complimentary 4th vial; save $575.",
    "IPL series of 3 for face: $700; save $350.",
    "Clear + Brilliant series of 3 for face: $975; save $300.",
    "Fraxel series of 3 for face: $2,650; save $347.",
    "Hydrafacial Deluxe series: 3 sessions for $650; save $85.",
    "Microneedling series of 3 for face: $1,300; save $50.",
    "Morpheus8 RF Microneedling series of 3 for face: $2,650; save $200.",
    "Go & Glow Hydrafacial series: 3 sessions for $525; save $60.",
    "High-Touch Hydrafacial series: 3 sessions for $800; save $85.",
    "Hydrafacial Platinum series: 3 sessions for $925; save $110.",
    "Botox & Dysport bundle pricing: 100 Botox units $1,200; 300 Dysport units $1,200; 200 Botox units $2,200; 600 Dysport units $2,200; 300 Botox units $3,000; 900 Dysport units $3,000.",
  ], is_offer_extracted: false }],
  [217, { promotion_content: [
    "Letybo: $8/unit; regular $10/unit.",
    "CO2 Face: $1,100; regular $1,250.",
    "CO2 Face & Neck: $1,400; regular $1,750.",
    "VI Peel: $250; regular $350.",
    "Laser Hair Removal packages: 30% off.",
    "Laser Hair Removal package pricing: Upper Lip or Chin $525 (regular $750); Upper Lip and Chin $630 (regular $900); Face $875 (regular $1,250); Face + Beard $1,050 (regular $1,500); Legs below knees $1,400 (regular $2,000); Chest and Abdomen $1,750 (regular $2,500).",
  ], is_offer_extracted: false }],
  [694, { promotion_content: [
    "New patients receive 20% off their first treatment.",
    "Free shipping on skincare orders $300+.",
  ], is_offer_extracted: false }],
  [815, { promotion_content: [
    "Laser Hair Removal — One Year Unlimited: $649; regular $1,800.",
    "Choose from 3 Small Areas; 1 Small + 1 Medium Area; or 1 Large Area.",
  ], is_offer_extracted: false }],
  [921, { promotion_content: [
    "BACK TO SCHOOL SPECIAL — expires September 29th.",
  ], is_offer_extracted: false, requires_cf_verification: true }],
  [3407, { promotion_content: [
    "Brilliant Distinction & Inspire patient offer: $150 off BOTOX with a minimum of 30 units purchased.",
    "$150 off one dermal filler syringe.",
  ], is_offer_extracted: false }],
  [3482, { promotion_title: "Complimentary One Year Medspa Membership", promotion_content: [
    "Complimentary One Year Medspa Membership.",
    "10% off all medspa services for one year when the first appointment is pre-booked by November 1st.",
    "Membership benefits include priority access to exclusive events, VIP event invitations, transfer of VIP status for a guest or family member, and the ability to bring a guest.",
  ], is_offer_extracted: false }],
  [3519, { promotion_content: [
    "Botox: $8.99/unit for the first 10 units, then $10/unit; regular $12.",
    "Xeomin: $8.99/unit; regular $12.",
    "Sculptra: $599/vial; regular $799.",
    "Custom Facial: $99; regular $135.",
    "HydraFacial + Dermaplane: $149; regular $289.",
    "RHA filler: $499/syringe; regular $699.",
    "SkinPen microneedling: $259/session; regular $349.",
    "Buy 2 filler syringes and receive a free half syringe of lip filler; one per client.",
    "New clients: free 30-minute Member Facial, free vitamin shot, and 30 days of membership; one per person and no card required.",
  ], is_offer_extracted: false }],
  [3526, { is_active: false, is_offer_extracted: false }],
  [3541, { promotion_content: [
    "Fall Event: A Night in White — October 15, 2026, 5:00–7:00 PM; more details to come.",
  ], is_offer_extracted: false, requires_cf_verification: false }],
]);

const env = parseEnv(await readFile(`${ROOT}/.env`, "utf8"));
const baseUrl = String(env.SUPABASE_URL || "").replace(/\/$/, "");
const key = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SECRET_KEY;
if (!baseUrl || !key) throw new Error("SUPABASE_URL or server key is missing");

const result = { mode: process.argv.includes("--apply") ? "apply" : "dry_run", generated_at: new Date().toISOString(), changes: [], conflicts: [] };
for (const [id, body] of changes) {
  const row = await getOne(baseUrl, key, id);
  const change = { promotion_id: id, old_updated_at: row.updated_at, old_content: row.promotion_content, proposed: body };
  if (result.mode === "apply") {
    try {
      const updated = await patchOne(baseUrl, key, row, body);
      change.status = "updated";
      change.new_updated_at = updated.updated_at;
      change.new_content = updated.promotion_content;
    } catch (error) {
      change.status = "conflict_or_failure";
      change.error = String(error);
      result.conflicts.push(change);
      continue;
    }
  } else {
    change.status = "ready";
  }
  result.changes.push(change);
}
const output = `${ROOT}/one-off/20260921_promotion_content_repair_${result.mode}.json`;
await writeFile(output, JSON.stringify(result, null, 2) + "\n");
console.log(JSON.stringify({ mode: result.mode, changes: result.changes.length, conflicts: result.conflicts.length, output }, null, 2));
