"""관리자 비밀번호와 인증 앱 번호는 새 수거폰을 사용할 수 있게 만드는 순간에만 요구한다."""
import base64
import hashlib
import hmac
import time
import unittest
import uuid
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from test_label_usage_api import UsageHTTPCase
from dgpc import devices
import dgproto as P


def number(seed, step):
    key = base64.b32decode(seed + '=' * (-len(seed) % 8))
    digest = hmac.digest(key, step.to_bytes(8, 'big'), 'sha1')
    offset = digest[-1] & 15
    return f'{(int.from_bytes(digest[offset:offset + 4], "big") & 0x7fffffff) % 1000000:06d}'


def device_code():
    raw = uuid.uuid4().bytes + P.pub_raw(P.gen_key()) + P.pub_raw(P.gen_key())
    return 'DGD1.' + base64.urlsafe_b64encode(raw + hashlib.sha256(raw).digest()[:4]).decode().rstrip('=')


class ApprovalHTTP(UsageHTTPCase):
    password = "session-admin-test-123"

    def test_setup_and_approval_require_current_admin_and_distinct_credentials(self):
        status_path = "devices/approval/status"
        self.assertEqual(self.get(status_path, self.admin_cookie), (200, {"configured": False}))
        self.assertEqual(self.get(status_path, self.worker_cookie)[0], 403)
        code = device_code()
        status, result, _ = self.post("devices/register", {"code": code, "confirm": True}, self.admin_cookie)
        self.assertEqual(status, 200)
        self.assertFalse(result["confirmed"])
        path = f'devices/{result["device_id"]}/confirm'
        self.assertEqual(self.post(path, {}, self.admin_cookie)[0], 400)
        self.assertEqual(self.post(path, {}, self.worker_cookie)[0], 403)
        self.assertFalse(devices.list_devices(self.con)[0]["confirmed_at"])
        uri = []
        def image(value):
            uri.append(value)
            return "data:image/png;base64,fixture", 1
        with patch("pc_tool.qr_png_data_uri", side_effect=image):
            start = "devices/approval/setup/start"
            self.assertEqual(self.post(start, {"password": self.password}, self.worker_cookie)[0], 403)
            self.assertEqual(self.post(start, {"password": "wrong"}, self.admin_cookie)[0], 400)
            self.assertFalse(uri)
            status, data, _ = self.post(start, {"password": self.password}, self.admin_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(set(data), {"qr_image"})
        secret = parse_qs(urlsplit(uri.pop()).query)["secret"][0]
        slot = int(time.time()) // 30
        self.enterContext(patch("dgpc.device_approval.time", return_value=slot * 30 + 15))
        first = number(secret, slot - 1)
        status, data, _ = self.post("devices/approval/setup/finish", {"otp": first}, self.admin_cookie)
        self.assertEqual(status, 200)
        self.assertTrue(data["recovery_codes"])
        self.assertEqual(self.get(status_path, self.admin_cookie), (200, {"configured": True}))
        otp = number(secret, slot)
        self.assertEqual(self.post(path, {"admin_password": self.password, "admin_otp": first}, self.admin_cookie)[0], 400)
        self.assertEqual(self.post(path, {"admin_password": "wrong", "admin_otp": otp}, self.admin_cookie)[0], 400)
        self.assertFalse(devices.list_devices(self.con)[0]["confirmed_at"])
        self.assertEqual(self.post(path, {"admin_password": self.password, "admin_otp": otp}, self.admin_cookie)[0], 200)
        self.assertTrue(devices.list_devices(self.con)[0]["confirmed_at"])
        self.assertEqual(self.post(path, {}, self.admin_cookie)[0], 200)
        _, other, _ = self.post("devices/register", {"code": device_code()}, self.admin_cookie)
        self.assertEqual(self.post(f'devices/{other["device_id"]}/confirm',
                                   {"admin_password": self.password, "admin_otp": otp}, self.admin_cookie)[0], 400)


if __name__ == "__main__":
    unittest.main()
