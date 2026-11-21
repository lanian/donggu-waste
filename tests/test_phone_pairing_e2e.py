"""서로 다른 로컬 출처의 PC·정적 휴대폰 앱·중계에서 QR과 등록번호로 기기를 인증한다."""
import os
import re
import sys
import threading
import unittest
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from device_approval_fixture import ApprovalApp

from test_label_usage_api import UsageHTTPCase
from test_review_ui import PIN, ROOT
from dgpc import devices

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import pairing_relay


@contextmanager
def serving(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class PagesHandler(SimpleHTTPRequestHandler):
    prefix = "/donggu-waste/"
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript", ".mjs": "text/javascript"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "web"), **kwargs)

    def do_GET(self):
        if not self.path.startswith(self.prefix):
            self.send_error(404)
            return
        self.path = "/" + self.path[len(self.prefix):]
        super().do_GET()

    def log_message(self, fmt, *args):
        pass


class PagesServer(ThreadingHTTPServer):
    request_queue_size = 128


@contextmanager
def relay_server():
    requests = []

    class ObservedHandler(pairing_relay.Handler):
        def _observe(self):
            requests.append((self.command, self.path.split("/")[-1], self.headers.get("Origin")))

        def do_OPTIONS(self):
            self._observe()
            super().do_OPTIONS()

        def do_POST(self):
            self._observe()
            super().do_POST()

    server = pairing_relay.RelayHTTPServer(("127.0.0.1", 0), pairing_relay.Rooms())
    server.RequestHandlerClass = ObservedHandler
    with serving(server) as url:
        yield url, requests


def show_qr_to_camera(phone, image):
    """PC가 표시한 실제 QR 픽셀을 합성 영상으로 제공한다. 해독·등록 코드는 앱 원본이 실행한다."""
    phone.evaluate("""async src => {
        const image = new Image();
        image.src = src;
        await image.decode();
        navigator.mediaDevices.getUserMedia = async () => {
            const canvas = document.createElement('canvas');
            canvas.width = canvas.height = Math.max(720, image.naturalWidth);
            const ctx = canvas.getContext('2d');
            ctx.imageSmoothingEnabled = false;
            const draw = () => ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
            draw();
            const stream = canvas.captureStream(12);
            const redraw = setInterval(() => {
                if (stream.active) draw();
                else clearInterval(redraw);
            }, 80);
            return stream;
        };
    }""", image)


def open_new_phone(phone, mobile):
    phone.goto(mobile)
    phone.get_by_placeholder("숫자 6자리 이상").fill(PIN)
    phone.get_by_placeholder("한 번 더", exact=True).fill(PIN)
    phone.get_by_role("button", name="PIN 설정", exact=True).click()
    phone.get_by_role("button", name="PC와 연결해서 사용", exact=True).click()
    register = phone.locator(".next-action").get_by_role("button", name="기기 등록", exact=True)
    expect(register).to_be_visible()
    return register


class PhonePairingE2E(UsageHTTPCase):
    def test_registration_file_from_offline_phone_then_admin_approval_without_relay(self):
        pc_url = f"http://127.0.0.1:{self.port}/"
        with serving(PagesServer(("127.0.0.1", 0), PagesHandler)) as site, sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path,
                proxy={"server": "http://127.0.0.1:1", "bypass": "127.0.0.1,localhost"})
            try:
                admin = browser.new_context(service_workers="block")
                admin.add_cookies([{"name": "dg_s", "value": self.admin_cookie.split("=", 1)[1], "url": pc_url}])
                approver = ApprovalApp(admin, pc_url, "session-admin-test-123")
                pc = admin.new_page()
                pc.goto(pc_url + "#/settings/devices")
                expect(pc.get_by_role("button", name="6자리 간편 등록 시작")).to_be_disabled()
                phone_context = browser.new_context(service_workers="block")
                phone = phone_context.new_page()
                register = open_new_phone(phone, site + PagesHandler.prefix)
                qr = pc.get_by_role("img", name="기기 등록 QR", exact=True)
                expect(qr).to_have_attribute("src", re.compile(r"^data:image/png"))
                show_qr_to_camera(phone, qr.get_attribute("src"))
                register.click()
                save = phone.get_by_role("button", name="등록 파일 저장")
                expect(save).to_be_visible()
                phone_context.set_offline(True)
                with phone.expect_download() as completed:
                    save.click()
                file = completed.value
                pc.get_by_label("수거폰 등록 파일", exact=True).set_input_files({
                    "name": file.suggested_filename, "mimeType": "application/octet-stream", "buffer": file.path().read_bytes()})
                pending = pc.get_by_role("button", name="지문 같음 · 사용 시작")
                expect(pending).to_be_visible()
                fingerprint = phone.locator(".fp").inner_text().strip()
                expect(pc.locator("tr", has=pending)).to_contain_text(fingerprint)
                self.assertIsNone(devices.list_devices(self.con)[0]["confirmed_at"])
                pending.click()
                approver.approve_dialog(pc)
                pc.get_by_role("dialog", name="어느 팀에 연결할까요?").get_by_role("button", name="나중에").click()
                self.assertTrue(devices.list_devices(self.con)[0]["confirmed_at"])
            finally:
                browser.close()

    def test_scanning_pc_qr_then_typing_totp_registers_the_phone_for_use(self):
        pc_url = f"http://127.0.0.1:{self.port}/"
        team_id = devices.save_crew(self.con, {"name": "간편 등록 검증팀"})
        with relay_server() as (relay, requests), serving(PagesServer(("127.0.0.1", 0), PagesHandler)) as site, sync_playwright() as pw:
            mobile = site + PagesHandler.prefix
            # 로컬 세 출처만 직접 연결한다. CDP 요청 가로채기는 Chromium의 실제 CORS 사전 요청을 바꿀 수 있다.
            browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path,
                proxy={"server": "http://127.0.0.1:1", "bypass": "127.0.0.1,localhost"})
            try:
                pc_context = browser.new_context(service_workers="block")
                phone_context = browser.new_context(service_workers="block", viewport={"width": 390, "height": 844})
                allowed = {urlsplit(url).netloc for url in (pc_url, mobile, relay)}
                self.assertEqual(len(allowed), 3)
                external = []

                def observe_request(request):
                    target = urlsplit(request.url)
                    if target.scheme in ("http", "https") and target.netloc not in allowed:
                        external.append(target.netloc)

                for context in (pc_context, phone_context):
                    context.on("request", observe_request)
                pc_context.add_cookies([{"name": "dg_s", "value": self.admin_cookie.split("=", 1)[1], "url": pc_url}])
                approver = ApprovalApp(pc_context, pc_url, "session-admin-test-123")
                pc, phone = pc_context.new_page(), phone_context.new_page()
                pc.goto(pc_url + "#/settings/devices")
                pc.evaluate("""() => {
                    window.cameraCalls = 0;
                    navigator.mediaDevices.getUserMedia = async () => { window.cameraCalls++; throw Error('PC camera unavailable'); };
                }""")
                register = open_new_phone(phone, mobile)

                pc.get_by_text("간편 등록 중계 설정", exact=True).click()
                pc.get_by_label("간편 등록 중계 주소", exact=True).fill(relay)
                pc.get_by_role("button", name="중계 주소 저장", exact=True).click()
                with pc.expect_response(lambda r: r.url.endswith("/api/devices/pair/start")) as start:
                    pc.get_by_role("button", name="6자리 간편 등록 시작", exact=True).click()
                self.assertEqual(start.value.status, 200)
                dialog = pc.get_by_role("dialog", name="휴대폰 간편 등록")
                qr = dialog.get_by_role("img", name="간편 등록 QR")
                expect(qr).to_be_visible()
                show_qr_to_camera(phone, qr.evaluate("img => img.src"))
                register.click()
                phone.get_by_role("button", name="등록번호 받기", exact=True).click()
                otp = phone.get_by_label("PC에 입력할 등록번호", exact=True)
                expect(otp).to_have_text(re.compile(r"^\s*\d{3}\s?\d{3}\s*$"))
                digits = re.sub(r"\D", "", otp.inner_text())
                self.assertFalse(devices.list_devices(self.con))
                dialog.get_by_label("휴대폰 등록번호 6자리", exact=True).fill(digits)
                with pc.expect_response(lambda r: "/api/devices/pair/" in r.url and r.url.endswith("/confirm")) as confirm:
                    dialog.get_by_role("button", name="등록번호 확인", exact=True).click()
                self.assertEqual(confirm.value.status, 200)
                self.assertIs(confirm.value.json()["approval_required"], True)
                self.assertFalse(devices.list_devices(self.con)[0]["confirmed_at"])
                approver.approve_dialog(pc)
                teams = pc.get_by_role("dialog", name="어느 팀에 연결할까요?")
                expect(teams).to_be_visible()
                teams.get_by_label("연결할 팀", exact=True).select_option(team_id)
                with pc.expect_response(lambda r: r.url.endswith("/api/crews") and r.request.method == "POST") as linked:
                    teams.get_by_role("button", name="연결", exact=True).click()
                self.assertEqual(linked.value.status, 200)
                expect(teams).to_have_count(0)
                expect(pc.get_by_role("button", name="지문 같음 · 사용 시작", exact=True)).to_have_count(0)
                registered = devices.list_devices(self.con)
                self.assertEqual(len(registered), 1)
                self.assertTrue(registered[0]["confirmed_at"])
                phone_id = phone.evaluate("""async () => {
                    const store = await import('./store.js'), proto = await import('./proto.js');
                    return proto.uuidStr((await store.getRaw('device')).id);
                }""")
                self.assertEqual(registered[0]["id"], phone_id)
                self.assertEqual([c["id"] for c in devices.list_crews(self.con) if c["device_id"] == phone_id], [team_id])
                repeated = self.post(urlsplit(confirm.value.url).path.removeprefix("/api/"),
                                     {"otp": digits}, self.admin_cookie)
                self.assertGreaterEqual(repeated[0], 400)
                self.assertEqual(len(devices.list_devices(self.con)), 1)
                self.assertIn(("OPTIONS", "submit", "null"), requests)
                self.assertIn(("POST", "submit", "null"), requests)
                self.assertEqual(pc.evaluate("window.cameraCalls"), 0)
                self.assertEqual(external, [])
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
