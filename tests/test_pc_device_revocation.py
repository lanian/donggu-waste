"""파일 준비 뒤 기기 취소 회귀: 실제 임시 SQLCipher·암호화 묶음·HTTP를 사용한다."""
from __future__ import annotations

import base64
import hashlib
import unittest
import uuid

import test_pc_session_revocation as T


class DeviceRevocationTest(T.SessionHTTPTestCase):
    preparing = T.UnlockedSessionRevocationTest.preparing

    def package(self, photos=False):
        registration = T.P.b45decode(T.S.registration_text(self.con))
        pc_kex = T.P.pub_from_raw(registration[69:134])
        kex, sign, device = T.P.gen_key(), T.P.gen_key(), uuid.uuid4().bytes
        raw = device + T.P.pub_raw(kex) + T.P.pub_raw(sign)
        code = "DGD1." + base64.urlsafe_b64encode(raw + hashlib.sha256(raw).digest()[:4]).decode().rstrip("=")
        dev_id = T.S.register_device(self.con, code, "가상 취소 폰", confirm=True)["device_id"]
        rid = T.S.create_request(self.con, self.request_body())["id"]
        if photos:
            content = b"device-revocation-fixture-photo"
            payload = {"schema": "dg.photos/1", "photos": [{"photo_id": str(uuid.uuid4()), "request_id": rid,
                       "phase": "AFTER", "sha256": hashlib.sha256(content).hexdigest(), "data": base64.b64encode(content).decode()}]}
            kind = T.S.TYPE_PHOTOS
        else:
            payload = {"schema": "dg.result/1", "work_date": T.S.today_kst(), "events": [{
                "event_id": str(uuid.uuid4()), "seq": 1, "request_id": rid, "assign_ver": 0,
                "kind": "CONTACT", "body": {"result": "REACHED"}, "device_ts": T.S.now()}]}
            kind = T.P.TYPE_RESULT
        header = T.P.Header(kind, uuid.uuid4().bytes, device, 1, 1, T.P.key_id(T.P.pub_raw(sign)))
        return dev_id, rid, T.P.seal(payload, header, pc_kex, sign)[0]

    def import_state(self):
        with T.SV.LOCK:
            return tuple(self.con.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                         for table in ("result_import", "result_event", "result_seq", "photo_import", "photo"))

    def test_lost_and_retired_devices_after_prepare_do_not_apply(self):
        for photos in (False, True):
            route, prepare = ("import/photos", "prepare_photos") if photos else ("import/result", "prepare_result")
            for revoked in ("LOST", "RETIRED"):
                with self.subTest(route=route, status=revoked):
                    dev_id, _, body = self.package(photos)
                    before = self.import_state()
                    with self.preparing(route, body, prepare) as finish:
                        self.assertEqual(self.post(f"devices/{dev_id}/status", {"status": revoked}, self.admin_cookie)[0], 200)
                        status, response, _ = finish()
                    self.assertEqual(status, 400, response)
                    self.assertEqual(self.import_state(), before)

    def test_confirmation_revoked_after_prepare_does_not_apply(self):
        for photos in (False, True):
            with self.subTest(photos=photos):
                dev_id, _, body = self.package(photos)
                route, prepare = ("import/photos", "prepare_photos") if photos else ("import/result", "prepare_result")
                before = self.import_state()
                with self.preparing(route, body, prepare) as finish:
                    with T.SV.LOCK, T.S.tx(self.con):
                        self.con.execute("UPDATE device SET confirmed_at=NULL WHERE id=?", (dev_id,))
                    status, response, _ = finish()
                self.assertEqual(status, 400, response)
                self.assertEqual(self.import_state(), before)

    def test_revoked_before_prepare_is_also_denied(self):
        for photos in (False, True):
            with self.subTest(photos=photos):
                dev_id, _, body = self.package(photos)
                self.assertEqual(self.post(f"devices/{dev_id}/status", {"status": "LOST"}, self.admin_cookie)[0], 200)
                route = "import/photos" if photos else "import/result"
                before = self.import_state()
                status, response, _ = self.post(route, body, self.worker_cookie)
                self.assertEqual(status, 400, response)
                self.assertEqual(self.import_state(), before)

    def test_active_device_applies_once_and_duplicate_receipt_is_preserved(self):
        for photos in (False, True):
            with self.subTest(photos=photos):
                dev_id, _, body = self.package(photos)
                route = "import/photos" if photos else "import/result"
                status, response, _ = self.post(route, body, self.worker_cookie)
                self.assertEqual(status, 200, response)
                self.assertEqual(response["recovered" if photos else "applied"], 1)
                before = self.import_state()
                status, duplicate, _ = self.post(route, body, self.worker_cookie)
                self.assertEqual(status, 200, duplicate)
                self.assertTrue(duplicate["duplicate_file"])
                self.assertEqual(duplicate["release_code"], response["release_code"])
                self.assertEqual(self.import_state(), before)
                self.assertEqual(self.post(f"devices/{dev_id}/status", {"status": "LOST"}, self.admin_cookie)[0], 200)
                status, duplicate, _ = self.post(route, body, self.worker_cookie)
                self.assertEqual(status, 200, duplicate)
                self.assertTrue(duplicate["duplicate_file"])
                self.assertEqual(self.import_state(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
