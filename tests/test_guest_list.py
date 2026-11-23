"""미등록 휴대폰 조회용 목록의 실제 HTTP 발급과 암호화 전송 계약."""
import base64
import datetime as dt
import json
import os
import unittest
import uuid
import zlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import test_label_usage_api as fixture
import dgproto
from dgpc import assign, core, server, service


class GuestListTests(fixture.UsageHTTPCase):
    def setUp(self):
        super().setUp()
        self.day = core.today_kst()
        self.team = service.save_crew(self.con, {"name": "가상 조회팀"})

    def assign(self, *requests, work_date=None):
        batch, = assign.confirm_plan(self.con, work_date or self.day, {
            self.team: [{"request_id": r["id"]} for r in requests],
        })
        return batch["batch_id"]

    def issue(self, batch):
        status, reply, _ = self.post("guest-lists", {"batch_id": batch}, self.worker_cookie)
        self.assertEqual(status, 200, reply)
        return reply

    def open(self, reply):
        self.assertRegex(reply["key_qr"], r"^DGVIEW1:[0-9a-f]{32}:[A-Za-z0-9_-]{43}$")
        prefix, identifier, encoded = reply["key_qr"].split(":")
        key = base64.urlsafe_b64decode(encoded + "=")
        collector = dgproto.Collector()
        for text in reversed(reply["frames"]):
            self.assertTrue(text.startswith("DGV1."))
            frame_id, _, _ = collector.add(text[5:])
            self.assertEqual(frame_id.hex(), identifier)
        envelope = collector.assemble(bytes.fromhex(identifier))
        aad = f"{prefix}:{identifier}".encode("ascii")
        with self.assertRaises(InvalidTag):
            AESGCM(key).decrypt(envelope[:12], envelope[12:], b"different context")
        payload = zlib.decompress(AESGCM(key).decrypt(envelope[:12], envelope[12:], aad))
        return json.loads(payload)

    def test_issue_without_device_shares_only_visit_fields_and_audits_no_secret(self):
        first, _ = self.request()
        second, _ = self.request()
        batch = self.assign(first, second)
        with core.tx(self.con):
            self.con.execute("UPDATE request SET memo='숨긴메모', spot_note='숨긴상세위치' WHERE id=?", (first["id"],))
            self.con.execute("UPDATE request SET address=? WHERE id=?", ("가상 주소 " + os.urandom(1300).hex(), second["id"]))
        reply = self.issue(batch)
        self.assertEqual(set(reply), {"key_qr", "frames", "count", "work_date", "crew", "assign_ver"})
        self.assertEqual((reply["count"], reply["work_date"], reply["crew"], reply["assign_ver"]),
                         (2, self.day, "가상 조회팀", 1))
        self.assertGreater(len(reply["frames"]), 1)
        opened = self.open(reply)
        self.assertEqual(set(opened), {"schema", "crew_id", "crew", "work_date", "assign_ver", "issue_no", "stops"})
        self.assertEqual((opened["schema"], opened["crew_id"]), ("dg.guest-list/1", self.team))
        self.assertEqual([stop["seq"] for stop in opened["stops"]], [1, 2])
        item = first["items"][0]
        self.assertEqual(opened["stops"][0], {"seq": 1, "dong": first["dong"], "address": first["address"],
                         "items": [{"name": item["name"], "spec": item["spec"], "qty": 3, "unit": item["unit_label"]}]})
        for hidden in [first["id"], first["phone"], first["name"], "숨긴메모", "숨긴상세위치"]:
            self.assertNotIn(hidden, json.dumps(opened, ensure_ascii=False))
        for table in ("device", "transfer_bundle", "result_import", "result_event"):
            self.assertEqual(self.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        row = core.one(self.con, "SELECT id, detail FROM audit_log WHERE entity='guest_list'")
        self.assertEqual(opened["issue_no"], row["id"])
        self.assertEqual(json.loads(row["detail"]), {"crew_id": self.team, "work_date": self.day, "count": 2})
        reissued = self.issue(batch)
        self.assertNotEqual(reissued["key_qr"], reply["key_qr"])
        self.assertNotEqual(reissued["frames"], reply["frames"])

    def test_reissue_keeps_only_outstanding_quantity_and_current_batch(self):
        partial, _ = self.request()
        done, _ = self.request()
        batch = self.assign(done, partial)
        before = self.open(self.issue(batch))
        for request, quantity in [(partial, 1), (done, 3)]:
            current = service.get_request(self.con, request["id"])
            status, reply, _ = self.post("requests/" + request["id"] + "/collection", {
                "req_ver": current["req_ver"], "basis": current["collection"]["basis"], "collected_on": self.day,
                "items": [{"line_id": current["items"][0]["line_id"], "qty": quantity}],
                "reason": "가상 수거", "client_req_id": str(uuid.uuid4()),
            }, self.worker_cookie)
            self.assertEqual(status, 200, reply)
        updated = self.open(self.issue(batch))
        self.assertEqual(updated["assign_ver"], before["assign_ver"])
        self.assertGreater(updated["issue_no"], before["issue_no"])
        self.assertEqual(len(before["stops"]), 2)
        visit, = updated["stops"]
        self.assertEqual((visit["seq"], visit["items"][0]["qty"]), (2, 2))
        new_batch = self.assign(partial)
        self.assertEqual(self.post("guest-lists", {"batch_id": batch}, self.worker_cookie)[0], 400)
        self.assertEqual(self.open(self.issue(new_batch))["assign_ver"], 2)

    def test_office_roles_date_archive_and_bad_request(self):
        for role in ("ADMIN", "INTAKE", "FIELD", "DONG", "LEAD"):
            self.assertEqual(server.allowed(role, "POST", "guest-lists"), role in ("ADMIN", "INTAKE"))
        self.assertEqual(self.post("guest-lists", {})[0], 401)
        for body in ({}, [], {"batch_id": {}}, {"batch_id": "unknown"}):
            self.assertEqual(self.post("guest-lists", body, self.admin_cookie)[0], 400)
        yesterday = dt.date.fromisoformat(self.day) - dt.timedelta(days=1)
        old = self.assign(work_date=yesterday.isoformat())
        self.assertEqual(self.post("guest-lists", {"batch_id": old}, self.admin_cookie)[0], 400)
        batch = self.assign()
        self.assertEqual(self.open(self.issue(batch))["stops"], [])
        with core.tx(self.con):
            self.con.execute("UPDATE crew SET archived_at=? WHERE id=?", (core.now(), self.team))
        self.assertEqual(self.post("guest-lists", {"batch_id": batch}, self.admin_cookie)[0], 400)

    def test_oversized_list_returns_actionable_error_and_no_issuance(self):
        req, _ = self.request()
        batch = self.assign(req)
        for address in ("가" * 350000, os.urandom(230000).hex()):
            with core.tx(self.con):
                self.con.execute("UPDATE request SET address=? WHERE id=?", (address, req["id"]))
            status, reply, _ = self.post("guest-lists", {"batch_id": batch}, self.worker_cookie)
            self.assertEqual(status, 400, reply)
            self.assertIn("목록", reply["error"])
        self.assertIsNone(core.one(self.con, "SELECT id FROM audit_log WHERE entity='guest_list'"))


if __name__ == "__main__":
    unittest.main()
