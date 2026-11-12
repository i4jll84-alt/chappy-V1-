// Build a local panel from an explicitly frozen study list; only price-data add-in functions.
import fs from "node:fs/promises";
import path from "node:path";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const [panelPath, output, flag, packages] = process.argv.slice(2);
const requireFrom = packages && flag === "--runtime-packages"
  ? createRequire(path.join(packages, "..", "package.json")) : createRequire(import.meta.url);
const { Workbook, SpreadsheetFile } = await import(pathToFileURL(requireFrom.resolve("@oai/artifact-tool")).href);
const JSZip = requireFrom("jszip");
const panel = JSON.parse(await fs.readFile(panelPath, "utf8"));
const rawCodes = [panel.control, ...(panel.selected ?? panel.sample).map(row => row.code)];
if (rawCodes.length !== 31 || new Set(rawCodes).size !== 31 ||
    !rawCodes.every(code => /^[0-9][A-Z0-9]{3}0$/.test(code))) throw Error("invalid study panel");
const symbols = rawCodes.map(code => code.slice(0, 4) + ".T");
const quotes = ["現在日付","現在値","現在値詳細時刻","最良買気配値","最良売気配値","最良買気配数量","最良売気配数量","最良買気配詳細時刻","最良売気配詳細時刻","出来高"];
const bars = ["日付","時刻","始値","高値","安値","終値","出来高"];
const book = Workbook.create();
const sheet = book.worksheets.add("気配");
sheet.getRange("A1:K1").values = [["銘柄", ...quotes]];
sheet.getRange("A2:A32").values = symbols.map(s => [s]);
sheet.getRange("B2:K32").formulas = symbols.map(s => quotes.map(item => '=_xll.RssMarket("'+s+'","'+item+'")'));
function header(range) {
  range.format.fill = "#175459";
  range.format.font.color = "#FFFFFF";
  range.format.font.bold = true;
  range.format.columnWidth = 17;
}
header(sheet.getRange("A1:K1"));
const indexFields=["現在日付","更新時刻","現在値"];
sheet.getRange("A35:D35").values=[["市場指標",...indexFields]];
sheet.getRange("A36:A37").values=[["N225"],["TOPX"]];
sheet.getRange("B36:D37").formulas=["N225","TOPX"].map(code =>
  indexFields.map(field=>'=_xll.RssIndexMarket("'+code+'","'+field+'")'));
header(sheet.getRange("A35:D35"));
for (const symbol of symbols) {
  const ws = book.worksheets.add(symbol);
  ws.getRange("A2:G2").values = [bars];
  header(ws.getRange("A2:G2"));
  ws.getRange("A1").formulas = [['=_xll.RssChart(A2:G2,"'+symbol+'","1M",400)']];
}
await fs.mkdir(output,{recursive:true});
const bookName=panel.target_session ? `研究用分足-${panel.target_session}.xlsx` : "研究用分足.xlsx";
if (panel.target_session && !/^\d{4}-\d{2}-\d{2}$/.test(panel.target_session)) throw Error("invalid session");
const target=path.join(output,bookName);
const xlsx=await SpreadsheetFile.exportXlsx(book);
await xlsx.save(target);
// Only Excel with the broker's XLL can calculate these external functions.
// Drop the offline engine's error caches and request recalculation on open.
const zip=await JSZip.loadAsync(await fs.readFile(target));
let cleared=0;
for (const file of Object.keys(zip.files).filter(n=>/^xl\/worksheets\/sheet\d+\.xml$/.test(n))) {
  const xml=await zip.file(file).async("string");
  zip.file(file,xml.replace(/(<x:c\b[^<>]*?) t="e"([^<>]*><x:f>_xll\.Rss(?:Market|Chart|IndexMarket)\([^<>]*<\/x:f>)<x:v>#NAME\?<\/x:v>/g,
    (_,a,b)=>{cleared++;return a+b;}));
}
if(cleared!==347) throw Error("Unexpected external formula caches");
const wx=await zip.file("xl/workbook.xml").async("string");
zip.file("xl/workbook.xml",wx.replace("</x:workbook>",'<x:calcPr calcId="0" calcMode="auto" fullCalcOnLoad="1"/></x:workbook>'));
await fs.writeFile(target,await zip.generateAsync({type:"nodebuffer",compression:"DEFLATE"}));
await fs.writeFile(path.join(output,"panel.json"),JSON.stringify({
  version:1, symbols, ...(panel.target_session ? {session:panel.target_session} : {}),
  workbook_sha256:createHash("sha256").update(await fs.readFile(target)).digest("hex")
},null,2));
