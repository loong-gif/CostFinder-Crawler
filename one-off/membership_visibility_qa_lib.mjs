export function asClaims(value) {
  if (Array.isArray(value)) {
    return value.map(item => String(item ?? '').trim()).filter(Boolean);
  }
  if (typeof value === 'string' && value.trim()) return [value.trim()];
  return [];
}

export function normalize(value) {
  let text = String(value || '').normalize('NFKC');
  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, ' $1 ');
  text = text.replace(/\[([^\]]*)\]\([^)]*\)/g, ' $1 ');
  text = text.replace(/<[^>]*>/g, ' ').replace(/[–—]/g, '-');
  text = text.replace(/[*_~`#>\\]/g, ' ');
  return text
    .replace(/([\p{N}])([\p{L}])/gu, '$1 $2')
    .replace(/([\p{L}])([\p{N}])/gu, '$1 $2')
    .replace(/[^\p{L}\p{N}$%]+/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .toLocaleLowerCase();
}

export async function collectPagedRows(fetchPage, pageSize = 500) {
  const rows = [];
  for (let offset = 0; ; offset += pageSize) {
    const batch = await fetchPage(offset, pageSize);
    if (!Array.isArray(batch) || batch.length === 0) break;
    rows.push(...batch);
    if (batch.length < pageSize) break;
  }
  return rows;
}

const VERIFYING_CLAIMS = new Map([
  [247, [
    '$11.50/unit Botox',
    '$11.50/equivalent dosing Dysport',
  ]],
  [644, ['Choose Any 2 Services Monthly']],
  [645, ['Choose Any 3 Services Monthly']],
  [646, ['Choose Any 3 Services Monthly']],
  [647, ['Choose Any 4 Services Monthly']],
]);

export function buildManifest(memberships) {
  const manifest = new Map();
  for (const row of memberships) {
    const sourceUrl = String(row.source_url || '').trim();
    if (!sourceUrl) continue;
    if (!manifest.has(sourceUrl)) manifest.set(sourceUrl, { sourceUrl, records: [] });
    const storedClaims = asClaims(row.benefits);
    const verificationClaims = VERIFYING_CLAIMS.get(Number(row.plan_id)) || [];
    manifest.get(sourceUrl).records.push({
      recordType: 'membership',
      recordId: Number(row.plan_id),
      membershipName: String(row.membership_name || ''),
      membershipPrice: row.membership_price == null ? null : String(row.membership_price),
      businessId: row.business_id == null ? null : Number(row.business_id),
      storedBenefits: storedClaims,
      claims: [
        ...storedClaims.map((claim, benefitIndex) => ({
          claim,
          claimKind: 'stored_benefit',
          benefitIndex,
        })),
        ...verificationClaims.map(claim => ({
          claim,
          claimKind: 'verification',
          benefitIndex: null,
        })),
      ],
      emptyBenefits: storedClaims.length === 0,
    });
  }
  return [...manifest.values()].sort((left, right) => left.sourceUrl.localeCompare(right.sourceUrl));
}

export function isReviewableImage(image) {
  if (!image || image.kind !== 'image') return false;
  const width = Number(image.rect?.width || 0);
  const height = Number(image.rect?.height || 0);
  if (width <= 0 || height <= 0) return false;
  const descriptor = `${image.src || ''} ${image.alt || ''}`.toLocaleLowerCase();
  if (/tracking|pixel|spacer|favicon|social[-_ ]?icon|instagram|facebook|twitter|youtube|logo/.test(descriptor)) {
    return /membership|benefit|plan|price|club|package|tier/.test(descriptor);
  }
  return width * height >= 100 * 100 || /membership|benefit|plan|price|club|package|tier/.test(descriptor);
}

export function claimHasRenderedEvidence(claim) {
  return ['visible_match', 'semantic_match', 'image_only'].includes(claim?.status);
}

export function buildRepairDryRun(memberships, report) {
  const rowsById = new Map(memberships.map(row => [Number(row.plan_id), row]));
  const resultByUrl = report?.results || {};
  const resultForPlan = planId => {
    const row = rowsById.get(planId);
    return row ? resultByUrl[String(row.source_url || '').trim()] || null : null;
  };
  const evidenceFor = (planId, claim) => {
    const result = resultForPlan(planId);
    return (result?.claimResults || []).find(item =>
      item.recordId === planId && item.claimKind === 'verification' && normalize(item.claim) === normalize(claim)
    ) || null;
  };
  const base = planId => {
    const row = rowsById.get(planId);
    const page = resultForPlan(planId);
    return {
      planId,
      membershipName: row?.membership_name || null,
      sourceUrl: row?.source_url || null,
      finalUrl: page?.finalUrl || page?.url || null,
      currentBenefits: asClaims(row?.benefits),
      suggestedBenefits: null,
      evidenceType: [],
      evidence: [],
      allowedToWrite: false,
      reason: null,
    };
  };
  const candidates = [];

  {
    const item = base(247);
    const botox = evidenceFor(247, '$11.50/unit Botox');
    const dysport = evidenceFor(247, '$11.50/equivalent dosing Dysport');
    item.suggestedBenefits = item.currentBenefits.flatMap(value => value === '$11.50/unit Botox and Dysport'
      ? ['$11.50/unit Botox', '$11.50/equivalent dosing Dysport']
      : [value]);
    item.evidence = [botox, dysport].filter(Boolean).map(value => ({
      claim: value.claim,
      status: value.status,
      evidence: value.visibleEvidence || value.visibleMatch || null,
    }));
    item.evidenceType = item.evidence.map(value => value.status);
    item.allowedToWrite = item.evidence.length === 2 && item.evidence.every(value => ['visible_match', 'image_only'].includes(value.status));
    item.reason = item.allowedToWrite ? null : 'split_price_terms_lack_complete_rendered_evidence';
    candidates.push(item);
  }

  for (const [planId, condition] of [[644, 'Choose Any 2 Services Monthly'], [645, 'Choose Any 3 Services Monthly'], [646, 'Choose Any 3 Services Monthly'], [647, 'Choose Any 4 Services Monthly']]) {
    const item = base(planId);
    const evidence = evidenceFor(planId, condition);
    item.suggestedBenefits = [...item.currentBenefits, condition];
    item.evidence = evidence ? [{ claim: evidence.claim, status: evidence.status, evidence: evidence.visibleEvidence || evidence.visibleMatch || null }] : [];
    item.evidenceType = item.evidence.map(value => value.status);
    item.allowedToWrite = Boolean(evidence && claimHasRenderedEvidence(evidence));
    item.reason = item.allowedToWrite ? null : 'shared_monthly_selection_condition_lacks_rendered_evidence';
    candidates.push(item);
  }

  for (const planId of [676, 677]) {
    const item = base(planId);
    const page = resultForPlan(planId);
    item.evidence = (page?.claimResults || [])
      .filter(value => value.recordId === planId)
      .map(value => ({ claim: value.claim, status: value.status, evidence: value.visibleEvidence || null }));
    item.evidenceType = [...new Set(item.evidence.map(value => value.status))];
    item.reason = 'image_review_required_before_benefit_acceptance';
    candidates.push(item);
  }

  for (const planId of [461, 462]) {
    const item = base(planId);
    item.reason = 'empty_benefits_pending_plan_identity_and_image_review';
    item.evidenceType = ['empty_benefits_pending'];
    candidates.push(item);
  }

  return {
    generatedAt: new Date().toISOString(),
    mode: 'dry_run',
    databaseWrites: 0,
    offerOperations: 0,
    clinicServiceOperations: 0,
    candidates,
  };
}

export function summarize(results, memberships, manifest) {
  const summary = {
    membershipRecords: memberships.length,
    membershipsWithBenefits: memberships.filter(row => asClaims(row.benefits).length > 0).length,
    emptyBenefitMemberships: memberships.filter(row => asClaims(row.benefits).length === 0).length,
    uniqueSourceUrls: manifest.length,
    urls: 0,
    accessibleUrls: 0,
    unavailableUrls: 0,
    benefitClaims: 0,
    visibleMatch: 0,
    semanticMatch: 0,
    imageOnly: 0,
    imageReviewPending: 0,
    domOnlyHidden: 0,
    notFound: 0,
    pageUnavailableClaims: 0,
    recordsVisible: 0,
    recordsReview: 0,
    recordsEmptyBenefitsPending: 0,
  };
  const recordStatuses = new Map();
  for (const result of Object.values(results)) {
    summary.urls += 1;
    if (result.status === 'page_accessible') summary.accessibleUrls += 1;
    else summary.unavailableUrls += 1;
    summary.recordsEmptyBenefitsPending += (result.emptyBenefitRecords || []).length;
    for (const claim of result.claimResults || []) {
      if (claim.claimKind !== 'stored_benefit') continue;
      summary.benefitClaims += 1;
      const key = `${claim.recordType}:${claim.recordId}`;
      if (!recordStatuses.has(key)) recordStatuses.set(key, []);
      recordStatuses.get(key).push(claim.status);
      if (claim.status === 'visible_match') summary.visibleMatch += 1;
      if (claim.status === 'semantic_match') summary.semanticMatch += 1;
      if (claim.status === 'image_only') summary.imageOnly += 1;
      if (claim.status === 'image_review_pending') summary.imageReviewPending += 1;
      if (claim.status === 'dom_only_hidden') summary.domOnlyHidden += 1;
      if (claim.status === 'not_found') summary.notFound += 1;
      if (claim.status === 'page_unavailable') summary.pageUnavailableClaims += 1;
    }
  }
  for (const statuses of recordStatuses.values()) {
    if (statuses.every(status => ['visible_match', 'image_only'].includes(status))) summary.recordsVisible += 1;
    else summary.recordsReview += 1;
  }
  return summary;
}
