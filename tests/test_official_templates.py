"""배포용 원본 서식은 예시 주민/번호 없이 독립적으로 열 수 있어야 한다."""
from pathlib import Path
import unittest
from xml.etree import ElementTree as E
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "data/report_templates"
S = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
O = {"o": "urn:oasis:names:tc:opendocument:xmlns:office:1.0", "t": "urn:oasis:names:tc:opendocument:xmlns:table:1.0", "p": "urn:oasis:names:tc:opendocument:xmlns:text:1.0"}


class OfficialTemplateTests(unittest.TestCase):
    def test_xlsx_keeps_four_sheets_but_removes_sample_rows_and_broken_references(self):
        path = TEMPLATES / "monthly.xlsx"
        self.assertTrue(path.is_file(), "원본 모양을 유지한 빈 월보 서식이 필요합니다")
        with ZipFile(path) as z:
            self.assertIn('<Types xmlns=', z.read('[Content_Types].xml').decode(), 'Office OPC content-types must use the default namespace')
            wb = E.fromstring(z.read("xl/workbook.xml"))
            self.assertEqual([s.get("name") for s in wb.findall("s:sheets/s:sheet", S)], ["목록", "수수료", "배출표", "1월"])
            for part in z.namelist():
                if part.startswith("xl/worksheets/") and part.endswith(".xml"):
                    self.assertNotIn(b"#REF!", z.read(part))
            sheet = E.fromstring(z.read("xl/worksheets/sheet4.xml"))
            for row in sheet.findall("s:sheetData/s:row", S):
                if int(row.get("r")) < 5:
                    continue
                self.assertNotEqual(row.get("hidden"), "1")
                for cell in row.findall("s:c", S):
                    self.assertIsNone(cell.find("s:v", S), cell.get("r"))
                    self.assertIsNone(cell.find("s:is", S), cell.get("r"))
            texts = "".join(z.read(p).decode("utf-8", "ignore") for p in z.namelist() if p.endswith(".xml"))
            self.assertNotIn("관리실", texts)
            self.assertNotIn("홍길동", texts)
            self.assertNotIn("printerSettings", " ".join(z.namelist()))

    def test_odt_has_flowing_ledger_and_no_sample_residents_or_numbers(self):
        path = TEMPLATES / "ledger.odt"
        self.assertTrue(path.is_file(), "기존 결재란과 표 모양을 유지한 빈 일일 대장이 필요합니다")
        with ZipFile(path) as z:
            self.assertEqual(z.namelist()[0], "mimetype")
            root = E.fromstring(z.read("content.xml"))
            body = root.find("o:body/o:text", O)
            tables = body.findall("t:table", O)
            self.assertEqual(len(tables), 2)
            ledger = tables[1]
            self.assertEqual(len(ledger.findall("t:table-header-rows/t:table-row", O)), 2)
            self.assertEqual(len(ledger.findall("t:table-column", O)), 11)
            for row in ledger.findall("t:table-row", O):
                self.assertFalse("".join(row.itertext()).strip())
            text = "".join(root.itertext())
            for sample in ("홍길동", "010-0000-0000", "128882", "2025. 6. 30.", "산수1동장"):
                self.assertNotIn(sample, text)


if __name__ == "__main__":
    unittest.main()
