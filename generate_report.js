'use strict';

const fs = require('fs');
const {
  Document,
  Packer,
  Paragraph,
  TextRun,
  HeadingLevel,
  AlignmentType,
  BorderStyle,
  Table,
  TableRow,
  TableCell,
  WidthType,
  ShadingType,
} = require('docx');

// ── CLI args ──────────────────────────────────────────────────────────────────
const [, , dataPath, outPath] = process.argv;

if (!dataPath || !outPath) {
  console.error('Usage: node generate_report.js <data.json> <out.docx>');
  process.exit(1);
}

// ── Load data ─────────────────────────────────────────────────────────────────
let data;
try {
  data = JSON.parse(fs.readFileSync(dataPath, 'utf8'));
} catch (err) {
  console.error('Failed to parse input JSON:', err.message);
  process.exit(1);
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function h1(text) {
  return new Paragraph({
    text: String(text || ''),
    heading: HeadingLevel.HEADING_1,
    spacing: { before: 300, after: 100 },
  });
}

function h2(text) {
  return new Paragraph({
    text: String(text || ''),
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 200, after: 80 },
  });
}

function para(text) {
  return new Paragraph({
    children: [new TextRun(String(text || ''))],
    spacing: { after: 80 },
  });
}

function bullet(text) {
  return new Paragraph({
    text: String(text || ''),
    bullet: { level: 0 },
    spacing: { after: 60 },
  });
}

function divider() {
  return new Paragraph({
    border: { bottom: { color: 'CCCCCC', space: 1, style: BorderStyle.SINGLE, size: 6 } },
    spacing: { before: 200, after: 200 },
  });
}

function tableRow(cells, isHeader = false) {
  return new TableRow({
    tableHeader: isHeader,
    children: cells.map((text) =>
      new TableCell({
        shading: isHeader ? { type: ShadingType.CLEAR, fill: '1F3864' } : undefined,
        children: [
          new Paragraph({
            children: [
              new TextRun({
                text: String(text || ''),
                bold: isHeader,
                color: isHeader ? 'FFFFFF' : '000000',
                size: 18,
              }),
            ],
          }),
        ],
      })
    ),
  });
}

// ── Build document sections ───────────────────────────────────────────────────
const children = [];

// Title block
children.push(
  new Paragraph({
    children: [new TextRun({ text: String(data.reportTitle || 'Compliance Report'), bold: true, size: 48 })],
    alignment: AlignmentType.CENTER,
    spacing: { after: 200 },
  }),
  new Paragraph({
    children: [new TextRun({ text: String(data.companyName || ''), size: 24, color: '444444' })],
    alignment: AlignmentType.CENTER,
  }),
  new Paragraph({
    children: [new TextRun({ text: `Period: ${data.reportingPeriod || ''}`, size: 22, color: '666666' })],
    alignment: AlignmentType.CENTER,
  }),
  new Paragraph({
    children: [new TextRun({ text: `Submitted: ${data.submissionDate || ''}  |  Prepared by: ${data.preparedBy || ''} (${data.preparedByRole || ''})  |  ${data.version || ''}`, size: 20, color: '888888' })],
    alignment: AlignmentType.CENTER,
    spacing: { after: 400 },
  }),
  divider()
);

// 1. Purpose & Scope
children.push(h1('1. Purpose & Scope'), para(data.purpose || ''));
if ((data.scopeItems || []).length) {
  children.push(para('Scope includes:'));
  (data.scopeItems || []).forEach((s) => children.push(bullet(s)));
}
if ((data.objectives || []).length) {
  children.push(para('Objectives:'));
  (data.objectives || []).forEach((o) => children.push(bullet(o)));
}
children.push(divider());

// 2. Methodology
children.push(
  h1('2. Methodology'),
  para(`Timeframe: ${data.timeframe || ''}`),
  para(`Sampling: ${data.sampling || ''}`),
  para('Data Sources:')
);
(data.dataSources || []).forEach((d) => children.push(bullet(d)));
children.push(para('Tools Used:'));
(data.toolsUsed || []).forEach((t) => children.push(bullet(t)));
children.push(divider());

// 3. Key Findings
children.push(h1('3. Key Findings'));
(data.keyFindings || []).forEach((f) => children.push(bullet(f)));
children.push(divider());

// 4. Overall Status
children.push(
  h1('4. Overall Compliance Status'),
  para(`Status: ${data.overallStatus || ''}`),
  para(`Audit Trail Integrity: ${data.integrity || ''}`)
);
children.push(divider());

// 5. Compliance Detail
children.push(h1('5. Compliance Detail'));

if ((data.compliantAreas || []).length) {
  children.push(h2('Compliant Areas'));
  (data.compliantAreas || []).forEach((a) => children.push(para(`✓  ${a.area}: ${a.evidence}`)));
}

