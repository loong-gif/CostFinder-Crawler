import assert from "node:assert/strict";
import test from "node:test";

import {
  buildMembershipRecordAudits,
  buildMembershipRepairDryRun,
  summarizeMembershipAudits,
} from "../one-off/membership_completeness_audit_lib.mjs";
import { compareCandidateSnapshot } from "../one-off/membership_repair_apply_lib.mjs";

function claim(recordId, text, status, benefitIndex, claimKind = "stored_benefit") {
  return { recordType: "membership", recordId, claim: text, status, benefitIndex, claimKind };
}

function reportFor(rows, overrides = {}) {
  const results = {};
  for (const row of rows) {
    const stored = (row.benefits || []).map((text, benefitIndex) => claim(row.plan_id, text, "visible_match", benefitIndex));
    results[row.source_url] = {
      status: "page_accessible",
      finalUrl: row.source_url,
      claimResults: stored,
      membershipScopes: [{ recordId: row.plan_id, status: "verified", candidateLines: [] }],
      ...overrides[row.plan_id],
    };
  }
  return { results };
}

test("record closure distinguishes supported existing benefits from completeness", () => {
  const rows = Array.from({ length: 245 }, (_, index) => ({
    plan_id: index + 1,
    business_id: 10,
    membership_name: `Plan ${index + 1}`,
    source_url: `https://example.test/${index + 1}`,
    updated_at: "2026-09-21T00:00:00Z",
    benefits: index < 2 ? [] : ["Visible benefit"],
  }));
  const report = reportFor(rows);
  const audits = buildMembershipRecordAudits(rows, report);
  const summary = summarizeMembershipAudits(audits, { membershipRecords: 245, benefitClaims: 243, visibleMatch: 243, imageOnly: 0 });
  assert.equal(audits.length, 245);
  assert.equal(audits.filter(item => item.record_class === "empty_benefits_pending").length, 2);
  assert.equal(audits.filter(item => item.record_class === "supported_existing_only").length, 243);
  assert.equal(summary.recordClosure, 245);
  assert.equal(summary.completenessVerifiedRecords, 243);
  assert.equal(summary.writeEligibleRecords, 0);
});

test("partial support preserves unresolved benefits and never proposes removal", () => {
  const rows = [{
    plan_id: 20,
    business_id: 10,
    membership_name: "Mixed",
    source_url: "https://example.test/mixed",
    updated_at: "2026-09-21T00:00:00Z",
    benefits: ["Visible benefit", "Hidden benefit"],
  }];
  const report = reportFor(rows, { 20: {
    claimResults: [claim(20, "Visible benefit", "visible_match", 0), claim(20, "Hidden benefit", "dom_only_hidden", 1)],
    membershipScopes: [{ recordId: 20, status: "verified", candidateLines: [{ text: "Possible new service", evidence: { kind: "rendered_membership_scope" } }] }],
  } });
  const audit = buildMembershipRecordAudits(rows, report)[0];
  assert.equal(audit.record_class, "partial_support");
  assert.equal(audit.supported_count, 1);
  assert.equal(audit.unresolved_count, 1);
  assert.deepEqual(audit.before_after_diff.removals, []);
  assert.equal(audit.page_completeness.status, "candidate_review_required");
  assert.equal(audit.write_eligible, false);
});

test("247 can patch only the evidence-closed price split and preserve unresolved benefits", () => {
  const row = {
    plan_id: 247,
    business_id: 55,
    membership_name: "TRU SIGNATURE",
    source_url: "https://tru.test/membership",
    updated_at: "2026-09-21T00:00:00Z",
    benefits: ["$11.50/unit Botox and Dysport", "Other unresolved benefit"],
  };
  const report = reportFor([row], { 247: {
    claimResults: [
      claim(247, "$11.50/unit Botox and Dysport", "visible_match", 0),
      claim(247, "Other unresolved benefit", "not_found", 1),
      claim(247, "$11.50/unit Botox", "visible_match", null, "verification"),
      claim(247, "$11.50/equivalent dosing Dysport", "visible_match", null, "verification"),
    ],
    membershipScopes: [{ recordId: 247, status: "verified", candidateLines: [] }],
  } });
  const audit = buildMembershipRecordAudits([row], report)[0];
  assert.deepEqual(audit.before_after_diff.after, ["$11.50/unit Botox", "$11.50/equivalent dosing Dysport", "Other unresolved benefit"]);
  assert.equal(audit.write_eligible, true);
  assert.equal(audit.before_after_diff.removals.length, 0);
  const dryRun = buildMembershipRepairDryRun([audit], report);
  assert.equal(dryRun.databaseWrites, 0);
  assert.equal(dryRun.candidates[0].write_eligible, true);
});

