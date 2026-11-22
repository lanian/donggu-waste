"""임시 PC에서 USB 연결 재시도와 수거폰·관리자 6자리 입력을 검증한다."""
import os
import unittest
from contextlib import contextmanager

from playwright.sync_api import expect, sync_playwright

from test_dong_work_e2e import api
from test_review_ui import pc_server


@contextmanager
def device_settings():
    with (pc_server() as url, sync_playwright() as pw,
          pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path) as browser):
        ctx = browser.new_context(service_workers="block")
        password = "usb-ui-password-test"
        api(ctx, url, "setup", {"username": "usb_ui", "password": password, "display_name": "등록 담당"})
        page = ctx.new_page()
        page.goto(url + "#/settings/devices")
        yield page, password


class UsbPairingUI(unittest.TestCase):
    def test_usb_without_relay_can_retry_and_submit_formatted_code(self):
        with device_settings() as (page, _):
            starts, confirms = [], []
            issue = "휴대폰의 USB 테더링을 켠 뒤 다시 시도하세요."

            def start(route):
                starts.append(route.request.post_data_json)
                if len(starts) == 1:
                    route.fulfill(status=400, json={"error": issue})
                else:
                    route.fulfill(json={"id": "usb-test", "expires_in": 300,
                                        "qr_image": "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="})

            def confirm(route):
                confirms.append(route.request.post_data_json)
                route.fulfill(status=400, json={"error": "시험 등록번호 확인 응답"})

            page.route("**/api/devices/pair/start", start)
            page.route("**/api/devices/pair/usb-test/confirm", confirm)
            page.route("**/api/devices/pair/usb-test/cancel", lambda route: route.fulfill(json={}))
            usb = page.get_by_role("button", name="USB로 수거폰 등록", exact=True)
            expect(usb).to_be_enabled(timeout=2500)
            expect(page.get_by_role("button", name="6자리 간편 등록 시작")).to_be_disabled()
            expect(page.get_by_text("USB 테더링", exact=False).first).to_be_visible()
            usb.click()
            expect(page.get_by_role("status").filter(has_text=issue)).to_be_visible()
            usb.click()
            dialog = page.get_by_role("dialog", name="USB로 수거폰 등록")
            expect(dialog.get_by_role("img")).to_be_visible()
            code = dialog.get_by_label("휴대폰 등록번호 6자리")
            code.fill("12x456")
            submit = dialog.get_by_role("button", name="등록번호 확인")
            submit.click()
            expect(dialog.get_by_role("alert")).to_contain_text("숫자 6자리")
            self.assertEqual(confirms, [])
            code.fill("1234567")
            submit.click()
            self.assertEqual(confirms, [])
            code.fill("")
            code.focus()
            page.keyboard.insert_text("012-345")
            expect(code).to_have_value("012345")
            submit.click()
            expect(dialog.get_by_role("alert")).to_contain_text("시험 등록번호 확인 응답")
            self.assertEqual(starts, [{"transport": "usb"}] * 2)
            self.assertEqual(confirms, [{"otp": "012345", "label": ""}])
            dialog.get_by_role("button", name="취소").click()
            expect(usb).to_be_enabled()

    def test_admin_otp_pasted_with_space_keeps_sixth_digit(self):
        with device_settings() as (page, password):
            page.get_by_role("button", name="관리자 OTP 설정").click()
            setup = page.get_by_role("dialog", name="관리자 OTP 설정")
            setup.get_by_label("관리자 비밀번호").fill(password)
            setup.get_by_role("button", name="설정 QR 받기").click()
            otp = page.get_by_role("dialog", name="관리자 인증 앱 연결").get_by_label("관리자 OTP 6자리")
            otp.focus()
            page.keyboard.insert_text("012 345")
            expect(otp).to_have_value("012345", timeout=1200)

    def test_admin_rotation_preserves_recovery_code_and_accepts_formatted_otp(self):
        with device_settings() as (page, password):
            page.route("**/api/devices/approval/status", lambda r: r.fulfill(json={"configured": True}))
            proofs = []

            def start(route):
                proofs.append(route.request.post_data_json["recovery_code"])
                route.fulfill(status=400, json={"error": "시험 교체 요청"})

            page.route("**/api/devices/approval/setup/start", start)
            page.reload()
            page.get_by_role("button", name="관리자 OTP 교체").click()
            dialog = page.get_by_role("dialog", name="관리자 OTP 교체")
            dialog.get_by_label("관리자 비밀번호").fill(password)
            for proof in ["12345-67890-ABDEF-12345", "012 345"]:
                dialog.get_by_label("기존 OTP 또는 복구코드").fill(proof)
                with page.expect_response(lambda r: r.url.endswith("/approval/setup/start")):
                    dialog.get_by_role("button", name="설정 QR 받기").click()
            self.assertEqual(proofs, ["12345-67890-ABDEF-12345", "012345"])
