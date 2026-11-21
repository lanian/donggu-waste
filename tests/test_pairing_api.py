"""등록 설정의 권한과 중계 조회 중 세션이 해제된 경우를 시험한다."""
import threading
import unittest
from unittest.mock import patch

from test_label_usage_api import UsageHTTPCase
from dgpc import service as S


class PairingAPI(UsageHTTPCase):
    def test_requires_admin_and_explicit_relay(self):
        path = "devices/pair/start"
        self.assertEqual(self.post(path, {}, self.worker_cookie)[0], 403)
        status, message, _ = self.post(path, {}, self.admin_cookie)
        self.assertEqual(status, 400, message)
        self.assertIn("중계", message["error"])
        for url in ("http://outside.invalid", "https://user:pass@example.org"):
            status, _, _ = self.post("settings", {"device_pairing_relay_url": url}, self.admin_cookie)
            self.assertEqual(status, 400)

    def test_logout_during_network_read_does_not_register_device(self):
        import device_pairing
        self.assertEqual(self.post("settings", {"device_pairing_relay_url": "https://example.invalid"}, self.admin_cookie)[0], 200)
        with patch.object(device_pairing.Pairing, "create_remote"):
            status, started, _ = self.post("devices/pair/start", {}, self.admin_cookie)
        self.assertEqual(status, 200, started)
        entered, resume = threading.Event(), threading.Event()
        def read():
            entered.set()
            self.assertTrue(resume.wait(5))
            return "untrusted"
        responses = []
        path = f"devices/pair/{started['id']}/confirm"
        with patch.object(device_pairing.Pairing, "read_payload", side_effect=read):
            worker = threading.Thread(target=lambda: responses.append(self.post(path, {"otp": "023456"}, self.admin_cookie)))
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.post("logout", {}, self.admin_cookie)[0], 200)
            finally:
                resume.set()
                worker.join(6)
        self.assertEqual(responses[0][0], 401)
        self.assertEqual(S.list_devices(self.con), [])


if __name__ == "__main__":
    unittest.main()
