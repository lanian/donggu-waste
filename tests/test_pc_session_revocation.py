"""느린 HTTP 본문을 받는 동안 취소·변경된 세션의 저장 차단 회귀.

실행: python tests/test_pc_session_revocation.py
실제 SQLCipher와 임시 자료만 사용하며 이벤트로 인증 완료 시점을 확인한다.
"""
from __future__ import annotations

from contextlib import contextmanager
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import queue
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
import zlib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))
from dgpc import server as SV, service as S, store  # noqa: E402
import dgproto as P  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402


class SessionHTTPTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="dg-session-revocation-")
        self.old_env = {name: os.environ.get(name) for name in ("DG_STATE", "DG_OUT", "DG_DB_KEY")}
        os.environ.update(DG_STATE=self.directory.name, DG_OUT=str(Path(self.directory.name) / "out"),
                          DG_DB_KEY="session-revocation-test-only-not-production")
        self.old_server = {name: getattr(SV, name) for name in ("CON", "PORT", "LAN", "SESSIONS", "RPOOL", "FAILS", "IP_FAILS", "FAIL_AGG")}
        SV.RPOOL, SV.SESSIONS, SV.FAILS, SV.IP_FAILS, SV.FAIL_AGG = queue.LifoQueue(), {}, {}, {}, {}
        self.old_jobs = SV.OPS.JOBS
        SV.OPS.JOBS = SV.OPS.JobRunner()
        SV.LAN = None
        self.con = SV.CON = store.connect(create=True)
        self.assertTrue(store.ENCRYPTED, "이 회귀는 실제 SQLCipher를 사용해야 함")
        S.ensure_pc_keys(self.con)
        S.ACTOR.set("session_fixture")
        self.admin_id = S.create_user(self.con, "session_admin", "가상 관리자", "ADMIN", "session-admin-test-123")
        self.worker_id = S.create_user(self.con, "session_worker", "가상 접수", "INTAKE", "session-worker-test-123")
        self.body_ready = threading.Event()
        self.entered_identity = None
        self.response_identity = None
        self.job_ready, self.job_release = threading.Event(), threading.Event()
        owner = self

        class ObservedHandler(SV.Handler):
            def body(self):
                if self.headers.get("X-DG-Test-Body") == "wait" and getattr(self, "body_cache", None) is None:
                    owner.entered_identity = ((self.user or {}).get("user_id"), S.ACTOR.get())
                    owner.body_ready.set()
                return super().body()

            def job(self, kind, **kw):
                if self.headers.get("X-DG-Test-Job") == "wait":
                    owner.job_ready.set()
                    if not owner.job_release.wait(5):
                        raise RuntimeError("시험 작업 등록 경계 대기 초과")
                return super().job(kind, **kw)

            def send_json(self, obj, status=200, extra=None):
                if self.headers.get("X-DG-Test-Body") == "wait":
                    acquired = SV.LOCK.acquire(blocking=False)
                    if acquired:
                        SV.LOCK.release()
                    owner.response_identity = {
                        "user_id": (self.user or {}).get("user_id"), "actor": S.ACTOR.get(),
                        "response_outside_lock": acquired,
                    }
                return super().send_json(obj, status, extra)

        self.server = SV.Server(("127.0.0.1", 0), ObservedHandler)
        self.server.daemon_threads = False
        SV.PORT = self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.admin_cookie = self.login("session_admin", "session-admin-test-123")
        self.worker_cookie = self.login("session_worker", "session-worker-test-123")
        self.worker_token = self.worker_cookie.split("=", 1)[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        while not SV.RPOOL.empty():
            SV.RPOOL.get_nowait().close()
        self.con.close()
        SV.OPS.JOBS = self.old_jobs
        for name, value in self.old_server.items():
            setattr(SV, name, value)
        for name, value in self.old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.directory.cleanup()

    def post(self, path, body=None, cookie=None, extra_headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            headers = {"X-DG-CSRF": "1", "Content-Type": "application/json"}
            if cookie:
                headers["Cookie"] = cookie
            headers.update(extra_headers or {})
            data = body if isinstance(body, bytes) else None if body is None else json.dumps(body).encode("utf-8")
            connection.request("POST", "/api/" + path, body=data, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read()), response.getheader("Set-Cookie")
        finally:
            connection.close()

    def login(self, username, password):
        status, _, cookie = self.post("login", {"username": username, "password": password})
        self.assertEqual(status, 200)
        self.assertTrue(cookie and cookie.startswith("dg_s="))
        return cookie.split(";", 1)[0]

    def mutation_state(self):
        with SV.LOCK:
            return (self.con.execute("SELECT prefs_json FROM app_user WHERE id=?", (self.worker_id,)).fetchone()[0],
                    self.con.execute("SELECT COUNT(*) FROM request").fetchone()[0])

    @contextmanager
    def delayed(self, path="prefs", body=None):
        self.body_ready.clear()
        data = body if isinstance(body, bytes) else json.dumps(body or {"theme": "dark"}).encode("utf-8")
        first = max(1, len(data) // 2)
        raw = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        raw.sendall((f"POST /api/{path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                     f"Cookie: {self.worker_cookie}\r\nX-DG-CSRF: 1\r\nX-DG-Test-Body: wait\r\n"
                     f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode()
                    + data[:first])
        finished = False

        def finish():
            nonlocal finished
            finished = True
            raw.sendall(data[first:])
            response = http.client.HTTPResponse(raw)
            try:
                response.begin()
                return response.status, json.loads(response.read())
            finally:
                response.close()

        try:
            self.assertTrue(self.body_ready.wait(5), "인증을 마친 느린 본문 대기 지점에 도달하지 못함")
            self.assertEqual(self.entered_identity, (self.worker_id, "session_worker"))
            yield finish
        finally:
            try:
                if not finished:
                    finish()
            finally:
                raw.close()

    def assert_denied_without_mutation(self, finish, expected=401, flag="login"):
        before = self.mutation_state()
        status, body = finish()
        after = self.mutation_state()
        self.assertEqual((status, after), (expected, before), "대기 중 취소·변경된 세션의 저장이 실행됨")
        self.assertTrue(body.get(flag), body)
        self.assertTrue(self.response_identity["response_outside_lock"], "오류 응답을 쓰기 잠금 안에서 전송함")

    def request_body(self):
        fee = next(row["id"] for row in S.FEE_TABLE["rows"] if row["status"] == "CONFIRMED" and row.get("price", 0) > 0)
        return {"name": "가상 세션 접수", "phone": "01000000000", "address": "가상시 시험로 1",
                "planned_date": S.today_kst(), "items": [{"fee_rate_id": fee, "qty": 1}]}


class SessionRevocationTest(SessionHTTPTestCase):

    def test_account_deactivation_during_body_wait_prevents_write(self):
        with self.delayed() as finish:
            self.assertEqual(self.post(f"users/{self.worker_id}/active", {"active": False}, self.admin_cookie)[0], 200)
            self.assert_denied_without_mutation(finish)

    def test_password_reset_during_body_wait_prevents_write(self):
        with self.delayed() as finish:
            self.assertEqual(self.post(f"users/{self.worker_id}/password", {"password": "session-reset-test-123"}, self.admin_cookie)[0], 200)
            self.assert_denied_without_mutation(finish)

    def test_logout_during_body_wait_prevents_write(self):
        with self.delayed() as finish:
            self.assertEqual(self.post("logout", cookie=self.worker_cookie)[0], 200)
            self.assert_denied_without_mutation(finish)

    def test_idle_expiry_during_body_wait_prevents_write(self):
        with self.delayed() as finish:
            with SV.LOCK:
                SV.SESSIONS[self.worker_token]["exp"] = time.time() - 1
            self.assert_denied_without_mutation(finish)

    def test_absolute_expiry_during_body_wait_prevents_write(self):
        with self.delayed() as finish:
            with SV.LOCK:
                SV.SESSIONS[self.worker_token]["created"] = time.time() - SV.ABS_S - 1
            self.assert_denied_without_mutation(finish)

    def test_role_change_during_body_wait_rechecks_permission(self):
        with self.delayed("requests", self.request_body()) as finish:
            with SV.LOCK:
                SV.SESSIONS[self.worker_token]["role"] = "FIELD"
            before = self.mutation_state()
            status, body = finish()
            self.assertEqual((status, self.mutation_state()), (403, before))
            self.assertIn("권한", body.get("error", ""))
            self.assertTrue(self.response_identity["response_outside_lock"])

    def test_forced_password_change_during_body_wait_rechecks_restriction(self):
        with self.delayed("requests", self.request_body()) as finish:
            with SV.LOCK:
                SV.SESSIONS[self.worker_token]["must_change"] = True
            self.assert_denied_without_mutation(finish, 403, "must_change")

    def test_valid_slow_body_allows_other_writes_and_preserves_actor(self):
        with self.delayed() as finish:
            self.assertEqual(self.post("prefs", {"theme": "light"}, self.admin_cookie)[0], 200)
            status, body = finish()
            self.assertEqual(status, 200)
            self.assertEqual(body["theme"], "dark")
            self.assertEqual(json.loads(self.mutation_state()[0])["theme"], "dark")
            self.assertEqual(self.response_identity, {"user_id": self.worker_id, "actor": "session_worker", "response_outside_lock": True})


class UnlockedSessionRevocationTest(SessionHTTPTestCase):
    """유효 파일을 그대로 검증하며, 준비 완료 관찰만 이벤트로 감싼다."""

    def database_state(self):
        with SV.LOCK:
            return tuple(self.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                         for table in ("request", "payment", "result_import", "photo_import", "addr_point", "recon_import"))

    def promote_worker(self):
        # ADMIN 경로용 가상 계정 구성. 실제 저장 자료나 계정은 사용하지 않는다.
        with SV.LOCK, S.tx(self.con):
            self.con.execute("UPDATE app_user SET role='ADMIN' WHERE id=?", (self.worker_id,))
            SV.SESSIONS[self.worker_token]["role"] = "ADMIN"

    def signed_package(self, photos=False):
        registration = P.b45decode(S.registration_text(self.con))
        pc_kex = P.pub_from_raw(registration[69:134])
        kex, sign, device = P.gen_key(), P.gen_key(), uuid.uuid4().bytes
        data = device + P.pub_raw(kex) + P.pub_raw(sign)
        code = "DGD1." + base64.urlsafe_b64encode(data + hashlib.sha256(data).digest()[:4]).decode().rstrip("=")
        S.register_device(self.con, code, "가상 세션 폰", confirm=True)
        kind = S.TYPE_PHOTOS if photos else P.TYPE_RESULT
        payload = {"schema": "dg.photos/1", "photos": []} if photos else {"schema": "dg.result/1", "work_date": S.today_kst(), "events": []}
        header = P.Header(kind, uuid.uuid4().bytes, device, 1, 1, P.key_id(P.pub_raw(sign)))
        return P.seal(payload, header, pc_kex, sign)[0]

    def solo_package(self):
        password = "session-solo-test-pass"
        salt, iv = os.urandom(16), os.urandom(12)
        payload = {"format": "dg.backup/1", "state": {"mode": "SOLO", "requests": [], "events": []}, "photos": {}}
        compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
        plain = compressor.compress(json.dumps(payload).encode()) + compressor.flush()
        key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000, 32)
        return b"DGB1" + salt + iv + AESGCM(key).encrypt(iv, plain, b"DGB1"), {"X-DG-Pass": base64.b64encode(password.encode()).decode()}

    @contextmanager
    def preparing(self, path, body, prepare=None, headers=None):
        ready, release = threading.Event(), threading.Event()
        results, errors = [], []
        original = getattr(S, prepare) if prepare else None

        def observed(*args, **kwargs):
            result = original(*args, **kwargs)
            ready.set()
            if not release.wait(5):
                raise RuntimeError("시험 파일 검증 완료 경계 대기 초과")
            return result

        if prepare:
            guard = patch.object(S, prepare, observed)
        else:
            self.job_ready.clear()
            self.job_release.clear()
            ready, release = self.job_ready, self.job_release
            guard = patch.object(SV.OPS.JOBS, "start", lambda: None)  # 실제 submit/큐만 검사하고 작업자는 실행하지 않음.
        request_headers = {"X-DG-Test-Body": "wait", **(headers or {})}
        if not prepare:
            request_headers["X-DG-Test-Job"] = "wait"

        def request():
            try:
                results.append(self.post(path, body, self.worker_cookie, request_headers))
            except Exception as error:
                errors.append(error)

        def finish():
            release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive(), "해제한 요청이 끝나지 않음")
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 1)
            return results[0]

        with guard:
            thread = threading.Thread(target=request)
            thread.start()
            try:
                self.assertTrue(ready.wait(5), f"유효 파일/작업 경계에 도달하지 못함: {results} {errors}")
                yield finish
            finally:
                release.set()
                thread.join(5)

    def assert_import_revoked(self, path, body, prepare, headers=None):
        with self.preparing(path, body, prepare, headers) as finish:
            self.assertEqual(self.post(f"users/{self.worker_id}/active", {"active": False}, self.admin_cookie)[0], 200)
            before = self.database_state()
            status, response, _ = finish()
            self.assertEqual((status, self.database_state()), (401, before))
            self.assertTrue(response.get("login"), response)
            self.assertTrue(self.response_identity["response_outside_lock"])

    def test_result_preparation_then_deactivation_prevents_apply(self):
        self.assert_import_revoked("import/result", self.signed_package(), "prepare_result")

    def test_photos_preparation_then_deactivation_prevents_apply(self):
        self.assert_import_revoked("import/photos", self.signed_package(photos=True), "prepare_photos")

    def test_solo_preparation_then_deactivation_prevents_apply(self):
        body, headers = self.solo_package()
        self.assert_import_revoked("import/solo", body, "prepare_solo", headers)

    def test_address_preparation_then_deactivation_prevents_apply(self):
        self.promote_worker()
        body = "시군구명,도로명,건물본번,건물부번,위도,경도\n동구,가상 세션로,1,0,35.15,126.92\n".encode()
        self.assert_import_revoked("addr-import", body, "parse_addr_points")

    def assert_csv_body(self, path, body, revoke=False):
        before = self.database_state()
        with self.delayed(path, body) as finish:
            if revoke:
                self.assertEqual(self.post("logout", cookie=self.worker_cookie)[0], 200)
                status, response = finish()
                self.assertEqual((status, self.database_state()), (401, before))
                self.assertTrue(response.get("login"), response)
            else:
                done, saved = threading.Event(), []
                def other_save():
                    try:
                        saved.append(self.post("prefs", {"theme": "light"}, self.admin_cookie)[0])
                    finally:
                        done.set()
                thread = threading.Thread(target=other_save)
                thread.start()
                try:
                    self.assertTrue(done.wait(2), "느린 파일 본문이 다른 저장의 잠금을 붙잡음")
                    self.assertEqual(saved, [200])
                    self.assertEqual(finish()[0], 200)
                finally:
                    if not done.is_set():
                        finish()
                    thread.join(5)

    def test_request_csv_waiting_body_allows_other_writes(self):
        self.assert_csv_body("requests/import", S.import_template())

    def test_recon_csv_waiting_body_allows_other_writes(self):
        self.assert_csv_body("recon", "부과번호,금액,납부일\n".encode())

    def test_request_csv_logout_before_body_complete_prevents_apply(self):
        self.assert_csv_body("requests/import", S.import_template(), revoke=True)

    def test_recon_csv_logout_before_body_complete_prevents_apply(self):
        self.assert_csv_body("recon", "부과번호,금액,납부일\n".encode(), revoke=True)

    def test_all_job_routes_logout_before_submission_prevents_queue_entry(self):
        self.promote_worker()
        for path in ("backup", "backup/auto", "backup/retention", "privacy/purge", "photo-purge", "refunds/sync", "integrity/full"):
            with self.subTest(path=path):
                with self.preparing(path, {"passphrase": "session-backup-test-pass"}) as finish:
                    self.assertEqual(self.post("logout", cookie=self.worker_cookie)[0], 200)
                    before = (len(SV.OPS.JOBS.jobs), SV.OPS.JOBS.q.qsize())
                    status, response, _ = finish()
                    self.assertEqual((status, len(SV.OPS.JOBS.jobs), SV.OPS.JOBS.q.qsize()), (401, *before))
                    self.assertTrue(response.get("login"), response)
            self.worker_cookie = self.login("session_worker", "session-worker-test-123")
            self.worker_token = self.worker_cookie.split("=", 1)[1]

    def test_team_change_after_result_preparation_rechecks_scope(self):
        raw = self.signed_package()
        device = str(uuid.UUID(bytes=P.Header.unpack(raw).device_id))
        with SV.LOCK:
            first = S.save_crew(self.con, {"name": "가상 원래팀", "device_id": device})
            second = S.save_crew(self.con, {"name": "가상 새팀"})
            self.con.execute("UPDATE app_user SET role='FIELD',crew_id=? WHERE id=?", (first, self.worker_id))
            SV.SESSIONS[self.worker_token].update(role="FIELD", crew_id=first)
        with self.preparing("import/result", raw, "prepare_result") as finish:
            self.assertEqual(self.post(f"users/{self.worker_id}/crew", {"crew_id": second}, self.admin_cookie)[0], 200)
            before = self.database_state()
            status, response, _ = finish()
            self.assertEqual((status, self.database_state()), (400, before))
            self.assertIn("내 팀", response.get("error", ""))

    def test_valid_prepared_imports_and_job_registration_still_work(self):
        solo, solo_headers = self.solo_package()
        cases = [("import/result", self.signed_package(), "prepare_result", None),
                 ("import/photos", self.signed_package(photos=True), "prepare_photos", None),
                 ("import/solo", solo, "prepare_solo", solo_headers),
                 ("addr-import", "시군구명,도로명,건물본번,건물부번,위도,경도\n동구,가상 세션로,1,0,35.15,126.92\n".encode(), "parse_addr_points", None)]
        for path, body, prepare, headers in cases:
            if path == "addr-import":
                self.promote_worker()
            with self.subTest(path=path), self.preparing(path, body, prepare, headers) as finish:
                self.assertEqual(self.post("prefs", {"theme": "light"}, self.admin_cookie)[0], 200)
                before = self.database_state()
                self.assertEqual(finish()[0], 200)
                self.assertGreater(sum(self.database_state()), sum(before))
        with self.preparing("backup", {"passphrase": "session-backup-test-pass"}) as finish:
            status, response, _ = finish()
            self.assertEqual(status, 200)
            self.assertEqual(response["status"], "QUEUED")
            self.assertEqual(SV.OPS.JOBS.q.qsize(), 1)
            self.assertEqual(SV.OPS.JOBS.jobs[response["job_id"]]["actor"], "session_worker")


if __name__ == "__main__":
    unittest.main(verbosity=2)
