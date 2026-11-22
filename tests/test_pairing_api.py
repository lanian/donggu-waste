"""등록 설정의 권한과 중계 조회 중 세션이 해제된 경우를 시험한다."""
import threading
import unittest
from unittest.mock import patch

from test_label_usage_api import UsageHTTPCase
from dgpc import service as S


class PairingAPI(UsageHTTPCase):
    def test_usb_registration_requires_local_admin_and_discovered_adapter(self):
        path = "devices/pair/start"
        body = {"transport": "usb"}
        with patch("device_pairing.discover_usb_ipv4",
                   side_effect=ValueError("연결한 USB 테더링을 켜 주세요.")) as discover:
            self.assertEqual(self.post(path, body, self.worker_cookie)[0], 403)
            with patch("dgpc.server.same_machine", return_value=False):
                self.assertEqual(self.post(path, body, self.admin_cookie)[0], 403)
            discover.assert_not_called()
            status, reply, _ = self.post(path, body, self.admin_cookie)
            self.assertEqual(status, 400, reply)
            self.assertIn("USB 테더링", reply["error"])
            self.assertEqual(discover.call_count, 1)
            for bad_body in ({"transport": "unknown"}, {"transport": "usb", "address": "192.168.1.1"}):
                self.assertEqual(self.post(path, bad_body, self.admin_cookie)[0], 400)
            self.assertEqual(discover.call_count, 1)

    def test_usb_without_relay_stays_pending_and_closes_on_cancel_replacement_logout(self):
        with patch("device_pairing.discover_usb_ipv4", return_value="192.168.42.2"), \
                patch("device_pairing.UsbReceiver") as factory, patch("device_pairing._post") as network:
            factory.return_value.start.return_value = "http://192.168.42.2:49200"
            factory.return_value.payload.return_value = None
            def start():
                status, reply, _ = self.post("devices/pair/start", {"transport": "usb"}, self.admin_cookie)
                self.assertEqual(status, 200, reply)
                self.assertTrue(reply["qr_image"].startswith("data:image/png"))
                return "devices/pair/" + reply["id"]
            first = start()
            status, reply, _ = self.post(first + "/confirm", {"otp": "000123"}, self.admin_cookie)
            self.assertEqual(status, 400)
            self.assertIn("수거폰", reply["error"])
            factory.return_value.payload.return_value = "invalid"
            for _ in range(5):
                self.assertEqual(self.post(first + "/confirm", {"otp": "000123"}, self.admin_cookie)[0], 400)
            self.assertEqual(self.post(first + "/cancel", {}, self.admin_cookie)[0], 200)
            factory.return_value.close.assert_called()
            second = start()
            factory.return_value.close.reset_mock()
            start()
            factory.return_value.close.assert_called()
            self.assertEqual(self.post(second + "/confirm", {"otp": "000123"}, self.admin_cookie)[0], 400)
            factory.return_value.close.reset_mock()
            self.assertEqual(self.post("logout", {}, self.admin_cookie)[0], 200)
            factory.return_value.close.assert_called()
            network.assert_not_called()
            self.assertFalse(S.list_devices(self.con))

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
