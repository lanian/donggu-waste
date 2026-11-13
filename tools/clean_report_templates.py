"""원본의 예시 자료·프린터 정보를 제거하고 기존 모양을 보존하는 개발용 서식 준비."""
from __future__ import annotations

import argparse
import copy
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as E
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

X = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
O = {'office': 'urn:oasis:names:tc:opendocument:xmlns:office:1.0', 'table': 'urn:oasis:names:tc:opendocument:xmlns:table:1.0',
     'text': 'urn:oasis:names:tc:opendocument:xmlns:text:1.0', 'style': 'urn:oasis:names:tc:opendocument:xmlns:style:1.0',
     'fo': 'urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0'}


def xml(root):
    namespace = root.tag.split('}')[0].lstrip('{')
    opc = namespace in ('http://schemas.openxmlformats.org/package/2006/content-types', 'http://schemas.openxmlformats.org/package/2006/relationships')
    if opc:
        E.register_namespace('', namespace)
    result = E.tostring(root, encoding='utf-8', xml_declaration=True)
    if opc:
        E.register_namespace('', X)
    return result


def register(raw):
    for _, (prefix, uri) in E.iterparse(BytesIO(raw), events=['start-ns']):
        if not (prefix.startswith('ns') and prefix[2:].isdigit()):
            E.register_namespace(prefix, uri)


def clean_xlsx(source, target):
    with ZipFile(source) as z:
        parts = {p: z.read(p) for p in z.namelist() if not p.endswith('/') and 'printerSettings' not in p}
    E.register_namespace('', X)
    E.register_namespace('r', R)
    sheets = {p: E.fromstring(raw) for p, raw in parts.items() if p.startswith('xl/worksheets/') and p.endswith('.xml')}
    monthly = sheets['xl/worksheets/sheet4.xml']
    for row in monthly.findall(f'{{{X}}}sheetData/{{{X}}}row'):
        if int(row.get('r')) >= 5:
            for key in ('hidden', 'outlineLevel', 'collapsed'):
                row.attrib.pop(key, None)
            for cell in row:
                for child in list(cell):
                    if child.tag in (f'{{{X}}}v', f'{{{X}}}is', f'{{{X}}}f'):
                        cell.remove(child)
                cell.attrib.pop('t', None)
    af = monthly.find(f'{{{X}}}autoFilter')
    if af is not None:
        af.clear()
        af.set('ref', 'A4:P234')
    setup = monthly.find(f'{{{X}}}pageSetup')
    if setup is None:
        setup = E.SubElement(monthly, f'{{{X}}}pageSetup')
    setup.attrib.pop('scale', None)
    setup.attrib.pop(f'{{{R}}}id', None)
    setup.attrib.update(paperSize='9', orientation='landscape', fitToWidth='1', fitToHeight='0')
    pr = monthly.find(f'{{{X}}}sheetPr')
    if pr is None:
        pr = E.Element(f'{{{X}}}sheetPr')
        monthly.insert(0, pr)
    fit = pr.find(f'{{{X}}}pageSetUpPr')
    if fit is None:
        fit = E.SubElement(pr, f'{{{X}}}pageSetUpPr')
    fit.set('fitToPage', '1')
    # Clear unused shared strings as well: merely clearing cells must not retain sample resident data.
    ss_part = 'xl/sharedStrings.xml'
    if ss_part in parts:
        ss = E.fromstring(parts[ss_part])
        refs = [c.find(f'{{{X}}}v') for sheet in sheets.values() for c in sheet.iter(f'{{{X}}}c') if c.get('t') == 's']
        refs = [v for v in refs if v is not None and v.text is not None]
        used = sorted({int(v.text) for v in refs})
        remap = {old: new for new, old in enumerate(used)}
        entries = list(ss)
        for entry in entries:
            ss.remove(entry)
        for index in used:
            ss.append(entries[index])
        ss.set('count', str(len(refs)))
        ss.set('uniqueCount', str(len(used)))
        for value in refs:
            value.text = str(remap[int(value.text)])
        parts[ss_part] = xml(ss)
    wb = E.fromstring(parts['xl/workbook.xml'])
    names = wb.find(f'{{{X}}}definedNames')
    if names is None:
        names = E.SubElement(wb, f'{{{X}}}definedNames')
    for name in list(names):
        if name.get('localSheetId') == '3' and name.get('name') in ('_xlnm.Print_Area', '_xlnm.Print_Titles'):
            names.remove(name)
    E.SubElement(names, f'{{{X}}}definedName', name='_xlnm.Print_Area', localSheetId='3').text = "'1월'!$A$1:$P$234"
    E.SubElement(names, f'{{{X}}}definedName', name='_xlnm.Print_Titles', localSheetId='3').text = "'1월'!$1:$4"
    parts['xl/workbook.xml'] = xml(wb)
    for p, sheet in sheets.items():
        for setup in sheet.iter(f'{{{X}}}pageSetup'):
            setup.attrib.pop(f'{{{R}}}id', None)
        parts[p] = xml(sheet)
    for p in list(parts):
        if p.endswith('.rels') or p == '[Content_Types].xml':
            tree = E.fromstring(parts[p])
            for child in list(tree):
                if 'printerSettings' in (child.get('Target', '') + child.get('PartName', '') + child.get('Type', '')):
                    tree.remove(child)
            parts[p] = xml(tree)
        elif p == 'docProps/core.xml':
            tree = E.fromstring(parts[p])
            for child in list(tree):
                tree.remove(child)
            parts[p] = xml(tree)
    with ZipFile(target, 'w', ZIP_DEFLATED) as z:
        for name, raw in parts.items():
            z.writestr(name, raw)