if ((data.nonCompliantAreas || []).length) {
  children.push(h2('Non-Compliant Areas'));
  (data.nonCompliantAreas || []).forEach((a) =>
    children.push(para(`✗  ${a.area} (${a.regulation}): ${a.evidence}  [Risk: ${a.riskLevel}]`))
  );
}

if ((data.partialAreas || []).length) {
  children.push(h2('Partially Compliant Areas'));
  (data.partialAreas || []).forEach((a) => children.push(para(`~  ${a.area}: ${a.detail}`)));
}
children.push(divider());

// 6. Regulatory Mapping table
if ((data.regulatoryMap || []).length) {
  children.push(h1('6. Regulatory Mapping'));
  const regTable = new Table({
    width: { size: 100, type: WidthType.PERCENTAGE },
    rows: [
      tableRow(['Regulation', 'Requirement', 'Control', 'Status'], true),
      ...(data.regulatoryMap || []).map((r) =>
        tableRow([r.regulation, r.requirement, r.control, r.status])
      ),
    ],
  });
  children.push(regTable, para(''));
  children.push(divider());
}

// 7. Risk Register
children.push(h1('7. Risk Register'), para(data.riskMethodology || ''));
if ((data.risks || []).length) {
  const riskTable = new Table({
    width: { size: 100, type: WidthType.PERCENTAGE },
    rows: [
      tableRow(['Risk', 'Impact', 'Likelihood', 'Rating', 'Owner'], true),
      ...(data.risks || []).map((r) =>
        tableRow([r.risk, r.impact, r.likelihood, r.rating, r.owner])
      ),
    ],
  });
  children.push(riskTable, para(''));
}
children.push(divider());

// 8. Recommendations
if ((data.recommendations || []).length) {
  children.push(h1('8. Recommendations'));
  const recTable = new Table({
    width: { size: 100, type: WidthType.PERCENTAGE },
    rows: [
      tableRow(['Issue', 'Action', 'Priority', 'Responsible', 'Deadline'], true),
      ...(data.recommendations || []).map((r) =>
        tableRow([r.issue, r.action, r.priority, r.responsible, r.deadline])
      ),
    ],
  });
  children.push(recTable, para(''));
  children.push(divider());
}

// 9. Statistics
children.push(
  h1('9. Statistics'),
  para(`Total Documents Processed: ${data.stats?.totalDocuments ?? ''}`),
  para(`Verified Documents: ${data.stats?.verifiedDocuments ?? ''}`),
  para(`Total Compliance Decisions: ${data.stats?.totalDecisions ?? ''}`),
  para(`Average Extraction Confidence: ${((data.stats?.avgConfidence || 0) * 100).toFixed(1)}%`),
  para(`Low Confidence Flags (<75%): ${data.stats?.lowConfidenceFlags ?? ''}`),
  divider()
);

// 10. Major Risks & Actions
if ((data.majorRisks || []).length) {
  children.push(h1('10. Major Risks'));
  (data.majorRisks || []).forEach((r) => children.push(bullet(r)));
}
if ((data.actionsRequired || []).length) {
  children.push(h2('Actions Required'));
  (data.actionsRequired || []).forEach((a) => children.push(bullet(a)));
}
children.push(divider());

// 11. Conclusion
children.push(
  h1('11. Conclusion'),
  para(data.conclusion || ''),
  para(data.regulatoryReadiness || ''),
  divider()
);

// 12. Next Steps
if ((data.nextSteps || []).length) {
  children.push(h1('12. Next Steps'));
  (data.nextSteps || []).forEach((s) => children.push(bullet(s)));
  children.push(divider());
}

// 13. Limitations & Exclusions
if ((data.limitations || []).length || (data.exclusions || []).length) {
  children.push(h1('13. Limitations & Exclusions'));
  (data.limitations || []).forEach((l) => children.push(bullet(`Limitation: ${l}`)));
  (data.exclusions || []).forEach((e) => children.push(bullet(`Exclusion: ${e}`)));
  children.push(divider());
}

// 14. Glossary
if ((data.glossary || []).length) {
  children.push(h1('14. Glossary'));
  (data.glossary || []).forEach((g) =>
    children.push(para(`${g.term}: ${g.definition}`))
  );
  children.push(divider());
}

// Appendix
children.push(h1('Appendix'), para(data.appendixNote || ''));

// ── Assemble & write ──────────────────────────────────────────────────────────
const doc = new Document({
  styles: {
    paragraphStyles: [
      {
        id: 'Normal',
        name: 'Normal',
        run: { font: 'Calibri', size: 22 },
      },
    ],
  },
  sections: [
    {
      properties: {},
      children,
    },
  ],
});

Packer.toBuffer(doc)
  .then((buf) => {
    fs.writeFileSync(outPath, buf);
    console.log('Report written to', outPath);
  })
  .catch((err) => {
    console.error('Failed to generate report:', err.message);
    process.exit(1);
  });
