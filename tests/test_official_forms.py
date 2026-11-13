"""실제 수거 기준 월보/수불대장 데이터와 원본 서식 파일 생성 회귀."""
import importlib.util
from io import BytesIO
import json
import unittest
from xml.etree import ElementTree as E
from zipfile import ZipFile

import test_label_auto as fixtures
from dgpc import core as C


class OfficialFormsTests(unittest.TestCase):
    request = fixtures.LabelAutoTests.request
    events = fixtures.LabelAutoTests.events
    collect = fixtures.LabelAutoTests.collect
    stock = fixtures.LabelAutoTests.stock

    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("dgpc.official_forms"), "원본 서식 자동 작성 기능이 필요합니다")
        from dgpc import official_forms
        self.F = official_forms
        fixtures.LabelAutoTests.setUp(self)

    def tearDown(self):
        fixtures.LabelAutoTests.tearDown(self)

    def monthly(self, month="2026-06", dong="충장동"):
        return self.F.monthly_data(self.con, self.dongs[dong], month)

    def test_actual_collection_months_and_carry_stock(self):
        self.stock(day="2026-05-31")
        r, line = self.request()
        self.collect(r, line, 2, "2026-06-10")
        self.collect(r, line, 3, "2026-07-01")
        june, july = self.monthly(), self.monthly("2026-07")
        self.assertEqual((june["totals"]["qty"], july["totals"]["qty"]), (2, 1))
        self.assertEqual((june["totals"]["opening"], june["totals"]["received"], june["totals"]["issued"], june["totals"]["closing"]), (5, 0, 2, 3))
        self.assertEqual((july["totals"]["opening"], july["totals"]["closing"]), (3, 2))
        rows = [x for x in june["rows"] if x["kind"] == "COLLECTION"]
        self.assertEqual((rows[0]["date"], rows[0]["qty"], rows[0]["numbers"]), ("2026-06-10", 2, "0001-0002"))

    def test_other_dong_is_never_included(self):
        self.stock("0001", "0005")
        self.stock("0010", "0015", dong="동명동")
        r, line = self.request(dong="동명동")
        self.collect(r, line, 3)
        self.assertEqual(self.monthly()["totals"]["qty"], 0)
        self.assertEqual(self.monthly(dong="동명동")["totals"]["qty"], 3)

    def test_discount_rounding_reconciles_over_visits(self):
        self.stock(day="2026-05-31")
        r, line = self.request()
        self.con.execute("UPDATE request_item SET unit_price=3333,amount=9999 WHERE line_id=?", (line,))
        self.con.execute("UPDATE request SET reduction_code='FIXTURE',reduction_rate=30,reduction_label='가상 감면',reduction_round_unit=10 WHERE id=?", (r["id"],))
        self.collect(r, line, 2, "2026-06-10")
        self.collect(r, line, 3, "2026-07-01")
        self.assertEqual(self.monthly()["totals"]["fee"], 4676)
        self.assertEqual(self.monthly("2026-07")["totals"]["fee"], 2333)

    def test_shortage_and_pending_price_are_not_silently_written_as_complete(self):
        r, line = self.request()
        self.collect(r, line, 3)
        missing = self.monthly()
        self.assertEqual(missing["totals"]["pending_labels"], 3)
        self.assertFalse(missing["ready"])
        with self.assertRaises(C.UserError):
            self.F.xlsx_file(missing)
        self.stock()
        self.con.execute("UPDATE request_item SET unit_price=NULL,price_status='PENDING' WHERE line_id=?", (line,))
        pending = self.monthly()
        self.assertEqual(pending["totals"]["pending_prices"], 1)
        self.assertFalse(pending["ready"])
        with self.assertRaises(C.UserError):
            self.F.odt_file(pending)

    def test_quantity_correction_keeps_voided_stock_visible_and_balanced(self):
        self.stock()
        r, line = self.request()
        self.collect(r, line, 3, "2026-06-10")
        self.collect(r, line, 1, "2026-06-20")
        report = self.monthly()
        self.assertEqual((report["totals"]["qty"], report["totals"]["issued"], report["totals"]["closing"]), (1, 3, 2))
        self.assertEqual(sum(x["issued"] for x in report["rows"] if x["kind"] == "VOID"), 2)
        self.assertTrue(any("무효" in x["item"] for x in report["rows"] if x["kind"] == "VOID"))

    def test_preview_has_no_resident_fields_and_export_is_read_only(self):
        self.stock()
        r, line = self.request()
        self.collect(r, line, 3)
        preview = self.F.preview(self.con, self.dongs["충장동"], "2026-06-01")
        self.assertEqual(preview["monthly"]["qty"], 3)
        self.assertNotIn("가상비공개", json.dumps(preview, ensure_ascii=False))
        before = {t: self.con.execute("SELECT * FROM " + t).fetchall() for t in ("numbered_label", "numbered_label_allocation", "numbered_label_event", "audit_log")}
        self.F.xlsx_file(self.monthly())
        self.F.odt_file(self.F.daily_data(self.con, self.dongs["충장동"], "2026-06-01"))
        self.assertEqual(before, {t: self.con.execute("SELECT * FROM " + t).fetchall() for t in before})

    def test_files_are_native_xlsx_and_odt_and_include_literal_identifiers(self):
        self.stock()
        r, line = self.request()
        self.con.execute("UPDATE request SET name=?,phone='01000001234',address='가상 동구 긴주소 <&> 1' WHERE id=?", ('=가상주민<&\ufffe\uffff\x01', r["id"]))
        self.collect(r, line, 3)
        report = self.monthly()
        with ZipFile(BytesIO(self.F.xlsx_file(report))) as z:
            self.assertIn('<Types xmlns=', z.read('[Content_Types].xml').decode())
            ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            fee_sheet = E.fromstring(z.read('xl/worksheets/sheet2.xml'))
            fee_column = next(c for c in fee_sheet.findall('s:cols/s:col', ns) if int(c.get('min')) <= 6 <= int(c.get('max')))
            self.assertNotEqual(fee_column.get('hidden'), '1', '품목별 수거분 수수료 합계가 보여야 합니다')
            doc = E.fromstring(z.read("xl/worksheets/sheet4.xml"))
            text = "".join(doc.itertext())
            self.assertIn("=가상주민<&", text)
            self.assertIn("0001-0003", text)
            self.assertNotIn("#REF!", text)
            self.assertTrue(any(c.get('t') == 'inlineStr' for c in doc.findall('.//s:c', ns) if '=가상주민' in ''.join(c.itertext())))
        with ZipFile(BytesIO(self.F.odt_file(report))) as z:
            self.assertEqual(z.namelist()[0], "mimetype")
            xml = E.fromstring(z.read("content.xml"))
            text = "".join(xml.itertext())
            for expected in ("충장동장", "2026. 6.", "=가상주민<&", "01000001234", "0001-0003"):
                self.assertIn(expected, text)

    def test_number_ranges_preserve_zeroes_gaps_and_thirty_digits(self):
        self.assertEqual(self.F.number_ranges(["0001", "0002", "0004"]), "0001-0002, 0004")
        self.assertEqual(self.F.number_ranges(["9" * 30]), "9" * 30)
        self.assertEqual(self.F.number_ranges(["0001", "2"]), "0001, 2")

    def test_print_settings_follow_worksheet_order_and_keep_ledger_rows_together(self):
        report = self.monthly()
        with ZipFile(BytesIO(self.F.xlsx_file(report))) as z:
            for index in (2, 3, 4):
                root = E.fromstring(z.read(f'xl/worksheets/sheet{index}.xml'))
                tags = [c.tag.split('}')[-1] for c in root]
                for later in ('headerFooter', 'rowBreaks', 'colBreaks', 'drawing', 'legacyDrawing', 'extLst'):
                    if later in tags:
                        self.assertLess(tags.index('pageSetup'), tags.index(later))
        with ZipFile(BytesIO(self.F.odt_file(report))) as z:
            root = E.fromstring(z.read('content.xml'))
            ns = {'s': 'urn:oasis:names:tc:opendocument:xmlns:style:1.0', 'fo': 'urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0'}
            for properties in root.findall('.//s:table-row-properties', ns):
                self.assertEqual(properties.get('{' + ns['fo'] + '}keep-together'), 'always')

    def test_invalid_period_and_dong_are_rejected(self):
        for dong, month in (("missing", "2026-06"), (self.dongs["충장동"], "2026-13"), (self.dongs["충장동"], "bad")):
            with self.assertRaises(C.UserError):
                self.F.monthly_data(self.con, dong, month)
        with self.assertRaises(C.UserError):
            self.F.daily_data(self.con, self.dongs["충장동"], "2026-02-31")

    def test_writers_extend_beyond_original_capacity_without_missing_last_row(self):
        self.stock()
        r, line = self.request(qty=1)
        self.collect(r, line, 1)
        report = self.monthly()
        entry = next(row for row in report['rows'] if row['kind'] == 'COLLECTION')
        # 렌더러 대량 행 fixture. 실제 수거/재고 계산은 다른 회귀가 원자료로 대조한다.
        report['rows'] = [{**entry, 'receipt_no': f'가상대량-{i:03d}', 'numbers': str(100000 + i), 'balance': 300 - i} for i in range(1, 301)]
        report['receipts'] = []
        report['totals'].update(qty=300, fee=entry['fee'] * 300, opening=300, received=0, issued=300, closing=0)
        ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        with ZipFile(BytesIO(self.F.xlsx_file(report))) as z:
            root = E.fromstring(z.read('xl/worksheets/sheet4.xml'))
            last = root.find("s:sheetData/s:row[@r='304']", ns)
            self.assertIsNotNone(last)
            self.assertIn('가상대량-300', ''.join(last.itertext()))
            self.assertEqual(last.find("s:c[@r='N304']/s:v", ns).text, '0')
            self.assertIn('$P$304', z.read('xl/workbook.xml').decode())
        with ZipFile(BytesIO(self.F.odt_file(report))) as z:
            root = E.fromstring(z.read('content.xml'))
            self.assertIn('가상대량-300', ''.join(root.itertext()))
            t = {'o': 'urn:oasis:names:tc:opendocument:xmlns:office:1.0', 't': 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'}
            ledger = root.findall('o:body/o:text/t:table', t)[1]
            self.assertEqual(len(ledger.findall('t:table-row', t)), 301)


if __name__ == "__main__":
    unittest.main()
