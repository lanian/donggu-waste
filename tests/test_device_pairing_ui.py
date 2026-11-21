"""간편 등록의 명시적 제출·암호문 재시도·잠금/재스캔과 기존 PC QR 호환을 검증한다."""
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import unittest
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from playwright.sync_api import expect, sync_playwright
from test_dong_work_e2e import api
from test_review_ui import mobile_server, pc_server, PIN
from test_workflow_mobile import dgproto, open_mobile, seed


class DevicePairUI(unittest.TestCase):
    def test_existing_team_device_requires_explicit_replacement(self):
        with (pc_server() as url, sync_playwright() as pw,
              pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path) as browser):
            ctx = browser.new_context(service_workers="block")
            api(ctx, url, "setup", {"username": "phone_links", "password": "phone-links-123-test", "display_name": "연결 담당"})
            def device(label):
                raw = uuid.uuid4().bytes + dgproto.pub_raw(dgproto.gen_key()) + dgproto.pub_raw(dgproto.gen_key())
                code = "DGD1." + base64.urlsafe_b64encode(raw + hashlib.sha256(raw).digest()[:4]).decode().rstrip("=")
                return api(ctx, url, "devices/register", {"code": code, "label": label})["device_id"]
            previous = device("기존 업무폰")
            api(ctx, url, f"devices/{previous}/confirm", {})
            new = device("추가 업무폰")
            occupied = api(ctx, url, "crews", {"name": "000 기존 팀", "device_id": previous})["id"]
            available = api(ctx, url, "crews", {"name": "001 빈 팀"})["id"]
            page = ctx.new_page()
            page.goto(url + "#/settings/devices")
            page.get_by_role("button", name="지문 같음 · 사용 시작").click()
            chooser = page.get_by_role("dialog", name="어느 팀에 연결할까요?")
            pick = chooser.get_by_label("연결할 팀")
            expect(pick).to_have_value(available)
            pick.select_option(occupied)
            chooser.get_by_role("button", name="연결", exact=True).click()
            expect(chooser.get_by_role("alert")).to_contain_text("교체")
            def linked():
                return next(c["device_id"] for c in api(ctx, url, "bootstrap")["crews"] if c["id"] == occupied)
            self.assertEqual(linked(), previous)
            replace = chooser.get_by_role("checkbox", name="기존 휴대폰 교체 확인")
            replace.check()
            pick.select_option(available)
            pick.select_option(occupied)
            expect(replace).not_to_be_checked()
            replace.check()
            chooser.get_by_role("button", name="연결", exact=True).click()
            expect(chooser).to_have_count(0)
            self.assertEqual(linked(), new)
            for crew in api(ctx, url, "bootstrap")["crews"]:
                if crew["active"] and not crew["device_id"]:
                    api(ctx, url, "crews", {**crew, "active": False})
            device("대기 업무폰")
            page.reload()
            page.get_by_role("button", name="지문 같음 · 사용 시작").click()
            expect(pick).to_have_value("__new")
            chooser.get_by_role("button", name="나중에", exact=True).click()
            self.assertEqual(linked(), new)

    def test_pc_registration_methods_without_relay(self):
        with (pc_server() as url, sync_playwright() as pw,
              pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path) as browser):
            ctx = browser.new_context(service_workers="block")
            api(ctx, url, "setup", {"username": "pair_ui", "password": "pair-ui-123-test", "display_name": "등록 담당"})
            page = ctx.new_page()
            page.goto(url + "#/settings/devices")
            expect(page.get_by_role("button", name="휴대폰 QR 웹캠으로 읽기")).to_be_visible()
            page.get_by_text("간편 등록 중계 설정", exact=True).click(timeout=2500)
            relay = page.get_by_label("간편 등록 중계 주소")
            expect(relay).to_be_visible()
            self.assertEqual(relay.input_value(), "")
            expect(page.get_by_role("button", name="6자리 간편 등록 시작")).to_be_disabled()

    def test_encrypted_explicit_submission_and_lock(self):
        registration = dgproto.b45encode(dgproto.MAGIC + bytes([dgproto.VERSION, 9]) +
                                          dgproto.pub_raw(dgproto.gen_key()) + dgproto.pub_raw(dgproto.gen_key()))
        secret = "c" * 43
        relay = "https://relay.example.test"
        room = hashlib.sha256(("dg-pair-room-v1:" + secret).encode()).hexdigest()
        path = f"{relay}/v1/rooms/{room}/submit"

        def qr(base, **extra):
            data = json.dumps({"registration": registration, "relay": base, "secret": secret, **extra}).encode()
            return "DGPAIR1." + base64.urlsafe_b64encode(data).decode().rstrip("=")

        with (mobile_server() as url, sync_playwright() as pw,
              pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path,
                                 args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]) as browser):
            ctx, page = open_mobile(browser, url)
            ctx.grant_permissions(["camera"], origin=url.rstrip("/"))
            seed(page, registered=False)
            page.evaluate("window.jsQR = () => { const data = window.testScan; window.testScan = ''; return data ? {data} : null; }")
            submissions = []

            def relay_post(route):
                response = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST,OPTIONS",
                            "Access-Control-Allow-Headers": "Content-Type"}
                if route.request.method == "OPTIONS":
                    route.fulfill(status=204, headers=response)
                    return
                submissions.append(route.request.post_data_json["payload"])
                route.fulfill(status=503 if len(submissions) == 1 else 200, json={}, headers=response)

            ctx.route(path, relay_post)
            page.goto(url + "#/register")
            page.evaluate("value => window.testScan = value", qr("http://relay.example.test"))
            expect(page.get_by_text("HTTPS", exact=False)).to_be_visible()
            page.evaluate("value => window.testScan = value", qr(relay))
            send = page.get_by_role("button", name="등록번호 받기", exact=True)
            expect(send).to_be_visible()
            self.assertFalse(submissions)
            expect(page.get_by_label("PC에 입력할 등록번호")).to_have_count(0)
            send.click()
            expect(page.get_by_text("다시 시도", exact=False)).to_be_visible()
            expect(page.get_by_label("PC에 입력할 등록번호")).to_have_count(0)
            send.click()
            number = page.get_by_label("PC에 입력할 등록번호")
            expect(number).to_be_visible()
            self.assertEqual(len(submissions), 2)
            self.assertEqual(submissions[0], submissions[1])
            raw = base64.urlsafe_b64decode(submissions[0] + "===")
            key = hashlib.sha256(("dg-pair-data-v1:" + secret).encode()).digest()
            packet = json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], ("DGPAIR1:" + room).encode()))
            totp_key = base64.urlsafe_b64decode(packet["totp_key"] + "=")
            self.assertEqual(len(totp_key), 20)
            signature = base64.urlsafe_b64decode(packet["proof"] + "==")
            self.assertEqual(len(signature), 64)
            _, _, signing_pub = dgproto.parse_device_code(packet["code"])
            signed = f"DGPAIR1-proof:{room}:{packet['code']}:{packet['totp_key']}".encode()
            dgproto.pub_from_raw(signing_pub).verify(
                utils.encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")),
                signed, ec.ECDSA(hashes.SHA256()))
            def totp(counter):
                mac = hmac.digest(totp_key, counter.to_bytes(8, "big"), "sha1")
                start = mac[-1] & 15
                return f"{(int.from_bytes(mac[start:start + 4], 'big') & 0x7fffffff) % 1000000:06d}"
            tick = page.evaluate("Math.floor(Date.now()/30000)")
            self.assertIn(number.inner_text(), [totp(tick - 1), totp(tick)])
            next_tick = tick + 1
            page.clock.set_fixed_time(datetime.fromtimestamp(next_tick * 30 + 4, timezone.utc))
            expect(number).to_have_text(totp(next_tick))
            self.assertTrue(packet["code"].startswith("DGD1."))
            page.evaluate("async () => (await import('./core.js')).lockNow()")
            page.get_by_placeholder("PIN", exact=True).fill(PIN)
            page.get_by_role("button", name="잠금 해제", exact=True).click()
            page.goto(url + "#/register")
            old = page.get_by_role("button", name="PC 등록 QR 다시 읽기")
            expect(old).to_be_visible()
            expect(number).to_have_count(0)
            expect(send).to_have_count(0)
            old.click()
            page.evaluate("value => window.testScan = value", registration)
            expect(page.get_by_role("img", name="기기 코드 QR")).to_be_visible()
            expect(send).to_have_count(0)
            old.click()
            other_pc = dgproto.b45encode(dgproto.MAGIC + bytes([dgproto.VERSION, 9]) +
                                          dgproto.pub_raw(dgproto.gen_key()) + dgproto.pub_raw(dgproto.gen_key()))
            page.evaluate("value => window.testScan = value", qr(relay, registration=other_pc))
            replacement = page.get_by_role("dialog", name="등록된 PC 변경")
            expect(replacement).to_be_visible()
            replacement.get_by_role("button", name="취소", exact=True).click()
            expect(page.get_by_role("img", name="기기 코드 QR")).to_be_visible()
            saved = page.evaluate("""async () => {
                const P = await import('./proto.js'), S = await import('./store.js'), pc = await S.getRaw('pc');
                return P.b45encode(P.concat(new Uint8Array([0x44,0x47,1,9]), pc.pcSignPub, pc.pcKexPub));
            }""")
            self.assertEqual(saved, registration)
            old.click()
            near_expiry = page.evaluate("Math.floor(Date.now()/1000) + 3")
            page.evaluate("value => window.testScan = value", qr(relay, expires_at=near_expiry))
            expect(send).to_be_visible()
            expect(page.get_by_role("heading", name="등록 시간이 끝났습니다")).to_be_visible()
            expect(send).to_have_count(0)


if __name__ == "__main__":
    unittest.main()
