"""새 보고서·배출표 API의 실제 SQLCipher/HTTP·권한·인쇄·멱등 회귀."""
import http.cookiejar
import json
from pathlib import Path
import sys
import unittest
import urllib.error
import urllib.request

from test_review_ui import pc_server

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))
from dgpc import server


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.fixture = pc_server()
        self.url = self.fixture.__enter__()
        self.client = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.api("setup", {"username": "report_admin", "display_name": "가상 보고담당", "password": "fixture-report-password-123"})
        fees = json.loads((ROOT / "data/fee_table.json").read_text(encoding="utf-8"))
        fee = next(row["id"] for row in fees["rows"] if row["status"] == "CONFIRMED" and row.get("price", 0) > 0)
        self.request_id = self.api("requests", {"name": "가상비공개주민", "phone": "01000009987", "address": "가상비공개주소",
            "planned_date": "2099-01-02", "items": [{"fee_rate_id": fee, "qty": 1}]})["id"]

    def tearDown(self):
        self.fixture.__exit__(None, None, None)

    def api(self, path, body=None, status=200, raw=False):
        request = urllib.request.Request(self.url + "api/" + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"X-DG-CSRF": "1", "Content-Type": "application/json"})
        try:
            response = self.client.open(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            value = response.read()
            self.assertEqual(response.status, status, value.decode("utf-8", errors="replace"))
        return value if raw else json.loads(value)

    def document(self, token):
        with self.client.open(self.url + "print/doc/" + token, timeout=10) as response:
            self.assertEqual(response.status, 200)
            return response.read().decode("utf-8")

    def test_reports_and_print(self):
        for kind, query, body in (("daily", "date=2099-01-02", {"date": "2099-01-02"}),
                                  ("monthly", "month=2099-01", {"month": "2099-01"})):
            result = self.api(f"reports/{kind}?{query}&basis=planned")
            self.assertEqual(result["kind"], kind)
            self.assertEqual(result["totals"]["requests"], 1)
            self.assertIn("누적", result["source_note"])
            csv = self.api(f"reports/{kind}.csv?{query}&basis=planned", raw=True).decode("utf-8-sig")
            token = self.api("reports/print", {"kind": kind, "basis": "planned", **body})["token"]
            html = self.document(token)
            for private in ("가상비공개주민", "01000009987", "가상비공개주소"):
                self.assertNotIn(private, json.dumps(result, ensure_ascii=False) + csv + html)
            self.assertIn("2099-01", html)

    def test_inventory_lifecycle_and_print_does_not_use(self):
        body = {"start": "000001", "end": "000003", "client_req_id": "fixture-receive-one"}
        first = self.api("numbered-labels/receive", body)
        self.assertEqual(first["count"], 3)
        self.assertEqual(self.api("numbered-labels/receive", body), first)
        page = self.api("numbered-labels")
        self.assertEqual(page["counts"]["ALL"], 3)
        self.assertEqual(page["rows"][0]["number"], "000001")
        token = self.api("numbered-labels/000001/print", {})["token"]
        self.assertIn("000001", self.document(token))
        self.assertEqual(self.api("numbered-labels/000001/history")["label"]["status"], "AVAILABLE")
        self.api("numbered-labels/distribute", {"start": "000001", "end": "000002", "destination": "가상동", "client_req_id": "fixture-distribute"})
        used = self.api("numbered-labels/000001/use", {"request_id": self.request_id, "client_req_id": "fixture-use"})
        self.assertEqual(used["label"]["status"], "USED")
        voided = self.api("numbered-labels/000001/void", {"reason": "가상표훼손", "client_req_id": "fixture-void"})
        self.assertEqual(voided["label"]["status"], "VOID")
        self.api("numbered-labels/000001/use", {"request_id": self.request_id, "client_req_id": "fixture-cannot-reuse"}, status=400)
        self.assertEqual(self.api("numbered-labels")["counts"]["VOID"], 1)

    def test_invalid_input_and_overlap(self):
        self.api("reports/daily?date=bad", status=400)
        self.api("reports/monthly?month=2099-13", status=400)
        self.api("reports/print", {"kind": "other"}, status=400)
        self.api("numbered-labels/receive", {"start": 1, "end": 3}, status=400)
        self.api("numbered-labels/receive", {"start": "001", "end": "003"})
        self.api("numbered-labels/receive", {"start": "1", "end": "4"}, status=400)
        self.assertEqual(self.api("numbered-labels")["total"], 3)

    def test_route_roles(self):
        read = ("reports/daily", "reports/monthly", "reports/daily.csv", "reports/monthly.csv", "numbered-labels", "numbered-labels/001/history")
        writes = ("numbered-labels/receive", "numbered-labels/distribute", "numbered-labels/001/use", "numbered-labels/001/void")
        for path in read:
            self.assertIsNotNone(server.match("GET", path)[0], path)
            for role in ("ADMIN", "INTAKE", "LEAD"):
                self.assertTrue(server.allowed(role, "GET", path), (role, path))
            self.assertFalse(server.allowed("FIELD", "GET", path), path)
        for path in writes:
            self.assertTrue(server.allowed("INTAKE", "POST", path), path)
            self.assertFalse(server.allowed("LEAD", "POST", path), path)
            self.assertFalse(server.allowed("FIELD", "POST", path), path)


if __name__ == "__main__":
    unittest.main()
