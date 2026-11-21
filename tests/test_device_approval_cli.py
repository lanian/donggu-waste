"""register-device 명령은 새 기기에 관리자 2단계 승인을 거친다."""
import getpass
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import test_pc_labels as fixture
from test_device_approval_api import device_code, number

import pc_tool
from dgpc import device_approval, devices, users


class ApprovalCLITest(unittest.TestCase):
    tearDown = fixture.NumberedLabelsTest.tearDown
    password = "synthetic-cli-password"

    def setUp(self):
        fixture.NumberedLabelsTest.setUp(self)
        devices.ensure_pc_keys(self.con)
        self.uid = users.create_user(self.con, "cli_admin", "가상 관리자", "ADMIN", self.password)
        self.code = device_code()

    def test_new_phone_requires_configured_admin_even_with_confirm_flag(self):
        with patch("builtins.input", return_value="cli_admin"), patch.object(getpass, "getpass", side_effect=AssertionError):
            with self.assertRaisesRegex(SystemExit, "인증 앱"):
                pc_tool.main(["register-device", self.code, "--confirm"])
        self.assertFalse(devices.list_devices(self.con)[0]["confirmed_at"])

    def test_approval_uses_terminal_credentials_and_repeated_approval_needs_none(self):
        stamp = 1800000000
        with patch.object(device_approval, "time", return_value=stamp - 30):
            uri = device_approval.start_setup(self.con, self.uid, "fixture_session", self.password)["uri"]
            seed = parse_qs(urlsplit(uri).query)["secret"][0]
            device_approval.finish_setup(self.con, self.uid, "fixture_session", number(seed, stamp // 30 - 1))
        with patch.object(device_approval, "time", return_value=stamp):
            otp = number(seed, stamp // 30)
            output = io.StringIO()
            with redirect_stdout(output):
                args = ["register-device", self.code, "--admin", "cli_admin", "--confirm"]
                with patch.object(getpass, "getpass", side_effect=getpass.GetPassWarning):
                    with self.assertRaisesRegex(SystemExit, "터미널"):
                        pc_tool.main(args)
                with patch.object(getpass, "getpass", side_effect=["wrong password", otp]):
                    with self.assertRaises(SystemExit):
                        pc_tool.main(args)
                self.assertFalse(devices.list_devices(self.con)[0]["confirmed_at"])
                with patch.object(getpass, "getpass", side_effect=[self.password, otp]) as prompt:
                    pc_tool.main(args)
                    self.assertEqual(prompt.call_count, 2)
                self.assertTrue(devices.list_devices(self.con)[0]["confirmed_at"])
                with patch.object(getpass, "getpass", side_effect=AssertionError), patch("builtins.input", side_effect=AssertionError):
                    pc_tool.main(["register-device", self.code, "--confirm"])
            self.assertIn("사용 시작", output.getvalue())
            self.assertNotIn(self.password, output.getvalue())
            self.assertNotRegex(output.getvalue(), rf"(?<![0-9a-f]){otp}(?![0-9a-f])")


if __name__ == "__main__":
    unittest.main()
