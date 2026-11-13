"""VWorld 참고 조회: 실제 암호화 DB/HTTP, 키 보호·비저장·권한·잠금 회귀."""
from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import unittest
from unittest.mock import patch

from test_pc_session_revocation import SessionHTTPTestCase, S, SV
from vworld_fixture import FAKE_KEY, fake_vworld


class VWorldTest(SessionHTTPTestCase):
    def setUp(self):
        super().setUp()
        self.provider = fake_vworld()
        self.fake, url = self.provider.__enter__()
        self.environment = patch.dict(os.environ, {"DG_VWORLD_BASE": url})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.provider.__exit__(None, None, None)
        super().tearDown()

    def configure(self, enabled=True, domain="http://127.0.0.1:8765"):
        return self.post("settings", {"external": {"keys": {"vworld_api_key": FAKE_KEY},
                         "vworld": {"enabled": enabled, "domain": domain}}}, self.admin_cookie)

    def locate(self, address="광주광역시 동구 가상시험로 1", **kw):
        return self.post("vworld/locate", {"address": address, "type": "ROAD", **kw}, self.worker_cookie)

    def snapshot(self):
        tables = [r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: sorted((tuple(row) for row in self.con.execute('SELECT * FROM "' + name + '"')), key=repr)
                for name in tables}

    def test_default_disabled_and_key_required_never_contact_provider(self):
        status, data, _ = self.locate()
        self.assertEqual(status, 200)
        self.assertEqual(data["code"], "NOT_CONFIGURED")
        status, _, _ = self.post("settings", {"external": {"vworld": {"enabled": True}}}, self.admin_cookie)
        self.assertEqual(status, 400)
        self.assertEqual(self.fake["calls"], [])

    def test_settings_mask_key_and_clearing_key_disables_lookup(self):
        self.assertEqual(self.configure()[0], 200)
        settings = S.settings(self.con)["external"]
        self.assertTrue(settings["keys"]["vworld_api_key"]["set"])
        self.assertNotIn(FAKE_KEY, json.dumps(settings))
        self.assertTrue(settings["vworld"]["enabled"])
        self.assertEqual(self.post("settings", {"external": {"keys": {"vworld_api_key": ""}}}, self.admin_cookie)[0], 200)
        self.assertFalse(S.settings(self.con)["external"]["vworld"]["enabled"])

    def test_success_sends_only_building_address_and_has_no_db_writes(self):
        self.assertEqual(self.configure()[0], 200)
        before = self.snapshot()
        status, data, _ = self.locate("광주광역시 동구 가상시험로 1, 101동 1203호")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["reference_only"])
        self.assertEqual((data["lat"], data["lon"]), (35.1462, 126.9233))
        self.assertEqual(data["source"], "VWORLD")
        call = self.fake["calls"][0]
        self.assertEqual(call["path"], "/req/address")
        self.assertEqual(call["params"]["address"], "광주광역시 동구 가상시험로 1")
        self.assertEqual(call["params"]["crs"], "EPSG:4326")
        self.assertEqual(call["params"]["domain"], "http://127.0.0.1:8765")
        self.assertIsNone(call["referer"])
        self.assertEqual(set(call["params"]), {"service", "request", "version", "format", "errorFormat", "crs", "address", "type", "refine", "simple", "key", "domain"})
        self.assertEqual(self.snapshot(), before)

    def test_parcel_type_and_no_domain_are_supported(self):
        self.configure(domain="")
        status, data, _ = self.locate("광주광역시 동구 가상동 산 12-3 (가상건물)", type="PARCEL")
        self.assertEqual(status, 200, data)
        self.assertEqual(self.fake["calls"][0]["params"]["address"], "광주광역시 동구 가상동 산 12-3")
        self.assertNotIn("domain", self.fake["calls"][0]["params"])

    def test_invalid_type_body_address_names_and_phones_are_not_sent(self):
        self.configure()
        for body in ([], {"address": "광주광역시 동구 가상시험로 1", "type": "X"},
                     {"address": "가상시험로"}, {"address": 123},
                     {"address": "광주광역시 동구 가상시험로 1 010-1234-5678"},
                     {"address": "가상신청인 광주광역시 동구 가상시험로 1", "name": "가상신청인"}):
            with self.subTest(body=body):
                status, _, _ = self.post("vworld/locate", body, self.worker_cookie)
                self.assertEqual(status, 400)
        self.assertEqual(self.fake["calls"], [])

    def test_other_roles_cannot_lookup(self):
        self.configure()
        for role in ("LEAD", "DONG", "FIELD"):
            username = "vworld_" + role.lower()
            did = self.con.execute("SELECT id FROM dong LIMIT 1").fetchone()[0]
            S.create_user(self.con, username, "가상 역할", role, "vworld-role-test-123", dong_ids=[did] if role == "DONG" else None)
            cookie = self.login(username, "vworld-role-test-123")
            self.assertEqual(self.post("vworld/locate", {"address": "가상시험로 1", "type": "ROAD"}, cookie)[0], 403)
        self.assertEqual(self.fake["calls"], [])

    def test_detail_or_unrecognized_text_before_address_is_never_sent(self):
        self.configure()
        for address in ("101동 1203호 광주광역시 동구 가상시험로 1", "가상타인 광주광역시 동구 가상시험로 1",
                        "광주광역시 동구 가상타인 가상시험로 1", "101동 1203호 광주광역시 동구 가상동 12"):
            with self.subTest(address=address):
                status, _, _ = self.locate(address, type="PARCEL" if address.endswith("동 12") else "ROAD")
                self.assertEqual(status, 400)
        self.assertEqual(self.fake["calls"], [])

    def test_standard_address_prefix_variants_preserve_region(self):
        self.configure()
        for address, kind in (("광주 동구 가상시험로 1", "ROAD"), ("가상시험로 1", "ROAD"),
                              ("충청남도 예산군 삽교읍 가상시험로 1", "ROAD"),
                              ("세종특별자치시 가상동 1", "PARCEL"), ("서울특별시 종로구 가상1가 2", "PARCEL")):
            with self.subTest(address=address):
                status, result, _ = self.locate(address, type=kind)
                self.assertEqual(status, 200, result)
                self.assertNotIn("none", result)
                self.assertEqual(self.fake["calls"][-1]["params"]["address"], address)

    def test_provider_errors_are_generic_without_echoing_key_or_address(self):
        self.configure()
        self.fake["response"] = {"response": {"status": "ERROR", "error": {"code": "INVALID_KEY", "text": FAKE_KEY + " 가상시험로 1"}}}
        status, data, _ = self.locate()
        self.assertEqual(status, 200)
        self.assertTrue(data["none"])
        self.assertNotIn(FAKE_KEY, json.dumps(data))
        self.assertNotIn("가상시험로", json.dumps(data))

    def test_not_found_and_invalid_points_never_return_coordinates(self):
        self.configure()
        responses = [{"response": {"status": "NOT_FOUND"}}, [], {"response": {"status": "OK"}},
                     *[{"response": {"status": "OK", "result": {"point": {"x": x, "y": y}}}}
                       for x, y in (("nan", "35"), ("126", "inf"), (True, 35), (35, 126), (0, 0))]]
        for response in responses:
            self.fake["response"] = response
            status, data, _ = self.locate()
            self.assertEqual(status, 200, data)
            self.assertTrue(data["none"])
            self.assertNotIn("lat", data)

    def test_service_url_validation_rolls_back_changes(self):
        for domain in ("javascript:alert(1)", "https://example.org/?key=secret", "https://name:pw@example.org", "https://example.org/#x"):
            with self.subTest(domain=domain):
                before = self.snapshot()
                self.assertEqual(self.configure(domain=domain)[0], 400)
                self.assertEqual(self.snapshot(), before)

    def test_repeated_request_is_not_cached_and_logs_contain_no_query_key_or_point(self):
        self.configure()
        before = self.snapshot()
        with self.assertLogs(SV.LOG, level="INFO") as captured:
            self.assertEqual(self.locate(client_req_id="vworld-repeat-test")[0], 200)
            self.assertEqual(self.locate(client_req_id="vworld-repeat-test")[0], 200)
        self.assertEqual(len(self.fake["calls"]), 2)
        self.assertEqual(self.snapshot(), before)
        log = "\n".join(captured.output)
        for value in (FAKE_KEY, "가상시험로", "126.9233", "35.1462"):
            self.assertNotIn(value, log)

    def test_revocation_while_reading_body_prevents_external_call(self):
        self.configure()
        with self.delayed("vworld/locate", {"address": "광주광역시 동구 가상시험로 1", "type": "ROAD"}) as finish:
            SV.SESSIONS.pop(self.worker_token, None)
            self.assert_denied_without_mutation(finish)
        self.assertEqual(self.fake["calls"], [])

    def test_settings_changed_during_external_wait_discards_result(self):
        self.configure()
        self.fake["gate"] = threading.Event()
        result = []
        worker = threading.Thread(target=lambda: result.append(self.locate()))
        worker.start()
        try:
            self.assertTrue(self.fake["entered"].wait(4))
            self.assertEqual(self.post("settings", {"external": {"vworld": {"enabled": False}}}, self.admin_cookie)[0], 200)
        finally:
            self.fake["gate"].set()
            worker.join(5)
        self.assertEqual(result[0][1]["code"], "SETTINGS_CHANGED")
        self.assertNotIn("lat", result[0][1])

    def test_connection_test_rechecks_session_after_slow_admin_body(self):
        self.configure()
        payload = json.dumps({"service": "vworld"}).encode()
        first = len(payload) // 2
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as raw:
            raw.sendall((f"POST /api/external/test HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                         f"Cookie: {self.admin_cookie}\r\nX-DG-CSRF: 1\r\nX-DG-Test-Body: wait\r\n"
                         f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode() + payload[:first])
            self.assertTrue(self.body_ready.wait(4))
            with SV.LOCK:
                SV.SESSIONS.pop(self.admin_cookie.split("=", 1)[1], None)
            raw.sendall(payload[first:])
            response = http.client.HTTPResponse(raw)
            response.begin()
            data = json.loads(response.read())
            self.assertEqual(response.status, 401, data)
            response.close()
        self.assertEqual(self.fake["calls"], [])

    def test_connection_test_discards_result_after_configuration_changes(self):
        self.configure()
        self.fake["gate"] = threading.Event()
        result = []
        worker = threading.Thread(target=lambda: result.append(self.post("external/test", {"service": "vworld"}, self.admin_cookie)))
        worker.start()
        try:
            self.assertTrue(self.fake["entered"].wait(4))
            self.assertEqual(self.post("settings", {"external": {"vworld": {"enabled": False}}}, self.admin_cookie)[0], 200)
        finally:
            self.fake["gate"].set()
            worker.join(5)
        self.assertFalse(result[0][1]["ok"])

    def test_external_wait_releases_db_lock_and_revoked_session_gets_no_result(self):
        self.configure()
        self.fake["gate"] = threading.Event()
        result = []
        worker = threading.Thread(target=lambda: result.append(self.locate()))
        worker.start()
        try:
            self.assertTrue(self.fake["entered"].wait(4))
            self.assertEqual(self.post("prefs", {"theme": "dark"}, self.admin_cookie)[0], 200)
            with SV.LOCK:
                SV.SESSIONS.pop(self.worker_token, None)
        finally:
            self.fake["gate"].set()
            worker.join(5)
        self.assertEqual(result[0][0], 401)
        self.assertNotIn("lat", result[0][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