test("Flora conditions map one-to-one and protected plans stay non-writable", () => {
  const rows = [
    [644, "Silver", "Choose Any 2 Services Monthly"],
    [645, "Gold", "Choose Any 3 Services Monthly"],
    [646, "Platinum", "Choose Any 3 Services Monthly"],
    [647, "Diamond", "Choose Any 4 Services Monthly"],
  ].map(([plan_id, membership_name, condition]) => ({
    plan_id, business_id: 1, membership_name, source_url: "https://flora.test/membership", updated_at: "2026-09-21T00:00:00Z", benefits: ["Service option"], condition,
  }));
  const report = reportFor(rows);
  report.results[rows[0].source_url] = {
    status: "page_accessible",
    membershipScopes: rows.map(row => ({ recordId: row.plan_id, status: "verified", candidateLines: [] })),
    claimResults: rows.flatMap(row => [
      claim(row.plan_id, "Service option", "visible_match", 0),
      claim(row.plan_id, row.condition, "visible_match", null, "verification"),
    ]),
  };
  const audits = buildMembershipRecordAudits(rows, report);
  assert.deepEqual(audits.map(item => item.before_after_diff.additions[0].value), [
    "Choose Any 2 Services Monthly", "Choose Any 3 Services Monthly", "Choose Any 3 Services Monthly", "Choose Any 4 Services Monthly",
  ]);
  assert.deepEqual(audits.map(item => item.write_eligible), [true, true, true, true]);

  for (const plan_id of [676, 677]) {
    const protectedRow = { plan_id, business_id: 1, membership_name: `Plan ${plan_id}`, source_url: `https://image.test/${plan_id}`, updated_at: "2026-09-21T00:00:00Z", benefits: ["Image benefit"] };
    const protectedReport = reportFor([protectedRow], { [plan_id]: {
      claimResults: [claim(plan_id, "Image benefit", "not_found", 0)],
      imageReviewStatus: "completed",
      membershipScopes: [{ recordId: plan_id, status: "verified", candidateLines: [] }],
    } });
    const protectedAudit = buildMembershipRecordAudits([protectedRow], protectedReport)[0];
    assert.equal(protectedAudit.write_eligible, false);
  }
});

test("empty historical plans remain pending and do not create claims", () => {
  const rows = [461, 462].map(plan_id => ({ plan_id, business_id: 2, membership_name: `Legacy ${plan_id}`, source_url: "https://medspot.test/membership", updated_at: "2026-09-21T00:00:00Z", benefits: [] }));
  const report = reportFor(rows);
  const audits = buildMembershipRecordAudits(rows, report);
  assert.deepEqual(audits.map(item => item.record_class), ["empty_benefits_pending", "empty_benefits_pending"]);
  assert.deepEqual(audits.map(item => item.existing_benefits), [[], []]);
  assert.deepEqual(audits.map(item => item.before_after_diff.additions), [[], []]);
  assert.equal(buildMembershipRepairDryRun(audits, report).candidates.length, 2);
});

test("controlled apply detects identity/update conflicts and is idempotent", () => {
  const candidate = {
    plan_id: 644,
    business_id: 1,
    membership_name: "Silver",
    source_url: "https://flora.test/membership",
    updated_at: "2026-09-21T00:00:00Z",
    current_benefits: ["Service option"],
    suggested_benefits: ["Service option", "Choose Any 2 Services Monthly"],
  };
  const ready = compareCandidateSnapshot(candidate, {
    plan_id: 644, business_id: 1, membership_name: "Silver", source_url: candidate.source_url,
    updated_at: candidate.updated_at, benefits: candidate.current_benefits,
  });
  assert.equal(ready.status, "ready");
  const conflict = compareCandidateSnapshot(candidate, {
    plan_id: 644, business_id: 99, membership_name: "Silver", source_url: candidate.source_url,
    updated_at: candidate.updated_at, benefits: candidate.current_benefits,
  });
  assert.deepEqual(conflict.conflicts, ["business_id"]);
  const alreadyApplied = compareCandidateSnapshot(candidate, {
    plan_id: 644, business_id: 1, membership_name: "Silver", source_url: candidate.source_url,
    updated_at: "2026-09-22T00:00:00Z", benefits: candidate.suggested_benefits,
  });
  assert.equal(alreadyApplied.status, "already_applied");
});

test("semantic-equivalent candidate supports a claim and UI headings do not block completeness", () => {
  const row = {
    plan_id: 244,
    business_id: 1,
    membership_name: "Botox Membership",
    source_url: "https://example.test/membership",
    updated_at: "2026-09-21T00:00:00Z",
    benefits: ["15% off all skincare, make-up and Latisse"],
  };
  const report = reportFor([row], { 244: {
    claimResults: [claim(244, row.benefits[0], "not_found", 0)],
    membershipScopes: [{ recordId: 244, status: "verified", headingText: row.membership_name, container: { tag: "ARTICLE", rect: { x: 1, y: 2, width: 300, height: 400 } }, candidateLines: [
      { text: "15% Off All Skin Care, Make-Up And Latisse", evidence: { kind: "rendered_membership_scope" } },
      { text: "Membership Benefits", evidence: { kind: "rendered_membership_scope" } },
    ] }],
  } });
  const audit = buildMembershipRecordAudits([row], report)[0];
  assert.equal(audit.benefit_results[0].status, "semantic_match");
  assert.equal(audit.benefit_results[0].plan_anchor.headingText, row.membership_name);
  assert.equal(audit.page_candidates.length, 0);
  assert.equal(audit.page_completeness.verified, true);
});
