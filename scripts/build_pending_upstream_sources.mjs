import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const root = "/Users/wyl/BrandRap/costfinder";
const data = JSON.parse(await fs.readFile(`${root}/outputs/pending_upstream_sources_20260907.json`, "utf8"));
const outputDir = `${root}/outputs/pending_upstream_sources_20260907`;
await fs.mkdir(outputDir, { recursive: true });

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("待处理来源");
sheet.showGridLines = false;
sheet.tabColor = "#1F4E78";

sheet.getRange("A1:J1").merge();
sheet.getRange("A1").values = [["高置信上游来源待复核清单"]];
sheet.getRange("A2:J2").merge();
sheet.getRange("A2").values = [["当前无直接关联 Offer 的 53 个 Promotion / Membership 来源。正文保留原始内容，便于逐条判断是否应生成 Offer。"]];
sheet.getRange("A4:D4").values = [["来源总数", data.rows.length, "Promotion", data.rows.filter(r => r.source_type === "Promotion").length]];
sheet.getRange("E4:H4").values = [["Membership", data.rows.filter(r => r.source_type === "Membership").length, "导出日期", "2026-09-07"]];

const headers = ["来源类型", "来源ID", "商家ID", "Promotion标题 / Membership名称", "结束日期", "抽取标记", "当前Offer数", "来源URL", "原始正文 / Benefits", "复核状态"];
const rows = data.rows.map(r => [r.source_type, r.source_id, r.business_id, r.source_name, r.campaign_end_date || "", r.is_offer_extracted === true, r.current_offer_count, r.source_url, Array.isArray(r.source_text) ? r.source_text.join("\n") : String(r.source_text || ""), r.review_status]);
sheet.getRange(`A6:J${6 + rows.length}`).values = [headers, ...rows];
const table = sheet.tables.add(`A6:J${6 + rows.length}`, true, "PendingUpstreamSources");
table.style = "TableStyleMedium2";
table.showFilterButton = true;

sheet.getRange("A1:J1").format = { font: { name: "Arial", size: 14, bold: true, color: "#1F2937" } };
sheet.getRange("A2:J2").format = { font: { name: "Arial", size: 10, italic: true, color: "#4B5563" }, wrapText: false };
sheet.getRange("A4:H4").format = { font: { name: "Arial", size: 10, bold: true, color: "#1F2937" }, fill: "#E8F0F8" };
sheet.getRange("A6:J6").format = { font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" }, fill: "#1F4E78", horizontalAlignment: "center", verticalAlignment: "center", wrapText: true };
sheet.getRange(`A7:J${6 + rows.length}`).format = { font: { name: "Arial", size: 10, color: "#1F2937" }, verticalAlignment: "center", wrapText: true };
sheet.getRange(`B7:C${6 + rows.length}`).format.numberFormat = "0";
sheet.getRange(`G7:G${6 + rows.length}`).format.numberFormat = "0";
sheet.getRange(`I7:I${5 + rows.length}`).format.wrapText = true;
sheet.getRange(`A6:J${6 + rows.length}`).format.borders = { preset: "all", style: "thin", color: "#D9E2EC" };
sheet.getRange("A:A").format.columnWidth = 14;
sheet.getRange("B:C").format.columnWidth = 10;
sheet.getRange("D:D").format.columnWidth = 30;
sheet.getRange("E:E").format.columnWidth = 14;
sheet.getRange("F:G").format.columnWidth = 12;
sheet.getRange("H:H").format.columnWidth = 38;
sheet.getRange("I:I").format.columnWidth = 72;
sheet.getRange("J:J").format.columnWidth = 30;
sheet.getRange("A6:J6").format.rowHeight = 30;
sheet.getRange(`A7:J${6 + rows.length}`).format.rowHeight = 54;
sheet.freezePanes.freezeRows(6);

workbook.recalculate();
const check = await workbook.inspect({ kind: "table", sheetId: "待处理来源", range: `A1:J12`, include: "values,formulas", tableMaxRows: 12, tableMaxCols: 10, maxChars: 6000 });
console.log(check.ndjson);
const preview = await workbook.render({ sheetName: "待处理来源", range: "A1:J18", scale: 1, format: "png" });
await fs.writeFile(`${outputDir}/pending_upstream_sources_preview.png`, new Uint8Array(await preview.arrayBuffer()));
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(`${outputDir}/pending_upstream_sources_20260907.xlsx`);
console.log(`${outputDir}/pending_upstream_sources_20260907.xlsx`);
