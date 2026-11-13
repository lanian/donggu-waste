"""배포에 포함된 XLSX/ODT 서식에 앱에서 계산한 자료를 채운다. Office/Node 실행은 필요 없다."""
from __future__ import annotations

import copy
import datetime as dt
from io import BytesIO
import math
import re
import unicodedata
from xml.etree import ElementTree as E
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

from dgpc.paths import resource_root

X = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
O = {'office': 'urn:oasis:names:tc:opendocument:xmlns:office:1.0', 'table': 'urn:oasis:names:tc:opendocument:xmlns:table:1.0',
     'text': 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'}


def _text(value):
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]', '', str(value))


def _xml(root):
    namespace = root.tag.split('}')[0].lstrip('{')
    opc = namespace in ('http://schemas.openxmlformats.org/package/2006/content-types', 'http://schemas.openxmlformats.org/package/2006/relationships')
    if opc:
        E.register_namespace('', namespace)
    result = E.tostring(root, encoding='utf-8', xml_declaration=True)
    if opc:
        E.register_namespace('', X)
    return result


def _parts(name):
    with ZipFile(resource_root() / 'data/report_templates' / name) as z:
        return {part: z.read(part) for part in z.namelist() if not part.endswith('/')}


def _package(parts, odt=False):
    out = BytesIO()
    with ZipFile(out, 'w', ZIP_DEFLATED) as z:
        if odt:
            z.writestr('mimetype', parts['mimetype'], compress_type=ZIP_STORED)
        for name, raw in parts.items():
            if not odt or name != 'mimetype':
                z.writestr(name, raw)
    return out.getvalue()


def _date_number(day):
    return (dt.date.fromisoformat(day) - dt.date(1899, 12, 30)).days


def _column_number(column):
    n = 0
    for char in column:
        n = n * 26 + ord(char) - 64
    return n


def _cell(row, column, value=None, *, style=None, formula=None, cached=None):
    ref = column + row.get('r')
    old = next((c for c in row if c.get('r') == ref), None)
    if style is None and old is not None:
        style = old.get('s')
    if old is not None:
        row.remove(old)
    attrs = {'r': ref}
    if style is not None:
        attrs['s'] = str(style)
    cell = E.Element(f'{{{X}}}c', attrs)
    if formula is not None:
        E.SubElement(cell, f'{{{X}}}f').text = formula
        E.SubElement(cell, f'{{{X}}}v').text = str(cached if cached is not None else 0)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        E.SubElement(cell, f'{{{X}}}v').text = str(value)
    elif value is not None:
        cell.set('t', 'inlineStr')
        string = E.SubElement(E.SubElement(cell, f'{{{X}}}is'), f'{{{X}}}t', {'{http://www.w3.org/XML/1998/namespace}space': 'preserve'})
        string.text = _text(value)
    row.append(cell)
    row[:] = sorted(row, key=lambda c: _column_number(re.match(r'[A-Z]+', c.get('r')).group()))
    return cell


def _row(sheet, number):
    data = sheet.find(f'{{{X}}}sheetData')
    row = next((r for r in data if int(r.get('r')) == number), None)
    if row is None:
        row = E.SubElement(data, f'{{{X}}}row', r=str(number))
        data[:] = sorted(data, key=lambda r: int(r.get('r')))
    return row


def _style_map(row):
    return {re.match(r'[A-Z]+', c.get('r')).group(): c.get('s') for c in row}


def _dimension(sheet, ref):
    element = sheet.find(f'{{{X}}}dimension')
    if element is None:
        element = E.Element(f'{{{X}}}dimension')
        sheet.insert(1 if sheet.find(f'{{{X}}}sheetPr') is not None else 0, element)
    element.set('ref', ref)


