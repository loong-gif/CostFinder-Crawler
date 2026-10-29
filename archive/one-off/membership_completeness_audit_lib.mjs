import { asClaims, claimHasRenderedEvidence, normalize } from './membership_visibility_qa_lib.mjs';

const EXPLICIT_CONDITIONS = new Map([
  [644, 'Choose Any 2 Services Monthly'],
  [645, 'Choose Any 3 Services Monthly'],
  [646, 'Choose Any 3 Services Monthly'],
  [647, 'Choose Any 4 Services Monthly'],
]);

function pageFor(row, report) {
  return report?.results?.[String(row.source_url || '').trim()] || null;
}

function scopeAnchor(page, planId) {
  const scope = (page?.membershipScopes || []).find(item => Number(item.recordId) === Number(planId));
  if (!scope) return null;
  return {
    recordId: Number(planId),
    headingText: scope.headingText || null,
    scopeStatus: scope.status || null,
    container: scope.container || null,
  };
}

function enrichClaim(claim, page, planId) {
  const planAnchor = scopeAnchor(page, planId);
  if (!planAnchor) return { ...claim };
  const visibleEvidence = claim.visibleEvidence
    ? { ...claim.visibleEvidence, planAnchor }
    : claim.visibleMatch
      ? { kind: 'rendered_dom_range', rects: claim.visibleMatch.rects || [], planAnchor }
      : null;
  return { ...claim, planAnchor, visibleEvidence };
}

function claimsFor(row, page) {
  return (page?.claimResults || []).filter(claim =>
    claim.recordType === 'membership' &&
    Number(claim.recordId) === Number(row.plan_id) &&
    claim.claimKind === 'stored_benefit'
  ).map(claim => enrichClaim(claim, page, row.plan_id));
}

function verificationFor(row, page, text) {
  const claim = (page?.claimResults || []).find(claim =>
    claim.recordType === 'membership' &&
    Number(claim.recordId) === Number(row.plan_id) &&
    claim.claimKind === 'verification' &&
    normalize(claim.claim) === normalize(text)
  ) || null;
  return claim ? enrichClaim(claim, page, row.plan_id) : null;
}

function semanticTokens(value) {
  let text = String(value || '').normalize('NFKC').toLocaleLowerCase();
  text = text.replace(/\bw\s*\/\s*/g, ' with ');
  text = text.replace(/\b(\d+)\.00\b/g, '$1');
  text = text.replace(/\bbanked\s*\/\s*month\b/g, 'banked per month');
  text = normalize(text)
    .replace(/\bskin care\b/g, 'skincare')
    .replace(/\bmake up\b/g, 'makeup')
    .replace(/\bper area\b/g, 'perarea');
  return text.split(' ').filter(Boolean).map(token => {
    if (token.length > 4 && token.endsWith('ies')) return `${token.slice(0, -3)}y`;
    if (token.length > 4 && token.endsWith('s')) return token.slice(0, -1);
    return token;
  });
}

function semanticSimilarity(left, right) {
  const a = new Set(semanticTokens(left));
  const b = new Set(semanticTokens(right));
  if (!a.size || !b.size) return 0;
  const intersection = [...a].filter(token => b.has(token)).length;
  return (2 * intersection) / (a.size + b.size);
}

function isNonBenefitCandidate(text) {
  const normalized = normalize(text);
  if (!normalized) return true;
  const exactLabels = new Set([
    'membership', 'memberships', 'membership benefits', 'benefits', 'treatments',
    'services', 'member perks', 'monthly treatments', 'per month', 'choose your card',
    'choose a card', 'plan options', 'select a plan', 'membership options', 'what is included',
  ]);
  if (exactLabels.has(normalized)) return true;
  if (/^(book now|buy now|join now|sign up|learn more|read more|show more|view more|expand|accept|continue|subscribe|start today)$/.test(normalized)) return true;
  return false;
}

