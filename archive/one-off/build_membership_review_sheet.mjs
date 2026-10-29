import fs from 'node:fs/promises';
import { SpreadsheetFile, Workbook } from '@oai/artifact-tool';

const ROOT = '/Users/wyl/BrandRap/costfinder';
const REPORT = JSON.parse(await fs.readFile(`${ROOT}/outputs/membership_visibility_review_round2_20260921.json`, 'utf8'));
const DRY_RUN = JSON.parse(await fs.readFile(`${ROOT}/outputs/membership_benefits_repair_dry_run_round2_20260921.json`, 'utf8'));
const OUTPUT_DIR = `${ROOT}/outputs`;
const OUTPUT_XLSX = `${OUTPUT_DIR}/membership_visibility_review_round2_20260921.xlsx`;
const PREVIEW = `${OUTPUT_DIR}/membership_visibility_review_round2_20260921_preview.png`;

const compact = value => String(value ?? '').replace(/\s+/g, ' ').trim();
const jsonText = value => value == null ? '' : JSON.stringify(value);
const joinText = (values, separator = ' • ') => (values || []).map(compact).filter(Boolean).join(separator);
const checkedAtText = `UTC ${String(REPORT.checkedAt || '').replace('T', ' ').replace('Z', '')}`;
const pageByUrl = REPORT.results || {};
const pageFor = url => pageByUrl[String(url || '').trim()] || {};

const auditRows = REPORT.recordAudits.map(item => [
  item.plan_id,
  item.business_id,
  item.membership_name,
  item.source_url,
  item.updated_at,
  joinText(item.existing_benefits),
  item.supported_count,
  item.unresolved_count,
  joinText(item.unresolved_reasons, ', '),
  item.record_class,
  item.page_completeness?.status,
  item.page_completeness?.scope_status,
  item.page_completeness?.verified,
  item.page_candidates?.length || 0,
  joinText(item.existing_supported?.map(entry => entry.text)),
  joinText(item.proposed_additions?.map(entry => entry.value || entry.text || entry), ' • '),
  jsonText(item.proposed_modifications),
  jsonText(item.proposed_removals),
  joinText(item.unresolved_items?.map(entry => `${entry.status}: ${entry.text}`)),
  item.write_eligible,
  joinText(item.write_blockers, ', '),
]);

const evidenceRows = [];
for (const audit of REPORT.recordAudits) {
  const page = pageFor(audit.source_url);
  const claims = (page.claimResults || []).filter(claim => claim.recordType === 'membership' && Number(claim.recordId) === Number(audit.plan_id) && claim.claimKind === 'stored_benefit');
  for (const claim of claims) {
    const audited = (audit.benefit_results || []).find(item => Number(item.benefit_index) === Number(claim.benefitIndex));
    const evidence = audited?.evidence || claim.visibleEvidence || claim.visibleMatch || null;
    evidenceRows.push([
      audit.plan_id,
      audit.membership_name,
      audit.source_url,
      page.finalUrl || page.url || '',
      claim.benefitIndex,
      claim.claim,
      audited?.status || claim.status,
      evidence?.kind || '',
      jsonText(evidence?.rects || evidence?.matchRects || evidence),
      jsonText(claim.hiddenAncestors || []),
      page.imageReviewStatus || '',
      joinText(claim.imageOcrTranscript, ' / '),
      page.status || '',
      jsonText(audited?.plan_anchor || null),
    ]);
  }
}

const candidateRows = [];
for (const audit of REPORT.recordAudits) {
  for (const candidate of audit.page_candidates || []) {
    const container = candidate.evidence?.container || {};
    candidateRows.push([
      audit.plan_id,
      audit.membership_name,
      audit.source_url,
      audit.page_completeness?.status,
      candidate.text,
      candidate.evidence?.kind || '',
      container.tag || '',
      container.className || '',
      candidate.reason || '',
      '人工确认后再决定是否进入 benefits',
    ]);
  }
}