def _date_style(styles, original, code):
    formats = styles.find(f'{{{X}}}numFmts')
    if formats is None:
        formats = E.Element(f'{{{X}}}numFmts', count='0')
        styles.insert(0, formats)
    existing = next((f for f in formats if f.get('formatCode') == code), None)
    if existing is None:
        number = max([163] + [int(f.get('numFmtId')) for f in formats]) + 1
        existing = E.SubElement(formats, f'{{{X}}}numFmt', numFmtId=str(number), formatCode=code)
        formats.set('count', str(len(formats)))
    xfs = styles.find(f'{{{X}}}cellXfs')
    style = copy.deepcopy(xfs[int(original or 0)])
    style.set('numFmtId', existing.get('numFmtId'))
    style.set('applyNumberFormat', '1')
    xfs.append(style)
    xfs.set('count', str(len(xfs)))
    return len(xfs) - 1


def _display_rows(rows):
    """긴 주소·교부번호도 Excel 셀 길이/행 높이 한도에서 누락되지 않도록 이어 쓴다."""
    out = []
    limits = {'address': 150, 'name': 70, 'numbers': 100, 'note': 110}
    for original in rows:
        chunks = {key: [str(original.get(key) or '')[i:i + limit] for i in range(0, len(str(original.get(key) or '')), limit)] or [''] for key, limit in limits.items()}
        for index in range(max(len(values) for values in chunks.values())):
            if index == 0:
                row = dict(original)
            else:
                row = {**original, 'kind': 'CONTINUATION', 'receipt_no': '', 'phone': '', 'item': '계속', 'spec': '', 'category': '',
                       'price': None, 'qty': None, 'fee': None, 'received': 0, 'issued': 0}
            for key, values in chunks.items():
                row[key] = values[index] if index < len(values) else ''
            out.append(row)
    return out


def _height(values):
    widths = [5, 8, 12, 6, 7, 13, 14, 21, 7, 8, 13, 8, 9, 9, 10, 17]
    lines = 1
    for value, width in zip(values, widths):
        if value is None:
            continue
        units = sum(2 if unicodedata.east_asian_width(c) in ('F', 'W') else 1 for c in str(value))
        lines = max(lines, math.ceil(units / width))
    return min(400, max(20.1, lines * 12 + 5))


def _print_area(workbook, index, sheet_name, area, titles=None):
    names = workbook.find(f'{{{X}}}definedNames')
    if names is None:
        names = E.SubElement(workbook, f'{{{X}}}definedNames')
    for name in list(names):
        if name.get('localSheetId') == str(index) and name.get('name') in ('_xlnm.Print_Area', '_xlnm.Print_Titles'):
            names.remove(name)
    escaped = sheet_name.replace("'", "''")
    E.SubElement(names, f'{{{X}}}definedName', name='_xlnm.Print_Area', localSheetId=str(index)).text = f"'{escaped}'!{area}"
    if titles:
        E.SubElement(names, f'{{{X}}}definedName', name='_xlnm.Print_Titles', localSheetId=str(index)).text = f"'{escaped}'!{titles}"


def _fit_print(sheet, orientation):
    pr = sheet.find(f'{{{X}}}sheetPr')
    if pr is None:
        pr = E.Element(f'{{{X}}}sheetPr')
        sheet.insert(0, pr)
    fit = pr.find(f'{{{X}}}pageSetUpPr')
    if fit is None:
        fit = E.SubElement(pr, f'{{{X}}}pageSetUpPr')
    fit.set('fitToPage', '1')
    setup = sheet.find(f'{{{X}}}pageSetup')
    if setup is None:
        setup = E.Element(f'{{{X}}}pageSetup')
        following = {'headerFooter', 'rowBreaks', 'colBreaks', 'customProperties', 'cellWatches', 'ignoredErrors',
                     'smartTags', 'drawing', 'legacyDrawing', 'legacyDrawingHF', 'picture', 'oleObjects',
                     'controls', 'webPublishItems', 'tableParts', 'extLst'}
        index = next((i for i, child in enumerate(sheet) if child.tag.split('}')[-1] in following), len(sheet))
        sheet.insert(index, setup)
    setup.attrib.pop('scale', None)
    setup.attrib.pop(f'{{{R}}}id', None)
    setup.attrib.update(paperSize='9', orientation=orientation, fitToWidth='1', fitToHeight='0')


