"""브라우저 종단 테스트 (가상 자료).

헤드리스 Chrome의 가짜 카메라에 QR 영상을 넣어, 실제 화면 흐름 전체를 확인한다.
  기기 등록 → 목록 QR 수신 → 연락·현장 확인·품목 추가·수거 결과 → 결과 파일 → PC 가져오기 → 확인 코드
실행: python3 tests/e2e.py   (playwright, Chrome 필요)
"""
from __future__ import annotations

import base64
import http.server
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from functools import partial
from pathlib import Path

from PIL import Image
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
WORK = Path(os.environ.get("DG_TEST_DIR") or tempfile.mkdtemp(prefix="dg-e2e-"))
SHOTS = WORK / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)
ENV = {**os.environ, "DG_STATE": str(WORK / "state"), "DG_OUT": str(WORK / "out")}
PIN = "246813"
CHROME = os.environ.get("CHROME", "/usr/bin/google-chrome")
passed = 0


def ok(cond, name):
    global passed
    if not cond:
        raise AssertionError("실패: " + name)
    passed += 1
    print("  ✓", name)


def py(*args) -> str:
    return subprocess.run([sys.executable, str(ROOT / "pc" / "pc_tool.py"), *args], env=ENV,
                          capture_output=True, text=True, check=True).stdout


def qr_png(text: str) -> Image.Image:
    sys.path.insert(0, str(ROOT / "pc"))
    import pc_tool  # noqa: E402
    uri, _ = pc_tool.qr_png_data_uri(text, "M")
    return Image.open(io.BytesIO(base64.b64decode(uri.split(",")[1]))).convert("RGB")