function resolveSemanticMatches(claims, candidateLines, allowSemantic) {
  if (!allowSemantic) return claims;
  return claims.map(claim => {
    if (claim.status !== 'not_found') return claim;
    let best = null;
    for (const candidate of candidateLines) {
      const score = semanticSimilarity(claim.claim, candidate.text);
      if (score >= 0.9 && (!best || score > best.score)) best = { candidate, score };
    }
    if (!best) return claim;
    return {
      ...claim,
      status: 'semantic_match',
      semanticMatch: { candidateText: best.candidate.text, score: Number(best.score.toFixed(4)), evidence: best.candidate.evidence || null },
      visibleEvidence: {
        kind: 'semantic_equivalent_rendered_candidate',
        candidateText: best.candidate.text,
        score: Number(best.score.toFixed(4)),
        planAnchor: claim.planAnchor || null,
        candidateEvidence: best.candidate.evidence || null,
      },
    };
  });
}

function reasonList(claims, page, row) {
  const reasons = new Set();
  if (!claims.length && asClaims(row.benefits).length === 0) reasons.add('empty_benefits');
  if (claims.some(claim => claim.status === 'page_unavailable')) reasons.add('page_unavailable');
  if (claims.some(claim => claim.status === 'dom_only_hidden')) reasons.add('dom_only_hidden');
  if (claims.some(claim => claim.status === 'image_review_pending')) reasons.add('image_review_pending');
  if (claims.some(claim => claim.status === 'not_found')) reasons.add('not_found');
  if (page?.imageReviewStatus === 'completed' && claims.some(claim => claim.status === 'not_found')) reasons.add('image_ocr_no_exact_match');
  if (page?.imageReviewStatus && page.imageReviewStatus !== 'completed' && claims.some(claim => ['not_found', 'image_review_pending'].includes(claim.status))) reasons.add('image_review_incomplete');
  if (page?.status === 'page_accessible' && page?.membershipScopes?.find(scope => Number(scope.recordId) === Number(row.plan_id))?.status !== 'verified') reasons.add('page_scope_unverified');
  if ([461, 462].includes(Number(row.plan_id))) reasons.add('plan_identity_unresolved');
  return [...reasons].sort();
}

function recordClass(row, claims, reasons, explicitChange) {
  if (asClaims(row.benefits).length === 0) return 'empty_benefits_pending';
  if (claims.length > 0 && claims.every(claim => claim.status === 'page_unavailable')) return 'page_unavailable';
  if (claims.every(claim => !claimHasRenderedEvidence(claim))) return 'no_visible_support';
  if (claims.some(claim => !claimHasRenderedEvidence(claim))) return 'partial_support';
  if (explicitChange) return 'supported_with_explicit_change';
  return 'supported_existing_only';
}

function scopeFor(row, page) {
  const selected = (page?.membershipScopes || []).find(scope => Number(scope.recordId) === Number(row.plan_id));
  if (selected) {
    const signature = JSON.stringify(selected.container || null);
    const shared = (page?.membershipScopes || []).filter(scope => JSON.stringify(scope.container || null) === signature);
    if (signature !== 'null' && shared.length > 1) {
      return { ...selected, status: 'shared_container', reason: 'container_shared_by_multiple_membership_records' };
    }
    return selected;
  }
  return {
    recordId: Number(row.plan_id),
    status: page?.status === 'page_unavailable' ? 'page_unavailable' : 'unverified',
    reason: page?.status === 'page_unavailable' ? 'page_unavailable' : 'membership_scope_not_captured',
    candidateLines: [],
  };
}