def set_cell(cell, text=''):
    p = cell.find(f'{{{O["text"]}}}p')
    attrs = dict(p.attrib) if p is not None else {}
    span = p.find(f'{{{O["text"]}}}span') if p is not None else None
    span_attrs = dict(span.attrib) if span is not None else {}
    for child in list(cell):
        cell.remove(child)
    for attr in list(cell.attrib):
        if attr.startswith('{' + O['office'] + '}') or attr.endswith('}formula'):
            del cell.attrib[attr]
    p = E.SubElement(cell, f'{{{O["text"]}}}p', attrs)
    if text:
        E.SubElement(p, f'{{{O["text"]}}}span', span_attrs).text = text


def clean_odt(source, target):
    with ZipFile(source) as z:
        parts = {p: z.read(p) for p in z.namelist() if not p.endswith('/') and not p.startswith('Thumbnails/') and p != 'settings.xml'}
    register(parts['content.xml'])
    root = E.fromstring(parts['content.xml'])
    body = root.find('office:body/office:text', O)
    tables = []
    for child in list(body):
        table = child.find('.//table:table', O)
        if table is None:
            continue
        table = copy.deepcopy(table)
        body.insert(list(body).index(child), table)
        body.remove(child)
        tables.append(table)
    title, ledger = tables
    rows = title.findall('table:table-row', O)
    set_cell(list(rows[0])[0], '대형폐기물 신고처리 및 배출표 수불대장')
    set_cell(list(rows[0])[-1], '동장')
    set_cell(list(rows[1])[-1])
    set_cell(list(rows[3])[1])
    rows = ledger.findall('table:table-row', O)
    set_cell(list(rows[0])[1], '수거 월일')
    set_cell(list(rows[0])[5], '수거분 수수료')
    header = E.Element(f'{{{O["table"]}}}table-header-rows')
    position = list(ledger).index(rows[0])
    for row in rows[:2]:
        ledger.remove(row)
        header.append(row)
    ledger.insert(position, header)
    template_row = copy.deepcopy(rows[2])
    for cell in template_row:
        set_cell(cell)
    for row in rows[2:]:
        ledger.remove(row)
    for _ in range(21):
        ledger.append(copy.deepcopy(template_row))
    parts['content.xml'] = xml(root)
    manifest = E.fromstring(parts['META-INF/manifest.xml'])
    for entry in list(manifest):
        if any(value.startswith('Thumbnails/') or value == 'settings.xml' for value in entry.attrib.values()):
            manifest.remove(entry)
    parts['META-INF/manifest.xml'] = xml(manifest)
    if 'meta.xml' in parts:
        meta = E.fromstring(parts['meta.xml'])
        for child in list(meta):
            for entry in list(child):
                child.remove(entry)
        parts['meta.xml'] = xml(meta)
    with ZipFile(target, 'w', ZIP_DEFLATED) as z:
        z.writestr('mimetype', parts.pop('mimetype'), compress_type=ZIP_STORED)
        for name, raw in parts.items():
            z.writestr(name, raw)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xlsx', required=True, type=Path)
    parser.add_argument('--odt', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    clean_xlsx(args.xlsx, args.output_dir / 'monthly.xlsx')
    clean_odt(args.odt, args.output_dir / 'ledger.odt')