def monthly_xlsx(data):
    E.register_namespace('', X)
    E.register_namespace('r', R)
    parts = _parts('monthly.xlsx')
    sheet = E.fromstring(parts['xl/worksheets/sheet4.xml'])
    styles = E.fromstring(parts['xl/styles.xml'])
    base = _style_map(_row(sheet, 5))
    date_style = _date_style(styles, base.get('B'), 'm/d')
    body = sheet.find(f'{{{X}}}sheetData')
    for row in list(body):
        if int(row.get('r')) >= 5:
            body.remove(row)
    year, month = map(int, data['start'][:7].split('-'))
    _cell(_row(sheet, 1), 'A', f"{year}년 {month}월 대형폐기물 수거 월보 ({data['dong']})")
    _cell(_row(sheet, 2), 'R', data['totals']['opening'])
    display = _display_rows(data['rows'])
    if not display:
        display = [{'kind': 'EMPTY', 'date': data['start'], 'receipt_no': '', 'address': '', 'name': '', 'category': '', 'item': '', 'spec': '',
                    'price': None, 'qty': None, 'fee': None, 'received': 0, 'issued': 0, 'numbers': '', 'note': '', 'balance': data['totals']['opening']}]
    balance = data['totals']['opening']
    for number, entry in enumerate(display, 5):
        row = _row(sheet, number)
        is_collection = entry['kind'] == 'COLLECTION'
        values = [entry['receipt_no'], _date_number(entry['date']) if entry['kind'] not in ('CONTINUATION', 'EMPTY') else None, entry['address'], entry['name'],
                  entry['category'], entry['item'], entry['spec'], ' '.join(x for x in (entry['item'], entry['spec']) if x), entry['price'],
                  entry['qty'], entry['fee'], entry['received'], entry['issued'], None, entry['numbers'], entry['note']]
        if not is_collection:
            values[8] = None
        row.set('ht', str(_height(values)))
        row.set('customHeight', '1')
        for col, value in zip('ABCDEFGHIJKLMNOP', values):
            _cell(row, col, value, style=date_style if col == 'B' else base.get(col))
        balance += entry['received'] - entry['issued']
        _cell(row, 'N', style=base.get('N'), formula=f'$R$2+L{number}-M{number}' if number == 5 else f'N{number - 1}+L{number}-M{number}', cached=balance)
    end = len(display) + 4
    totals = data['totals']
    for col, key in [('J', 'qty'), ('K', 'fee'), ('L', 'received'), ('M', 'issued')]:
        _cell(_row(sheet, 4), col, formula=f'SUM({col}5:{col}{end})', cached=totals[key])
    _cell(_row(sheet, 4), 'N', formula='$R$2+L4-M4', cached=totals['closing'])
    _dimension(sheet, f'A1:R{max(5, end)}')
    af = sheet.find(f'{{{X}}}autoFilter')
    if af is not None:
        af.clear()
        af.set('ref', f'A4:P{end}')
    for validation in sheet.findall(f'{{{X}}}dataValidations/{{{X}}}dataValidation'):
        validation.set('sqref', f'E5:E{end}' if validation.get('sqref', '').startswith('E') else f'F5:G{end}')
    # The fee column is an app-calculated source value (including per-request discount rounding).
    # Group only the current collection rows, so old catalog spellings/prices cannot omit or double-count a fee.
    fee_sheet = E.fromstring(parts['xl/worksheets/sheet2.xml'])
    fee_base = _style_map(_row(fee_sheet, 2))
    fee_body = fee_sheet.find(f'{{{X}}}sheetData')
    for row in list(fee_body):
        if int(row.get('r')) >= 2:
            fee_body.remove(row)
    grouped = {}
    for entry in data['rows']:
        if entry['kind'] == 'COLLECTION':
            key = (entry['category'], entry['item'], entry['spec'], entry['price'])
            grouped[key] = grouped.get(key, 0) + entry['fee']
    groups = sorted(grouped.items(), key=lambda item: tuple(str(v) for v in item[0]))
    fee_amount_style = _date_style(styles, fee_base.get('E'), '#,##0')
    for index, (key, amount) in enumerate(groups, 2):
        category, name, spec, price = key
        row = _row(fee_sheet, index)
        for col, value in zip('ABCDEF', [category, name + spec, name, spec, price, amount]):
            _cell(row, col, value, style=fee_amount_style if col == 'F' else fee_base.get(col))
        row.set('ht', '30')
        row.set('customHeight', '1')
    fee_end = max(2, len(groups) + 1)
    _cell(_row(fee_sheet, 1), 'E', '단가(원)')
    _cell(_row(fee_sheet, 1), 'F', formula=f'SUM(F2:F{fee_end})', cached=totals['fee'])
    fee_header = next(c for c in _row(fee_sheet, 1) if c.get('r') == 'F1')
    fee_header.set('s', str(_date_style(styles, fee_header.get('s'), '"합계 "#,##0"원"')))
    for column in fee_sheet.findall(f'{{{X}}}cols/{{{X}}}col'):
        if int(column.get('min')) == 6:
            column.attrib.pop('hidden', None)
            column.set('width', '18')
        elif int(column.get('min')) == 2:
            column.set('hidden', '1')
    merges = fee_sheet.find(f'{{{X}}}mergeCells')
    if merges is not None:
        fee_sheet.remove(merges)
    _dimension(fee_sheet, f'A1:F{fee_end}')
    receipt_sheet = E.fromstring(parts['xl/worksheets/sheet3.xml'])
    receipt_base = _style_map(_row(receipt_sheet, 2))
    receipt_date_style = _date_style(styles, receipt_base.get('A'), 'yyyy-mm-dd')
    receipt_body = receipt_sheet.find(f'{{{X}}}sheetData')
    for row in list(receipt_body):
        if int(row.get('r')) >= 2:
            receipt_body.remove(row)
    index = 2
    from dgpc.official_forms import number_ranges
    for receipt in data['receipts']:
        for block in number_ranges(receipt['numbers']).split(', '):
            first, _, last = block.partition('-')
            last = last or first
            row = _row(receipt_sheet, index)
            for col, value in zip('ABCDE', [_date_number(receipt['date']), first, last, int(last) - int(first) + 1, '수령(' + block + ')']):
                _cell(row, col, value, style=receipt_date_style if col == 'A' else receipt_base.get(col))
            index += 1
    receipt_end = max(2, index - 1)
    _dimension(receipt_sheet, f'A1:E{receipt_end}')
    workbook = E.fromstring(parts['xl/workbook.xml'])
    month_name = str(month) + '월'
    workbook.find(f'{{{X}}}sheets')[3].set('name', month_name)
    for defined in workbook.findall(f'{{{X}}}definedNames/{{{X}}}definedName'):
        if defined.text:
            defined.text = defined.text.replace("'1월'!", f"'{month_name}'!")
    _print_area(workbook, 3, month_name, f'$A$1:$P${end}', '$1:$4')
    _print_area(workbook, 1, '수수료', f'$A$1:$F${fee_end}', '$1:$1')
    _print_area(workbook, 2, '배출표', f'$A$1:$E${receipt_end}', '$1:$1')
    _fit_print(sheet, 'landscape')
    _fit_print(fee_sheet, 'portrait')
    _fit_print(receipt_sheet, 'portrait')
    for view in workbook.findall(f'{{{X}}}bookViews/{{{X}}}workbookView'):
        view.set('activeTab', '3')
    calc = workbook.find(f'{{{X}}}calcPr')
    if calc is None:
        calc = E.SubElement(workbook, f'{{{X}}}calcPr')
    calc.attrib.update(calcMode='auto', fullCalcOnLoad='1', forceFullCalc='1')
    parts.update({'xl/workbook.xml': _xml(workbook), 'xl/styles.xml': _xml(styles), 'xl/worksheets/sheet4.xml': _xml(sheet),
                  'xl/worksheets/sheet2.xml': _xml(fee_sheet), 'xl/worksheets/sheet3.xml': _xml(receipt_sheet)})
    for part in list(parts):
        if part == 'xl/calcChain.xml':
            del parts[part]
        elif part.endswith('.rels') or part == '[Content_Types].xml':
            root = E.fromstring(parts[part])
            for child in list(root):
                if 'calcChain' in (child.get('Target', '') + child.get('PartName', '') + child.get('Type', '')):
                    root.remove(child)
            parts[part] = _xml(root)
    return _package(parts)