function explicitDiff(row, page, claims) {
  const planId = Number(row.plan_id);
  const before = asClaims(row.benefits);
  const unchanged = [...before];
  const base = {
    before,
    after: before,
    additions: [],
    modifications: [],
    removals: [],
    evidence: [],
    approved: false,
    reason: 'no_explicit_repair_spec',
  };
  if (planId === 247 && before.includes('$11.50/unit Botox and Dysport')) {
    const botox = verificationFor(row, page, '$11.50/unit Botox');
    const dysport = verificationFor(row, page, '$11.50/equivalent dosing Dysport');
    const evidence = [botox, dysport].filter(Boolean);
    const after = before.flatMap(value => value === '$11.50/unit Botox and Dysport'
      ? ['$11.50/unit Botox', '$11.50/equivalent dosing Dysport']
      : [value]);
    const unresolved = claims.some(claim => !claimHasRenderedEvidence(claim));
    return {
      ...base,
      after,
      modifications: [{ from: '$11.50/unit Botox and Dysport', to: ['$11.50/unit Botox', '$11.50/equivalent dosing Dysport'] }],
      evidence: evidence.map(item => ({ claim: item.claim, status: item.status, visibleEvidence: item.visibleEvidence || item.visibleMatch || null })),
      approved: evidence.length === 2 && evidence.every(claimHasRenderedEvidence),
      reason: evidence.length === 2 && evidence.every(claimHasRenderedEvidence) ? null : 'split_price_terms_lack_complete_rendered_evidence',
    };
  }
  const condition = EXPLICIT_CONDITIONS.get(planId);
  if (condition) {
    const evidence = verificationFor(row, page, condition);
    const supported = claims.length > 0 && claims.every(claimHasRenderedEvidence);
    return {
      ...base,
      after: [...unchanged, condition],
      additions: [{ value: condition, evidence: evidence?.visibleEvidence || evidence?.visibleMatch || null }],
      evidence: evidence ? [{ claim: evidence.claim, status: evidence.status, visibleEvidence: evidence.visibleEvidence || evidence.visibleMatch || null }] : [],
      approved: supported && Boolean(evidence && claimHasRenderedEvidence(evidence)),
      reason: !supported ? 'existing_benefits_have_unresolved_items' : evidence ? null : 'shared_monthly_selection_condition_lacks_rendered_evidence',
    };
  }
  return base;
}

export function buildMembershipRecordAudits(memberships, report) {
  return memberships.map(row => {
    const page = pageFor(row, report);
    const rawClaims = claimsFor(row, page);
    const scope = scopeFor(row, page);
    const pageHeadings = new Set((page?.membershipScopes || []).map(item => normalize(item.headingText)).filter(Boolean));
    const rawCandidateLines = (scope.candidateLines || []).filter(candidate => !isNonBenefitCandidate(candidate.text) && !pageHeadings.has(normalize(candidate.text)) && ![...pageHeadings].some(heading => semanticSimilarity(candidate.text, heading) >= 0.9));
    const claims = resolveSemanticMatches(rawClaims, rawCandidateLines, scope.status === 'verified');
    const matchedCandidateTexts = new Set(claims.map(claim => claim.semanticMatch?.candidateText).filter(Boolean).map(normalize));
    const reasons = reasonList(claims, page, row);
    const diff = explicitDiff(row, page, claims);
    const supported = claims.filter(claimHasRenderedEvidence);
    const unresolved = claims.filter(claim => !claimHasRenderedEvidence(claim));
    const pageCandidates = rawCandidateLines.filter(candidate => !matchedCandidateTexts.has(normalize(candidate.text))).map(candidate => ({
      text: candidate.text,
      reason: 'not_exactly_represented_by_stored_benefits',
      evidence: candidate.evidence || null,
    }));
    const completenessStatus = page?.status === 'page_unavailable'
      ? 'page_unavailable'
      : scope.status !== 'verified'
        ? 'unverified'
        : pageCandidates.length
          ? 'candidate_review_required'
          : unresolved.length
            ? 'unverified'
            : 'candidate_scan_clear';
    const completenessVerified = completenessStatus === 'candidate_scan_clear' && claims.length > 0;
    const existingSupported = supported.map(claim => ({
      benefit_index: claim.benefitIndex,
      text: claim.claim,
      status: claim.status,
      evidence: claim.visibleEvidence || claim.visibleMatch || null,
    }));
    const unresolvedItems = unresolved.map(claim => ({
      benefit_index: claim.benefitIndex,
      text: claim.claim,
      status: claim.status,
      evidence: claim.visibleEvidence || claim.visibleMatch || null,
    }));
    return {
      plan_id: Number(row.plan_id),
      business_id: row.business_id == null ? null : Number(row.business_id),
      membership_name: String(row.membership_name || ''),
      source_url: row.source_url || null,
      updated_at: row.updated_at || null,
      existing_benefits: asClaims(row.benefits),
      benefit_results: claims.map(claim => ({
        benefit_index: claim.benefitIndex,
        text: claim.claim,
        status: claim.status,
        evidence: claim.visibleEvidence || claim.visibleMatch || null,
        plan_anchor: claim.planAnchor || null,
        semantic_match: claim.semanticMatch || null,
        hidden_ancestors: claim.hiddenAncestors || [],
      })),
      existing_supported: existingSupported,
      proposed_additions: diff.additions || [],
      proposed_modifications: diff.modifications || [],
      proposed_removals: diff.removals || [],
      unresolved_items: unresolvedItems,
      supported_count: supported.length,
      unresolved_count: unresolved.length,
      unresolved_reasons: reasons,
      record_class: recordClass(row, claims, reasons, diff.modifications.length > 0 || diff.additions.length > 0),
      page_completeness: {
        status: completenessStatus,
        verified: completenessVerified,
        scope_status: scope.status,
        scope_reason: scope.reason || null,
        container: scope.container || null,
        candidate_count: pageCandidates.length,
      },
      page_candidates: pageCandidates,
      before_after_diff: diff,
      write_eligible: Boolean(diff.approved),
      write_blockers: diff.approved ? [] : [diff.reason, ...reasons].filter(Boolean),
    };
  });
}

