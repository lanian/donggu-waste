"""실제 서명 결과 반영과 동별 번호 배정·권한을 함께 검사한다."""
import unittest

import test_label_usage_api as fixture
from dgpc import area, core as C, server as SV


class LabelAutoAPITests(fixture.UsageHTTPCase):
    def test_signed_import_automatically_assigns_dongs_stock_once(self):
        dong = area.get_dong(self.con, "충장동")
        status, received, _ = self.post("numbered-labels/receive", {"start": "0401", "end": "0403", "dong_id": dong["id"], "received_on": C.today_kst()}, self.admin_cookie)
        self.assertEqual(status, 200, received)
        self.assertEqual(C.one(self.con, "SELECT stock_dong_id FROM numbered_label WHERE number_key='401'")["stock_dong_id"], dong["id"])
        r, line = self.request()
        seal, event = self.phone(r)
        events = [event("ITEM_RESULT", {"line_id": line, "qty_confirmed": 3, "qty_collected": 2}), event("ATTEMPT", {"outcome": "PARTIAL"})]
        package = seal(events)
        for _ in range(2):
            status, imported, _ = self.post("import/result", package, self.admin_cookie)
            self.assertEqual(status, 200, imported)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM numbered_label_allocation WHERE voided_at IS NULL").fetchone()[0], 2)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM numbered_label_event WHERE action='USE'").fetchone()[0], 2)
        status, summary = self.get("label-auto?dong_id=" + dong["id"], self.admin_cookie)
        self.assertEqual(status, 200, summary)
        self.assertEqual((summary["required"], summary["assigned"], summary["pending"]), (2, 2, 0))

    def test_summary_and_assignment_routes_have_expected_permissions(self):
        self.assertEqual(self.get("label-auto", self.worker_cookie)[0], 200)
        for role in ("ADMIN", "INTAKE", "LEAD", "FIELD", "DONG"):
            self.assertEqual(SV.allowed(role, "GET", "label-auto"), role in ("ADMIN", "INTAKE", "LEAD"))
            for path in ("label-auto/sync", "numbered-labels/stock-dong"):
                self.assertEqual(SV.allowed(role, "POST", path), role in ("ADMIN", "INTAKE"))
        self.assertEqual(self.get("label-auto?dong_id=missing", self.admin_cookie)[0], 400)


if __name__ == "__main__":
    unittest.main()