const repairRows = DRY_RUN.candidates.map(item => [
  item.plan_id,
  item.business_id,
  item.membership_name,
  item.source_url,
  item.final_url,
  item.updated_at,
  joinText(item.current_benefits),
  joinText(item.suggested_benefits),
  jsonText(item.before_after_diff?.additions || []),
  jsonText(item.before_after_diff?.modifications || []),
  jsonText(item.before_after_diff?.removals || []),
  joinText(item.evidence_type, ', '),
  item.page_completeness || '',
  item.write_eligible,
  item.reason || '',
  jsonText(item.evidence || []),
]);

const workbook = Workbook.create();
const overview = workbook.worksheets.add('Overview');
const audit = workbook.worksheets.add('Membership_Audit');
const evidence = workbook.worksheets.add('Benefit_Evidence');
const candidates = workbook.worksheets.add('Page_Candidates');
const repair = workbook.worksheets.add('Repair_Dry_Run');

const navy = '#1F4E78';
const lightBlue = '#D9EAF7';
const lightGray = '#F3F4F6';
const amber = '#FFF2CC';
const red = '#FCE4D6';
const green = '#E2F0D9';
const font = { name: 'Arial', size: 10 };

function styleSheet(sheet, usedRange, headerRange, widths = {}) {
  sheet.showGridLines = false;
  sheet.getRange(usedRange).format.font = font;
  sheet.getRange(headerRange).format = { fill: navy, font: { name: 'Arial', size: 10, bold: true, color: '#FFFFFF' }, wrapText: true, verticalAlignment: 'center' };
  sheet.getRange(usedRange).format.verticalAlignment = 'center';
  sheet.getRange(usedRange).format.borders = { insideHorizontal: { style: 'thin', color: '#E5E7EB' }, bottom: { style: 'thin', color: '#D1D5DB' } };
  for (const [column, width] of Object.entries(widths)) sheet.getRange(`${column}:${column}`).format.columnWidth = width;
}

overview.getRange('A1').values = [['Membership rendered-visibility review']];
overview.getRange('A1').format = { font: { name: 'Arial', size: 14, bold: true, color: navy } };
overview.getRange('A3:B3').values = [['Checked at', checkedAtText]];
overview.getRange('A4:B4').values = [['Scope', 'Existing benefit support and page-to-database completeness review; no Supabase writes']];
overview.getRange('A6:B6').values = [['Metric', 'Value']];
overview.getRange('A7:B16').values = [
  ['Membership records', REPORT.summary.membershipRecords],
  ['Memberships with benefits', REPORT.summary.membershipsWithBenefits],
  ['Empty benefits pending', REPORT.summary.emptyBenefitMemberships],
  ['Benefit entries', REPORT.summary.benefitClaims],
  ['Unique source URLs', REPORT.summary.uniqueSourceUrls],
  ['Accessible pages', REPORT.summary.accessibleUrls],
  ['Unavailable pages', REPORT.summary.unavailableUrls],
  ['Completeness verified records', REPORT.summary.completenessVerifiedRecords],
  ['Write eligible records', REPORT.summary.writeEligibleRecords],
  ['Page candidates for review', REPORT.summary.pageCandidateCount],
];
overview.getRange('D6:E6').values = [['Record class', 'Count']];
overview.getRange('D7:E12').values = Object.entries(REPORT.summary.recordClassCounts);
overview.getRange('D14:E14').values = [['Plan scope status', 'Count']];
overview.getRange('D15:E18').values = Object.entries(REPORT.summary.scopeStatusCounts || {});
overview.getRange('G6:H6').values = [['Benefit status', 'Count']];
overview.getRange('G7:H12').values = [
  ['visible_match', REPORT.summary.visibleMatch],
  ['image_only', REPORT.summary.imageOnly],
  ['image_review_pending', REPORT.summary.imageReviewPending],
  ['dom_only_hidden', REPORT.summary.domOnlyHidden],
  ['not_found', REPORT.summary.notFound],
  ['page_unavailable', REPORT.summary.pageUnavailableClaims],
];
overview.getRange('A19:H19').values = [['Interpretation', '', '', '', '', '', '', '']];
overview.getRange('A20:H23').values = [
  ['supported_existing_only', '现有 benefits 获得页面证据支持，不等于页面完整性通过。', '', '', '', '', '', ''],
  ['supported_with_explicit_change', '现有内容有明确可见修改证据；本轮 644–647 具备写入资格。', '', '', '', '', '', ''],
  ['partial_support / no_visible_support / page_unavailable', '仍有未决取证或页面访问问题，不自动删除或替换旧内容。', '', '', '', '', '', ''],
  ['Page candidates', '网页候选只供人工确认，未自动写入 benefits。', '', '', '', '', '', ''],
];
overview.getRange('A6:B6').format = { fill: navy, font: { name: 'Arial', size: 10, bold: true, color: '#FFFFFF' } };
overview.getRange('D6:E6').format = { fill: navy, font: { name: 'Arial', size: 10, bold: true, color: '#FFFFFF' } };
overview.getRange('D14:E14').format = { fill: navy, font: { name: 'Arial', size: 10, bold: true, color: '#FFFFFF' } };
overview.getRange('G6:H6').format = { fill: navy, font: { name: 'Arial', size: 10, bold: true, color: '#FFFFFF' } };
overview.getRange('A19:H19').format = { fill: lightBlue, font: { name: 'Arial', size: 10, bold: true, color: navy } };
overview.getRange('A20:H23').format.wrapText = true;
overview.getRange('A1:H23').format.font = font;
overview.getRange('A1:H23').format.verticalAlignment = 'center';
overview.getRange('B7:B16').format.horizontalAlignment = 'right';
overview.getRange('E7:E12').format.horizontalAlignment = 'right';
overview.getRange('H7:H12').format.horizontalAlignment = 'right';
overview.getRange('A:A').format.columnWidth = 30;
overview.getRange('B:B').format.columnWidth = 26;
overview.getRange('D:D').format.columnWidth = 30;
overview.getRange('E:E').format.columnWidth = 12;
overview.getRange('G:G').format.columnWidth = 24;
overview.getRange('H:H').format.columnWidth = 12;
overview.getRange('A20:A23').format.font = { name: 'Arial', size: 10, bold: true, color: navy };

