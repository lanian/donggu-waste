// Read-only Artifact Tool check of generated official workbooks.
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';
const args = Object.fromEntries(process.argv.slice(2).reduce((out, v, i, a) => { if (v.startsWith('--')) out.push([v.slice(2), a[i + 1]]); return out; }, []));
const loader = createRequire(path.join(path.resolve(args['artifact-workspace']), 'artifact-loader.cjs'));
const { FileBlob, SpreadsheetFile } = await import(pathToFileURL(loader.resolve('@oai/artifact-tool')).href);
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(args.xlsx));
workbook.recalculate();
const expected = JSON.parse(await fs.readFile(args.expected, 'utf8')).monthly;
const monthly = workbook.worksheets.getItemAt(3);
const actual = monthly.getRange('J4:N4').values[0];
assert.deepEqual(actual, [expected.qty, expected.fee, expected.received, expected.issued, expected.closing]);
assert.equal(workbook.worksheets.getItem('수수료').getRange('F1').values[0][0], expected.fee);
const opening = monthly.getRange('R2').values[0][0];
monthly.getRange('R2').values = [[opening + 7]];
workbook.recalculate();
assert.equal(monthly.getRange('N4').values[0][0], expected.closing + 7);
monthly.getRange('R2').values = [[opening]];
workbook.recalculate();
console.log(JSON.stringify({ sheet: monthly.name, totals: actual, feeTie: expected.fee, inputRecalculation: true }));
console.log((await workbook.inspect({ kind: 'match', searchTerm: '#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#CIRC!', options: { useRegex: true, maxResults: 20 }, maxChars: 2000 })).ndjson);
await fs.mkdir(args['png-dir'], { recursive: true });
for (const [sheetName, range, output] of [[monthly.name, 'A1:P10', 'monthly'], ['수수료', 'A1:F8', 'fees'], ['배출표', 'A1:E8', 'stock'], ['목록', 'A1:O9', 'catalog']]) {
  const image = await workbook.render({ sheetName, range, scale: 1.5, format: 'png' });
  await fs.writeFile(path.join(args['png-dir'], output + '.png'), new Uint8Array(await image.arrayBuffer()));
}
