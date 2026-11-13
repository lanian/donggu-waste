// Developer-only template preparation. Runtime exports do not require Node or Artifact Tool.
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { execFileSync } from 'node:child_process';

const args = Object.fromEntries(process.argv.slice(2).reduce((pairs, item, i, all) => {
  if (item.startsWith('--')) pairs.push([item.slice(2), all[i + 1]]);
  return pairs;
}, []));
for (const key of ['xlsx', 'odt', 'artifact-workspace', 'output-dir', 'python']) {
  if (!args[key]) throw new Error(`Missing --${key}`);
}
const loader = createRequire(path.join(path.resolve(args['artifact-workspace']), 'artifact-loader.cjs'));
const { FileBlob, SpreadsheetFile } = await import(pathToFileURL(loader.resolve('@oai/artifact-tool')).href);
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(args.xlsx));
const monthly = workbook.worksheets.getItem('1월');
monthly.getRange('A5:S234').clear({ applyTo: 'contents' });
monthly.getRange('A1').values = [['대형폐기물 수거 월보']];
monthly.getRange('B2').values = [['수거 월일']];
monthly.getRange('H2').values = [['수거분 수수료']];
monthly.getRange('R1:R2').values = [['기초 재고(장)'], [0]];
for (const column of ['J', 'K', 'L', 'M']) monthly.getRange(column + '4').formulas = [[`=SUM(${column}5:${column}234)`]];
monthly.getRange('N4').formulas = [['=R2+L4-M4']];
monthly.getRange('A5:P234').format.wrapText = true;
monthly.getRange('A5:P234').format.verticalAlignment = 'top';
monthly.freezePanes.freezeRows(4);
const fees = workbook.worksheets.getItem('수수료');
const feeRows = fees.getRange('B2:F176').values;
fees.getRange('F2:F176').formulas = feeRows.map((row, index) => [row[1] && typeof row[3] === 'number'
  ? `=SUMIF('1월'!$H$5:$H$234,B${index + 2},'1월'!$K$5:$K$234)` : null]);
fees.getRange('F1').formulas = [['=SUM(F2:F176)']];
workbook.worksheets.getItem('배출표').getRange('A2:E18').clear({ applyTo: 'contents' });
workbook.recalculate();
const errorScan = await workbook.inspect({ kind: 'match', searchTerm: '#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!|#CIRC!',
  options: { useRegex: true, maxResults: 20 }, summary: 'clean template formula scan', maxChars: 1800 });
console.log(errorScan.ndjson);
await fs.mkdir(args['output-dir'], { recursive: true });
const intermediate = path.join(args['artifact-workspace'], 'monthly-artifact.xlsx');
await (await SpreadsheetFile.exportXlsx(workbook)).save(intermediate);
execFileSync(args.python, [new URL('./clean_report_templates.py', import.meta.url).pathname.replace(/^\/(\w:)/, '$1'),
  '--xlsx', intermediate, '--odt', args.odt, '--output-dir', args['output-dir']], { stdio: 'inherit' });
console.log('Clean monthly.xlsx and ledger.odt prepared');
