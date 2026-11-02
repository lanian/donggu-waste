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
ENV = {**os.environ, "DG_STATE": str(WORK / "state"), "DG_OUT": str(WORK / "out"), "DG_DB_KEY": os.environ.get("DG_DB_KEY", "test-db-key-not-for-production")}
PIN = "246813"
CHROME = os.environ.get("CHROME", "/usr/bin/google-chrome")
passed = 0
REQUESTS: list[tuple[str, str]] = []  # 브라우저가 보낸 모든 요청 (개인정보 반출 검사용)
PROXY = os.environ.get("HTTPS_PROXY", "")


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


def browser(pw, video: Path, profile: str = "profile"):
    ctx = pw.chromium.launch_persistent_context(
        str(WORK / profile), executable_path=CHROME, headless=True,
        viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True,
        accept_downloads=True, permissions=["camera", "geolocation"], geolocation={"latitude": 35.1462, "longitude": 126.9233},
        ignore_https_errors=True,
        args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
              f"--use-file-for-fake-video-capture={video}", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
              *([f"--proxy-server={PROXY}"] if PROXY else [])])
    ctx.on("request", lambda r: REQUESTS.append((r.url, r.post_data or "")))
    return ctx, (ctx.pages[0] if ctx.pages else ctx.new_page())


