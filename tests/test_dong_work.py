"""동별 계정의 실제 SQLCipher/HTTP 읽기·변경·출력 권한 회귀.

실행: .venv/Scripts/python.exe tests/test_dong_work.py
가상 A/B/C 동과 임시 암호화 DB만 사용한다. 외부 경로 서비스는 호출하지 않는다.
"""
from __future__ import annotations

import http.client
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pc_session_revocation import SessionHTTPTestCase
from dgpc import area as A, dong_work as DW, server as SV, service as S, store, users as U


class DongWorkHTTPTest(SessionHTTPTestCase):
    def setUp(self):
        super().setUp()
        self.day = "2026-11-09"
        self.a = A.save_dong(self.con, {"name": "가상A동"})
        self.b = A.save_dong(self.con, {"name": "가상B동"})
        self.c = A.save_dong(self.con, {"name": "가상C동"})
        self.crew = S.save_crew(self.con, {"name": "가상1팀"})
        self.other_crew = S.save_crew(self.con, {"name": "가상2팀"})
        self.ra = S.create_request(self.con, self.payload(self.a))["id"]
        self.rb = S.create_request(self.con, self.payload(self.b))["id"]
        self.rc = S.create_request(self.con, self.payload(self.c))["id"]
        self.no_dong = S.create_request(self.con, {**self.payload(self.a), "dong": "", "address": "가상시 미지정로 10"})["id"]

    def payload(self, did=None, **values):
        body = self.request_body()
        body.update(planned_date=self.day, dong_id=did)
        if did:
            body["dong"] = {self.a: "가상A동", self.b: "가상B동", self.c: "가상C동"}.get(did, "가상A동")
        body.update(values)
        return body

    def get(self, path, cookie=None, raw=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        try:
            connection.request("GET", path if path.startswith("/") else "/api/" + path,
                               headers={"X-DG-CSRF": "1", "Cookie": cookie or self.admin_cookie})
            response = connection.getresponse()
            data = response.read()
            return response.status, data if raw else json.loads(data)
        finally:
            connection.close()

    @contextmanager
    def waiting_post(self, path, body, cookie):
        self.body_ready.clear()
        data = json.dumps(body).encode()
        first = max(1, len(data) // 2)
        raw = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        raw.sendall((f"POST /api/{path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nCookie: {cookie}\r\n"
                     f"X-DG-CSRF: 1\r\nX-DG-Test-Body: wait\r\nContent-Type: application/json\r\n"
                     f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode() + data[:first])
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
            self.assertTrue(self.body_ready.wait(5))
            yield finish
        finally:
            if not finished:
                finish()
            raw.close()

    def account(self, name="dong_a", dongs=None, role="DONG"):
        status, body, _ = self.post("users", {"username": name, "display_name": "가상 동 담당자", "role": role,
                                  "password": "dong-test-password-123", "dong_ids": dongs if dongs is not None else [self.a]}, self.admin_cookie)
        self.assertEqual(status, 200, body)
        uid = body["id"]
        # 임시 암호 변경은 기존 계약대로 처리한다.
        with SV.LOCK:
            self.con.execute("UPDATE app_user SET must_change=0 WHERE id=?", (uid,))
        return uid, self.login(name, "dong-test-password-123")

    def change_dongs(self, uid, dongs):
        return self.post(f"users/{uid}/dongs", {"dong_ids": dongs}, self.admin_cookie)

    def basis(self, cookie, dong="all", crew=None):
        status, out, _ = self.post("dong-work/route", {"date": self.day, "dong": dong, "crew_ids": [crew or self.crew]}, cookie)
        self.assertEqual(status, 200, out)
        return out

    def test_scoped_user_creation_and_existing_password_prefs_survive_migration(self):
        before = S.one(self.con, "SELECT * FROM app_user WHERE id=?", (self.admin_id,))
        S.save_prefs(self.con, self.admin_id, {"theme": "dark", "font": 17})
        self.assertIn("DONG", U.ROLES)
        uid, _ = self.account()
        self.assertEqual(U.user_dong_ids(self.con, uid), [self.a])
        self.assertEqual(store.run_migrations(self.con), [])
        after = S.one(self.con, "SELECT * FROM app_user WHERE id=?", (self.admin_id,))
        for field in ("id", "pw_salt", "pw_hash", "role", "created_at", "crew_id"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(S.user_prefs(self.con, self.admin_id), {"theme": "dark", "font": 17})
        self.assertEqual(self.con.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertTrue(S.check_login(self.con, "session_admin", "session-admin-test-123"))

    def test_single_multi_and_global_dong_selection(self):
        _, single = self.account()
        _, multi = self.account("dong_ab", [self.a, self.b])
        for cookie, expected in ((single, {self.ra}), (multi, {self.ra, self.rb}),
                                 (self.admin_cookie, {self.ra, self.rb, self.rc, self.no_dong})):
            status, result = self.get(f"dong-work/requests?date={self.day}&dong=all&bucket=day", cookie)
            self.assertEqual(status, 200, result)
            self.assertEqual({row["id"] for row in result["rows"]}, expected)
            self.assertLessEqual(result["size"], 50)
        self.assertEqual(self.get(f"dong-work?date={self.day}&dong={self.b}", single)[0], 403)
        self.assertEqual(self.get(f"dong-work?date={self.day}&dong=-", single)[0], 403)
        self.assertEqual(self.get(f"dong-work/requests?date={self.day}&dong=-", self.admin_cookie)[1]["total"], 1)

    def test_empty_scope_fail_closed_and_active_empty_assignment_rejected(self):
        uid, cookie = self.account()
        self.assertEqual(self.change_dongs(uid, [])[0], 400)
        with SV.LOCK:
            self.con.execute("DELETE FROM app_user_dong WHERE user_id=?", (uid,))
        status, result = self.get(f"dong-work/requests?date={self.day}&dong=all", cookie)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["rows"], [])
        self.assertEqual(self.post("dong-work/requests", self.payload(self.b), cookie)[0], 403)
        self.assertEqual(self.post("dong-work/route", {"date": self.day, "dong": "all", "crew_ids": [self.crew]}, cookie)[0], 403)

    def test_bootstrap_is_sanitized_and_every_legacy_api_fails_closed(self):
        _, cookie = self.account()
        status, boot = self.get("bootstrap", cookie)
        self.assertEqual(status, 200, boot)
        self.assertEqual(boot["me"]["dong_ids"], [self.a])
        self.assertEqual({d["id"] for d in boot["dongs"]}, {self.a})
        self.assertFalse(boot.get("devices"))
        self.assertFalse(boot.get("audit_entities"))
        self.assertNotIn("external", boot["settings"])
        self.assertTrue(all("device_id" not in c for c in boot["crews"]))
        allowed = {("GET", "bootstrap"), ("GET", "me"), ("POST", "prefs"), ("POST", "password")}
        for route in SV.ROUTES:
            if route.pattern.startswith("dong-work") or (route.method, route.pattern) in allowed:
                continue
            path = route.pattern.replace("{id}", self.rc)
            with self.subTest(method=route.method, path=path):
                status = self.get(path, cookie)[0] if route.method == "GET" else self.post(path, None, cookie)[0]
                self.assertEqual(status, 403)
        for path in ("/bundle/12345678", "/print/batch/12345678", "/qrtest/12345678", "/print/doc/fake", "/print/requests/fake"):
            self.assertEqual(self.get(path, cookie)[0], 403, path)
        self.assertEqual(self.get("me", cookie)[0], 200)
        self.assertEqual(self.post("prefs", {"theme": "dark"}, cookie)[0], 200)

    def test_detail_create_move_cancel_and_idempotency_recheck_scope(self):
        uid, cookie = self.account("dong_ab", [self.a, self.b])
        self.assertEqual(self.get(f"dong-work/requests/{self.rc}", cookie)[0], 403)
        self.assertEqual(self.post("dong-work/requests", self.payload(self.c), cookie)[0], 403)
        payload = self.payload(self.a, dong="가상C동", client_req_id="same-create")
        status, created, _ = self.post("dong-work/requests", payload, cookie)
        self.assertEqual(status, 200, created)
        self.assertEqual(created["dong"], "가상A동")
        self.assertEqual(self.post("dong-work/requests", payload, cookie)[1]["id"], created["id"])
        detail = self.get(f"dong-work/requests/{created['id']}", cookie)[1]
        update = self.payload(self.b, req_ver=detail["req_ver"], reason="가상 동 이동", items=detail["items"])
        self.assertEqual(self.post(f"dong-work/requests/{created['id']}", update, cookie)[0], 200)
        detail = self.get(f"dong-work/requests/{created['id']}", cookie)[1]
        self.assertEqual(detail["dong"], "가상B동")
        self.assertEqual(self.post(f"dong-work/requests/{created['id']}/cancel", {"req_ver": detail["req_ver"], "code": "OTHER", "reason": "가상 취소"}, cookie)[0], 200)
        self.assertEqual(self.change_dongs(uid, [self.b])[0], 200)
        self.assertEqual(self.post("dong-work/requests", self.payload(self.b, client_req_id="same-create"), cookie)[0], 200)
        self.assertEqual(self.post(f"dong-work/requests/{self.ra}", self.payload(self.b, req_ver=1, reason="외부 이동"), cookie)[0], 403)

    def test_cached_receipt_cannot_reveal_request_after_dong_scope_removed(self):
        uid, cookie = self.account("dong_ab", [self.a, self.b])
        body = self.payload(self.a, client_req_id="previous-a-receipt")
        status, created, _ = self.post("dong-work/requests", body, cookie)
        self.assertEqual(status, 200, created)
        self.assertEqual(self.change_dongs(uid, [self.b])[0], 200)
        count = self.con.execute("SELECT COUNT(*) FROM request").fetchone()[0]
        status, result, _ = self.post("dong-work/requests", {**body, "dong_id": self.b}, cookie)
        self.assertEqual(status, 403, result)
        self.assertNotIn(created["id"], json.dumps(result))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM request").fetchone()[0], count)

    def test_public_local_geocode_and_duplicate_candidates_remain_scoped(self):
        _, cookie = self.account()
        with SV.LOCK:
            self.con.execute("INSERT INTO addr_point (sigungu,road,main,sub,lat,lon,dong_h) VALUES (?,?,?,?,?,?,?)",
                             ("가상구", "시험로", 1, 0, 35.0, 126.0, "가상A동"))
        status, point = self.get(f"dong-work/geocode?dong={self.a}&address=" + __import__("urllib.parse", fromlist=["quote"]).quote("가상구 시험로 1"), cookie)
        self.assertEqual(status, 200, point)
        self.assertEqual((point["lat"], point["lon"]), (35.0, 126.0))
        self.assertEqual(self.get(f"dong-work/geocode?dong={self.b}&address=none", cookie)[0], 403)
        self.assertEqual(self.get(f"dong-work/geocode?dong={self.a}&address=none", cookie), (200, {"none": True}))
        status, candidates = self.get("dong-work/duplicates?phone=01000000000", cookie)
        self.assertEqual(status, 200, candidates)
        self.assertEqual({r["id"] for r in candidates}, {self.ra})
        self.assertEqual(self.get(f"dong-work/duplicates?phone=01000000000&dong={self.b}", cookie)[0], 403)

    def test_current_db_role_removes_cached_global_and_raw_permissions(self):
        # 열려 있는 기존 전역 세션도 DB의 새 역할을 읽고 즉시 전역 API와 출력을 거부한다.
        with SV.LOCK:
            self.con.execute("UPDATE app_user SET role='DONG' WHERE id=?", (self.worker_id,))
            self.con.execute("INSERT INTO app_user_dong (user_id,dong_id) VALUES (?,?)", (self.worker_id, self.a))
        self.assertEqual(self.get("requests", self.worker_cookie)[0], 403)
        self.assertEqual(self.get("/print/batch/12345678", self.worker_cookie)[0], 403)
        self.assertEqual(self.get(f"dong-work/requests?date={self.day}", self.worker_cookie)[1]["total"], 1)

    def test_rename_keeps_id_scope_and_mapped_dong_cannot_be_deleted(self):
        uid, cookie = self.account()
        A.save_dong(self.con, {"id": self.a, "name": "가상A새이름동"})
        status, detail = self.get(f"dong-work/requests/{self.ra}", cookie)
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail["dong"], "가상A새이름동")
        self.assertEqual(U.user_dong_ids(self.con, uid), [self.a])
        self.assertEqual(self.change_dongs(uid, [self.c])[0], 200)
        # 자료가 없는 동도 담당 연결이 있으면 삭제하지 않는다.
        empty = A.save_dong(self.con, {"name": "가상빈동"})
        self.assertEqual(self.change_dongs(uid, [empty])[0], 200)
        with self.assertRaises(S.UserError):
            A.delete_dong(self.con, empty)

    def test_summary_csv_and_buckets_never_include_foreign_dong(self):
        _, cookie = self.account()
        with SV.LOCK:
            self.con.execute("UPDATE request SET planned_date='2026-11-01' WHERE id=?", (self.ra,))
        status, overview = self.get(f"dong-work?date={self.day}&dong=all", cookie)
        self.assertEqual(status, 200, overview)
        self.assertEqual(overview["summary"]["overdue"], 1)
        self.assertEqual(overview["summary"]["scheduled"], 0)
        self.assertEqual({r["id"] for r in overview["rows"]}, {self.a})
        status, listing = self.get(f"dong-work/requests?date={self.day}&dong=all&bucket=overdue", cookie)
        self.assertEqual((status, [r["id"] for r in listing["rows"]]), (200, [self.ra]))
        status, csv = self.get(f"dong-work/summary.csv?date={self.day}&dong=all", cookie, raw=True)
        text = csv.decode("utf-8-sig")
        self.assertEqual(status, 200)
        self.assertIn("가상A동", text)
        for forbidden in ("가상B동", "가상C동", "01000000000", "가상시 시험로"):
            self.assertNotIn(forbidden, text)

    def test_done_bucket_contains_only_selected_day_completed_requests_in_scope(self):
        _, cookie = self.account()
        open_id = S.create_request(self.con, self.payload(self.a))["id"]
        past_id = S.create_request(self.con, self.payload(self.a, planned_date="2026-11-08"))["id"]
        with SV.LOCK:
            self.con.execute("UPDATE request SET status='DONE' WHERE id IN (?,?,?)", (self.ra, self.rb, past_id))
        status, listing = self.get(f"dong-work/requests?date={self.day}&dong=all&bucket=done", cookie)
        self.assertEqual(status, 200, listing)
        self.assertEqual([r["id"] for r in listing["rows"]], [self.ra])
        self.assertNotIn(open_id, json.dumps(listing))
        overview = self.get(f"dong-work?date={self.day}&dong=all", cookie)[1]
        self.assertEqual(overview["summary"]["done"], 1)
        self.assertEqual(overview["summary"]["scheduled"], 2)

    def test_lead_is_read_only_and_field_is_denied(self):
        _, lead = self.account("dong_lead", role="LEAD")
        _, field = self.account("dong_field", role="FIELD")
        self.assertEqual(self.get(f"dong-work?date={self.day}", lead)[0], 200)
        self.assertEqual(self.post("dong-work/route", {"date": self.day, "dong": "all", "crew_ids": [self.crew]}, lead)[0], 403)
        self.assertEqual(self.post("dong-work/requests", self.payload(self.a), lead)[0], 403)
        self.assertEqual(self.get(f"dong-work?date={self.day}", field)[0], 403)

    def test_scope_revocation_while_post_body_waits_prevents_create(self):
        uid, cookie = self.account()
        with self.waiting_post("dong-work/requests", self.payload(self.a), cookie) as finish:
            self.assertEqual(self.change_dongs(uid, [self.b])[0], 200)
            count = self.con.execute("SELECT COUNT(*) FROM request").fetchone()[0]
            self.assertEqual(finish()[0], 403)
            self.assertEqual(self.con.execute("SELECT COUNT(*) FROM request").fetchone()[0], count)

    def test_route_basis_scope_identity_date_and_batch_versions(self):
        uid, cookie = self.account()
        route = self.basis(cookie)
        self.assertEqual(set(route["requests"]), {self.ra})
        plan = {self.crew: [{"request_id": self.ra, "eta": "10:00"}]}
        payload = {"date": self.day, "dong": "all", "plan": plan, "basis": route["basis"], "client_req_id": "assign-once"}
        status, assigned, _ = self.post("dong-work/assign", payload, cookie)
        self.assertEqual(status, 200, assigned)
        self.assertEqual(self.post("dong-work/assign", payload, cookie)[1], assigned)
        self.assertEqual(self.post("dong-work/assign", {**payload, "client_req_id": "new-stale"}, cookie)[0], 409)
        self.assertEqual(self.post("dong-work/assign", {**payload, "date": "2026-11-10"}, cookie)[0], 409)
        self.assertEqual(self.post("dong-work/assign", payload, self.admin_cookie)[0], 409)
        fresh = self.basis(cookie)
        self.assertEqual(self.change_dongs(uid, [self.a, self.b])[0], 200)
        self.assertEqual(self.post("dong-work/assign", {**payload, "basis": fresh["basis"], "client_req_id": "changed-scope"}, cookie)[0], 409)

    def test_foreign_or_wrong_date_plan_and_mixed_batch_rejected_without_mutation(self):
        _, cookie = self.account()
        route = self.basis(cookie)
        for rid in (self.rb, self.rc):
            payload = {"date": self.day, "dong": "all", "plan": {self.crew: [{"request_id": rid}]}, "basis": route["basis"]}
            self.assertIn(self.post("dong-work/assign", payload, cookie)[0], (403, 409))
        with SV.LOCK:
            S.confirm_plan(self.con, self.day, {self.crew: [{"request_id": self.ra}, {"request_id": self.rb}]})
        status, current = self.get(f"dong-work/assignments?date={self.day}&dong=all", cookie)
        self.assertEqual(status, 200, current)
        self.assertTrue(current[0]["shared"])
        self.assertEqual([s["request_id"] for s in current[0]["stops"]], [self.ra])
        before = list(self.con.execute("SELECT * FROM assignment_batch").fetchall())
        route = self.basis(cookie)
        payload = {"date": self.day, "dong": "all", "plan": {self.crew: [{"request_id": self.ra}]}, "basis": route["basis"]}
        self.assertEqual(self.post("dong-work/assign", payload, cookie)[0], 409)
        self.assertEqual(self.con.execute("SELECT * FROM assignment_batch").fetchall(), before)
        # 전체 계정도 한 동 필터에서 다른 동 배정을 통째로 덮어쓸 수 없다.
        global_route = self.basis(self.admin_cookie, self.a)
        self.assertEqual(self.post("dong-work/assign", {**payload, "dong": self.a, "basis": global_route["basis"]}, self.admin_cookie)[0], 409)

    def test_route_result_request_version_and_planned_date_are_rechecked(self):
        _, cookie = self.account()
        route = self.basis(cookie)
        payload = {"date": self.day, "dong": "all", "plan": {self.crew: [{"request_id": self.ra}]}, "basis": route["basis"]}
        for change in ("planned_date='2026-11-10'", "planned_date='2026-11-09',req_ver=req_ver+1"):
            with SV.LOCK:
                self.con.execute("UPDATE request SET " + change + " WHERE id=?", (self.ra,))
            self.assertEqual(self.post("dong-work/assign", payload, cookie)[0], 409)
            self.assertEqual(self.con.execute("SELECT COUNT(*) FROM assignment_batch").fetchone()[0], 0)

    def test_cancel_and_move_foreign_requests_do_not_change_any_row(self):
        _, cookie = self.account()
        before = self.con.execute("SELECT * FROM request WHERE id=?", (self.rb,)).fetchone()
        self.assertEqual(self.post(f"dong-work/requests/{self.rb}/cancel", {"req_ver": 1, "code": "OTHER", "reason": "가상 거부"}, cookie)[0], 403)
        self.assertEqual(self.post(f"dong-work/requests/{self.rb}", self.payload(self.a, req_ver=1, reason="가상 이동"), cookie)[0], 403)
        self.assertEqual(self.post(f"dong-work/requests/{self.ra}", self.payload(self.b, req_ver=1, reason="가상 이동"), cookie)[0], 403)
        self.assertEqual(self.con.execute("SELECT * FROM request WHERE id=?", (self.rb,)).fetchone(), before)

    def test_scoped_create_and_update_warnings_do_not_expose_global_bookings(self):
        _, cookie = self.account()
        A.save_capacity(self.con, {"daily_count": 1, "daily_m3": 0.5})
        status, created, _ = self.post("dong-work/requests", self.payload(self.a), cookie)
        self.assertEqual(status, 200, created)
        self.assertFalse(any("전체" in text or "이미 4건" in text for text in created["warnings"]), created)
        detail = self.get(f"dong-work/requests/{created['id']}", cookie)[1]
        status, updated, _ = self.post(f"dong-work/requests/{created['id']}", self.payload(self.a, items=detail["items"], req_ver=detail["req_ver"], reason="가상 예정일 유지"), cookie)
        self.assertEqual(status, 200, updated)
        self.assertFalse(any("전체" in text for text in updated["warnings"]), updated)

    def test_read_check_and_detail_form_a_single_authorized_snapshot(self):
        _, cookie = self.account()
        ready, release, changed = threading.Event(), threading.Event(), threading.Event()
        original = DW.require_request
        result, mutation = [], []
        def paused(*args, **kwargs):
            out = original(*args, **kwargs)
            ready.set()
            if not release.wait(5):
                raise RuntimeError("가상 상세 읽기 대기 초과")
            return out
        def move():
            mutation.append(self.post(f"requests/{self.ra}", self.payload(self.c, req_ver=1, reason="가상 이관", memo="가상C 전용 새 메모"), self.admin_cookie))
            changed.set()
        with patch.object(DW, "require_request", paused):
            reader = threading.Thread(target=lambda: result.append(self.get(f"dong-work/requests/{self.ra}", cookie)))
            reader.start()
            self.assertTrue(ready.wait(5))
            writer = threading.Thread(target=move)
            writer.start()
            changed.wait(2)
            release.set()
            reader.join(6)
            writer.join(6)
        self.assertEqual(mutation[0][0], 200, mutation)
        self.assertNotIn("가상C 전용 새 메모", json.dumps(result, ensure_ascii=False))
        if result[0][0] == 200:
            self.assertEqual(result[0][1]["dong"], "가상A동")
        else:
            self.assertIn(result[0][0], (403, 409))
        self.assertEqual(self.get(f"dong-work/requests/{self.ra}", cookie)[0], 403)

    def test_list_scope_check_and_resident_rows_are_serialized_with_scope_change(self):
        uid, cookie = self.account()
        ready, release, changed = threading.Event(), threading.Event(), threading.Event()
        original = S.search_requests
        result, mutation = [], []
        def paused(*args, **kwargs):
            ready.set()
            if not release.wait(5):
                raise RuntimeError("가상 목록 읽기 대기 초과")
            return original(*args, **kwargs)
        def revoke():
            mutation.append(self.change_dongs(uid, [self.b]))
            changed.set()
        with patch.object(S, "search_requests", paused):
            reader = threading.Thread(target=lambda: result.append(self.get(f"dong-work/requests?date={self.day}", cookie)))
            reader.start()
            self.assertTrue(ready.wait(5))
            writer = threading.Thread(target=revoke)
            writer.start()
            changed_before_read = changed.wait(2)
            release.set()
            reader.join(6)
            writer.join(6)
        self.assertEqual(mutation[0][0], 200, mutation)
        if changed_before_read:
            self.assertNotIn(self.ra, json.dumps(result))
        self.assertEqual({r["id"] for r in self.get(f"dong-work/requests?date={self.day}", cookie)[1]["rows"]}, {self.rb})

    def test_scope_revoked_during_route_does_not_return_old_resident_data(self):
        uid, cookie = self.account()
        ready, release = threading.Event(), threading.Event()
        original = SV.routing.plan
        def paused(*args, **kwargs):
            ready.set()
            if not release.wait(5):
                raise RuntimeError("가상 노선 계산 대기 초과")
            return original(*args, **kwargs)
        result = []
        with patch.object(SV.routing, "plan", paused):
            thread = threading.Thread(target=lambda: result.append(self.post("dong-work/route", {"date": self.day, "dong": "all", "crew_ids": [self.crew]}, cookie)))
            thread.start()
            self.assertTrue(ready.wait(5))
            self.assertEqual(self.change_dongs(uid, [self.b])[0], 200)
            release.set()
            thread.join(6)
        self.assertEqual(result[0][0], 409, result)
        self.assertNotIn(self.ra, json.dumps(result))


class DongUserMigrationTest(unittest.TestCase):
    def test_real_v7_upgrade_preserves_every_account_column_business_rows_keys_and_audit(self):
        with tempfile.TemporaryDirectory(prefix="dg-dong-migration-") as directory:
            with patch.dict(os.environ, DG_STATE=directory, DG_OUT=str(Path(directory) / "out"), DG_DB_KEY="dong-migration-test-only"):
                with patch.object(store, "LATEST", 7), patch.object(store, "MIGRATIONS", [m for m in store.MIGRATIONS if m[0] <= 7]):
                    con = store.connect(create=True)
                self.assertTrue(store.ENCRYPTED)
                S.ensure_pc_keys(con)
                crew = S.save_crew(con, {"name": "가상 이전팀"})
                salt = b"migration-salt-01"
                con.execute("""INSERT INTO app_user (id,username,display_name,role,pw_salt,pw_hash,active,must_change,created_at,last_login_at,crew_id,prefs_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", ("old-admin", "old_admin", "가상 이전 관리자", "ADMIN", salt.hex(), U._pw_hash("old-test-password", salt),
                                                         1, 0, "2020-01-01T00:00:00Z", "2026-11-01T00:00:00Z", crew, '{"theme":"dark","font":17}'))
                fee = next(row["id"] for row in S.FEE_TABLE["rows"] if row["status"] == "CONFIRMED" and row.get("price", 0) > 0)
                S.create_request(con, {"name": "가상 이전 접수", "phone": "01000000000", "address": "가상구 시험로 1", "items": [{"fee_rate_id": fee, "qty": 1}]})
                before = {table: con.execute("SELECT * FROM " + table).fetchall() for table in ("app_user", "request", "pc_key", "audit_log")}
                con.close()
                con = store.connect()
                try:
                    self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], store.LATEST)
                    self.assertEqual(store.run_migrations(con), [])
                    for table, values in before.items():
                        self.assertEqual(con.execute("SELECT * FROM " + table).fetchall(), values, table)
                    self.assertEqual(S.user_prefs(con, "old-admin"), {"theme": "dark", "font": 17})
                    self.assertTrue(S.check_login(con, "old_admin", "old-test-password"))
                    self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
                    U.create_user(con, "new_dong", "가상 동 담당자", "DONG", "new-test-password", dong_ids=[A.save_dong(con, {"name": "가상 이전동"})])
                finally:
                    con.close()

    def test_v8_migration_failure_rolls_back_role_table_and_scope_mapping(self):
        with tempfile.TemporaryDirectory(prefix="dg-dong-rollback-") as directory:
            with patch.dict(os.environ, DG_STATE=directory, DG_OUT=str(Path(directory) / "out"), DG_DB_KEY="dong-migration-test-only"):
                with patch.object(store, "LATEST", 7), patch.object(store, "MIGRATIONS", [m for m in store.MIGRATIONS if m[0] <= 7]):
                    con = store.connect(create=True)
                try:
                    old_sql = con.execute("SELECT sql FROM sqlite_master WHERE name='app_user'").fetchone()[0]
                    with patch.object(store, "_TEST_FAIL_STEP", 8), self.assertRaises(RuntimeError):
                        store.run_migrations(con)
                    self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 7)
                    self.assertEqual(con.execute("SELECT sql FROM sqlite_master WHERE name='app_user'").fetchone()[0], old_sql)
                    self.assertIsNone(con.execute("SELECT 1 FROM sqlite_master WHERE name='app_user_dong'").fetchone())
                    self.assertEqual(store.run_migrations(con), list(range(8, store.LATEST + 1)))
                finally:
                    con.close()


if __name__ == "__main__":
    unittest.main()