const auditHeaders = ['plan_id', 'business_id', 'membership_name', 'source_url', 'updated_at', 'existing_benefits', 'supported_count', 'unresolved_count', 'unresolved_reasons', 'record_class', 'page_completeness', 'scope_status', 'completeness_verified', 'page_candidates_count', 'existing_supported', 'proposed_additions', 'proposed_modifications', 'proposed_removals', 'unresolved_items', 'write_eligible', 'write_blockers'];
audit.getRange('A1').values = [['Membership record audit']];
audit.getRange('A1').format = { font: { name: 'Arial', size: 14, bold: true, color: navy } };
audit.getRange('A2').values = [['One row per Membership record. Review eligibility is separate from completeness verification.']];
audit.getRange('A4:U4').values = [auditHeaders];
audit.getRange(`A5:U${4 + auditRows.length}`).values = auditRows;
styleSheet(audit, `A4:U${4 + auditRows.length}`, 'A4:U4', { A: 10, B: 10, C: 24, D: 42, E: 22, F: 46, G: 13, H: 14, I: 28, J: 25, K: 24, L: 15, M: 18, N: 18, O: 46, P: 40, Q: 42, R: 32, S: 46, T: 14, U: 34 });
audit.getRange(`F5:S${4 + auditRows.length}`).format.wrapText = true;
audit.getRange(`A5:B${4 + auditRows.length}`).format.horizontalAlignment = 'right';
audit.getRange(`G5:H${4 + auditRows.length}`).format.horizontalAlignment = 'right';
audit.getRange(`N5:N${4 + auditRows.length}`).format.horizontalAlignment = 'right';
audit.getRange(`M5:M${4 + auditRows.length}`).format.horizontalAlignment = 'center';
audit.getRange(`T5:T${4 + auditRows.length}`).format.horizontalAlignment = 'center';
audit.freezePanes.freezeRows(4);

