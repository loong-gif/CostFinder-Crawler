import assert from "node:assert/strict";
import test from "node:test";

import { buildBenefits } from "../one-off/20260921_apply_membership_third_batch_safe.mjs";

test("452 corrects IV savings semantics without changing other benefits", () => {
  assert.deepEqual(buildBenefits(452, ["$35 IV member pricing", "Birthday benefit"]), ["$35 off IV Therapy", "Birthday benefit"]);
});

test("247 expands the visible skin-quality treatment scope", () => {
  assert.deepEqual(buildBenefits(247, ["$11.50/unit Botox", "10% off skin-quality treatments including SkinVive"]), [
    "$11.50/unit Botox",
    "10% off Chemical Peels, Microneedling, PRF Treatments, and SkinVive",
  ]);
});

test("454 removes only the cross-tier banked and duplicate injectable claims", () => {
  assert.deepEqual(buildBenefits(454, [
    "10% OFF Regular-Priced Single Units of Injectable (Filler • Sculptra® • Radiesse® • Fat Lipolysis)",
    "Unuse dues may be BANKED and applied toward other skin laser services (Ulthera, Mixto, Neogen, Sylfirm/Morpheus, etc) OR filler packages 15% OFF Retail & 10% OFF other regular priced skin/laser services Exclude neurotoxins",
    "Injectable Membership — 10% off regular-priced single units of injectable: filler, Sculptra, Radiesse, and fat lipolysis.",
    "Botox $10.50/unit (reg. $12.50/unit)",
  ]), [
    "10% OFF Regular-Priced Single Units of Injectable (Filler • Sculptra® • Radiesse® • Fat Lipolysis)",
    "Botox $10.50/unit (reg. $12.50/unit)",
  ]);
});

test("523 restores the visible Classic, Upgrade, and Combo membership context", () => {
  assert.deepEqual(buildBenefits(523, ["Get 1 Classic service per month", "Get member pricing on neurotoxins"]), [
    "Get 1 Classic service per month: IPL, Genesis or Sandro toning, Classic Diamond Glow or Oxygeneo, Dermaplane & Peel, Dermaneedling with serum, Nano facial with PDRN, or Factor 5 Peel",
    "Upgrade services: $250 member price; add $75 on the day of service and $125 for an additional area",
    "Combo services: $350–$550 member price; add $175–$375 on the day of service and $200 for an additional area",
    "Get member pricing on neurotoxins",
  ]);
  assert.deepEqual(buildBenefits(523, [
    "Get 1 Classic service per month: IPL, Genesis or Sandro toning, Classic Diamond Glow or Oxygeneo, Dermaplane & Peel, Dermaneedling with serum, Nano facial with PDRN, or Factor 5 Peel",
    "Upgrade services: $250 member price; add $75 on the day of service and $125 for an additional area",
    "Combo services: $350–$550 member price; add $175–$375 on the day of service and $200 for an additional area",
    "Get member pricing on neurotoxins",
    "Get member pricing on neurotoxins",
  ]), [
    "Get 1 Classic service per month: IPL, Genesis or Sandro toning, Classic Diamond Glow or Oxygeneo, Dermaplane & Peel, Dermaneedling with serum, Nano facial with PDRN, or Factor 5 Peel",
    "Upgrade services: $250 member price; add $75 on the day of service and $125 for an additional area",
    "Combo services: $350–$550 member price; add $175–$375 on the day of service and $200 for an additional area",
    "Get member pricing on neurotoxins",
  ]);
});
