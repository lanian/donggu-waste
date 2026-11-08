"""파기된 접수의 일반 PC 쓰기·재정리 회귀: 실제 임시 SQLCipher와 HTTP만 사용한다."""
from __future__ import annotations

import json
import unittest

import test_pc_session_revocation as T
from dgpc import privacy as PV


class PrivacyWriteGuardsTest(T.SessionHTTPTestCase):
    def request(self, finished=False):
        body = self.request_body()
        with T.SV.LOCK:
            rid = T.S.create_request(self.con, body)["id"]
            if finished:
                with T.S.tx(self.con):
                    self.con.execute("UPDATE request SET status='DONE', completed_at='2018-01-01T00:00:00Z', updated_at='2018-01-01T00:00:00Z' WHERE id=?", (rid,))
            current = T.S.get_request(self.con, rid)
        editor = {**body, "req_ver": current["req_ver"], "reason": "가상 수정",
                  "items": [{"line_id": item["line_id"], "fee_rate_id": item["fee_rate_id"], "qty": item["qty"]} for item in current["items"]]}
        return rid, editor

    def purge(self):
        with T.SV.LOCK:
            return PV.purge_pii(self.con, today="2026-11-08")

    def row(self, rid):
        with T.SV.LOCK:
            return T.S.one(self.con, "SELECT * FROM request WHERE id=?", (rid,))

    def test_purge_invalidates_open_editor_version(self):
        rid, editor = self.request(finished=True)
        self.assertEqual(self.purge()["count"], 1)
        self.assertGreater(self.row(rid)["req_ver"], editor["req_ver"])

    def test_stale_and_current_version_cannot_restore_purged_details(self):
        rid, editor = self.request(finished=True)
        self.purge()
        for version in (editor["req_ver"], self.row(rid)["req_ver"]):
            with self.subTest(version=version):
                before = self.row(rid)
                status, response, _ = self.post("requests/" + rid, {**editor, "req_ver": version}, self.worker_cookie)
                self.assertIn(status, (400, 409), response)
                self.assertEqual(self.row(rid), before)

    def test_contacts_and_location_cannot_add_purged_personal_data(self):
        rid, _ = self.request(finished=True)
        self.purge()
        before = self.row(rid)
        for path, body in (("contacts", {"kind": next(iter(T.S.CONTACT_KINDS)), "content": "가상 주민 연락처 01000000000"}),
                           ("location", {"lat": 35.15, "lon": 126.92})):
            with self.subTest(path=path):
                status, response, _ = self.post(f"requests/{rid}/{path}", body, self.worker_cookie)
                self.assertEqual(status, 400, response)
        self.assertEqual(self.row(rid), before)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM contact_log WHERE request_id=?", (rid,)).fetchone()[0], 0)

    def test_purge_while_edit_body_is_waiting_blocks_the_late_write(self):
        rid, editor = self.request(finished=True)
        with self.delayed("requests/" + rid, editor) as finish:
            self.assertEqual(self.purge()["count"], 1)
            before = self.row(rid)
            status, response = finish()
        self.assertIn(status, (400, 409), response)
        self.assertEqual(self.row(rid), before)

    def test_unpurged_request_still_accepts_details_contacts_and_location(self):
        rid, editor = self.request()
        for path, body in (("", {**editor, "memo": "가상 업무 메모"}),
                           ("/contacts", {"kind": next(iter(T.S.CONTACT_KINDS)), "content": "가상 통화 내용"}),
                           ("/location", {"lat": 35.15, "lon": 126.92})):
            with self.subTest(path=path):
                self.assertEqual(self.post("requests/" + rid + path, body, self.worker_cookie)[0], 200)
        self.assertEqual(self.row(rid)["lat"], 35.15)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM contact_log WHERE request_id=?", (rid,)).fetchone()[0], 1)

    def test_purge_scrubs_personal_correction_reason_as_well_as_fields(self):
        rid, _ = self.request(finished=True)
        with T.SV.LOCK, T.S.tx(self.con):
            self.con.execute("INSERT INTO result_import (id,channel,device_id,package_sha256,imported_at) VALUES ('privacy-import','USB','fixture-device','privacy-fixture','2018-01-01T00:00:00Z')")
            self.con.execute("""INSERT INTO result_event
                (event_id,import_id,device_id,device_seq,request_id,assign_ver,kind,body_json,apply_status,device_ts)
                VALUES ('privacy-event','privacy-import','fixture-device',1,?,1,'CORRECTION',?,'APPLIED','2018-01-01T00:00:00Z')""",
                (rid, json.dumps({"fields": {"name": "가상 비공개 이름"}, "reason": "가상 비공개 연락처 01000000000"})))
        self.assertEqual(self.purge()["count"], 1)
        body = self.con.execute("SELECT body_json FROM result_event WHERE event_id='privacy-event'").fetchone()[0]
        self.assertNotIn("01000000000", body)
        self.assertNotIn("가상 비공개", body)

    def test_existing_purged_reentry_is_repaired_once_without_changing_business_records(self):
        rid, _ = self.request(finished=True)
        with T.SV.LOCK:
            T.S.add_payment(self.con, rid, {"amount": 1000, "method": T.S.PAY_METHODS[0], "paid_on": "2018-01-01"})
            T.S.receive_range(self.con, "000501", "000501")
            T.S.use_number(self.con, "000501", rid)
        self.purge()
        original = self.row(rid)
        with T.SV.LOCK:
            payment = T.S.rows(self.con, "SELECT * FROM payment WHERE request_id=?", (rid,))
            label = T.S.rows(self.con, "SELECT * FROM numbered_label WHERE request_id=?", (rid,))
            old_chain = T.S.rows(self.con, "SELECT id, detail, prev_hash, hash FROM audit_log ORDER BY id")
            with T.S.tx(self.con):
                self.con.execute("UPDATE request SET name='가상 재유입', phone='01000000000', address='가상주소', memo='가상 메모', lat=35.15, lon=126.92 WHERE id=?", (rid,))
                self.con.execute("INSERT INTO contact_log (id, request_id, at, kind, content, actor, recorded_at) VALUES ('repair-contact',?,'2026-11-08T01:00',?,'가상 연락 내용','fixture','2026-11-08T01:00:00Z')", (rid, next(iter(T.S.CONTACT_KINDS))))
                self.con.execute("INSERT INTO request_version (request_id, req_ver, snapshot_json, reason, at) VALUES (?,99,?,'가상 수정 사유','2026-11-08T01:00:00Z')", (rid, json.dumps({"name": "가상 버전 이름", "phone": "01000000000"})))
        result = self.purge()
        self.assertEqual(result["count"], 0)
        self.assertEqual(result.get("repaired_requests"), 1)
        repaired = self.row(rid)
        self.assertEqual((repaired["name"], repaired["phone"], repaired["address"], repaired["memo"], repaired["lat"], repaired["lon"]),
                         (PV.PURGED, PV.PURGED_PHONE, PV.PURGED, "", None, None))
        self.assertEqual(repaired["pii_purged_at"], original["pii_purged_at"])
        self.assertGreater(repaired["req_ver"], original["req_ver"])
        self.assertEqual(T.S.rows(self.con, "SELECT * FROM payment WHERE request_id=?", (rid,)), payment)
        self.assertEqual(T.S.rows(self.con, "SELECT * FROM numbered_label WHERE request_id=?", (rid,)), label)
        self.assertEqual(T.S.rows(self.con, "SELECT id, detail, prev_hash, hash FROM audit_log ORDER BY id LIMIT ?", (len(old_chain),)), old_chain)
        self.assertTrue(PV.verify_audit(self.con)["ok"])
        snapshot = T.S.rows(self.con, "SELECT * FROM request_version WHERE request_id=?", (rid,))
        self.assertNotIn("가상 버전 이름", json.dumps(snapshot, ensure_ascii=False))
        self.assertEqual(self.con.execute("SELECT content FROM contact_log WHERE id='repair-contact'").fetchone()[0], PV.PURGED)
        chain_count = self.con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        second = self.purge()
        self.assertIsNone(second.get("repaired_requests"))
        self.assertEqual(self.row(rid), repaired)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], chain_count)


if __name__ == "__main__":
    unittest.main(verbosity=2)