export function summarizeMembershipAudits(audits, visibilitySummary) {
  const recordClassCounts = {};
  const reasonCounts = {};
  const completenessCounts = {};
  const scopeStatusCounts = {};
  let completenessVerified = 0;
  let writeEligible = 0;
  let candidateCount = 0;
  let semanticMatch = 0;
  let supportedRecords = 0;
  let reviewRecords = 0;
  let storedBenefitClaims = 0;
  for (const audit of audits) {
    recordClassCounts[audit.record_class] = (recordClassCounts[audit.record_class] || 0) + 1;
    for (const reason of audit.unresolved_reasons) reasonCounts[reason] = (reasonCounts[reason] || 0) + 1;
    const status = audit.page_completeness.status;
    completenessCounts[status] = (completenessCounts[status] || 0) + 1;
    const scopeStatus = audit.page_completeness.scope_status;
    scopeStatusCounts[scopeStatus] = (scopeStatusCounts[scopeStatus] || 0) + 1;
    if (audit.page_completeness.verified) completenessVerified += 1;
    if (audit.write_eligible) writeEligible += 1;
    candidateCount += audit.page_candidates.length;
    storedBenefitClaims += audit.benefit_results.length;
    semanticMatch += audit.benefit_results.filter(item => item.status === 'semantic_match').length;
    if (audit.existing_benefits.length > 0) {
      if (audit.benefit_results.length > 0 && audit.benefit_results.every(item => claimHasRenderedEvidence(item))) supportedRecords += 1;
      else reviewRecords += 1;
    }
  }
  return {
    ...visibilitySummary,
    benefitClaims: storedBenefitClaims,
    semanticMatch,
    notFoundLiteral: Number(visibilitySummary.notFound || 0),
    notFound: Math.max(0, Number(visibilitySummary.notFound || 0) - semanticMatch),
    notFoundExact: Math.max(0, Number(visibilitySummary.notFound || 0) - semanticMatch),
    recordsVisible: supportedRecords,
    recordsReview: reviewRecords,
    recordsEmptyBenefitsPending: audits.filter(audit => audit.record_class === 'empty_benefits_pending').length,
    recordClassCounts,
    unresolvedReasonRecordCounts: reasonCounts,
    pageCompletenessCounts: completenessCounts,
    scopeStatusCounts,
    completenessVerifiedRecords: completenessVerified,
    writeEligibleRecords: writeEligible,
    pageCandidateCount: candidateCount,
    recordClosure: audits.length,
  };
}

function repairCandidateFromAudit(audit) {
  const diff = audit.before_after_diff || {};
  const protectedPlan = [461, 462, 676, 677].includes(Number(audit.plan_id));
  const hasChange = Boolean((diff.additions || []).length || (diff.modifications || []).length || (diff.removals || []).length);
  if (!hasChange && !protectedPlan) return null;
  const page = audit.page_completeness || {};
  const evidence = [
    ...(diff.evidence || []),
    ...((audit.benefit_results || []).filter(item => item.evidence).map(item => ({
      claim: item.text,
      status: item.status,
      evidence: item.evidence,
    }))),
  ];
  return {
    plan_id: audit.plan_id,
    business_id: audit.business_id,
    membership_name: audit.membership_name,
    source_url: audit.source_url,
    updated_at: audit.updated_at,
    final_url: null,
    current_benefits: audit.existing_benefits,
    suggested_benefits: diff.after || audit.existing_benefits,
    evidence_type: [...new Set(evidence.map(item => item.evidence?.kind || item.status).filter(Boolean))],
    evidence,
    page_completeness: page.status,
    write_eligible: audit.write_eligible,
    allowed_to_write: audit.write_eligible,
    reason: audit.write_eligible ? null : (audit.write_blockers || [])[0] || 'not_evidence_closed',
    before_after_diff: diff,
  };
}

