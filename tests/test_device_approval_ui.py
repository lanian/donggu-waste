"""PC 카메라·등록 중계 없이 수거폰 파일을 가져오고 관리자 2단계 인증으로 승인한다."""
import base64
import hashlib
import hmac
import os
import time
import unittest
import uuid
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

from test_dong_work_e2e import api
from test_review_ui import pc_server
from test_workflow_mobile import dgproto


def token(secret, offset=0):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = int(time.time()) // 30 + offset
    mac = hmac.digest(key, counter.to_bytes(8, "big"), "sha1")
    start = mac[-1] & 15
    return f"{(int.from_bytes(mac[start:start + 4], 'big') & 0x7fffffff) % 1000000:06d}"


class DeviceApprovalUI(unittest.TestCase):
    def test_file_fingerprint_enrollment_approval_and_recovery(self):
        with (pc_server() as url, sync_playwright() as pw,
              pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path) as browser):
            ctx = browser.new_context(service_workers="block")
            password = "test-admin-otp-9876"
            api(ctx, url, "setup", {"username": "approval_test", "password": password, "display_name": "가상 관리자"})
            page = ctx.new_page()
            page.goto(url + "#/settings/devices")
            select = page.get_by_label("수거폰 등록 파일", exact=True)
            expect(select).to_be_visible(timeout=2500)
            writes = []
            page.on("request", lambda r: writes.append(r.post_data_json) if r.url.endswith("/devices/register") else None)
            def load(content, name="시험.dgdevice"):
                select.set_input_files({"name": name, "mimeType": "application/octet-stream", "buffer": content})
            load(b"X" * 1025)
            expect(page.get_by_text("1KB", exact=False).last).to_be_visible()
            load(b'{"residents":[]}')
            expect(page.get_by_text("수거폰 등록 파일의 내용", exact=False)).to_be_visible()
            self.assertEqual(writes, [])
            raw = uuid.uuid4().bytes + dgproto.pub_raw(dgproto.gen_key()) + dgproto.pub_raw(dgproto.gen_key())
            code = "DGD1." + base64.urlsafe_b64encode(raw + hashlib.sha256(raw).digest()[:4]).decode().rstrip("=")
            load(code.encode())
            approve = page.get_by_role("button", name="지문 같음 · 사용 시작")
            expect(approve).to_be_visible()
            self.assertEqual(writes, [{"code": code, "label": ""}])
            device = api(ctx, url, "bootstrap")["devices"][0]
            self.assertIsNone(device["confirmed_at"])
            self.assertIn(" ".join([device["fingerprint"][i:i+3] for i in (0, 3, 6)]), page.locator(".fp-inline").inner_text())
            page.get_by_role("button", name="관리자 OTP 설정", exact=True).click()
            setup = page.get_by_role("dialog", name="관리자 OTP 설정", exact=True)
            setup.get_by_label("관리자 비밀번호", exact=True).fill(password)
            setup.get_by_role("button", name="설정 QR 받기").click()
            enroll = page.get_by_role("dialog", name="관리자 인증 앱 연결")
            image = enroll.get_by_role("img", name="관리자 OTP 설정 QR")
            expect(image).to_be_visible()
            page.add_script_tag(url=url + "web/vendor/jsQR.js")
            uri = image.evaluate("""async image => {
                await image.decode();
                const canvas = document.createElement('canvas');
                canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
                const ctx = canvas.getContext('2d'); ctx.drawImage(image, 0, 0);
                const px = ctx.getImageData(0, 0, canvas.width, canvas.height);
                return jsQR(px.data, px.width, px.height).data;
            }""")
            self.assertEqual(urlsplit(uri).scheme, "otpauth")
            secret = parse_qs(urlsplit(uri).query)["secret"][0]
            enroll.get_by_label("관리자 OTP 6자리", exact=True).fill(token(secret, -1))
            enroll.get_by_role("button", name="연결 확인").click()
            recovery = page.get_by_role("dialog", name="복구코드 보관")
            expect(recovery.get_by_role("listitem").first).to_be_visible()
            backup = recovery.get_by_role("listitem").first.inner_text()
            recovery.get_by_role("button", name="보관했습니다").click()
            approve.click()
            decision = page.get_by_role("dialog", name="새 수거폰 등록 승인")
            decision.get_by_role("button", name="취소").click()
            self.assertIsNone(api(ctx, url, "bootstrap")["devices"][0]["confirmed_at"])
            approve.click()
            decision.get_by_label("관리자 비밀번호").fill(password)
            decision.get_by_label("관리자 OTP 6자리").fill(token(secret))
            with page.expect_response(lambda res: res.url.endswith("/confirm")) as result:
                decision.get_by_role("button", name="등록 승인").click()
            self.assertEqual(result.value.status, 200, result.value.text())
            chooser = page.get_by_role("dialog", name="어느 팀에 연결할까요?")
            chooser.get_by_role("button", name="나중에").click()
            expect(approve).to_have_count(0)
            self.assertTrue(api(ctx, url, "bootstrap")["devices"][0]["confirmed_at"])
            load(code.encode())
            expect(page.get_by_text("이미 등록된 기기입니다.", exact=True)).to_be_visible()
            expect(decision).to_have_count(0)
            page.get_by_role("button", name="관리자 OTP 교체").click()
            rotate = page.get_by_role("dialog", name="관리자 OTP 교체")
            rotate.get_by_label("관리자 비밀번호").fill(password)
            rotate.get_by_label("기존 OTP 또는 복구코드").fill(backup)
            rotate.get_by_role("button", name="설정 QR 받기").click()
            expect(enroll).to_be_visible()
            page.evaluate("""async () => {
                const core = await import('/core.js');
                await core.post('logout');
                core.setBoot(null, {sessionChanged: true});
                await core.render();
            }""")
            expect(enroll).to_have_count(0, timeout=2000)
            api(ctx, url, "login", {"username": "approval_test", "password": password})
            page.reload()
            expect(page.get_by_role("button", name="관리자 OTP 교체")).to_be_visible()
            for value in (secret, backup, password):
                self.assertNotIn(value, page.url)
                self.assertNotIn(value, page.evaluate("JSON.stringify([localStorage,sessionStorage])"))
                self.assertNotIn(value, page.locator("body").inner_text())


if __name__ == "__main__":
    unittest.main()
