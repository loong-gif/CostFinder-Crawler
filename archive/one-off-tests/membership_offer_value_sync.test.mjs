import assert from "node:assert/strict";
import test from "node:test";

import { desiredOfferText } from "../one-off/20260921_sync_membership_offer_values.mjs";

test("SkinVive Offer text carries the verified discount semantics", () => {
  assert.equal(desiredOfferText({ offer_raw_text: "SkinVive (regularly $450)" }), "10% OFF SkinVive (regularly $450)");
  assert.equal(desiredOfferText({ offer_raw_text: "10% OFF SkinVive (regularly $450)" }), "10% OFF SkinVive (regularly $450)");
});