export function buildMembershipRepairDryRun(audits, report) {
  const pageByUrl = report?.results || {};
  const candidates = audits.map(audit => {
    const item = repairCandidateFromAudit(audit);
    if (!item) return null;
    const page = pageByUrl[String(audit.source_url || '').trim()];
    return {
      ...item,
      final_url: page?.finalUrl || page?.url || null,
      page_evidence: {
        scope: page?.membershipScopes?.find(scope => Number(scope.recordId) === Number(audit.plan_id)) || null,
        image_review: page?.imageReview || [],
        screenshot_path: page?.screenshotPath || null,
      },
    };
  }).filter(Boolean);
  return {
    generatedAt: new Date().toISOString(),
    mode: 'dry_run',
    databaseWrites: 0,
    offerOperations: 0,
    clinicServiceOperations: 0,
    candidates,
  };
}

export function markdownMembershipAudit(report) {
  const s = report.summary;
  const lines = [
    '# Membership 双向完整性对账',
    '',
    `- Checked at: \`${report.checkedAt}\``,
    '- Database writes: `0`',
    `- Membership records: \`${s.membershipRecords}\``,
    `- Benefit entries: \`${s.benefitClaims}\``,
    `- Existing benefit support: \`${s.visibleMatch + s.imageOnly}\` supported entries`,
    `- Existing benefit support is not completeness verification: \`true\``,
    `- Completeness verified: \`${s.completenessVerifiedRecords}\` records`,
    `- Write eligible candidates: \`${s.writeEligibleRecords}\` records`,
    `- Page candidates requiring review: \`${s.pageCandidateCount}\``,
    `- Semantic-equivalent matches: \`${s.semanticMatch || 0}\``,
    `- Remaining exact-not-found: \`${s.notFoundExact ?? s.notFound}\``,
    '',
    '## Record Classes',
    '',
  ];
  for (const [key, value] of Object.entries(s.recordClassCounts || {})) lines.push(`- \`${key}\`: ${value}`);
  lines.push('', '## Pending Reason Distribution', '');
  for (const [key, value] of Object.entries(s.unresolvedReasonRecordCounts || {})) lines.push(`- \`${key}\`: ${value} records`);
  lines.push('', '## Page Completeness', '');
  for (const [key, value] of Object.entries(s.pageCompletenessCounts || {})) lines.push(`- \`${key}\`: ${value} records`);
  lines.push('', '## Plan-level Scope Attribution', '');
  for (const [key, value] of Object.entries(s.scopeStatusCounts || {})) lines.push(`- \`${key}\`: ${value} records`);
  lines.push('', '## Write Candidates', '');
  for (const audit of (report.recordAudits || []).filter(item => item.before_after_diff.modifications.length || item.before_after_diff.additions.length)) {
    lines.push(`- plan \`${audit.plan_id}\` — ${audit.membership_name} — eligible: \`${audit.write_eligible}\` — ${audit.write_blockers.join(', ') || 'none'}`);
  }
  lines.push('', '## Page Candidates', '');
  for (const audit of (report.recordAudits || []).filter(item => item.page_candidates.length)) {
    lines.push(`- plan \`${audit.plan_id}\` — ${audit.page_candidates.length} candidate lines — completeness: \`${audit.page_completeness.status}\``);
    for (const candidate of audit.page_candidates.slice(0, 20)) lines.push(`  - ${candidate.text}`);
  }
  lines.push('', '## Scope', '', 'Existing benefit support and Membership completeness are separate conclusions. Page candidates are review evidence only and are not written to Supabase. This report does not execute Offer or clinic_services operations.', '');
  return lines.join('\n');
}