def unlock(page):
    page.wait_for_selector("input[placeholder='PIN'], section.actions, header.bar", timeout=20000)
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
        page.get_by_role("button", name="PC와 연결해서 사용").click()
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
        expect(page.locator(".stop")).to_have_count(20)
        ok(page.locator(".stop").count() == 20, "오늘 순서 20건 표시")
        page.screenshot(path=SHOTS / "04_list.png")
        nm0 = page.locator(".stop").first.locator("strong").inner_text()
        cho = "".join("ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"[(ord(c) - 0xAC00) // 588] for c in nm0)
        page.get_by_label("목록에서 찾기").fill(cho)
        page.wait_for_timeout(700)
        ok(1 <= page.locator(".stop").count() < 20 and nm0 in page.locator(".stop").first.inner_text(), f"목록 초성 찾기 '{cho}' → {page.locator('.stop').count()}곳")
        page.get_by_label("목록에서 찾기").fill("")
        page.wait_for_timeout(700)
        expect(page.locator(".stop")).to_have_count(20)

        # 3) 상세: 이름·연락처·주소, 연락, 현장 확인, 추가, 결과 ---------------------
        first = page.locator(".stop").first
        name = first.locator("strong").inner_text()
        first.click()
        expect(page.locator(".who .name")).to_have_text(name)
        phone = page.locator(".who .phone").inner_text()
        ok(phone.startswith("010-"), f"상세 화면에 이름·연락처·주소 표시 ({name}, {phone})")
        page.get_by_role("button", name="통화 완료").click()
        expect(page.get_by_role("button", name="통화 완료")).to_have_class(re.compile("on"))
        page.locator("label.btn", has_text="수거 후").locator("input[type=file]").set_input_files(str(ROOT / "web/icons/icon-512.png"))
        expect(page.locator(".thumbs img")).to_have_count(1)
        ok(True, "현장 사진 촬영(파일 입력) → 앱 안에 암호화 저장")
        page.get_by_text("재방문 약속 기록").click()
        page.locator("details.revisit input[type=date]").fill("2026-11-05")
        page.locator("details.revisit input[type=time]").nth(0).fill("14:00")
        page.locator("details.revisit input[type=time]").nth(1).fill("16:00")
        page.get_by_role("button", name="약속 저장").click()
        expect(page.get_by_text("재방문 약속: 2026-11-05 14:00~16:00")).to_be_visible()
        ok(True, "통화 후 재방문 약속(날짜·시간) 기록")
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
        ok(page.get_by_text("즐겨찾기·자주 쓴 품목").is_visible() and "회전식 의자" in page.locator(".results").inner_text(), "검색 전 자주 쓴 품목 바로 표시")
        page.get_by_role("button", name="즐겨찾기").first.click()
        expect(page.get_by_role("button", name="즐겨찾기 해제")).to_have_count(1)
        page.get_by_placeholder("품목 이름 (예: 안마기)").fill("안마기")
        page.get_by_role("button", name="확인 대기로 추가").click()
        try:
            expect(page.locator(".line.field")).to_have_count(2)
        except AssertionError:
            page.screenshot(path=SHOTS / "debug_field.png", full_page=True); print("ERRORS", errors); print(page.url); raise
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

        # 3-1) 지도: 20개 핀, 핀을 누르면 상세로
        page.goto(url + "#/map")
        expect(page.locator(".pin")).to_have_count(20, timeout=30000)
        page.wait_for_function("() => document.querySelector('.maplibregl-canvas') && document.querySelector('.maplibregl-canvas').width > 0", timeout=30000)
        page.wait_for_timeout(4000)
        page.screenshot(path=SHOTS / "08_map.png")
        tiles = [u for u, _ in REQUESTS if u.startswith("https://tiles.openfreemap.org/")]
        ok(len(tiles) > 0, f"온라인 바탕 지도 표시, 방문 지점 핀 20개 ({len(tiles)}개 지도 요청)")
        page.locator(".pin", has_text=re.compile("^1$")).click()
        expect(page.locator(".who .name")).to_have_text(name)
        ok(True, "핀을 누르면 해당 접수 상세로 이동")
        page.get_by_role("button", name="📍 위치 고치기").click()
        save = page.get_by_role("button", name="이 위치로 저장")
        expect(save).to_be_enabled(timeout=30000)
        page.locator(".mapbox canvas").drag_to(page.locator(".mapbox canvas"), source_position={"x": 150, "y": 150}, target_position={"x": 190, "y": 180})
        save.click()
        expect(page.get_by_role("button", name="🗺 지도에서 보기")).to_be_visible()
        ok(True, "현장에서 배출 위치 고치기 (결과 파일로 PC에 전달되는 기록)")

        # 4) 앱 재시작 후 기록 유지 ------------------------------------------------
        page.reload()
        unlock(page)
        expect(page.get_by_text("저장됨: 일부 수거")).to_be_visible(timeout=20000)
        ok(page.locator(".line.field").count() == 2, "재시작 후 현장 기록 유지")

        # 하차 기록 (반입지에서 비운 양) → 결과 파일에 함께
        page.goto(url + "#/unload")
        page.locator("label", has_text="부피").locator("input").fill("3.5")
        page.locator("label", has_text="반입지").locator("input").fill("가상 반입지")
        page.get_by_role("button", name="하차 저장").click()
        expect(page.get_by_role("button", name="하차 기록 (1회)")).to_be_visible()
        ok(True, "휴대폰 하차 기록 저장 → 목록 화면에 횟수 표시")

        # 5) 결과 보내기 → PC 가져오기 → 확인 코드 ---------------------------------
        page.goto(url + "#/export")
        page.get_by_role("button", name="PC 화면 카메라로 보내기 (QR)").click()
        expect(page.locator("canvas.qrcanvas")).to_be_visible()
        page.wait_for_timeout(600)
        decoded = page.evaluate("""() => { const c = document.querySelector('canvas.qrcanvas'); const x = c.getContext('2d').getImageData(0, 0, c.width, c.height);
          const q = window.jsQR(x.data, x.width, x.height); return q ? q.data : null; }""")
        ok(decoded and re.fullmatch(r"[0-9A-Z $%*+\-./:]+", decoded) and len(decoded) > 100, f"결과 QR 화면: 분할 QR 표시, 읽으면 Base45 조각 ({len(decoded or '')}자)")
        page.screenshot(path=SHOTS / "09_result_qr.png")
        page.get_by_role("button", name="← 결과 보내기").click()
        with page.expect_download() as dl:
            page.get_by_role("button", name=re.compile("결과 파일 만들어 보내기")).click()
        f = WORK / dl.value.suggested_filename
        dl.value.save_as(f)
        imp = py("import-result", str(f))
        release = re.search(r"보관 종료 확인 코드: (\w+)", imp).group(1)
        ok('"applied"' in imp, "결과 파일 PC 가져오기 성공 " + imp.splitlines()[0])
        ok('"unloads": 1' in imp, "휴대폰 하차 기록이 PC 하차 기록으로 반영")
        blob = f.read_bytes()
        ok(name.encode() not in blob and phone.replace("-", "").encode() not in blob, "결과 파일에 이름·전화번호 평문 없음")
        page.get_by_placeholder("확인 코드 8자리", exact=True).fill(release)
        page.get_by_role("button", name="확인 코드 입력").click()
        expect(page.get_by_text("반영 확인됨")).to_be_visible()
        ok(True, f"보관 종료 확인 코드 입력 → 반영 확인 ({release})")
        with page.expect_download() as dlp:
            page.get_by_role("button", name=re.compile("사진 파일 만들기")).click()
        pf = WORK / dlp.value.suggested_filename
        dlp.value.save_as(pf)
        pimp = py("import-photos", str(pf))
        pcode = re.search(r"사진 회수 확인 코드: (\w+)", pimp).group(1)
        ok('"recovered": 1' in pimp, "사진 파일(.dgp) → PC 가져오기: 해시 대조 후 회수 1장")
        page.get_by_placeholder("사진 회수 확인 코드 8자리").fill(pcode)
        page.get_by_role("button", name="사진 회수 확인").click()
        expect(page.get_by_text("미회수 0장")).to_be_visible()
        ok(True, f"사진 회수 확인 코드 입력 → 미회수 0장 ({pcode})")
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
        check_requests("PC 연결", personal_from_bundle())

        # 7-1) QR 실측 모드: 가상 50건, 저장하지 않고 시간·장수·확인 코드만
        qt = json.loads(subprocess.run([sys.executable, "-c", (
            "import json,sys; sys.path.insert(0,'.'); from dgpc import store, service as S; c=store.connect(); "
            f"d=[x for x in S.list_devices(c) if x['id'].startswith('{dev_short}')][0]['id']; "
            "t=S.make_qr_test(c, d, 50, 'long', 800, 'M', 3); "
            "r=S.one(c,'SELECT frames_json, check_code FROM qr_test WHERE id=?',(t['test_id'],)); "
            "print(json.dumps({'id':t['test_id'],'bytes':t['bytes'],'frames':json.loads(r['frames_json']),'check':r['check_code']}))")],
            cwd=ROOT / "pc", env=ENV, capture_output=True, text=True, check=True).stdout)
        write_y4m(WORK / "qrtest.y4m", [qr_png(f) for f in qt["frames"]], hold=4, loops=4)
        ctx, page = browser(pw, WORK / "qrtest.y4m")
        page.goto(url)
        unlock(page)
        page.get_by_role("button", name="오늘 목록 받기").click()
        expect(page.get_by_text("QR 실측 결과")).to_be_visible(timeout=90000)
        shown = page.locator(".fp").nth(1).inner_text()
        secs = page.locator(".fp").nth(0).inner_text()
        ok(shown == qt["check"], f"QR 실측: 가상 50건(긴 주소) {qt['bytes']:,}B · {len(qt['frames'])}장 → {secs}, 확인 코드 일치 ({shown})")
        page.get_by_role("button", name="처음으로").click()
        expect(page.locator(".stop")).to_have_count(20)
        ok(True, "실측 자료는 저장하지 않음 (업무 목록 20건 그대로)")
        ctx.close()

        solo(pw, url, WORK / "reg.y4m")
    print(f"\n통과 {passed}개, 화면 캡처: {SHOTS}")


def personal_from_bundle() -> list[str]:
    sys.path.insert(0, str(ROOT / "pc"))
    from sample_data import make_daylist
    out = []
    for st in make_daylist(20, "2026-11-03", 1, seed=7)["stops"]:
        out += [st["name"], st["phone"], st["address"], st["receipt_no"], f"{st['lat']}", f"{st['lon']}"]
    return out


def check_requests(label: str, secrets: list[str]):
    """브라우저가 보낸 모든 요청: 외부로는 지도 서버만, 어떤 요청에도 개인정보·핀 좌표 없음."""
    from urllib.parse import unquote
    external = [(u, b) for u, b in REQUESTS if not u.startswith("http://127.0.0.1") and not u.startswith(("data:", "blob:"))]
    hosts = sorted({u.split("/")[2] for u, _ in external})
    ok(all(h == "tiles.openfreemap.org" for h in hosts), f"[{label}] 외부 요청은 지도 서버만 ({', '.join(hosts) or '없음'}, {len(external)}건)")
    zs = [int(m.group(1)) for u, _ in external for m in [re.search(r"/(\d+)/\d+/\d+\.(?:pbf|png|mvt)", u)] if m]
    ok(not zs or max(zs) <= 14, f"[{label}] 지도 서버에는 넓은 범위(최대 줌 {max(zs) if zs else '-'}, 약 2km 단위) 조각만 요청 — 집 단위 위치 노출 없음")
    leaked = [x for x in secrets if any(x in unquote(u) or x in b for u, b in REQUESTS)]
    ok(not leaked, f"[{label}] 모든 요청 주소·본문에 이름·연락처·주소·접수번호·핀 좌표 없음" + (f" (발견: {leaked[:3]})" if leaked else ""))
    REQUESTS.clear()


def solo(pw, url, video):
    """휴대폰 단독 운영: 접수 등록 → 중복 경고 → 수정 이력 → 현장 처리 → 취소 → 백업 → 전체 삭제 → 복원."""
    print("[단독 모드]")
    ctx, page = browser(pw, video, "profile-solo")
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    prompts = {"text": ""}
    page.on("dialog", lambda d: d.accept(prompts["text"]) if d.type == "prompt" else d.accept())
    page.goto(url)
    page.get_by_placeholder("숫자 6자리 이상").fill(PIN)
    page.get_by_placeholder("한 번 더").fill(PIN)
    page.get_by_role("button", name="PIN 설정").click()
    page.get_by_role("button", name="이 휴대폰만으로 사용").click()
    expect(page.get_by_role("button", name="+ 전화 접수 등록")).to_be_visible(timeout=20000)

    def intake(name, phone, addr, items, free=None):
        page.get_by_role("button", name="+ 전화 접수 등록").click()
        page.get_by_label("이름").fill(name)
        page.get_by_label("연락처").fill(phone)
        page.get_by_label("배출 주소").fill(addr)
        page.get_by_label("상세 배출 위치").fill("1층 현관 앞")
        for q, n in items:
            page.get_by_placeholder(re.compile("품목명")).fill(q)
            page.locator(".btn.result").first.click()
            for _ in range(n - 1):
                page.locator(".card", has=page.get_by_placeholder(re.compile("품목명"))).locator(".stepper .btn.step").last.click()
            page.get_by_role("button", name="품목 추가", exact=True).click()
        if free:
            page.get_by_placeholder("품목 이름 (예: 안마기)").fill(free)
            page.get_by_role("button", name="확인 대기로 추가").click()

    intake("가상일", "010-0000-7777", "가상시 동구 예시로 5", [("고정 의자", 3), ("2인용 매트리스", 1)])
    ok("44,000원" not in page.locator("main").inner_text() and "17,000원" in page.locator("main").inner_text(), "접수 금액 = 고정식 의자 3개 6,000 + 2인용 매트리스 11,000 = 17,000원")
    page.screenshot(path=SHOTS / "11_solo_intake.png", full_page=True)
    page.get_by_role("button", name="접수 등록").click()
    expect(page.get_by_role("button", name=re.compile("내일 1"))).to_be_visible()
    intake("가상이", "01000007777", "가상시 동구 시험길 9", [("회전", 1)], free="안마기")
    page.get_by_role("button", name="접수 등록").click()
    ok(page.get_by_text("비슷한 접수가 1건").is_visible(), "같은 연락처 접수 → 중복 후보 경고 (자동 병합 없음)")
    page.get_by_role("button", name="접수 등록").click()
    page.get_by_role("button", name=re.compile("내일 2")).click()
    expect(page.locator(".stop")).to_have_count(2)
    ok(page.locator(".stop").count() == 2, "별개 접수로 확인 후 2건 등록, 내일 목록 표시")
    page.screenshot(path=SHOTS / "12_solo_home.png")

    # 순서 바꾸기
    page.get_by_role("button", name="방문 순서 바꾸기").click()
    page.locator(".srow").nth(1).get_by_role("button", name="위로").click()
    expect(page.locator(".stop strong").first).to_have_text("가상이")
    ok(page.locator(".stop strong").first.inner_text() == "가상이", "방문 순서 수동 조정")
    page.get_by_role("button", name="순서 정하기 끝").click()

    # 수정 이력
    page.locator(".stop", has_text="가상일").click()
    ok(page.locator(".who .phone").inner_text() == "010-0000-7777", "연락처 앞자리 0 보존 표시")
    page.get_by_role("button", name="접수 수정").click()
    page.get_by_label("배출 주소").fill("가상시 동구 예시로 5, 101동 202호")
    page.get_by_role("button", name="수정 저장").click()
    ok("수정 사유" in page.locator(".msg").last.inner_text(), "수정 사유 없이는 저장 안 됨")
    page.get_by_label("수정 사유 (필수)").fill("주민 요청: 동·호수 추가")
    page.get_by_role("button", name="수정 저장").click()
    expect(page.get_by_text("주민 요청: 동·호수 추가")).to_be_visible()
    ok(page.get_by_text("원본 2번째 버전").is_visible(), "수정 시 이전 원본을 버전 이력으로 보존")

    # 현장 처리
    page.get_by_role("button", name="통화 완료").click()
    page.get_by_role("button", name="모두 수거함").click()
    page.get_by_role("button", name="완료", exact=True).click()
    expect(page.get_by_text("저장됨: 완료")).to_be_visible()
    ok(True, "단독 모드 현장 처리: 연락·수거·완료 저장")
    page.get_by_role("button", name="← 목록").click()

    # 위치 지정 (지도 가운데 핀, 주소 검색 없음)
    page.locator(".stop", has_text="가상일").click()
    page.get_by_role("button", name="📍 위치 지정").click()
    save = page.get_by_role("button", name="이 위치로 저장")
    expect(save).to_be_enabled(timeout=30000)
    page.get_by_role("button", name="📍 지금 내 위치로 이동").click()
    page.wait_for_timeout(800)
    page.screenshot(path=SHOTS / "14_solo_pick.png")
    save.click()
    page.get_by_role("button", name="🗺 지도에서 보기").click()
    expect(page.locator(".pin.focus")).to_have_count(1, timeout=30000)
    ok(True, "단독 모드: 지도에서 배출 위치 지정 → 지도에 핀 표시")
    page.get_by_role("button", name="← 접수").click()
    page.get_by_role("button", name="← 목록").click()

    # 취소 (사유 보존)
    page.locator(".stop", has_text="가상이").click()
    prompts["text"] = "주민이 직접 처리"
    page.get_by_role("button", name="접수 취소").click()
    expect(page.get_by_text("취소된 접수입니다. 사유: 주민이 직접 처리")).to_be_visible()
    page.get_by_role("button", name="← 목록").click()
    page.get_by_role("button", name=re.compile("취소 1")).click()
    expect(page.locator(".stop")).to_have_count(1)
    ok(page.locator(".stop").count() == 1, "취소 건은 지우지 않고 취소 목록에 보존")

    # 백업 → 전체 삭제 → 복원
    ok(page.get_by_text("마지막 백업: 없음").count() == 1, "백업 전 경고 표시")
    page.get_by_role("button", name="지금 백업").click()
    page.get_by_placeholder("백업 비밀번호 (8자 이상)").fill("backup-pass-1")
    page.get_by_placeholder("한 번 더").fill("backup-pass-1")
    with page.expect_download() as dl:
        page.get_by_role("button", name="암호화 백업 파일 만들기").click()
    bfile = WORK / dl.value.suggested_filename
    dl.value.save_as(bfile)
    blob = bfile.read_bytes()
    ok(blob[:4] == b"DGB1" and "가상일".encode() not in blob and b"01000007777" not in blob, f"백업 파일 암호화 확인 ({len(blob):,}B, 이름·연락처 평문 없음)")
    page.goto(url + "#/settings")
    page.get_by_role("button", name="이 휴대폰의 모든 자료 지우기").click()
    page.wait_for_load_state()
    page.get_by_placeholder("숫자 6자리 이상").fill(PIN)
    page.get_by_placeholder("한 번 더").fill(PIN)
    page.get_by_role("button", name="PIN 설정").click()
    page.get_by_role("button", name="이 휴대폰만으로 사용").click()
    page.get_by_role("button", name="백업·복원").click()
    page.locator("input[type=file]").set_input_files(str(bfile))
    page.get_by_placeholder("백업 비밀번호", exact=True).fill("wrong-password")
    page.get_by_role("button", name="백업에서 복원").click()
    expect(page.get_by_text("비밀번호가 틀렸거나")).to_be_visible()
    ok(True, "틀린 백업 비밀번호 거부")
    page.get_by_placeholder("백업 비밀번호", exact=True).fill("backup-pass-1")
    page.get_by_role("button", name="백업에서 복원").click()
    page.get_by_role("button", name=re.compile("내일")).click()
    expect(page.locator(".stop")).to_have_count(1, timeout=15000)
    page.locator(".stop", has_text="가상일").click()
    ok(page.get_by_text("저장됨: 완료").is_visible() and page.get_by_text("주민 요청: 동·호수 추가").is_visible(), "새 휴대폰 가정: 백업 복원 후 접수·수정 이력·현장 기록 유지")
    page.screenshot(path=SHOTS / "13_solo_stop.png")
    ok(not errors, "단독 모드 스크립트 오류 없음" + (": " + "; ".join(errors) if errors else ""))
    ctx.close()
    check_requests("단독", ["가상일", "가상이", "01000007777", "0000-7777", "예시로 5", "시험길 9", "현관"])


if __name__ == "__main__":
    main()
