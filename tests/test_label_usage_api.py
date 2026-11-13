"""자동 배출표 집계: 실제 HTTP/서명된 결과 파일/권한 회귀."""
import base64
import hashlib
import http.client
import json
import unittest
import uuid

import test_label_usage as fixtures
from test_pc_session_revocation import SessionHTTPTestCase
from dgpc import core as C, server as SV, service as S
import dgproto as P


class UsageHTTPCase(SessionHTTPTestCase):
    request = fixtures.LabelUsageTests.request
    events = fixtures.LabelUsageTests.events
    result = fixtures.LabelUsageTests.result

    def setUp(self):
        super().setUp()
        self.import_seq = 0
        self.event_seq = 0
        self.fee = next(r["id"] for r in S.FEE_ROWS.values() if r["status"] == "CONFIRMED")

    def get(self, path="label-usage", cookie=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request("GET", "/api/" + path, headers={"Cookie": cookie or "", "X-DG-CSRF": "1"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def phone(self, r):
        registration = P.b45decode(S.registration_text(self.con))
        pc_kex = P.pub_from_raw(registration[69:134])
        kex, sign, device = P.gen_key(), P.gen_key(), uuid.uuid4().bytes
        data = device + P.pub_raw(kex) + P.pub_raw(sign)
        code = "DGD1." + base64.urlsafe_b64encode(data + hashlib.sha256(data).digest()[:4]).decode().rstrip("=")
        dev_id = S.register_device(self.con, code, "가상 장수 집계폰", confirm=True)["device_id"]
        with C.tx(self.con):
            self.con.execute("UPDATE request SET origin_device=? WHERE id=?", (dev_id, r["id"]))
        self.phone_seq = 0

        def seal(events):
            header = P.Header(P.TYPE_RESULT, uuid.uuid4().bytes, device, 1, 1, P.key_id(P.pub_raw(sign)))
            return P.seal({"schema": "dg.result/1", "work_date": S.today_kst(), "events": events}, header, pc_kex, sign)[0]

        def event(kind, body):
            self.phone_seq += 1
            return {"event_id": str(uuid.uuid4()), "seq": self.phone_seq, "request_id": r["id"], "assign_ver": 0,
                    "base_req_ver": r["req_ver"], "kind": kind, "body": body, "device_ts": C.now()}

        return seal, event


class UsageAPITests(UsageHTTPCase):
    def test_late_item_before_already_received_final_attempt_counts(self):
        r, line = self.request()
        seal, event = self.phone(r)
        item = event("ITEM_RESULT", {"line_id": line, "qty_confirmed": 3, "qty_collected": 3})
        final = event("ATTEMPT", {"outcome": "DONE"})
        for events in ([final], [item]):
            status, value, _ = self.post("import/result", seal(events), self.admin_cookie)
            self.assertEqual(status, 200, value)
            self.assertEqual(value["applied"], 1)
        self.assertEqual(self.get(cookie=self.admin_cookie)[1]["totals"]["used"], 3)

    def test_late_older_final_does_not_replace_newer_cumulative_snapshot(self):
        r, line = self.request()
        seal, event = self.phone(r)
        old = [event("ITEM_RESULT", {"line_id": line, "qty_confirmed": 3, "qty_collected": 1}),
               event("ATTEMPT", {"outcome": "PARTIAL"})]
        new = [event("ITEM_RESULT", {"line_id": line, "qty_confirmed": 3, "qty_collected": 3}),
               event("ATTEMPT", {"outcome": "DONE"})]
        for events in (new, old):
            status, value, _ = self.post("import/result", seal(events), self.admin_cookie)
            self.assertEqual(status, 200, value)
            self.assertEqual(value["applied"], 2)
        self.assertEqual(self.get(cookie=self.admin_cookie)[1]["totals"]["used"], 3)

    def test_http_summary_paging_privacy_and_validation(self):
        r, line = self.request()
        self.result(r, line, 3)
        status, page = self.get(cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(page["totals"], {"used": 3, "requests": 1})
        self.assertEqual(page["rows"][0]["receipt_no"], r["receipt_no"])
        self.assertNotIn("가상비공개", json.dumps(page, ensure_ascii=False))
        for query in ("page=x", "page=-1", "q=" + "x" * 61, "dong=" + "x" * 101):
            self.assertEqual(self.get("label-usage?" + query, self.admin_cookie)[0], 400)
        self.assertEqual(self.get("label-usage?q=missing", self.admin_cookie)[1]["totals"]["used"], 0)

    def test_office_read_roles_and_unauthenticated_denial(self):
        self.assertEqual(self.get()[0], 401)
        self.assertEqual(self.get(cookie=self.worker_cookie)[0], 200)
        for role in ("ADMIN", "INTAKE", "LEAD", "FIELD", "DONG"):
            self.assertEqual(SV.allowed(role, "GET", "label-usage"), role in ("ADMIN", "INTAKE", "LEAD"))
        with C.tx(self.con):
            self.con.execute("UPDATE app_user SET role='LEAD' WHERE id=?", (self.worker_id,))
        self.assertEqual(self.get(cookie=self.worker_cookie)[0], 200)
        with C.tx(self.con):
            self.con.execute("UPDATE app_user SET role='FIELD' WHERE id=?", (self.worker_id,))
        self.assertEqual(self.get(cookie=self.worker_cookie)[0], 403)

    def test_signed_result_duplicate_file_and_events_do_not_double_usage(self):
        r, line = self.request()
        seal, event = self.phone(r)
        events = [event("ITEM_RESULT", {"line_id": line, "qty_confirmed": 3, "qty_collected": 2}),
                  event("ATTEMPT", {"outcome": "PARTIAL"})]
        package = seal(events)
        status, first, _ = self.post("import/result", package, self.admin_cookie)
        self.assertEqual(status, 200, first)
        self.assertEqual(first["applied"], 2)
        self.assertEqual(self.get(cookie=self.admin_cookie)[1]["totals"]["used"], 2)
        status, duplicate, _ = self.post("import/result", package, self.admin_cookie)
        self.assertEqual(status, 200, duplicate)
        self.assertTrue(duplicate["duplicate_file"])
        status, duplicate, _ = self.post("import/result", seal(events), self.admin_cookie)
        self.assertEqual(status, 200, duplicate)
        self.assertEqual(duplicate["skipped_duplicate"], 2)
        self.assertEqual(self.get(cookie=self.admin_cookie)[1]["totals"]["used"], 2)
        updated = [event("ITEM_RESULT", {"line_id": line, "qty_confirmed": 3, "qty_collected": 3}),
                   event("ATTEMPT", {"outcome": "DONE"})]
        status, final, _ = self.post("import/result", seal(updated), self.admin_cookie)
        self.assertEqual(status, 200, final)
        self.assertEqual(self.get(cookie=self.admin_cookie)[1]["totals"]["used"], 3)


if __name__ == "__main__":
    unittest.main()
