"""USB 등록 QR의 수신 주소와 모바일 브라우저의 암호문 전달을 합성 자료로 확인한다."""
import base64
import json
import os
import re
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from test_phone_pairing_e2e import PagesHandler, PagesServer, open_new_phone, serving, show_qr_to_camera
import device_pairing
import dgproto
from pc_tool import qr_png_data_uri


class USBPhoneHandoff(unittest.TestCase):
    def setUp(self):
        self.site = self.enterContext(serving(PagesServer(("127.0.0.1", 0), PagesHandler))) + PagesHandler.prefix
        pw = self.enterContext(sync_playwright())
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path,
                                     proxy={"server": "http://127.0.0.1:1", "bypass": "127.0.0.1,localhost"})
        self.addCleanup(browser.close)
        self.context = browser.new_context(service_workers="block")
        self.phone = self.context.new_page()
        self.origin = "http://192.168.42.100:54123"
        self.pair = device_pairing.Pairing(self.origin)
        registration = bytes([68, 71, dgproto.VERSION, 9]) + b"".join(dgproto.pub_raw(dgproto.gen_key()) for _ in range(2))
        self.fields = dict(registration=dgproto.b45encode(registration), relay=self.origin,
                           secret=self.pair.secret, expires_at=int(time.time()) + 300)

    def qr(self, **fields):
        raw = json.dumps({**self.fields, **fields}).encode("ascii")
        return "DGUSB1." + base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def test_only_expiring_canonical_private_ipv4_receiver_origins_are_accepted(self):
        self.phone.goto(self.site + "pair-submit.html")
        parse = """async (qrs) => {
            const { readPairRegistration } = await import('./pairing.js');
            return qrs.map(qr => {try {return Boolean(readPairRegistration(qr).session);} catch {return false;}});
        }"""
        valid = [self.qr(relay=f"http://{ip}:54123") for ip in ("10.0.0.4", "172.16.0.4", "172.31.255.254", "192.168.42.100")]
        invalid = [self.qr(relay=url) for url in (
            "http://127.0.0.1:54123", "http://172.32.0.4:54123", "http://169.254.1.2:54123",
            "http://2130706433:54123", "http://192.168.42.100", "http://192.168.42.100:0",
            "http://192.168.42.100:65536", "http://192.168.042.100:54123", "http://192.168.42.100:054123",
            self.origin + "/", self.origin + "/a", self.origin + "?", self.origin + "#", "http://user@192.168.42.100:54123",
            "https://192.168.42.100:54123", "http://outside.test:54123",
        )] + [self.qr(expires_at=None), self.qr(expires_at=int(time.time()) - 1)]
        self.assertEqual(self.phone.evaluate(parse, valid + invalid), [True] * len(valid) + [False] * len(invalid))
        self.assertEqual(self.phone.evaluate(parse, [self.qr(secret=[self.pair.secret])]), [False])

    def test_phone_opens_only_room_and_ciphertext_and_keeps_number_in_app(self):
        phone = self.phone
        register = open_new_phone(phone, self.site)
        show_qr_to_camera(phone, qr_png_data_uri(self.qr())[0])
        register.click()
        link = phone.get_by_role("link", name="USB로 PC에 전달")
        expect(link).to_be_visible(timeout=7000)
        expect(phone.get_by_text("새로 열린 화면에 ‘PC가 등록 자료를 받았습니다’가 보이면", exact=False)).to_be_visible(timeout=1000)
        expect(link).to_have_attribute("target", "_blank")
        expect(link).to_have_attribute("rel", re.compile(r"noopener.*noreferrer|noreferrer.*noopener"))
        url = urlsplit(link.get_attribute("href"))
        self.assertEqual(url.scheme + "://" + url.netloc, self.origin)
        self.assertEqual(url.path, "/register/" + self.pair.room)
        self.assertFalse(url.query)
        self.assertRegex(url.fragment, r"^[A-Za-z0-9_-]{100,2048}$")
        self.assertNotIn(self.pair.secret, link.get_attribute("href"))
        number = phone.get_by_label("PC에 입력할 등록번호")
        expect(number).to_have_count(0, timeout=750)

        requests = []
        def receive(route):
            requests.append((route.request.method, route.request.header_value("referer"), route.request.post_data))
            route.fulfill(status=200, content_type="text/html", body="<title>USB fixture</title>")

        self.context.route(self.origin + "/**", receive)
        with self.context.expect_page() as opened:
            link.click()
        opened.value.wait_for_load_state()
        self.assertEqual(requests, [("GET", None, None)])
        self.assertIsNone(opened.value.evaluate("window.opener"))
        self.assertEqual(urlsplit(opened.value.url).fragment, url.fragment)
        opened.value.close()
        phone.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
        expect(link).to_have_attribute("href", url.geturl())
        expect(number).to_have_text(re.compile(r"^\d{6}$"))
        self.assertTrue(self.pair.device_code(url.fragment, number.inner_text()).startswith("DGD1."))
        phone.evaluate("async () => (await import('./core.js')).lockNow()")
        expect(link).to_have_count(0)
        expect(number).to_have_count(0)

    def test_expiration_blocks_a_stale_link_before_timers_resume(self):
        phone = self.phone
        register = open_new_phone(phone, self.site)
        show_qr_to_camera(phone, qr_png_data_uri(self.qr())[0])
        register.click()
        link = phone.get_by_role("link", name="USB로 PC에 전달")
        expect(link).to_be_visible()
        allowed = link.evaluate("""anchor => {
            const now = Date.now();
            Date.now = () => now + 301000;
            return anchor.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
        }""")
        self.assertFalse(allowed)
        expect(phone.get_by_role("heading", name="등록 시간이 끝났습니다")).to_be_visible()
        expect(phone.get_by_label("PC에 입력할 등록번호")).to_have_count(0)


if __name__ == "__main__":
    unittest.main()
