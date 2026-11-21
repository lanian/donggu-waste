"""가상 SQLCipher DB에서 새 휴대폰 승인용 관리자 2단계 인증을 검증한다."""
import base64
import hmac
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import test_pc_labels as fixture
from dgpc import core, device_approval as approval, store, users


def otp(key, at):
    digest = hmac.digest(key, (at // 30).to_bytes(8, "big"), "sha1")
    start = digest[-1] % 16
    return "%06d" % ((int.from_bytes(digest[start:start + 4], "big") & 0x7fffffff) % 1000000)


class DeviceApprovalTests(unittest.TestCase):
    tearDown = fixture.NumberedLabelsTest.tearDown
    password = "synthetic-admin-password"
    owner = "synthetic-login-session"

    def setUp(self):
        fixture.NumberedLabelsTest.setUp(self)
        self.uid = users.create_user(self.con, "fixtureadmin", "가상 운영자", "ADMIN", self.password)
        self.at = 1_700_000_010
        self.enterContext(patch.object(approval, "time", side_effect=lambda: self.at))

    def start(self, proof=None, owner=None):
        result = approval.start_setup(self.con, self.uid, owner or self.owner, self.password, proof)
        parsed = urlsplit(result["uri"])
        self.assertEqual((parsed.scheme, parsed.netloc), ("otpauth", "totp"))
        params = parse_qs(parsed.query)
        self.assertEqual(params["issuer"], ["동구 수거"])
        self.assertIn("fixtureadmin", parsed.path)
        return base64.b32decode(params["secret"][0])

    def enroll(self):
        key = self.start()
        codes = approval.finish_setup(self.con, self.uid, self.owner, otp(key, self.at))["recovery_codes"]
        self.assertGreaterEqual(len(codes), 2)
        self.assertEqual(len(codes), len(set(codes)))
        return key, codes

    @staticmethod
    def pending(con, device_id):
        con.execute("INSERT INTO device(id,kex_pub,sign_pub,status,registered_at) VALUES(?,X'00',X'00','ACTIVE','2026-01-01')", (device_id,))

    def approve(self, device_id, code, password=None):
        return approval.approve_device(self.con, self.uid, device_id, self.password if password is None else password, code)

    def test_migration_preserves_legacy_login_and_requires_approval_on_new_enrollment(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, DG_STATE=folder):
            with patch.object(store, "LATEST", 17), patch.object(store, "MIGRATIONS", [m for m in store.MIGRATIONS if m[0] <= 17]):
                old = store.connect(create=True)
                try:
                    uid = users.create_user(old, "legacy", "가상 관리자", "ADMIN", self.password)
                finally:
                    old.close()
            con = store.connect()
            try:
                self.assertEqual(approval.status(con, uid), {"configured": False})
                self.assertEqual(users.check_login(con, "legacy", self.password)["id"], uid)
                self.pending(con, "new")
                with self.assertRaises(core.UserError):
                    approval.approve_device(con, uid, "new", self.password, "000000")
            finally:
                con.close()

    def test_enrollment_is_bound_to_authenticated_admin_session_and_password(self):
        with self.assertRaises(core.UserError):
            approval.start_setup(self.con, self.uid, self.owner, "wrong")
        key = self.start()
        with self.assertRaises(core.UserError):
            approval.finish_setup(self.con, self.uid, "another-session", otp(key, self.at))
        self.at += 301
        with self.assertRaises(core.UserError):
            approval.finish_setup(self.con, self.uid, self.owner, otp(key, self.at))
        key = self.start()
        users.set_password(self.con, self.uid, "replacement-password")
        with self.assertRaises(core.UserError):
            approval.finish_setup(self.con, self.uid, self.owner, otp(key, self.at))
        self.assertEqual(approval.status(self.con, self.uid), {"configured": False})
        worker = users.create_user(self.con, "fieldfixture", "가상 수거팀", "FIELD", self.password)
        with self.assertRaises(core.UserError):
            approval.start_setup(self.con, worker, self.owner, self.password)

    def test_setup_totp_cannot_approve_again_and_approval_is_atomic_with_device_write(self):
        key, _ = self.enroll()
        for device_id in ("first", "second", "third"):
            self.pending(self.con, device_id)
        self.assertEqual(approval.status(self.con, self.uid), {"configured": True})
        with self.assertRaises(core.UserError):
            self.approve("first", otp(key, self.at))
        self.at += 30
        proof = otp(key, self.at)
        with patch.object(approval, "confirm_device", side_effect=ValueError("기기 등록 실패")):
            with self.assertRaisesRegex(ValueError, "기기 등록 실패"):
                self.approve("first", proof)
        with self.assertRaises(core.UserError):
            self.approve("first", proof, "wrong")
        self.assertEqual(self.approve("first", proof), {"ok": True})
        with self.assertRaises(core.UserError):
            self.approve("second", proof)
        self.at += 90
        with self.assertRaises(core.UserError):
            self.approve("second", otp(key, self.at - 60))
        self.approve("second", otp(key, self.at - 30))
        self.approve("third", otp(key, self.at + 30))

    def test_approval_throttling_persists_across_connections_and_administrators_are_independent(self):
        key, _ = self.enroll()
        self.pending(self.con, "new")
        self.at += 30
        proof = otp(key, self.at)
        valid = {otp(key, self.at + delta) for delta in (-30, 0, 30)}
        wrong = next(f"{i:06}" for i in range(4) if f"{i:06}" not in valid)
        for _ in range(5):
            with self.assertRaises(core.UserError):
                self.approve("new", wrong)
        other = store.connect()
        try:
            with self.assertRaisesRegex(core.UserError, "5분|잠시"):
                approval.approve_device(other, self.uid, "new", self.password, proof)
            other_admin = users.create_user(other, "otheradmin", "가상 운영자2", "ADMIN", self.password)
            self.assertIn("uri", approval.start_setup(other, other_admin, "session2", self.password))
        finally:
            other.close()
        self.at += 301
        self.approve("new", otp(key, self.at))

    def test_recovery_rotates_only_after_confirmation_and_old_recovery_cannot_be_reused(self):
        key, codes = self.enroll()
        self.pending(self.con, "first")
        self.pending(self.con, "second")
        self.at += 30
        with self.assertRaises(core.UserError):
            self.start()
        with self.assertRaises(core.UserError):
            approval.start_setup(self.con, self.uid, self.owner, "wrong", codes[0])
        self.start(codes[0])
        self.approve("first", otp(key, self.at))
        self.at += 301
        with self.assertRaises(core.UserError):
            self.start(codes[0])
        replacement = self.start(codes[1])
        new_codes = approval.finish_setup(self.con, self.uid, self.owner, otp(replacement, self.at))["recovery_codes"]
        self.at += 30
        with self.assertRaises(core.UserError):
            self.start(codes[-1])
        self.assertNotIn(new_codes[0], codes)
        self.approve("second", otp(replacement, self.at))
        self.at += 30
        self.start(otp(replacement, self.at))
        fields = str(self.con.execute("SELECT * FROM admin_device_factor").fetchall())
        self.assertNotIn(new_codes[0], fields)
        audit = str(self.con.execute("SELECT detail FROM audit_log").fetchall())
        self.assertNotIn(new_codes[0], audit)
        self.assertNotIn(base64.b32encode(replacement).decode(), audit)

    def test_retry_lost_rotation_qr_uses_original_proof_in_same_session_only(self):
        key, codes = self.enroll()
        self.at += 30
        for kind in ("복구 코드", "현재 OTP"):
            with self.subTest(kind=kind):
                proof = codes[0] if kind == "복구 코드" else otp(key, self.at)
                replacement = self.start(proof)
                self.assertEqual(self.start(proof), replacement)
                with self.assertRaises(core.UserError):
                    self.start(owner=self.owner)
                with self.assertRaises(core.UserError):
                    self.start(proof, owner="different-session")
                self.at += 301
                with self.assertRaises(core.UserError):
                    self.start(proof)

    def test_simultaneous_approval_consumes_one_step_once(self):
        key, _ = self.enroll()
        self.pending(self.con, "first")
        self.pending(self.con, "second")
        self.at += 30
        barrier = Barrier(2)
        def attempt(device_id):
            connection = store.connect()
            try:
                barrier.wait(timeout=8)
                approval.approve_device(connection, self.uid, device_id, self.password, otp(key, self.at))
                return True
            except core.UserError:
                return False
            finally:
                connection.close()
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sorted(pool.map(attempt, ("first", "second"))), [False, True])

    def test_new_device_needs_approval_and_repeated_confirm_is_idempotent_for_admin_only(self):
        for device_id, state in (("old", "ACTIVE"), ("new", "ACTIVE"), ("lost", "LOST")):
            self.con.execute("""INSERT INTO device(id,kex_pub,sign_pub,status,registered_at,confirmed_at)
                              VALUES(?,X'00',X'00',?,'2026-01-01',?)""",
                             (device_id, state, "2026-01-01" if device_id == "old" else None))
        self.assertEqual(approval.approve_device(self.con, self.uid, "old", None, None), {"ok": True})
        with self.assertRaises(core.UserError):
            approval.approve_device(self.con, self.uid, "new", self.password, "111111")
        key, _ = self.enroll()
        self.at += 30
        code = otp(key, self.at)
        for missing in ("lost", "unknown"):
            with self.assertRaises(core.UserError):
                approval.approve_device(self.con, self.uid, missing, self.password, code)
        self.assertEqual(approval.approve_device(self.con, self.uid, "new", self.password, code), {"ok": True})
        self.assertEqual(approval.approve_device(self.con, self.uid, "new", None, None), {"ok": True})
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM audit_log WHERE entity='device' AND action='CONFIRM'").fetchone()[0], 1)
        self.con.execute("UPDATE app_user SET active=0 WHERE id=?", (self.uid,))
        with self.assertRaises(core.UserError):
            approval.approve_device(self.con, self.uid, "new", None, None)


if __name__ == "__main__":
    unittest.main()
