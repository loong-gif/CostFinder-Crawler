import assert from "node:assert/strict";
import test from "node:test";

import {
  buildManifest,
  buildRepairDryRun,
  collectPagedRows,
  isReviewableImage,
  summarize,
} from "../one-off/membership_visibility_qa_lib.mjs";

test("paged membership reads continue past the REST page size", async () => {
  const calls = [];
  const rows = await collectPagedRows(async (offset, limit) => {
    calls.push([offset, limit]);
    if (offset === 0) return Array.from({ length: 500 }, (_, index) => ({ plan_id: index }));
    if (offset === 500) return Array.from({ length: 500 }, (_, index) => ({ plan_id: index + 500 }));
    return [{ plan_id: 1000 }];
  }, 500);
  assert.equal(rows.length, 1001);
  assert.deepEqual(calls, [[0, 500], [500, 500], [1000, 500]]);
});

test("same URL keeps independent Membership records and empty benefits produce no claims", () => {
  const manifest = buildManifest([
    { plan_id: 461, membership_name: "Platinum", source_url: "https://example.test/membership", benefits: [] },
    { plan_id: 462, membership_name: "Micro", source_url: "https://example.test/membership", benefits: [] },
    { plan_id: 644, membership_name: "Silver", source_url: "https://example.test/membership", benefits: ["15 Units of Botox"] },
  ]);
  assert.equal(manifest.length, 1);
  assert.equal(manifest[0].records.length, 3);
  assert.deepEqual(manifest[0].records.find(record => record.recordId === 461).claims, []);
  assert.equal(manifest[0].records.find(record => record.recordId === 644).claims.filter(claim => claim.claimKind === "stored_benefit").length, 1);
});

test("rendered image review includes a small image with a real layout box", () => {
  assert.equal(isReviewableImage({ kind: "image", src: "https://example.test/membership-card.png", alt: "membership card", rect: { width: 197, height: 228 } }), true);
  assert.equal(isReviewableImage({ kind: "image", src: "https://example.test/social-icon.png", alt: "Facebook", rect: { width: 197, height: 228 } }), false);
});

test("dry-run only allows the five evidence-closed Membership repairs", () => {
  const rows = [
    [247, "TRU SIGNATURE", ["$11.50/unit Botox and Dysport", "$50 off each filler"], "https://tru.test"],
    [644, "Silver", ["15 Units of Botox"], "https://flora.test"],
    [645, "Gold", ["25 Units of Botox"], "https://flora.test"],
    [646, "Platinum", ["35 Units of Botox"], "https://flora.test"],
    [647, "Diamond", ["40 Units of Botox"], "https://flora.test"],
    [676, "Diamond Glow", ["One Diamond Glow facial treatment per month"], "https://image.test"],
    [677, "Laser", ["Pick one service per month"], "https://image.test"],
    [461, "Platinum", [], "https://medspot.test"],
    [462, "Micro", [], "https://medspot.test"],
  ].map(([plan_id, membership_name, benefits, source_url]) => ({ plan_id, membership_name, benefits, source_url }));
  const manifest = buildManifest(rows);
  const results = {};
  for (const entry of manifest) {
    const claimResults = entry.records.flatMap(record => record.claims.map(claim => ({
      recordType: "membership",
      recordId: record.recordId,
      claim: claim.claim,
      claimKind: claim.claimKind,
      status: claim.claimKind === "verification" || record.recordId === 247 ? "visible_match" : "image_review_pending",
    })));
    results[entry.sourceUrl] = { status: "page_accessible", url: entry.sourceUrl, claimResults, emptyBenefitRecords: entry.records.filter(record => record.emptyBenefits).map(record => ({ recordId: record.recordId })) };
  }
  const report = { results };
  const dryRun = buildRepairDryRun(rows, report);
  const byId = new Map(dryRun.candidates.map(candidate => [candidate.planId, candidate]));
  assert.deepEqual(byId.get(247).suggestedBenefits, ["$11.50/unit Botox", "$11.50/equivalent dosing Dysport", "$50 off each filler"]);
  assert.equal(byId.get(247).allowedToWrite, true);
  assert.deepEqual(byId.get(644).suggestedBenefits, ["15 Units of Botox", "Choose Any 2 Services Monthly"]);
  assert.equal(byId.get(644).allowedToWrite, true);
  assert.equal(byId.get(676).allowedToWrite, false);
  assert.equal(byId.get(461).allowedToWrite, false);
  assert.equal(dryRun.offerOperations, 0);
  assert.equal(dryRun.clinicServiceOperations, 0);
});

test("summary separates page unavailable, image pending, and empty benefits", () => {
  const memberships = [
    { plan_id: 1, benefits: ["A"] },
    { plan_id: 2, benefits: [] },
  ];
  const manifest = buildManifest([
    { plan_id: 1, membership_name: "A", source_url: "https://available.test", benefits: ["A"] },
    { plan_id: 2, membership_name: "B", source_url: "https://unavailable.test", benefits: [] },
  ]);
  const results = {
    "https://available.test": { status: "page_accessible", claimResults: [{ recordType: "membership", recordId: 1, claimKind: "stored_benefit", status: "image_review_pending" }], emptyBenefitRecords: [] },
    "https://unavailable.test": { status: "page_unavailable", claimResults: [{ recordType: "membership", recordId: 2, claimKind: "stored_benefit", status: "page_unavailable" }], emptyBenefitRecords: [{ recordId: 2 }] },
  };
  const summary = summarize(results, memberships, manifest);
  assert.equal(summary.imageReviewPending, 1);
  assert.equal(summary.pageUnavailableClaims, 1);
  assert.equal(summary.recordsEmptyBenefitsPending, 1);
});
