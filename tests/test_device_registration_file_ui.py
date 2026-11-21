"""휴대폰 기본 등록의 오프라인 파일 저장과 운영체제 공유창을 검증한다."""
import os
import unittest

from playwright.sync_api import expect, sync_playwright

from test_review_ui import mobile_server
from test_workflow_mobile import dgproto, open_mobile, route, seed


class RegistrationFileTest(unittest.TestCase):
    def setUp(self):
        url = self.enterContext(mobile_server())
        pw = self.enterContext(sync_playwright())
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path)
        self.addCleanup(browser.close)
        self.context, self.page = open_mobile(browser, url)
        seed(self.page, {"worker": "가상 작업자", "requests": [{"id": "가상 접수", "name": "가상 주민"}]})

    def test_download_matches_pc_parser_and_existing_code_offline(self):
        self.page.evaluate("Object.defineProperty(navigator, 'canShare', {value: () => false})")
        route(self.page, "#/register")
        self.context.set_offline(True)
        save = self.page.get_by_role("button", name="등록 파일 저장", exact=True)
        expect(save).to_be_visible()
        expect(self.page.get_by_role("button", name="등록 파일 공유", exact=True)).to_have_count(0)
        expect(self.page.get_by_role("img", name="기기 코드 QR")).to_be_visible()
        with self.page.expect_download() as download:
            save.click()
        file = download.value
        self.assertTrue(file.suggested_filename.endswith(".dgdevice"))
        data = file.path().read_bytes()
        self.assertLessEqual(len(data), 1024)
        self.assertEqual(data, self.page.locator("textarea.code").input_value().encode("utf-8"))
        device, kex, sig = dgproto.parse_device_code(data.decode("utf-8"))
        keys = self.page.evaluate("""async () => {
            const d = window.__workflowDevice, proto = await import('./proto.js');
            return [d.id, await proto.exportRaw(d.kex.publicKey), await proto.exportRaw(d.sign.publicKey)]
                .map(value => [...value]);
        }""")
        self.assertEqual([device, kex, sig], list(map(bytes, keys)))

    def test_share_cancel_deny_and_retry_leave_download_available(self):
        self.page.evaluate("""() => {
            Object.defineProperty(navigator, 'canShare', {value: ({files}) => files.length === 1});
            Object.defineProperty(navigator, 'share', {value: async ({files}) => {
                if (window.shareError) throw new DOMException('synthetic', window.shareError);
                window.sharedRegistration = await files[0].text();
            }});
        }""")
        route(self.page, "#/register")
        share = self.page.get_by_role("button", name="등록 파일 공유", exact=True)
        save = self.page.get_by_role("button", name="등록 파일 저장", exact=True)
        expect(share).to_be_visible()
        downloads = []
        self.page.on("download", lambda download: downloads.append(download))
        for failure, hint in [("AbortError", "취소"), ("NotAllowedError", "등록 파일 저장")]:
            self.page.evaluate("name => window.shareError = name", failure)
            share.click()
            expect(self.page.locator(".msg").filter(has_text=hint)).to_be_visible()
            expect(save).to_be_enabled()
            self.assertFalse(downloads)
        self.page.evaluate("() => { window.shareError = null; }")
        share.click()
        self.page.wait_for_function("() => !!window.sharedRegistration")
        self.assertEqual(self.page.evaluate("window.sharedRegistration"), self.page.locator("textarea.code").input_value())
        self.assertFalse(downloads)


if __name__ == "__main__":
    unittest.main()
