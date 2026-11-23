"""미등록 개인폰이 PC 화면의 두 QR을 실제로 판독하고 오프라인에서 다시 조회한다."""
import base64
import json
import os

from playwright.sync_api import expect, sync_playwright

from test_dong_work_e2e import api
from test_review_ui import PIN, ROOT, mobile_server, pc_server


def send_camera_image(phone, image):
    src = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
    phone.evaluate("""async src => {
        const img = new Image(); img.src = src; await img.decode();
        const ctx = window.cameraCanvas.getContext('2d');
        ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 820, 820);
        ctx.imageSmoothingEnabled = false; ctx.drawImage(img, 30, 30, 760, 760);
    }""", src)


def open_scanner(phone, pwa):
    phone.goto(pwa)
    phone.get_by_placeholder("숫자 6자리 이상").fill(PIN)
    phone.get_by_placeholder("한 번 더", exact=True).fill(PIN)
    phone.get_by_role("button", name="PIN 설정", exact=True).click()
    phone.get_by_role("button", name="수거팀으로 시작", exact=True).click()
    entry = phone.get_by_role("button", name="배차 QR로 업무 시작", exact=True)
    expect(entry).to_be_visible()
    phone.evaluate("""() => {
        const canvas = document.createElement('canvas'); canvas.width = canvas.height = 820;
        window.cameraCanvas = canvas;
        navigator.mediaDevices.getUserMedia = async () => {
            const stream = canvas.captureStream(15);
            const track = stream.getVideoTracks()[0];
            const redraw = () => {
                if (track.readyState === 'ended') return;
                canvas.getContext('2d').fillRect(0, 0, 1, 1);
                setTimeout(redraw, 100);
            };
            redraw(); return stream;
        };
    }""")
    entry.click()
    expect(phone).to_have_url(pwa + "#/guest-list")
    phone.get_by_role("button", name="배차 QR 읽기", exact=True).click()


def assign_visit(pc, office):
    api(pc, office, "setup", {"username": "guest_fixture", "display_name": "가상 담당자",
                              "password": "guest-list-fixture-password"})
    day = api(pc, office, "bootstrap")["today"]
    crew = api(pc, office, "crews", {"name": "가상 방문팀", "capacity_m3": 12})["id"]
    fee = next(r for r in json.loads((ROOT / "data/fee_table.json").read_text(encoding="utf8"))["rows"]
               if r["status"] == "CONFIRMED" and r["price"] > 0)
    address = "가상구 시험로 123"
    req = api(pc, office, "requests", {"name": "가상 비공개 신청인", "address": address,
        "phone": "01000001234", "memo": "시험용 비공개 메모", "planned_date": day,
        "lat": 35.146, "lon": 126.922, "items": [{"fee_rate_id": fee["id"], "qty": 3}]})
    api(pc, office, "assign", {"date": day, "plan": {crew: [{"request_id": req["id"]}]}})
    return day, crew, req, fee, address


def main():
    with mobile_server() as pwa, pc_server() as office, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path)
        phone_ctx = browser.new_context(viewport={"width": 360, "height": 640})
        pc = browser.new_context()
        try:
            phone = phone_ctx.new_page()
            open_scanner(phone, pwa)

            day, crew, req, fee, address = assign_visit(pc, office)
            desk = pc.new_page()
            desk.goto(office + "#/send?date=" + day)
            desk.get_by_role("button", name="미등록폰 조회용 QR", exact=True).click()
            dialog = desk.get_by_role("dialog")
            qr = dialog.get_by_role("img", name="열쇠 QR", exact=True)
            expect(qr).to_be_visible()
            send_camera_image(phone, qr.screenshot())
            expect(phone.get_by_text("목록 QR을 비추세요", exact=False).first).to_be_visible(timeout=15000)
            key_read = phone.get_by_text("열쇠 읽음", exact=True)
            expect(key_read).to_be_in_viewport(ratio=1)
            assert key_read.evaluate("el => parseFloat(getComputedStyle(el).fontSize)") >= 24
            expect(phone.get_by_text("PC에서 [목록 QR 표시]를 누르세요.", exact=True)).to_be_in_viewport(ratio=1)
            desk.get_by_role("button", name="목록 QR 표시", exact=True).click()
            qr = dialog.get_by_role("img", name="목록 QR", exact=True)
            expect(qr).to_be_visible()
            phone.wait_for_function("navigator.serviceWorker.controller !== null")
            phone_ctx.set_offline(True)
            send_camera_image(phone, qr.screenshot())
            expect(phone.get_by_text(address, exact=False).first).to_be_visible(timeout=20000)
            assert fee["name"] in phone.locator("main").inner_text()
            for private in ("가상 비공개 신청인", "01000001234", "시험용 비공개 메모"):
                assert private not in phone.locator("main").inner_text()
            assert not api(pc, office, "batches?date=" + day)[0]["bundles"]
            assert phone.evaluate("""async () => {
                const C = await import('./core.js'), S = await import('./store.js');
                return !await S.getRaw('device') && !await S.getRaw('pc') &&
                       !C.st.daylist && !C.st.events.length && !C.st.lastReceipt;
            }""")
            phone.reload()
            expect(phone.get_by_text(address)).to_have_count(0)
            phone.get_by_label("PIN", exact=True).fill(PIN)
            phone.get_by_role("button", name="잠금 해제").click()
            if phone.url.endswith("#/home"):
                phone.get_by_role("button", name="받은 배차 목록 열기", exact=True).click()
            expect(phone.get_by_text(address, exact=False).first).to_be_visible()
            saved = phone.evaluate("import('./store.js').then(S => S.getSecure('guestList'))")
            phone.get_by_role("button", name="목록 지우기", exact=True).click()
            confirm = phone.get_by_role("dialog")
            confirm.get_by_role("button", name="취소", exact=True).click()
            expect(phone.get_by_text(address, exact=False).first).to_be_visible()
            phone.get_by_role("button", name="목록 지우기", exact=True).click()
            confirm.get_by_role("button", name="지우기", exact=False).click()
            expect(phone.get_by_text(address, exact=False)).to_have_count(0)
            assert phone.evaluate("import('./store.js').then(S => S.getSecure('guestList'))") is None

            # 같은 기기에서 수거일이 지난 뒤 앱을 잠금 해제하는 경우.
            phone.evaluate("""async saved => {
                const C = await import('./core.js'), S = await import('./store.js');
                await S.putSecure('guestList', { ...saved, work_date: C.todayKST(-1) });
                location.hash = '#/home'; await C.lockNow();
            }""", saved)
            phone.get_by_label("PIN", exact=True).fill(PIN)
            phone.get_by_role("button", name="잠금 해제").click()
            expect(phone.get_by_role("button", name="배차 QR로 업무 시작", exact=True)).to_be_visible()
            assert phone.evaluate("import('./store.js').then(S => S.getSecure('guestList'))") is None
            print("미등록폰 PC QR 판독, 비공개 필드 제외, 등록·업무 기록 분리, 오프라인 재열람, 삭제·만료 통과")
        finally:
            pc.close()
            phone_ctx.close()
            browser.close()


if __name__ == "__main__":
    main()
