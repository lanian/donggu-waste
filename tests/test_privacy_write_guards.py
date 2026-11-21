"""파기된 접수의 일반 PC 쓰기·재정리 회귀: 실제 임시 SQLCipher와 HTTP만 사용한다."""
from __future__ import annotations

import json
import unittest
import uuid

import test_pc_session_revocation as T
from dgpc import area, official_forms, ops, privacy as PV


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

    def test_pc_field_item_purge_preserves_quantities_accounting_and_labels(self):
        with T.SV.LOCK:
            dong_id = area.save_dong(self.con, {"name": "가상 파기동"})
            T.S.receive_range(self.con, "0101", "0105", dong_id=dong_id, received_on="2020-01-01")
            rid = T.S.create_request(self.con, {**self.request_body(), "dong": "가상 파기동"})["id"]
            detail = T.S.get_request(self.con, rid)
        catalog = detail["items"][0]
        status, response, _ = self.post(f"requests/{rid}/collection", {
            "req_ver": detail["req_ver"], "basis": detail["collection"]["basis"], "client_req_id": "purge-collection",
            "collected_on": "2020-02-05", "reason": "가상 현장 확인",
            "items": [{"line_id": catalog["line_id"], "qty": 1}],
            "field_items": [{"fee_rate_id": catalog["fee_rate_id"], "confirmed_qty": 1, "qty": 1},
                            {"name": "가상 개인이름 포함 품명", "spec": "가상 연락처 포함 규격", "confirmed_qty": 2, "qty": 2}]
        }, self.admin_cookie)
        self.assertEqual(status, 200, response)
        self.assertEqual(response["labels_assigned"], 4)

        def allocations():
            return T.S.rows(self.con, """SELECT number_key,line_id,unit_index,dong_id FROM numbered_label_allocation
                WHERE request_id=? ORDER BY number_key""", (rid,))

        before = T.S.get_request(self.con, rid)
        labels = allocations()
        totals = official_forms.monthly_data(self.con, dong_id, "2020-02")["totals"]
        self.assertEqual(totals["qty"], 4)
        self.assertEqual(len(labels), 4)
        self.assertIn("가상 개인이름 포함 품명", [line["name"] for line in before["state"]["lines"]])
        self.assertEqual(self.purge()["count"], 1)
        after = T.S.get_request(self.con, rid)
        self.assertEqual(allocations(), labels)
        self.assertEqual(official_forms.monthly_data(self.con, dong_id, "2020-02")["totals"], totals)
        self.assertEqual(after["state"]["fee"], before["state"]["fee"])
        self.assertEqual(after["payment"], before["payment"])
        self.assertEqual(after["dong"], before["dong"])
        free, = [line for line in after["state"]["lines"] if not line["fee_rate_id"]]
        self.assertEqual((free["name"], free["spec"], free["qty_confirmed"], free["qty_collected"]), (PV.PURGED, "", 2, 2))
        imported = self.con.execute("SELECT import_id FROM result_event WHERE request_id=? LIMIT 1", (rid,)).fetchone()[0]
        review = ops.import_field_review(self.con, imported)["items"][0]
        self.assertEqual(review["added_items"][-1]["name"], PV.PURGED)
        self.assertEqual(review["added_items"][-1]["spec"], "")
        listed, = [line for line in after["state"]["lines"] if line["kind"] == "FIELD" and line["fee_rate_id"]]
        self.assertEqual((listed["name"], listed["spec"], listed["fee_rate_id"]), (catalog["name"], catalog["spec"], catalog["fee_rate_id"]))
        self.assertIsNone(self.purge().get("repaired_requests"))

    def test_repair_old_mobile_field_name_and_spec_only_once(self):
        rid, _ = self.request(finished=True)
        self.purge()
        initial = self.row(rid)
        with T.SV.LOCK, T.S.tx(self.con):
            self.con.execute("INSERT INTO result_import (id,channel,device_id,package_sha256,imported_at) VALUES ('field-import','USB','fixture-phone','field-test','2018-01-01T00:00:00Z')")
            self.con.execute("""INSERT INTO result_event
                (event_id,import_id,device_id,device_seq,request_id,assign_ver,kind,body_json,apply_status,device_ts)
                VALUES ('field-event','field-import','fixture-phone',1,?,1,'FIELD_ITEM_ADD',?,'APPLIED','2018-01-01T00:00:00Z')""",
                (rid, json.dumps({"line_id": "field-line", "name": PV.PURGED, "spec": "가상 잔존 규격", "qty": 2, "pii_purged": True})))
        self.assertEqual(self.purge().get("repaired_requests"), 1)
        body = json.loads(self.con.execute("SELECT body_json FROM result_event WHERE event_id='field-event'").fetchone()[0])
        self.assertEqual((body["name"], body["spec"], body["qty"]), (PV.PURGED, "", 2))
        self.assertEqual(self.row(rid)["pii_purged_at"], initial["pii_purged_at"])
        self.assertIsNone(self.purge().get("repaired_requests"))

    def test_already_purged_solo_restore_scrubs_field_item_before_first_read(self):
        rid, original, field = (str(uuid.uuid4()) for _ in range(3))
        item = self.request_body()["items"][0]
        request = {"request_id": rid, "receipt_no": "DG-20200101-0091", "pii_purged": "2020-01-01T00:00:00Z",
                   "planned_date": "2020-01-01", "items": [{**item, "line_id": original}]}
        records = [("FIELD_ITEM_ADD", {"line_id": field, "name": "가상 비공개 품명", "spec": "가상 비공개 규격",
                                      "qty": 2, "unit": "EA", "unit_label": "개"}),
                   ("ITEM_RESULT", {"line_id": field, "qty_confirmed": 2, "qty_collected": 1}),
                   ("ATTEMPT", {"outcome": "PARTIAL"})]
        events = [{"seq": i, "event_id": str(uuid.uuid4()), "request_id": rid, "kind": kind, "body": body}
                  for i, (kind, body) in enumerate(records, 1)]
        opened = {"format": "dg.backup/1", "state": {"mode": "SOLO", "requests": [request], "events": events}}
        with T.SV.LOCK:
            # 서버가 파일 복호화를 마친 뒤 사용하는 동일한 진입점으로 바로 확인한다.
            summary = T.S.import_solo(self.con, b"fixture-solo-field", "", opened=opened)
            state = T.S.get_request(self.con, rid)["state"]
        self.assertEqual(summary["requests"], 1)
        restored, = [line for line in state["lines"] if line["kind"] == "FIELD"]
        self.assertEqual((restored["name"], restored["spec"], restored["qty_confirmed"], restored["qty_collected"]),
                         (PV.PURGED, "", 2, 1))
        self.assertEqual(state["fee"]["pending_count"], 1)

    def test_existing_purged_conflict_reason_only_is_repaired_once(self):
        rid, _ = self.request(finished=True)
        self.purge()
        original = self.row(rid)
        with T.SV.LOCK, T.S.tx(self.con):
            self.con.execute("INSERT INTO result_import (id,channel,device_id,package_sha256,imported_at) VALUES ('reason-import','USB','fixture-device','reason-fixture','2018-01-01T00:00:00Z')")
            self.con.execute("""INSERT INTO result_event
                (event_id,import_id,device_id,device_seq,request_id,assign_ver,kind,body_json,apply_status,resolved_reason,device_ts)
                VALUES ('reason-event','reason-import','fixture-device',1,?,1,'CONTACT',?,'REJECTED',?,'2018-01-01T00:00:00Z')""",
                (rid, json.dumps({"result": "REACHED", "pii_purged": True}), "가상 주민 전화 01000000000"))
        result = self.purge()
        self.assertEqual(result.get("repaired_requests"), 1)
        self.assertEqual(self.con.execute("SELECT resolved_reason FROM result_event WHERE event_id='reason-event'").fetchone()[0], PV.PURGED)
        repaired = self.row(rid)
        self.assertEqual(repaired["req_ver"], original["req_ver"] + 1)
        self.assertEqual(repaired["pii_purged_at"], original["pii_purged_at"])
        chain_count = self.con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        self.assertTrue(PV.verify_audit(self.con)["ok"])
        self.assertIsNone(self.purge().get("repaired_requests"))
        self.assertEqual(self.row(rid), repaired)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], chain_count)

    def test_purged_pending_correction_is_not_repaired_again_for_its_system_reason(self):
        rid, editor = self.request(finished=True)
        with T.SV.LOCK, T.S.tx(self.con):
            self.con.execute("INSERT INTO result_import (id,channel,device_id,package_sha256,imported_at) VALUES ('pending-import','USB','fixture-device','pending-fixture','2018-01-01T00:00:00Z')")
            self.con.execute("""INSERT INTO result_event
                (event_id,import_id,device_id,device_seq,request_id,assign_ver,kind,body_json,apply_status,device_ts)
                VALUES ('pending-event','pending-import','fixture-device',1,?,1,'CORRECTION',?,'CONFLICT','2018-01-01T00:00:00Z')""",
                (rid, json.dumps({"fields": {"name": "가상 비공개 이름"}, "reason": "가상 비공개 연락처 01000000000"})))
        first = self.purge()
        self.assertEqual(first["count"], 1)
        self.assertIsNone(first.get("repaired_requests"))
        purged = self.row(rid)
        self.assertEqual(purged["req_ver"], editor["req_ver"] + 1)
        event = self.con.execute("SELECT apply_status, resolved_reason FROM result_event WHERE event_id='pending-event'").fetchone()
        self.assertEqual(event, ("REJECTED", "보유기간 경과로 개인정보 파기(미처리 요청)"))
        chain_count = self.con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        self.assertIsNone(self.purge().get("repaired_requests"))
        self.assertEqual(self.row(rid), purged)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], chain_count)

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