const evidenceHeaders = ['plan_id', 'membership_name', 'source_url', 'final_url', 'benefit_index', 'benefit_text', 'status', 'evidence_type', 'match_or_ocr_evidence', 'hidden_ancestors', 'image_review_status', 'ocr_transcript', 'page_status', 'plan_anchor'];
evidence.getRange('A1').values = [['Benefit evidence']];
evidence.getRange('A1').format = { font: { name: 'Arial', size: 14, bold: true, color: navy } };
evidence.getRange('A2').values = [['One row per stored benefit claim.']];
evidence.getRange('A4:N4').values = [evidenceHeaders];
evidence.getRange(`A5:N${4 + evidenceRows.length}`).values = evidenceRows;
styleSheet(evidence, `A4:N${4 + evidenceRows.length}`, 'A4:N4', { A: 10, B: 24, C: 42, D: 42, E: 12, F: 50, G: 22, H: 26, I: 42, J: 42, K: 24, L: 42, M: 18, N: 48 });
evidence.getRange(`F5:N${4 + evidenceRows.length}`).format.wrapText = true;
evidence.freezePanes.freezeRows(4);

const candidateHeaders = ['plan_id', 'membership_name', 'source_url', 'page_completeness', 'candidate_text', 'evidence_type', 'container_tag', 'container_class', 'candidate_reason', 'review_action'];
candidates.getRange('A1').values = [['Page-to-database candidates']];
candidates.getRange('A1').format = { font: { name: 'Arial', size: 14, bold: true, color: navy } };
candidates.getRange('A2').values = [['Candidates are review-only and are not database facts.']];
candidates.getRange('A4:J4').values = [candidateHeaders];
candidates.getRange(`A5:J${4 + candidateRows.length}`).values = candidateRows;
styleSheet(candidates, `A4:J${4 + candidateRows.length}`, 'A4:J4', { A: 10, B: 24, C: 42, D: 24, E: 54, F: 32, G: 16, H: 46, I: 38, J: 34 });
candidates.getRange(`E5:J${4 + candidateRows.length}`).format.wrapText = true;
candidates.freezePanes.freezeRows(4);

const repairHeaders = ['plan_id', 'business_id', 'membership_name', 'source_url', 'final_url', 'updated_at', 'current_benefits', 'suggested_benefits', 'additions', 'modifications', 'removals', 'evidence_type', 'page_completeness', 'write_eligible', 'reason', 'evidence'];
repair.getRange('A1').values = [['Membership benefits repair dry-run']];
repair.getRange('A1').format = { font: { name: 'Arial', size: 14, bold: true, color: navy } };
repair.getRange('A2').values = [['No apply was executed. Only explicit evidence-closed diffs can become write eligible.']];
repair.getRange('A4:P4').values = [repairHeaders];
repair.getRange(`A5:P${4 + repairRows.length}`).values = repairRows;
styleSheet(repair, `A4:P${4 + repairRows.length}`, 'A4:P4', { A: 10, B: 10, C: 24, D: 42, E: 42, F: 22, G: 48, H: 48, I: 38, J: 44, K: 32, L: 30, M: 24, N: 14, O: 38, P: 54 });
repair.getRange(`G5:P${4 + repairRows.length}`).format.wrapText = true;
repair.freezePanes.freezeRows(4);

for (const sheet of [audit, evidence, candidates, repair]) {
  const used = sheet.getUsedRange();
  used.format.rowHeight = 18;
}
audit.getRange('J5:J249').conditionalFormats.add('containsText', { text: 'partial_support', format: { fill: amber } });
audit.getRange('J5:J249').conditionalFormats.add('containsText', { text: 'page_unavailable', format: { fill: red } });
audit.getRange('T5:T249').conditionalFormats.add('containsText', { text: 'TRUE', format: { fill: green } });
repair.getRange(`N5:N${4 + repairRows.length}`).conditionalFormats.add('containsText', { text: 'TRUE', format: { fill: green } });

workbook.recalculate();
const preview = await workbook.render({ sheetName: 'Overview', autoCrop: 'all', scale: 1, format: 'png' });
await fs.writeFile(PREVIEW, new Uint8Array(await preview.arrayBuffer()));
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(OUTPUT_XLSX);
console.log(JSON.stringify({ output: OUTPUT_XLSX, preview: PREVIEW, sheets: ['Overview', 'Membership_Audit', 'Benefit_Evidence', 'Page_Candidates', 'Repair_Dry_Run'], rows: { audit: auditRows.length, evidence: evidenceRows.length, candidates: candidateRows.length, repair: repairRows.length } }, null, 2));