def write_y4m(path: Path, images: list[Image.Image], hold: int = 4, w: int = 1280, h: int = 720, loops: int = 3):
    """QR 이미지를 순환 재생하는 Y4M 영상 (Chrome 가짜 카메라 입력, 1280x720 가운데 배치)."""
    with open(path, "wb") as f:
        f.write(f"YUV4MPEG2 W{w} H{h} F10:1 Ip A1:1 C420jpeg\n".encode())
        frames = []
        side = h - 40
        for im in images:
            canvas = Image.new("RGB", (w, h), "white")
            canvas.paste(im.resize((side, side), Image.NEAREST), ((w - side) // 2, 20))
            y, cb, cr = canvas.convert("YCbCr").split()
            half = (w // 2, h // 2)
            frames.append(y.tobytes() + cb.resize(half).tobytes() + cr.resize(half).tobytes())
        for _ in range(loops):
            for fr in frames:
                for _ in range(hold):
                    f.write(b"FRAME\n" + fr)


def serve(directory: Path) -> int:
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    handler = partial(Quiet, directory=str(directory))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def browser(pw, video: Path):
    ctx = pw.chromium.launch_persistent_context(
        str(WORK / "profile"), executable_path=CHROME, headless=True,
        viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True,
        accept_downloads=True, permissions=["camera"],
        args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
              f"--use-file-for-fake-video-capture={video}"])
    return ctx, (ctx.pages[0] if ctx.pages else ctx.new_page())


def unlock(page):
    if page.get_by_placeholder("PIN").count():
        page.get_by_placeholder("PIN").fill(PIN)
        page.get_by_role("button", name="잠금 해제").click()


def main():
    print("작업 폴더:", WORK)
    port = serve(ROOT / "web")
    url = f"http://127.0.0.1:{port}/index.html"
    py("init")
    reg_text = subprocess.run([sys.executable, "-c", "import pc_tool;print(pc_tool.registration_text())"],
                              cwd=ROOT / "pc", env=ENV, capture_output=True, text=True, check=True).stdout.strip()
    write_y4m(WORK / "reg.y4m", [qr_png(reg_text)], hold=10, loops=2)

    with sync_playwright() as pw:
        # 1) PIN 설정과 기기 등록 -------------------------------------------------
        ctx, page = browser(pw, WORK / "reg.y4m")
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.get_by_placeholder("숫자 6자리 이상").fill(PIN)
        page.get_by_placeholder("한 번 더").fill(PIN)
        page.get_by_role("button", name="PIN 설정").click()
        expect(page.get_by_text("먼저 관리자와 함께 기기 등록")).to_be_visible(timeout=20000)
        page.screenshot(path=SHOTS / "01_home_unregistered.png")
        page.get_by_role("button", name="기기 등록").click()
        code_box = page.locator("textarea.code")
        expect(code_box).to_be_visible(timeout=30000)
        code = code_box.input_value()
        fp_phone = page.locator(".fp").inner_text().replace(" ", "")
        page.screenshot(path=SHOTS / "02_register.png", full_page=True)
        out = py("register-device", code)
        ok(fp_phone in out, f"카메라로 등록 QR 인식, PC·휴대폰 지문 일치 ({fp_phone})")
        page.get_by_role("button", name="완료").click()
        dev_short = re.search(r"등록 완료: ([0-9a-f]{8})", out).group(1)
        ctx.close()

        # 2) 목록 QR 수신 ----------------------------------------------------------
        mk = py("make-bundle", "--device", dev_short, "--count", "20", "--date", "2026-11-03")
        short = re.search(r"묶음 ([0-9a-f]{8})", mk).group(1)
        frames = json.loads((WORK / "out" / f"bundle_{short}.frames.json").read_text())
        print("  ·", mk.splitlines()[0])
        write_y4m(WORK / "list.y4m", [qr_png(f) for f in frames], hold=4, loops=4)
        ctx, page = browser(pw, WORK / "list.y4m")
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        unlock(page)
        page.get_by_role("button", name="오늘 목록 받기").click()
        expect(page.get_by_text("목록을 받았습니다")).to_be_visible(timeout=60000)
        receipt = page.locator(".fp").inner_text().replace(" ", "")
        page.screenshot(path=SHOTS / "03_received.png")
        py("confirm-receipt", short, receipt)
        ok("수신 확인" in py("status"), f"분할 QR {len(frames)}장 카메라 수신 → 수신 확인 코드 PC 대조 ({receipt})")
        page.get_by_role("button", name="오늘 순서 보기").click()
        ok(page.locator(".stop").count() == 20, "오늘 순서 20건 표시")
        page.screenshot(path=SHOTS / "04_list.png")

        # 3) 상세: 이름·연락처·주소, 연락, 현장 확인, 추가, 결과 ---------------------
        first = page.locator(".stop").first
        name = first.locator("strong").inner_text()
        first.click()
        expect(page.locator(".who .name")).to_have_text(name)
        phone = page.locator(".who .phone").inner_text()
        ok(phone.startswith("010-"), f"상세 화면에 이름·연락처·주소 표시 ({name}, {phone})")
        page.get_by_role("button", name="통화 완료").click()
        expect(page.get_by_role("button", name="통화 완료")).to_have_class(re.compile("on"))
        received_total = page.locator(".fee dd").nth(0).inner_text()

        # 현장 추가: 회전 의자 2개
        page.get_by_role("button", name="+ 현장 품목 추가").click()
        page.get_by_placeholder(re.compile("품목명")).fill("회전 의자")
        page.locator(".btn.result").first.click()
        page.locator(".stepper").first.locator(".btn.step").last.click()  # 수량 2
        # 검색어를 바꾸면 선택이 풀리는지
        page.get_by_placeholder(re.compile("품목명")).fill("매트")
        ok(page.get_by_role("button", name="추가", exact=True).is_disabled(), "검색어 변경 시 이전 선택 해제")
        page.get_by_placeholder(re.compile("품목명")).fill("사무용의자")
        ok("회전식" in page.locator(".btn.result").first.inner_text(), "현장 표현(사무용의자) → 공식 품목(의자 회전식) 연결")
        page.locator(".btn.result").first.click()
        page.get_by_role("button", name="추가", exact=True).click()
        page.get_by_role("button", name="+ 현장 품목 추가").click()
        page.get_by_placeholder("품목 이름 (예: 안마기)").fill("안마기")
        page.get_by_role("button", name="확인 대기로 추가").click()
        expect(page.locator(".line.field")).to_have_count(2)
        fee_text = page.locator(".fee").inner_text()
        ok("미확정" in fee_text and "1건" in fee_text, "목록에 없는 품목은 0원이 아니라 확인 대기, 최종 금액 미확정")
        ok("6,000원" in page.locator(".fee dd").nth(1).inner_text(), "회전식 의자 2개 = 현장 추가분 6,000원")

        # 첫 원접수 품목은 1개만 수거하고 나머지는 사유 기록
        orig = page.locator(".line.orig").first
        orig.locator(".row").nth(2).locator(".btn.step").last.click()  # 실제 수거 +1
        page.get_by_role("button", name="완료", exact=True).click()
        ok("남은 대상이 있어" in page.locator("section .msg").last.inner_text(), "남은 대상이 있으면 완료 저장 막음")
        page.get_by_role("button", name="모두 수거함").click()
        # 안마기는 수거 못 함: 수거 0 → 사유
        fld = page.locator(".line.field").last
        fld.locator(".row").nth(1).locator(".btn.step").first.click()  # 실제 수거 −1
        fld.locator("select.reason").select_option("CANNOT_LOAD")
        page.get_by_role("button", name="일부 수거", exact=True).click()
        expect(page.get_by_text("저장됨: 일부 수거")).to_be_visible()
        ok(page.locator(".line.field").last.locator(".row.left .val").inner_text().startswith("1"),
           "현장 추가 품목의 남은 대상 1 유지 (다른 품목 수거로 상쇄 안 됨)")
        page.screenshot(path=SHOTS / "05_stop_top.png")
        page.locator(".fee").scroll_into_view_if_needed()
        page.screenshot(path=SHOTS / "06_stop_fee.png")

        # 4) 앱 재시작 후 기록 유지 ------------------------------------------------
        page.reload()
        unlock(page)
        expect(page.get_by_text("저장됨: 일부 수거")).to_be_visible(timeout=20000)
        ok(page.locator(".line.field").count() == 2, "재시작 후 현장 기록 유지")

        # 5) 결과 보내기 → PC 가져오기 → 확인 코드 ---------------------------------
        page.goto(url + "#/export")
        with page.expect_download() as dl:
            page.get_by_role("button", name=re.compile("결과 파일 만들어 보내기")).click()
        f = WORK / dl.value.suggested_filename
        dl.value.save_as(f)
        imp = py("import-result", str(f))
        release = re.search(r"보관 종료 확인 코드: (\w+)", imp).group(1)
        ok('"applied"' in imp, "결과 파일 PC 가져오기 성공 " + imp.splitlines()[0])
        blob = f.read_bytes()
        ok(name.encode() not in blob and phone.replace("-", "").encode() not in blob, "결과 파일에 이름·전화번호 평문 없음")
        page.get_by_placeholder("확인 코드 8자리").fill(release)
        page.get_by_role("button", name="확인 코드 입력").click()
        expect(page.get_by_text("반영 확인됨")).to_be_visible()
        ok(True, f"보관 종료 확인 코드 입력 → 반영 확인 ({release})")
        page.screenshot(path=SHOTS / "07_export.png", full_page=True)

        # 6) 오프라인에서도 동작 (서비스 워커) -----------------------------------
        page.goto(url + "#/home")
        page.wait_for_timeout(1500)
        ctx.set_offline(True)
        page.reload()
        unlock(page)
        expect(page.locator(".stop").first).to_be_visible(timeout=20000)
        ok(page.locator(".stop").count() == 20, "통신 차단 상태에서 재실행해도 목록 표시")
        ctx.set_offline(False)

        # 7) 저장소에 개인정보 평문이 없는지 ---------------------------------------
        dump = page.evaluate("""async () => {
          const db = await new Promise(r => { const q = indexedDB.open('dg-proto'); q.onsuccess = () => r(q.result); });
          const all = await new Promise(r => { const q = db.transaction('kv').objectStore('kv').getAll(); q.onsuccess = () => r(q.result); });
          const keys = await new Promise(r => { const q = db.transaction('kv').objectStore('kv').getAllKeys(); q.onsuccess = () => r(q.result); });
          const parts = all.map(v => { try { return JSON.stringify(v, (k, x) => x instanceof Uint8Array ? Array.from(x) : x); } catch { return ''; } });
          return { keys, text: parts.join('|') };
        }""")
        raw_bytes = bytes(int(x) for x in re.findall(r"\d+", dump["text"]) if int(x) < 256)
        ok(name not in dump["text"] and phone.replace("-", "") not in dump["text"] and name.encode() not in raw_bytes,
           f"휴대폰 저장소(IndexedDB {len(dump['keys'])}개 항목)에 이름·전화번호 평문 없음")
        ok(not errors, "페이지 스크립트 오류 없음" + (": " + "; ".join(errors) if errors else ""))
        ctx.close()
    print(f"\n통과 {passed}개, 화면 캡처: {SHOTS}")


if __name__ == "__main__":
    main()
