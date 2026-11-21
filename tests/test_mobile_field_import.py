"""서명한 모바일 현장접수를 PC로 가져올 때 모든 품목과 후속 수거 기록이 연결되는지 검증한다."""
import base64
import hashlib
import json
import os
import unittest
import uuid
import zlib

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from test_pc import Phone
from test_pc_session_revocation import SessionHTTPTestCase
from dgpc import routing, service as S
import dgproto as P


class MobileFieldImportTest(SessionHTTPTestCase):
    def setUp(self):
        super().setUp()
        self.phone = Phone(S.registration_text(self.con))
        dev = S.register_device(self.con, self.phone.code(), "가상 현장폰", confirm=True)["device_id"]
        self.crew = S.save_crew(self.con, {"name": "가상 현장팀", "device_id": dev})
        self.request_id = str(uuid.uuid4())
        self.stop = {"request_id": self.request_id, "assign_ver": 0, "req_ver": 1}
        self.fees = [r for r in S.FEE_TABLE["rows"] if r["status"] == "CONFIRMED" and r["price"] > 0]

    def intake(self, items):
        return self.phone.ev(self.stop, "FIELD_INTAKE", {
            "name": "가상 현장접수", "phone": "01000000000", "address": "가상길 10", "items": items
        })

    def seal(self, events):
        return self.phone.seal({"schema": "dg.result/1", "work_date": "2026-11-03", "events": events}, P.TYPE_RESULT)

    def upload(self, raw):
        status, result, _ = self.post("import/result", raw, self.admin_cookie, {"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 200, result)
        return result

    def test_51_items_keep_prices_and_final_line_result_after_retransmission(self):
        items = [{"line_id": str(uuid.uuid4()), "fee_rate_id": fee["id"], "unit_price": 1, "qty": 1}
                 for fee in self.fees[:50]]
        last = str(uuid.uuid4())
        items.append({"line_id": last, "fee_rate_id": "unknown", "name": "표에 없는 가상 품목", "qty": 2, "unit_price": 1})
        self.assertEqual(len(items), 51)
        intake = self.intake(items)
        collected = self.phone.ev(self.stop, "ITEM_RESULT", {"line_id": last, "qty_confirmed": 2, "qty_collected": 2})
        events = [intake, collected]
        raw = self.seal(events)
        result = self.upload(raw)
        self.assertEqual(result["field_intakes"], 1)
        stored = S.rows(self.con, "SELECT * FROM request_item WHERE request_id=?", (self.request_id,))
        self.assertEqual(len(stored), 51)
        by_line = {row["line_id"]: row for row in stored}
        self.assertEqual(set(by_line), {row["line_id"] for row in items})
        for source, fee in zip(items, self.fees[:50]):
            row = by_line[source["line_id"]]
            self.assertEqual((row["fee_rate_id"], row["unit_price"], row["amount"]), (fee["id"], fee["price"], fee["price"]))
        pending = by_line[last]
        self.assertEqual((pending["price_status"], pending["unit_price"], pending["amount"], pending["qty"]), ("PENDING", None, None, 2))
        self.assertEqual(result["conflict"], 0)
        field_result = S.one(self.con, "SELECT apply_status, body_json FROM result_event WHERE event_id=?", (collected["event_id"],))
        self.assertEqual((field_result["apply_status"], json.loads(field_result["body_json"])["line_id"]), ("APPLIED", last))
        lines = S.get_request(self.con, self.request_id)["state"]["lines"]
        tail = next(line for line in lines if line["line_id"] == last)
        self.assertEqual((tail["qty_confirmed"], tail["qty_collected"], tail["remaining"]), (2, 2, 0))
        self.assertTrue(self.upload(raw)["duplicate_file"])
        self.assertEqual(self.upload(self.seal(events))["skipped_duplicate"], 2)
        self.assertEqual(S.one(self.con, "SELECT COUNT(*) n FROM request_item WHERE request_id=?", (self.request_id,))["n"], 51)

    def test_invalid_later_item_holds_the_whole_request(self):
        items = [{"fee_rate_id": self.fees[0]["id"], "qty": 1} for _ in range(50)]
        intake = self.intake([*items, {"name": "수량 오류", "qty": 0}])
        result = self.upload(self.seal([intake]))
        self.assertEqual(result.get("review"), 1)
        self.assertIsNone(S.one(self.con, "SELECT id FROM request WHERE id=?", (self.request_id,)))
        event = S.one(self.con, "SELECT apply_status, body_json FROM result_event WHERE event_id=?", (intake["event_id"],))
        self.assertEqual(event["apply_status"], "CONFLICT")
        self.assertEqual(len(json.loads(event["body_json"])["items"]), 51)

    def test_old_intake_without_line_id_keeps_generated_id(self):
        receipt = self.intake([{"fee_rate_id": self.fees[0]["id"], "qty": 1}])
        self.assertEqual(self.upload(self.seal([receipt]))["field_intakes"], 1)
        rows = S.rows(self.con, "SELECT line_id FROM request_item WHERE request_id=?", (self.request_id,))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["line_id"])

    def solo_backup(self, records):
        password = b"test-solo-mobile-import"
        salt, iv = os.urandom(16), os.urandom(12)
        state = {"mode": "SOLO", "requests": records, "events": []}
        payload = json.dumps({"format": "dg.backup/1", "state": state, "photos": {}}).encode()
        key = hashlib.pbkdf2_hmac("sha256", password, salt, 600000, 32)
        raw = b"DGB1" + salt + iv + AESGCM(key).encrypt(iv, zlib.compress(payload, wbits=-15), b"DGB1")
        return raw, {"X-DG-Pass": base64.b64encode(password).decode(), "Content-Type": "application/octet-stream"}

    def check_work_minutes(self, mode):
        day = "2026-11-03"
        records = []
        for i, value in enumerate((None, 37, 1440, 1441, -1, 1.5, True)):
            r = {"request_id": str(uuid.uuid4()), "receipt_no": f"가상-{i}", "planned_date": day,
                 "name": "가상 접수", "phone": "01000000000", "address": f"가상길 {i + 1}",
                 "lat": 35.15, "lon": 126.92, "items": [{"fee_rate_id": self.fees[0]["id"], "qty": 1}]}
            if value is not None:
                r["extra_work_min"] = value
            records.append(r)
        if mode == "field":
            events = [self.phone.ev({**self.stop, "request_id": r["request_id"]}, "FIELD_INTAKE", r) for r in records]
            result = self.upload(self.seal(events))
            self.assertEqual(result.get("review", 0), 4)
        else:
            raw, headers = self.solo_backup(records)
            for path in ("import/solo?preview=1", "import/solo"):
                status, result, _ = self.post(path, raw, self.admin_cookie, headers)
                self.assertEqual(status, 200, result)
                self.assertEqual(result["invalid"], 4)
        saved = S.rows(self.con, "SELECT id,extra_work_min FROM request")
        self.assertEqual({r["id"]: r["extra_work_min"] for r in saved},
                         {r["request_id"]: mins for r, mins in zip(records, (0, 37, 1440))})
        status, result, _ = self.post("route", {"date": day, "crew_ids": [self.crew], "time_limit": 1}, self.admin_cookie)
        self.assertEqual(status, 200, result)
        stops = {s["request_id"]: s for route in result["routes"] for s in route["stops"]}
        self.assertEqual(set(stops), {r["request_id"] for r in records[:2]})
        base = routing.work_min([self.fees[0]])
        self.assertEqual(stops[records[0]["request_id"]]["service_min"], base)
        self.assertEqual(stops[records[1]["request_id"]]["service_min"], base + 37)
        self.assertEqual(stops[records[1]["request_id"]]["extra_work_min"], 37)

    def test_field_work_minutes_reach_route(self):
        self.check_work_minutes("field")

    def test_solo_work_minutes_reach_route(self):
        self.check_work_minutes("solo")


if __name__ == "__main__":
    unittest.main(verbosity=2)
