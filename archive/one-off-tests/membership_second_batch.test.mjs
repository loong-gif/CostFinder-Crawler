import assert from "node:assert/strict";
import test from "node:test";

import { buildDesiredBenefits } from "../one-off/20260921_apply_membership_second_batch.mjs";

test("Elevate plans preserve choices and add the NuEra prerequisite", () => {
  const result = buildDesiredBenefits(530, ["1 Chemical Peel", "1 NuEra Tight Face & Neck Maintenance Treatment", "15% off all other treatments or products"]);
  assert.deepEqual(result, [
    "Choose one monthly treatment: Chemical Peel, Facial Rejuvenation Treatment with the Lightpod Neo Elite, Microneedling Treatment, Deluxe Hydrafacial, Stellar M22 IPL Treatment, Jan Marini Starter Kit, Laser Hair Removal Session of a Medium Area, or NuEra Tight Face & Neck Maintenance Treatment (only after completing 4 weekly sessions)",
    "15% off all other treatments or products",
  ]);
});

test("annual and Aphrodite plans preserve existing values while adding the supported group or wallet meaning", () => {
  assert.deepEqual(buildDesiredBenefits(345, ["Signature tier annual billing — one month free ($3,025)", "Choose one monthly treatment: The Perfect Derma Peel", "Skinwave"]), [
    "Signature tier annual billing — one month free ($3,025)",
    "Choose one monthly treatment: The Perfect Derma Peel, Skinwave, Target Cool Infusion Facial, Microneedling, 60 min Bespoke Facial, IPL, or LHR Lip, Chin & Underarm",
  ]);
  assert.deepEqual(buildDesiredBenefits(392, ["$11/unit of Botox (reg $12)" ]), [
    "$11/unit of Botox (reg $12)",
    "Monthly membership payment is placed into your account to use toward any service",
  ]);
});
