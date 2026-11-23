"""조회용 QR 저장 재시도·교체 취소·구본 거절, 일반 수거 기록 보존.

카메라의 판독 문자열만 가상으로 전달한다. 목록 암호·QR 조각·PIN·IndexedDB는 실제 경로를 쓴다.
"""
import base64
import json
import os
import sys
import uuid
import zlib
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc"))
import dgproto
from test_review_ui import mobile_server
from test_workflow_mobile import daylist, event, open_mobile, route, seed, stop


def guest_qr(payload):
    key = AESGCM.generate_key(bit_length=256)
    bundle = uuid.uuid4()
    context = "DGVIEW1:" + bundle.hex
    nonce = os.urandom(12)
    data = zlib.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    envelope = nonce + AESGCM(key).encrypt(nonce, data, context.encode("ascii"))
    return [context + ":" + base64.urlsafe_b64encode(key).decode("ascii").rstrip("="),
            *["DGV1." + f for f in dgproto.split_frames(envelope, bundle.bytes)]]


def scan(page, payload):
    page.evaluate("frames => { window.guestFrames = frames; }", guest_qr(payload))
    page.get_by_role("button", name="조회용 QR 읽기", exact=True).click()


def main():
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path)
        ctx, page = open_mobile(browser, url)
        try:
            today = page.evaluate("async () => (await import('./core.js')).todayKST()")
            state = {"daylist": daylist(today, [stop(1, today)]), "events": [event(1)]}
            seed(page, state, registered=False)
            page.evaluate("""() => {
                window.jsQR = () => guestFrames.length ? { data: guestFrames.shift() } : null;
                navigator.mediaDevices.getUserMedia = async () => {
                    const canvas = document.createElement('canvas'); canvas.width = canvas.height = 40;
                    const stream = canvas.captureStream(10);
                    const track = stream.getVideoTracks()[0];
                    const draw = () => {
                        if (track.readyState !== 'ended') {
                            canvas.getContext('2d').fillRect(0, 0, 1, 1); setTimeout(draw, 100);
                        }
                    };
                    draw(); return stream;
                };
            }""")
            normal = """async () => { const C = await import('./core.js'), S = await import('./store.js');
                const disk = await S.getSecure('state');
                return [C.st.events, C.st.daylist, disk.events, disk.daylist]; }"""
            original = page.evaluate(normal)
            current = {"schema": "dg.guest-list/1", "work_date": today, "crew": "가상 1팀", "crew_id": "mock-1",
                       "assign_ver": 2, "issue_no": 11, "stops": [
                           {"seq": 1, "dong": "가상동", "address": "시험로 원주소", "items": [
                               {"name": "시험 의자", "spec": "", "qty": 2, "unit": "개"}]}]}
            guest = "async () => (await import('./store.js')).getSecure('guestList')"
            page.evaluate("async list => (await import('./store.js')).putSecure('guestList', list)", current)
            route(page, "#/guest-list")
            newer = {**current, "issue_no": 12, "stops": [{**current["stops"][0], "address": "시험로 변경주소"}]}
            page.evaluate("""() => {
                window.guestPut = IDBObjectStore.prototype.put;
                IDBObjectStore.prototype.put = function (record, key) {
                    if (key === 's:guestList') throw new DOMException('시험 저장 실패', 'QuotaExceededError');
                    return window.guestPut.apply(this, arguments);
                };
            }""")
            scan(page, newer)
            retry = page.get_by_role("button", name="조회 목록 저장 다시 시도", exact=True)
            expect(retry).to_be_visible(timeout=10000)
            assert page.evaluate(guest) == current
            assert page.evaluate(normal) == original
            page.evaluate("() => { IDBObjectStore.prototype.put = window.guestPut; }")
            retry.click()
            expect(page.get_by_role("heading", name="1번 · 시험로 변경주소")).to_be_visible()
            assert page.evaluate(guest) == newer
            assert page.evaluate(normal) == original

            scan(page, {**newer, "crew": "가상 2팀", "crew_id": "mock-2", "issue_no": 13, "stops": []})
            dialog = page.get_by_role("dialog", name="조회 목록을 바꿀까요?")
            expect(dialog).to_be_visible()
            dialog.get_by_role("button", name="취소", exact=True).click()
            expect(page.get_by_role("heading", name="1번 · 시험로 변경주소")).to_be_visible()
            assert page.evaluate(guest) == newer

            scan(page, current)
            expect(page.get_by_text("이전 배차 QR입니다.", exact=False)).to_be_visible()
            assert page.evaluate(guest) == newer
            assert page.evaluate(normal) == original

            final = {**newer, "issue_no": 15, "stops": []}
            codes = guest_qr(final)
            page.evaluate("frames => { window.guestFrames = frames; }", [guest_qr(newer)[0], codes[0]])
            restart = page.get_by_role("button", name="열쇠부터 다시 읽기", exact=True)
            restart.click()
            expect(page.get_by_text("다른 열쇠 QR입니다.", exact=False)).to_be_visible()
            expect(page.get_by_text("열쇠 읽음", exact=True)).to_be_visible()
            assert page.evaluate(guest) == newer
            restart.click()
            expect(page.get_by_text("1단계 · 열쇠 QR", exact=True)).to_be_visible()
            expect(page.get_by_text("열쇠 읽음", exact=True)).to_have_count(0)
            page.evaluate("frames => { window.guestFrames = frames; }", codes)
            expect(page.get_by_text("이번 배차에는 방문할 수거지가 없습니다.")).to_be_visible()
            assert page.evaluate(guest) == final
            assert page.evaluate(normal) == original
            print("저장 재시도, 교체 취소, 구본 거절, 새 열쇠로 다시 읽기, 빈 배차, 일반 업무 상태 보존 통과")
        finally:
            ctx.close()
            browser.close()


if __name__ == "__main__":
    main()