def _odt_cell(cell, value):
    para = cell.find('text:p', O)
    attrs = dict(para.attrib) if para is not None else {}
    span = para.find('text:span', O) if para is not None else None
    span_attrs = dict(span.attrib) if span is not None else {}
    for child in list(cell):
        cell.remove(child)
    cell.set(f'{{{O["office"]}}}value-type', 'float' if isinstance(value, int) else 'string')
    if isinstance(value, int):
        cell.set(f'{{{O["office"]}}}value', str(value))
        text = f'{value:,}'
    else:
        cell.attrib.pop(f'{{{O["office"]}}}value', None)
        text = '' if value is None else _text(value)
    for line in text.split('\n') or ['']:
        p = E.SubElement(cell, f'{{{O["text"]}}}p', attrs)
        if line:
            E.SubElement(p, f'{{{O["text"]}}}span', span_attrs).text = line


def daily_odt(data):
    parts = _parts('ledger.odt')
    for _, (prefix, uri) in E.iterparse(BytesIO(parts['content.xml']), events=['start-ns']):
        if not (prefix.startswith('ns') and prefix[2:].isdigit()):
            E.register_namespace(prefix, uri)
    root = E.fromstring(parts['content.xml'])
    style_ns = 'urn:oasis:names:tc:opendocument:xmlns:style:1.0'
    fo_ns = 'urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0'
    for properties in root.iter(f'{{{style_ns}}}table-row-properties'):
        properties.set(f'{{{fo_ns}}}keep-together', 'always')
    title, ledger = root.findall('office:body/office:text/table:table', O)
    date = dt.date.fromisoformat(data['end'])
    date_ko = f'{date.year}. {date.month}. {date.day}.'
    title_rows = title.findall('table:table-row', O)
    _odt_cell(list(title_rows[0])[0], f'대형폐기물 신고처리 및 배출표 수불대장({date.year % 100:02d}. {date.month}. {date.day}.)')
    _odt_cell(list(title_rows[0])[-1], data['dong'] + '장')
    _odt_cell(list(title_rows[3])[1], date_ko)
    body_rows = ledger.findall('table:table-row', O)
    blank = copy.deepcopy(body_rows[0])
    for row in body_rows:
        ledger.remove(row)
    display = _display_rows(data['rows'])
    for entry in display:
        row = copy.deepcopy(blank)
        is_collection = entry['kind'] == 'COLLECTION'
        description = ' '.join(x for x in (entry['item'], entry['spec'], entry['note']) if x)
        if entry['kind'] == 'VOID':
            number_text = '무효 ' + entry['numbers']
        else:
            number_text = entry['numbers']
        day = dt.date.fromisoformat(entry['date'])
        values = [entry['receipt_no'], f'{day.month}/{day.day}' if entry['kind'] != 'CONTINUATION' else '', entry['address'], entry['name'], entry['phone'],
                  description, entry['qty'] if is_collection else None, entry['fee'] if is_collection else None,
                  entry['received'] or None, entry['balance'], number_text]
        for cell, value in zip(list(row), values):
            _odt_cell(cell, value)
        ledger.append(row)
    total = copy.deepcopy(blank)
    for cell, value in zip(list(total), ['계', '', '', '', '', '합계', data['totals']['qty'], data['totals']['fee'], data['totals']['received'], data['totals']['closing'], '']):
        _odt_cell(cell, value)
    ledger.append(total)
    parts['content.xml'] = _xml(root)
    return _package(parts, odt=True)
