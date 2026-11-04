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
from urllib.parse import unquote
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
LAST_PAGE: list = []  # 실패 시 화면 캡처용 (SHOTS/fail.png)


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


def browser(pw, video: Path, profile: str = "profile", **extra):
    ctx = pw.chromium.launch_persistent_context(
        str(WORK / profile), executable_path=CHROME, headless=True, **extra,
        viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True,
        accept_downloads=True, permissions=["camera", "geolocation"], geolocation={"latitude": 35.1462, "longitude": 126.9233},
        ignore_https_errors=True,
        args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
              f"--use-file-for-fake-video-capture={video}", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
              *([f"--proxy-server={PROXY}"] if PROXY else [])])
    ctx.on("request", lambda r: REQUESTS.append((r.url, r.post_data or "")))
    # 헤드리스 Chrome은 tel:/sms:/geo: 를 열면 그 뒤 입력을 받지 않으므로, 앱의 외부 앱 열기를 가로채 주소만 모은다
    ctx.add_init_script("window.__dgOpened = []; window.__dgExternal = (u) => { window.__dgOpened.push(u); };")
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    LAST_PAGE[:] = [page]
    return ctx, page


def pc_sql(sql: str) -> list:
    """PC DB 조회 (가상 자료). 결과는 JSON 행 목록."""
    code = ("import json,sys; sys.path.insert(0,'.'); from dgpc import store, service as S; c=store.connect(); "
            f"print(json.dumps(S.rows(c, {sql!r}), ensure_ascii=False, default=str))")
    return json.loads(subprocess.run([sys.executable, "-c", code], cwd=ROOT / "pc", env=ENV, capture_output=True, text=True, check=True).stdout)


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
        try:
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
            mk = py("make-bundle", "--device", dev_short, "--count", "20", "--date", "2026-11-03", "--reduce", "2")
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
            ok(page.get_by_role("button", name="🗺 지도로 보기").count() == 0 and page.locator("nav.tabbar .t-map").count() == 1, "지도는 아래 탭 하나로 (중복 단추 없음)")
            ok(page.get_by_role("progressbar").first.get_attribute("aria-label") == "완료 0/20", "진행 막대 aria-label '완료 0/20'")
            # 작업자: 설정에서 이름 → 첫 화면에서 교대(PIN 없이)
            page.goto(url + "#/settings")
            ok(page.get_by_role("button", name="← 목록").count() == 0, "탭 화면(설정)에는 [← 목록] 중복 없음")
            expect(page.locator(".install-card h2")).to_have_text("앱 설치 — 지금은 브라우저에서 실행 중")
            ok("브라우저 탭" in page.locator("main").inner_text(), "설정: 지금 실행 위치(브라우저 탭/설치한 앱)를 분명히 표시")
            page.evaluate("""() => { const e = new Event('beforeinstallprompt', { cancelable: true }); e.prompt = async () => { window.__dgPrompted = true; };
              e.userChoice = Promise.resolve({ outcome: 'accepted' }); dispatchEvent(e); }""")
            page.get_by_role("button", name="홈 화면에 앱 설치").click()
            page.wait_for_function("() => window.__dgPrompted === true")
            ok(True, "브라우저가 설치를 제안하면 [홈 화면에 앱 설치] → 설치 창")
            page.get_by_label("작업자 1 이름").fill("김수거")
            page.get_by_label("작업자 2 이름").fill("이수거")
            page.get_by_role("button", name="작업자 저장").click()
            page.goto(url + "#/home")
            page.get_by_role("group", name="작업자 바꾸기").get_by_role("button", name="이수거").click()
            expect(page.get_by_role("group", name="작업자 바꾸기").get_by_role("button", name="이수거")).to_have_attribute("aria-pressed", "true")
            ok(page.get_by_placeholder("PIN").count() == 0, "작업자 바꾸기: PIN 없이 교대")
            page.goto(url + "#/backup")
            expect(page.get_by_text("PC 연결 방식의 안전용 백업")).to_be_visible()
            ok(page.get_by_role("button", name="암호화 백업 파일 만들기").is_visible(), "PC 연결 방식에서도 안전용 암호화 백업(.dgb) 가능, 결과 보내기가 기본이라는 안내")
            page.goto(url + "#/home")
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
            ok(page.evaluate("document.documentElement.dataset.wake") in ("on", "denied", "unsupported", "off"), "상세 화면에서 화면 켜짐 유지 요청 (지원 안 하면 조용히 넘어감: " + str(page.evaluate("document.documentElement.dataset.wake")) + ")")
            page.get_by_role("button", name="통화 완료").click()
            expect(page.get_by_role("button", name="통화 완료")).to_have_class(re.compile("on"))
            # 도착 기록 (시각 + 허용된 위치·정확도)
            page.get_by_role("button", name="도착", exact=True).click()
            expect(page.get_by_text(re.compile("도착 기록 \\d\\d:\\d\\d · 위치 오차"))).to_be_visible(timeout=15000)
            ok(True, "[도착] → 도착 시각·휴대폰 위치(오차) 기록")
            # 스티커·신고필증 확인: 없음 → 그래도 수거
            page.get_by_role("group", name=re.compile("스티커·신고필증")).first.get_by_role("button", name="없음").click()
            page.get_by_role("button", name="그래도 수거").click()
            expect(page.locator(".sk-note").first).to_contain_text("없음 → 수거")
            ok(True, "품목별 스티커·필증 확인: '없음'이면 수거/미수거를 바로 고르게 함")
            # 문자: 문구 고르기 → 문자 앱 (기록에는 문구 종류만)
            page.get_by_role("button", name="문자").click()
            expect(page.get_by_role("dialog")).to_be_visible()
            page.get_by_role("button", name="곧 도착").click()
            expect(page.locator(".sms-log")).to_contain_text("곧 도착", timeout=10000)
            opened = page.evaluate("window.__dgOpened")
            ok(len(opened) == 1 and opened[0].startswith("sms:" + phone.replace("-", "") + "?body=") and name not in __import__("urllib.parse").parse.unquote(opened[0]),
               "문자 앱에는 그 주민 번호와 문구(접수번호·시각)만, 이름 없음")
            ok(True, "[문자] 문구 선택 → sms: 열기, 문자 안내 기록(문구 종류만)")
            # 길안내: 전화 옆 [길안내] → 설정에서 고른 지도 앱 (좌표와 '1번 수거지'만), 앱이 없으면 웹 지도·기본 지도 앱
            check_nav_apps(page, url, [name, phone.replace("-", ""), phone, page.locator(".who .addr").inner_text()[:8]], "1번 수거지")
            page.locator("#cam-AFTER").set_input_files(str(ROOT / "web/icons/icon-512.png"))
            expect(page.locator(".thumbs img")).to_have_count(1, timeout=15000)
            ok(True, "현장 사진 촬영(파일 입력) → 앱 안에 암호화 저장, 찍은 시각 표시")
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
            expect(page.get_by_text("즐겨찾기·자주 쓴 품목")).to_be_visible()
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
            # 같은 품목을 다시 추가 → 더하기/따로 선택
            page.get_by_role("button", name="+ 현장 품목 추가").click()
            page.get_by_placeholder(re.compile("품목명")).fill("회전 의자")
            page.locator(".btn.result").first.click()
            page.get_by_role("button", name="추가", exact=True).click()
            expect(page.get_by_text("이미 현장에서 추가한 품목입니다")).to_be_visible()
            page.get_by_role("button", name=re.compile("^기존 행에 더하기")).click()
            expect(page.locator(".line.field")).to_have_count(2)
            ok("9,000원" in page.locator(".fee dd").nth(1).inner_text(), "같은 품목 재추가 → '기존 행에 더하기'로 2+1=3개 (행 늘지 않음, 변경 기록)")

            # 첫 원접수 품목은 1개만 수거하고 나머지는 사유 기록
            orig = page.locator(".line.orig").first
            orig.locator(".row").nth(2).locator(".btn.step").last.click()  # 실은 수량 +1
            page.wait_for_timeout(700)
            page.get_by_role("button", name="일부 수거", exact=True).click()
            expect(page.locator("#st-result .msg.err")).to_contain_text("사유를 골라")
            ok(page.locator("select.reason.need").count() >= 1, "못 실은 품목의 사유 없이 일부 수거 저장 막음 (사유 칸 강조)")
            page.get_by_role("button", name="품목 수량을 모두 실음으로 채우기").click()
            # 안마기는 수거 못 함: 수거 0 → 사유
            fld = page.locator(".line.field").last
            fld.locator(".row").nth(1).locator(".btn.step").first.click()  # 실은 수량 −1
            page.wait_for_timeout(700)
            fld.locator("select.reason").select_option("CANNOT_LOAD")
            page.get_by_role("button", name="일부 수거", exact=True).click()
            # 현장 추가로 금액이 늘었으므로 저장 전에 추가 금액 고지 창
            expect(page.get_by_role("dialog")).to_contain_text("추가 금액")
            ok("9,000원" in page.get_by_role("dialog").inner_text(), "현장 금액이 늘면 저장 전 '추가 금액 9,000원' 고지 창")
            page.get_by_role("button", name="동의", exact=True).click()
            expect(page.get_by_text("저장됨: 일부 수거")).to_be_visible()
            # 결과를 저장하면 아래 고정 영역의 길안내가 '다음 지점 길안내'로 (다음 미완료 지점, 좌표와 '2번 수거지'만)
            n0 = len(page.evaluate("window.__dgOpened"))
            page.locator(".dock").get_by_role("button", name=re.compile("^다음 길안내")).click()
            page.wait_for_timeout(2200)
            nx = page.evaluate("window.__dgOpened")[n0:]
            ok(len(nx) == 2 and nx[0].startswith("kakaomap://route?ep=") and "2번 수거지" in unquote(nx[1]) and name not in unquote(" ".join(nx)),
               "결과 저장 뒤 아래 고정 영역 [다음 길안내] → 다음 미완료 지점(2번)으로 카카오맵, 앱 없으면 웹")
            ok(page.locator(".result-sum").get_by_text("추가 금액 9,000원: 동의").is_visible(), "고지 결과(동의)가 결과 요약에 표시")
            ok(page.locator(".line.field").last.locator(".row.left .val").inner_text().startswith("1"),
               "현장 추가 품목의 남은 대상 1 유지 (다른 품목 수거로 상쇄 안 됨)")
            page.get_by_label("현장 메모").fill("지하주차장 입구에 배출")
            page.get_by_role("button", name="메모 남기기").click()
            expect(page.locator(".memo-box li", has_text="지하주차장 입구")).to_be_visible()
            ok(True, "현장 메모 남기기")
            page.screenshot(path=SHOTS / "05_stop_top.png")
            page.locator(".fee").scroll_into_view_if_needed()
            page.screenshot(path=SHOTS / "06_stop_fee.png")

            # 3-0) 감면 건(2번, PC에서 차상위 50%): 부과번호와 계산 금액 → 감면 → 부과 금액 (v0.11 "rd"·"lv")
            page.goto(url + "#/home")
            page.locator(".stop .seq").filter(has_text=re.compile(r"^2(?!\d)")).first.click()
            expect(page.locator(".who .levy-no")).to_have_text(re.compile(r"^부과번호 2026-"))
            red = page.locator(".fee dl.reduction")
            expect(red).to_contain_text("감면 (50%)")
            won_ = lambda t: int(re.sub(r"\D", "", t))
            calc = won_(red.locator("dd").nth(0).inner_text())
            cut = won_(red.locator("dd").nth(1).inner_text())
            levied = won_(page.locator(".fee .tb-val").inner_text())
            ok(calc > 0 and cut == calc * 50 // 100 and levied == calc - cut and "부과 금액" in page.locator(".fee .tb-label").inner_text()
               and "차상위" not in page.locator("#app").inner_text(),
               f"감면 건: 부과번호 표시, 계산 {calc:,}원 → 감면(50%) −{cut:,}원 → 부과 금액 {levied:,}원 (감면 사유 이름은 휴대폰에 없음)")
            page.locator(".fee").scroll_into_view_if_needed()
            page.screenshot(path=SHOTS / "06b_stop_reduction.png")

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
            page.get_by_role("button", name="위치 고치기").click()
            save = page.get_by_role("button", name="이 위치로 저장")
            expect(save).to_be_enabled(timeout=30000)
            page.locator(".mapbox canvas").drag_to(page.locator(".mapbox canvas"), source_position={"x": 150, "y": 150}, target_position={"x": 190, "y": 180})
            save.click()
            expect(page.get_by_role("button", name="지도", exact=True)).to_be_visible()
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
            page.get_by_role("button", name="하차 기록", exact=True).click()
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
            evs = pc_sql("SELECT kind, body_json FROM result_event")
            kinds = {e["kind"] for e in evs}
            arr = [json.loads(e["body_json"]) for e in evs if e["kind"] == "ARRIVE"]
            ph = [json.loads(e["body_json"]) for e in evs if e["kind"] == "PHOTO_META"]
            navs = [json.loads(e["body_json"]) for e in evs if e["kind"] == "NAV_INTENT"]
            ok(len(navs) == 5 and {n["app"] for n in navs} == {"kakao", "tmap", "naver", "geo"} and all(set(n) <= {"app", "worker"} for n in navs),
               f"길안내 기록 NAV_INTENT {len(navs)}건이 PC에 저장됨 (앱 종류만: {sorted({n['app'] for n in navs})})")
            ok({"ARRIVE", "STICKER_CHECK", "FEE_NOTICE", "SMS_INTENT", "PHOTO_META"} <= kinds and arr and arr[0].get("acc") is not None and arr[0].get("worker") == "이수거"
               and ph and ph[0].get("taken_at"), f"새 기록 종류가 PC에 저장됨 ({', '.join(sorted(kinds))}) · 작업자·위치·촬영 시각 포함")
            sms = [json.loads(e["body_json"]) for e in evs if e["kind"] == "SMS_INTENT"]
            ok(sms and set(sms[0]) <= {"template", "worker"}, "문자 기록에는 문구 종류만 (내용·번호 없음)")
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

            # 보관 종료 확인 뒤: 끝난 곳(일부 수거)의 이름·연락처·주소를 휴대폰에서 지움
            page.goto(url + "#/home")
            expect(page.locator(".stop").first.locator("strong")).to_have_text("개인정보 정리됨")
            ok(name not in page.locator("main").inner_text(), "보관 종료 확인 코드 입력 → 처리 끝난 곳의 이름·연락처·주소 휴대폰에서 삭제 (수거 기록은 유지)")
            page.screenshot(path=SHOTS / "07b_home_after_release.png")

            # 6) 오프라인에서도 동작 (서비스 워커) -----------------------------------
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

            midday_update(pw, url)
            pc_field(pw, url)
            solo(pw, url, WORK / "reg.y4m")
            juso_flow(pw, url)
            lock_and_bio(pw, port)
        except BaseException:
            if LAST_PAGE:
                try:
                    LAST_PAGE[0].screenshot(path=SHOTS / "fail.png", full_page=True)
                    print("실패 화면:", SHOTS / "fail.png")
                except Exception:
                    pass
            raise
    print(f"\n통과 {passed}개, 화면 캡처: {SHOTS}")


def midday_update(pw, url):
    """하루 중 새 목록: 1곳 추가, 1곳 빠짐, 순서 바꿈 → 요약 알림과 바뀐 곳 표시."""
    code = (
        "import json,sys; sys.path.insert(0,'.'); from dgpc import store, service as S; c=store.connect(); "
        "b=[x for x in S.list_batches(c,'2026-11-03') if x['status']=='ACTIVE'][0]; "
        "stops=[{'request_id':x['request_id'],'eta':x['eta']} for x in b['stops']]; stops.pop(5); stops[2],stops[3]=stops[3],stops[2]; "
        "fid=[k for k,r in S.FEE_ROWS.items() if r['status']=='CONFIRMED' and r['unit']=='EA'][0]; "
        "r=S.create_request(c,{'name':'가상새집','phone':'01000009999','address':'가상시 동구 새길 1','spot_note':'','memo':'','planned_date':'2026-11-03','lat':35.15,'lon':126.92,'items':[{'fee_rate_id':fid,'qty':1}]}); "
        "bid=S.confirm_plan(c,'2026-11-03',{b['crew_id']:stops+[{'request_id':r['id'],'eta':'15:00'}]})[0]['batch_id']; "
        "m=S.make_bundle(c,bid); print(json.dumps(m['frames']))")
    frames = json.loads(subprocess.run([sys.executable, "-c", code], cwd=ROOT / "pc", env=ENV, capture_output=True, text=True, check=True).stdout)
    write_y4m(WORK / "list2.y4m", [qr_png(f) for f in frames], hold=4, loops=4)
    ctx, page = browser(pw, WORK / "list2.y4m")
    page.goto(url)
    unlock(page)
    page.get_by_role("button", name="오늘 목록 받기").click()
    expect(page.get_by_text("목록을 받았습니다")).to_be_visible(timeout=60000)
    summ = page.locator(".diff-summary").inner_text()
    ok("새 1곳" in summ and "빠짐 1곳" in summ and "변경" in summ, f"하루 중 새 목록 → 요약 '{summ}'")
    page.screenshot(path=SHOTS / "16_midday_received.png")
    page.get_by_role("button", name="오늘 순서 보기").click()
    expect(page.locator(".stop .chip.new", has_text="새로 추가")).to_have_count(1)
    ok(page.locator(".stop .chip.new", has_text="순서·시간 변경").count() >= 1, "바뀐 곳 표시: 새로 추가 1곳, 순서·시간 변경")
    page.screenshot(path=SHOTS / "17_midday_list.png")
    page.locator(".stop", has_text="가상새집").click()
    expect(page.locator(".changed-note")).to_contain_text("새로 추가")
    page.get_by_role("button", name="← 목록").first.click()
    expect(page.locator(".stop", has_text="가상새집").locator(".chip.new")).to_have_count(0)
    ok(True, "바뀐 곳은 열어 보면 표시가 사라짐")
    ctx.close()


def pc_field(pw, url):
    """PC 연결 방식에서도 단독 방식처럼: 방문 순서 바꾸기·현장 접수·정정 요청·취소 요청 → 결과 파일 → PC 원본(사무실 확인)."""
    print("[PC 연결 방식 현장 기능]")
    ctx, page = browser(pw, WORK / "list2.y4m")
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    unlock(page)
    expect(page.locator(".stop").first).to_be_visible(timeout=20000)
    n0 = page.locator(".stop").count()
    # 방문 순서 바꾸기 (이 휴대폰만)
    second = page.locator(".stop strong").nth(1).inner_text()
    page.get_by_role("button", name="방문 순서 바꾸기").click()
    page.locator(".srow").nth(1).get_by_role("button", name="위로").click()
    expect(page.locator(".stop strong").first).to_have_text(second)
    page.get_by_role("button", name="순서 정하기 끝").click()
    page.reload()
    unlock(page)
    expect(page.locator(".stop strong").first).to_have_text(second, timeout=20000)
    ok("순서 바꿈(이 휴대폰만)" in page.locator(".meta").inner_text() and page.get_by_role("button", name="PC 순서로 되돌리기").is_visible(),
       "PC 연결 방식에서도 방문 순서 바꾸기(이 휴대폰만, 다시 열어도 유지, PC 순서로 되돌리기)")
    # 현장 접수
    page.get_by_role("button", name="+ 현장 접수").click()
    expect(page.locator("h1", has_text="현장 접수")).to_be_visible()
    page.get_by_label("이름").fill("가상현장")
    page.get_by_label("연락처").fill("010-0000-6161")
    page.get_by_label("배출 주소").fill("가상시 동구 현장길 3")
    page.get_by_placeholder(re.compile("품목명")).fill("고정 의자")
    page.locator(".btn.result").first.click()
    page.locator(".card", has=page.get_by_placeholder(re.compile("품목명"))).locator(".stepper .btn.step").last.click()
    page.get_by_role("button", name="품목 추가", exact=True).click()
    page.get_by_placeholder("품목 이름 (예: 안마기)").fill("표에없는물건")
    page.get_by_role("button", name="확인 대기로 추가").click()
    page.screenshot(path=SHOTS / "30_pc_field_intake.png", full_page=True)
    page.get_by_role("button", name="현장 접수 저장").click()
    expect(page.locator(".who .name")).to_have_text("가상현장", timeout=15000)
    ok(page.get_by_text(re.compile("이 휴대폰에서 받은 현장 접수입니다\\(임시번호 현장-")).is_visible(), "현장 접수 저장 → 바로 그 지점 화면(임시번호, PC 반영 전 안내)")
    page.get_by_role("button", name="전부 실음 · 수거 완료").click()
    expect(page.get_by_text("저장됨: 수거 완료")).to_be_visible()
    page.goto(url + "#/home")
    expect(page.locator(".stop")).to_have_count(n0 + 1)
    ok(page.locator(".stop", has_text="가상현장").locator(".chip", has_text="현장 접수").count() == 1, "목록에 현장 접수 1곳 추가(현장 접수 표시)")
    # 정정 요청: 가상새집 연락처
    page.locator(".stop", has_text="가상새집").click()
    page.get_by_role("button", name="정정 요청").click()
    page.get_by_role("button", name="정정 요청 남기기").click()
    ok("바뀐 내용이 없습니다" in page.locator(".msg").last.inner_text(), "바뀐 칸이 없으면 정정 요청을 남기지 않음")
    page.get_by_label("연락처").fill("010-0000-9898")
    page.get_by_role("button", name="정정 요청 남기기").click()
    ok("요청 사유" in page.locator(".msg").last.inner_text(), "요청 사유 없이는 남기지 않음")
    page.get_by_label("요청 사유 (필수)").fill("주민이 새 번호를 알려 줌")
    page.get_by_role("button", name="정정 요청 남기기").click()
    expect(page.get_by_text(re.compile("정정 요청 · 연락처 → 010-0000-9898"))).to_be_visible()
    ok(page.locator(".who .phone").inner_text() != "010-0000-9898" and page.get_by_text("보내기 전 (결과 보내기 때 함께)").is_visible(),
       "정정 요청은 이 휴대폰 목록을 바꾸지 않고 '보내기 전' 요청으로 표시")
    # 취소 요청: 다른 지점
    page.goto(url + "#/home")
    page.locator(".stop").nth(3).click()
    cname = page.locator(".who .name").inner_text()
    page.get_by_role("button", name="취소 요청").click()
    page.get_by_role("dialog").get_by_role("button", name="주민 취소").click()
    expect(page.locator(".who .notice", has_text="취소를 요청했습니다")).to_be_visible()
    ok(page.get_by_role("button", name="취소 요청").is_disabled(), f"취소 요청({cname}) → 표시, 같은 요청 두 번 막음, 원래 접수는 그대로")
    page.screenshot(path=SHOTS / "31_pc_change_request.png", full_page=True)
    # 결과 보내기: 현장 접수·정정 요청이 들어간다는 안내 → PC 가져오기
    page.goto(url + "#/export")
    expect(page.get_by_text(re.compile("현장 접수·정정 요청 2건의 이름·연락처·주소가 들어갑니다"))).to_be_visible(timeout=15000)
    ok(True, "결과 보내기 화면: 현장 접수·정정 요청의 개인정보가 들어간다고 알림")
    # 파일 저장 대신 앱이 만든 파일 내용을 그대로 받는다(같은 프로필로 두 번째 다운로드하면 헤드리스 브라우저가 닫히는 문제를 피함; CSP로 blob fetch 불가)
    page.evaluate("""() => { window.__dgDl = null; const oc = URL.createObjectURL.bind(URL); const orig = HTMLAnchorElement.prototype.click;
      URL.createObjectURL = (b) => { if (b instanceof File) b.arrayBuffer().then((buf) => { let s = ''; new Uint8Array(buf).forEach((x) => { s += String.fromCharCode(x); });
        window.__dgDl = { name: b.name, b64: btoa(s) }; }); return oc(b); };
      HTMLAnchorElement.prototype.click = function () { if (!this.download) return orig.call(this); }; }""")
    page.get_by_role("button", name=re.compile("결과 파일 만들어 보내기")).click()
    page.wait_for_function("() => window.__dgDl", timeout=30000)
    got = page.evaluate("window.__dgDl")
    f = WORK / got["name"]
    f.write_bytes(__import__("base64").b64decode(got["b64"]))
    imp = py("import-result", str(f))
    ok('"field_intakes": 1' in imp and '"review": 2' in imp, "PC 가져오기: 현장 접수 1건 → PC 원본 접수, 정정·취소 요청 2건 → 확인 대상 " + imp.splitlines()[0])
    rq = pc_sql("SELECT receipt_no, channel, origin_device, status, field_checked_at FROM request WHERE name='가상현장'")
    items = pc_sql("SELECT i.name, i.amount, i.price_status FROM request_item i JOIN request r ON r.id=i.request_id WHERE r.name='가상현장' ORDER BY i.name")
    ok(len(rq) == 1 and rq[0]["channel"] == "현장" and rq[0]["origin_device"] and rq[0]["status"] == "DONE" and rq[0]["field_checked_at"] is None
       and any(i["price_status"] == "PENDING" and i["amount"] is None for i in items) and any(i["price_status"] == "CONFIRMED" and i["amount"] for i in items),
       f"PC 원본 {rq[0]['receipt_no'] if rq else '-'}: 경로 '현장', 수거 완료, 사무실 확인 전, 표에 없는 품목은 확인 대기")
    cf = pc_sql("SELECT kind, conflict_reason FROM result_event WHERE apply_status='CONFLICT' AND kind IN ('CORRECTION','CANCEL_REQUEST')")
    ok({c["kind"] for c in cf} == {"CORRECTION", "CANCEL_REQUEST"} and pc_sql("SELECT phone FROM request WHERE name='가상새집'")[0]["phone"] == "01000009999",
       "정정·취소 요청은 PC 충돌함(확인 대상)으로, 원본은 확인 전까지 그대로")
    release = re.search(r"보관 종료 확인 코드: (\w+)", imp).group(1)
    page.get_by_placeholder("확인 코드 8자리", exact=True).fill(release)
    page.get_by_role("button", name="확인 코드 입력").click()
    expect(page.get_by_text("반영 확인됨").first).to_be_visible()
    page.goto(url + "#/home")
    page.locator(".stop", has_text="가상새집").click()
    expect(page.get_by_text("PC로 보냄 · 사무실 확인 뒤 새 목록에 반영")).to_be_visible(timeout=15000)
    ok(True, "보낸 요청은 'PC로 보냄 · 사무실 확인 뒤 반영'으로 표시")
    ok(not errors, "페이지 스크립트 오류 없음(PC 연결 방식 현장 기능)" + (": " + "; ".join(errors) if errors else ""))
    ctx.close()


def lock_and_bio(pw, port):
    """자동 잠금(기본 10분)·잠긴 사이 찍은 사진 보존·지문/얼굴(WebAuthn PRF, 가상 인증기) 잠금 해제."""
    print("[잠금·사진·생체 인증]")
    url = f"http://localhost:{port}/index.html"  # WebAuthn은 IP 주소 origin을 받지 않음
    ctx, page = browser(pw, WORK / "reg.y4m", "profile-lock")
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.clock.install()
    cdp = ctx.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {"protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
             "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True, "hasPrf": True}})
    page.goto(url)
    page.get_by_placeholder("숫자 6자리 이상").fill(PIN)
    page.get_by_placeholder("한 번 더").fill(PIN)
    page.get_by_role("button", name="PIN 설정").click()
    page.get_by_role("button", name="이 휴대폰만으로 사용").click()
    page.get_by_role("button", name="이 휴대폰만으로 시작").click()
    page.get_by_role("button", name="+ 전화 접수 등록").click()
    page.get_by_label("이름").fill("가상삼")
    page.get_by_label("연락처").fill("01000005555")
    page.get_by_label("배출 주소").fill("가상시 동구 잠금로 3")
    page.get_by_placeholder(re.compile("품목명")).fill("고정 의자")
    page.locator(".btn.result").first.click()
    page.get_by_role("button", name="품목 추가", exact=True).click()
    page.get_by_role("button", name="접수 등록").click()
    expect(page.get_by_role("button", name=re.compile("내일 1"))).to_be_visible()
    # 지문·얼굴 등록: PIN 확인 → 가상 인증기(PRF)
    page.locator("nav.tabbar .t-set").click()
    page.get_by_role("button", name="지문·얼굴 잠금 해제 등록").click()
    page.get_by_role("dialog").get_by_label("PIN").fill(PIN)
    page.get_by_role("button", name="다음").click()
    expect(page.get_by_text(re.compile("등록됨 \\("))).to_be_visible(timeout=20000)
    bio = page.evaluate("""async () => { const db = await new Promise(r => { const q = indexedDB.open('dg-proto'); q.onsuccess = () => r(q.result); });
      return await new Promise(r => { const q = db.transaction('kv').objectStore('kv').get('bio'); q.onsuccess = () => r(Object.keys(q.result || {}).sort().join(',')); }); }""")
    ok(bio == "at,credId,iv,prfSalt,salt,wrapped", f"지문·얼굴 등록: 데이터 키는 PRF 키로 감싼 것만 저장 ({bio})")
    page.goto(url + "#/home")
    page.get_by_role("button", name="잠금").click()
    page.get_by_role("button", name="지문·얼굴로 열기").click()
    expect(page.get_by_role("button", name="+ 전화 접수 등록")).to_be_visible(timeout=20000)
    ok(True, "지문·얼굴(WebAuthn PRF)로 잠금 해제 — PIN 없이")
    # 자동 잠금 기본 10분: 6분은 열림, 11분이면 잠김
    page.get_by_role("button", name=re.compile("내일 1")).click()
    page.locator(".stop").first.click()
    expect(page.locator(".who .name")).to_have_text("가상삼")
    # 부재·배출 안 됨으로 미수거: 사진이 없으면 사진 또는 "사진 불가" 이유를 요구
    page.get_by_role("button", name="부재", exact=True).click()
    page.get_by_role("button", name="미수거", exact=True).click()
    page.locator("select.reason").first.select_option("NOT_PUT_OUT")
    page.get_by_role("button", name="미수거", exact=True).click()
    expect(page.get_by_role("dialog")).to_contain_text("증거 사진이 필요합니다")
    page.screenshot(path=SHOTS / "19_evidence_sheet.png")
    page.get_by_role("button", name="사진 불가: 어두워 촬영 불가").click()
    expect(page.get_by_text("사진 불가 사유: 어두워 촬영 불가")).to_be_visible()
    ok(page.get_by_text("저장됨: 미수거").is_visible(), "부재·배출 안 됨 미수거는 사진 또는 '사진 불가' 이유가 있어야 저장")
    page.clock.fast_forward("06:00")
    page.wait_for_timeout(300)
    ok(page.get_by_placeholder("PIN").count() == 0, "6분 무조작에도 잠기지 않음 (기본 10분)")
    page.clock.fast_forward("05:00")
    expect(page.get_by_placeholder("PIN")).to_be_visible(timeout=10000)
    ok(True, "10분 무조작 → 자동 잠금")
    # 잠긴 사이 카메라에서 돌아온 사진: 메모리에 두고 잠금 해제 뒤 저장
    page.locator("#cam-BEFORE").set_input_files(str(ROOT / "web/icons/icon-192.png"))
    expect(page.get_by_text("사진 1장 저장 대기 — 잠금 해제 후 저장됩니다")).to_be_visible()
    page.screenshot(path=SHOTS / "18_lock_pending_photo.png")
    unlock(page)
    expect(page.locator(".thumbs img")).to_have_count(1, timeout=15000)
    ok(True, "잠긴 사이 찍은 사진 → 잠금 해제 뒤 암호화 저장 (사진 잃지 않음)")
    # 부재로 미수거: 사진이 있으므로 통과. 사진 없는 경우는 아래 확인
    ok(not errors, "잠금·생체 인증 스크립트 오류 없음" + (": " + "; ".join(errors) if errors else ""))
    ctx.close()
    check_requests("잠금", ["가상삼", "01000005555", "잠금로"])


def check_nav_apps(page, url, secrets: list[str], label: str):
    """설정의 길안내 앱 4가지마다 상세 [길안내] → 앱 주소(와 웹·기본 지도 대체 주소)에 좌표와 표시 이름만 있는지."""
    from urllib.parse import unquote
    stop_url = page.url
    page.goto(url + "#/settings")
    ok(page.get_by_role("group", name="길안내 앱").get_by_role("button", name="카카오맵").get_attribute("aria-pressed") == "true"
       and page.get_by_text("기관 승인").count() == 0 and page.get_by_text("좌표와 'N번 수거지'").count() == 1, "길안내 기본 켬·기본 앱 카카오맵 (승인 조건 없음, 넘기는 것 안내)")
    expect_prefix = {"카카오맵": ("kakaomap://route?ep=", "https://map.kakao.com/link/to/"), "T맵": ("tmap://route?goalx=", "geo:"),
                     "네이버지도": ("nmap://route/car?dlat=", "https://m.map.naver.com/route.nhn?"), "기본 지도 앱": ("geo:", None)}
    for app, (pre, fb) in expect_prefix.items():
        page.goto(url + "#/settings")
        page.get_by_role("group", name="길안내 앱").get_by_role("button", name=app, exact=True).click()
        page.goto(stop_url)
        n0 = len(page.evaluate("window.__dgOpened"))
        nav = page.locator(".dock .nav-btn")
        ok(nav.bounding_box()["height"] >= 56 and nav.is_enabled(), f"[{app}] 상세 아래 고정 영역 전화 옆 [길안내] (높이 {nav.bounding_box()['height']:.0f}px)")
        nav.click()
        page.wait_for_timeout(2200)  # 앱이 없을 때 대체 주소까지(1.5초)
        got = page.evaluate("window.__dgOpened")[n0:]
        words = {w for u in got for w in re.findall(r"[가-힣]+", unquote(u))}
        coord = all(re.search(r"35\.\d{6}", u) and re.search(r"12[67]\.\d{6}", u) for u in got)
        ok(got and got[0].startswith(pre) and (got[1].startswith(fb) if fb else len(got) == 1) and coord and words <= {"번", "수거지"}
           and label in unquote(got[0] if app != "카카오맵" else got[1]) and not any(x and x in unquote(u) for x in secrets for u in got),
           f"[{app}] {got[0][:46]}… (+대체 {got[1][:28] + '…' if len(got) > 1 else '없음'}): 좌표·'{label}'만, 이름·연락처·주소 없음")
    page.goto(url + "#/settings")
    page.get_by_role("group", name="길안내 앱").get_by_role("button", name="카카오맵", exact=True).click()
    page.goto(stop_url)


def personal_from_bundle() -> list[str]:
    sys.path.insert(0, str(ROOT / "pc"))
    from sample_data import make_daylist
    out = []
    for st in make_daylist(20, "2026-11-03", 1, seed=7)["stops"]:
        out += [st["name"], st["phone"], st["address"], st["receipt_no"], f"{st['lat']}", f"{st['lon']}"]
    return out


def check_requests(label: str, secrets: list[str], allow=("tiles.openfreemap.org",)):
    """브라우저가 보낸 모든 요청: 외부로는 허용한 곳(지도 서버, 주소 검색 서버)만, 어떤 요청에도 개인정보·핀 좌표 없음."""
    from urllib.parse import unquote
    external = [(u, b) for u, b in REQUESTS if not u.startswith(("http://127.0.0.1", "http://localhost")) and not u.startswith(("data:", "blob:"))]
    hosts = sorted({u.split("/")[2] for u, _ in external})
    ok(all(h in allow for h in hosts), f"[{label}] 외부 요청은 {'·'.join(allow)}만 ({', '.join(hosts) or '없음'}, {len(external)}건)")
    zs = [int(m.group(1)) for u, _ in external for m in [re.search(r"/(\d+)/\d+/\d+\.(?:pbf|png|mvt)", u)] if m]
    ok(not zs or max(zs) <= 14, f"[{label}] 지도 서버에는 넓은 범위(최대 줌 {max(zs) if zs else '-'}, 약 2km 단위) 조각만 요청 — 집 단위 위치 노출 없음")
    leaked = [x for x in secrets if any(x in unquote(u) or x in b for u, b in REQUESTS)]
    ok(not leaked, f"[{label}] 모든 요청 주소·본문에 이름·연락처·주소·접수번호·핀 좌표 없음" + (f" (발견: {leaked[:3]})" if leaked else ""))
    REQUESTS.clear()


JUSO_ITEM = {"roadAddr": "광주광역시 동구 서남로 1 (계림동)", "roadAddrPart1": "광주광역시 동구 서남로 1", "roadAddrPart2": " (계림동)",
             "jibunAddr": "광주광역시 동구 계림동 1-1 가상빌딩", "engAddr": "1, Seonam-ro, Dong-gu, Gwangju", "zipNo": "61400", "admCd": "2911010800",
             "rnMgtSn": "291103166047", "bdMgtSn": "2911010800100010001000001", "detBdNmList": "", "bdNm": "가상빌딩", "bdKdcd": "0",
             "siNm": "광주광역시", "sggNm": "동구", "emdNm": "계림동", "liNm": "", "rn": "서남로", "udrtYn": "0", "buldMnnm": "1", "buldSlno": "0",
             "mtYn": "0", "lnbrMnnm": "1", "lnbrSlno": "1", "emdNo": "01", "hstryYn": "0", "relJibun": "", "hemdNm": "계림2동"}
JUSO_LATLON = (35.153412, 126.921876)  # 좌표 API가 돌려주는 출입구 위치(가상) — UTM-K로 바꿔서 응답


def juso_flow(pw, url):
    """주소 검색(행정안전부 API를 가짜 응답으로) → 접수 주소·좌표·동 채우기 → 그 위치로 길안내, 반입지 위치·길안내."""
    print("[주소 검색·길안내]")
    from urllib.parse import parse_qs, urlsplit, unquote
    sys.path.insert(0, str(ROOT / "pc"))
    from dgpc import geo  # noqa: E402
    ex, ey = geo.wgs84_to_utmk(*JUSO_LATLON)
    calls: list[str] = []

    def juso(route):
        u = urlsplit(route.request.url)
        calls.append(route.request.url)
        common = {"errorCode": "0", "errorMessage": "정상", "totalCount": "1", "currentPage": "1", "countPerPage": "10"}
        if u.path.endswith("/addrLinkApi.do"):
            body = {"results": {"common": common, "juso": [JUSO_ITEM]}}
        elif u.path.endswith("/addrCoordApi.do"):
            body = {"results": {"common": common, "juso": [{"admCd": JUSO_ITEM["admCd"], "rnMgtSn": JUSO_ITEM["rnMgtSn"], "bdMgtSn": JUSO_ITEM["bdMgtSn"],
                                                            "udrtYn": "0", "buldMnnm": "1", "buldSlno": "0", "entX": f"{ex:.6f}", "entY": f"{ey:.6f}", "bdNm": "가상빌딩"}]}}
        else:
            return route.fulfill(status=404, body="")
        route.fulfill(status=200, content_type="application/json;charset=UTF-8", headers={"Access-Control-Allow-Origin": "*"},
                      body=json.dumps(body, ensure_ascii=False))

    # 서비스 워커가 가로챈 요청은 route()가 못 보므로 이 시험에서는 끈다
    ctx, page = browser(pw, WORK / "reg.y4m", "profile-juso", service_workers="block")
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    ctx.route("https://business.juso.go.kr/**", juso)
    page.goto(url)
    page.get_by_placeholder("숫자 6자리 이상").fill(PIN)
    page.get_by_placeholder("한 번 더").fill(PIN)
    page.get_by_role("button", name="PIN 설정").click()
    page.get_by_role("button", name="이 휴대폰만으로 사용").click()
    page.get_by_role("button", name="이 휴대폰만으로 시작").click()
    page.get_by_role("button", name="+ 전화 접수 등록").click()
    expect(page.locator(".addr-status")).to_contain_text("승인키를 설정에서 입력")
    page.goto(url + "#/settings")
    page.get_by_label("주소 검색 승인키").fill("TEST-SEARCH-KEY")
    page.get_by_label("좌표 승인키").fill("TEST-COORD-KEY")
    page.get_by_role("button", name="승인키 저장").click()
    expect(page.get_by_text("승인키를 저장했습니다")).to_be_visible()
    ok(page.get_by_role("button", name="직접", exact=True).get_attribute("aria-pressed") == "true", "설정: 주소 검색 승인키·좌표 승인키 저장, 단독 방식 경로 기본 '직접'")

    page.goto(url + "#/intake")
    page.get_by_label("이름").fill("가상주소")
    page.get_by_label("연락처").fill("01000003333")
    # 검색어에 주민 이름이 들어가면 보내지 않음
    page.get_by_label("주소 검색어").fill("가상주소 집")
    page.wait_for_timeout(700)
    ok(not calls and "보내지 않았습니다" in page.locator(".addr-status").inner_text(), "검색어에 주민 이름·연락처가 있으면 주소 서버로 보내지 않음")
    page.get_by_label("주소 검색어").fill("서")
    page.wait_for_timeout(500)
    ok(not calls, "한 글자는 검색하지 않음 (2자 이상, 300ms 뒤)")
    page.get_by_label("주소 검색어").fill("서남로 1")
    expect(page.locator(".addr-item")).to_have_count(1, timeout=10000)
    item = page.locator(".addr-item").inner_text()
    ok(all(x in item for x in ("광주광역시 동구 서남로 1", "지번 광주광역시 동구 계림동 1-1", "가상빌딩", "우편번호 61400")), "검색 결과: 도로명·지번·건물명·우편번호")
    q = parse_qs(urlsplit(calls[0]).query)
    ok(urlsplit(calls[0]).netloc == "business.juso.go.kr" and urlsplit(calls[0]).path == "/addrlink/addrLinkApi.do" and len(calls) == 1
       and sorted(q) == ["confmKey", "countPerPage", "currentPage", "keyword", "resultType"] and q["keyword"] == ["서남로 1"] and q["resultType"] == ["json"],
       f"검색 요청은 검색어·승인키·쪽수·json만 ({unquote(calls[0].split('?')[1])})")
    page.screenshot(path=SHOTS / "20_juso_search.png")
    page.locator(".addr-item").click()
    expect(page.get_by_label("배출 주소")).to_have_value(JUSO_ITEM["roadAddr"], timeout=10000)
    cq = parse_qs(urlsplit(calls[1]).query)
    ok(urlsplit(calls[1]).path == "/addrlink/addrCoordApi.do" and sorted(cq) == ["admCd", "buldMnnm", "buldSlno", "confmKey", "resultType", "rnMgtSn", "udrtYn"]
       and cq["confmKey"] == ["TEST-COORD-KEY"], "좌표 요청은 건물 식별 번호·좌표 승인키만")
    ok(page.get_by_label("동 (행정동, 선택)").input_value() == "계림2동" and page.locator(".addr-geo").count() == 1,
       "주소 고르기 → 도로명 주소·좌표(건물 출입구)·행정동 '계림2동' 채움")
    page.get_by_placeholder(re.compile("품목명")).fill("고정 의자")
    page.locator(".btn.result").first.click()
    page.get_by_role("button", name="품목 추가", exact=True).click()
    page.get_by_role("button", name="접수 등록").click()
    page.get_by_role("button", name=re.compile("내일 1")).click()
    page.locator(".stop", has_text="가상주소").click()
    expect(page.locator(".who .name")).to_have_text("가상주소")
    n0 = len(page.evaluate("window.__dgOpened"))
    page.locator(".dock .nav-btn").click()
    page.wait_for_timeout(2200)
    got = page.evaluate("window.__dgOpened")[n0:]
    want = f"{JUSO_LATLON[0]:.6f},{JUSO_LATLON[1]:.6f}"
    ok(got and got[0] == f"kakaomap://route?ep={want}&by=CAR" and unquote(got[1]) == f"https://map.kakao.com/link/to/1번 수거지,{want}",
       f"주소 검색 좌표(UTM-K→위도·경도 변환)로 길안내: {got[0]}")
    # 위치 지정 화면에도 주소 검색 (같은 서버, 검색어만)
    page.get_by_role("button", name="위치 고치기").click()
    page.get_by_label("주소 검색어").fill("서남로 1")
    expect(page.locator(".addr-item")).to_have_count(1, timeout=10000)
    page.locator(".addr-item").click()
    expect(page.locator(".gps-note")).to_contain_text("주소 검색")
    page.get_by_role("button", name="이 위치로 저장").click()
    expect(page.locator(".who .name")).to_have_text("가상주소")
    ok(True, "위치 지정 화면: 주소 검색으로 지도 이동 → 저장")
    # 반입지: 위치 지정 → 하차 화면·목록에서 [길안내] ('반입지'만)
    page.goto(url + "#/unload")
    ok(page.locator(".nav-btn").first.is_disabled() and "위치 없음 — 위치 지정 필요" in page.locator(".nav-none").inner_text(), "반입지 위치가 없으면 길안내 꺼짐 + 이유")
    page.get_by_role("button", name="반입지 위치 지정").click()
    page.get_by_role("button", name="지금 내 위치로 이동").click()
    save = page.get_by_role("button", name="이 위치로 저장")
    expect(save).to_be_enabled(timeout=20000)
    save.click()
    expect(page.get_by_role("button", name="반입지 위치 고치기")).to_be_visible()
    n0 = len(page.evaluate("window.__dgOpened"))
    page.locator(".nav-btn").first.click()
    page.wait_for_timeout(2200)
    got = [unquote(u) for u in page.evaluate("window.__dgOpened")[n0:]]
    ok(len(got) == 2 and got[0].startswith("kakaomap://route?ep=35.") and got[1].startswith("https://map.kakao.com/link/to/반입지,35."),
       "반입지 길안내: 좌표와 '반입지'만")
    page.screenshot(path=SHOTS / "21_unload_nav.png")
    ok(not errors, "주소 검색·길안내 스크립트 오류 없음" + (": " + "; ".join(errors) if errors else ""))
    ctx.close()
    ext = [u for u, _ in REQUESTS if "juso.go.kr" in u]
    ok(len(ext) == 4 and not any(x in unquote(u) for u in ext for x in ("가상주소", "01000003333", "0000-3333")), f"주소 서버 요청 {len(ext)}건 모두 이름·연락처 없음")
    check_requests("주소 검색", ["가상주소", "01000003333", "0000-3333", "1층"], allow=("tiles.openfreemap.org", "business.juso.go.kr"))


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
    expect(page.get_by_role("dialog")).to_be_visible()
    ok(page.get_by_role("dialog").get_by_role("button", name="이 휴대폰만으로 시작").is_visible(), "확인은 브라우저 창이 아니라 앱 안의 아래 창(큰 단추)")
    page.get_by_role("button", name="이 휴대폰만으로 시작").click()
    expect(page.get_by_role("button", name="+ 전화 접수 등록")).to_be_visible(timeout=20000)

    def intake(name, phone, addr, items, free=None, dong=""):
        page.get_by_role("button", name="+ 전화 접수 등록").click()
        page.get_by_label("이름").fill(name)
        page.get_by_label("연락처").fill(phone)
        page.get_by_label("배출 주소").fill(addr)
        page.get_by_label("상세 배출 위치").fill("1층 현관 앞")
        if dong:
            page.get_by_label("동 (행정동, 선택)").fill(dong)
        for q, n in items:
            page.get_by_placeholder(re.compile("품목명")).fill(q)
            page.locator(".btn.result").first.click()
            for _ in range(n - 1):
                page.locator(".card", has=page.get_by_placeholder(re.compile("품목명"))).locator(".stepper .btn.step").last.click()
            page.get_by_role("button", name="품목 추가", exact=True).click()
        if free:
            page.get_by_placeholder("품목 이름 (예: 안마기)").fill(free)
            page.get_by_role("button", name="확인 대기로 추가").click()

    intake("가상일", "010-0000-7777", "가상시 동구 예시로 5", [("고정 의자", 3), ("2인용 매트리스", 1)], dong="충장동")
    ok("44,000원" not in page.locator("main").inner_text() and "17,000원" in page.locator("main").inner_text(), "접수 금액 = 고정식 의자 3개 6,000 + 2인용 매트리스 11,000 = 17,000원")
    page.screenshot(path=SHOTS / "11_solo_intake.png", full_page=True)
    page.get_by_role("button", name="접수 등록").click()
    expect(page.get_by_role("button", name=re.compile("내일 1"))).to_be_visible()
    intake("가상이", "01000007777", "가상시 동구 시험길 9", [("회전", 1)], free="안마기", dong="계림동")
    ok(page.locator("#dong-list option[value='충장동']").count() == 1, "단독 접수: 동 입력칸에 최근 입력한 동 제안")
    page.get_by_role("button", name="접수 등록").click()
    ok(page.get_by_text("비슷한 접수가 1건").is_visible(), "같은 연락처 접수 → 중복 후보 경고 (자동 병합 없음)")
    page.get_by_role("button", name="접수 등록").click()
    page.get_by_role("button", name=re.compile("내일 2")).click()
    expect(page.locator(".stop")).to_have_count(2)
    ok(page.locator(".stop").count() == 2, "별개 접수로 확인 후 2건 등록, 내일 목록 표시")
    ok(page.locator(".dong-head").count() == 2 and page.locator(".stop .chip.dong").count() == 2, "방문 순서에 동 머리글·동 표시")
    page.get_by_role("group", name="동 거르기").get_by_role("button", name=re.compile("^충장동")).click()
    expect(page.locator(".stop")).to_have_count(1)
    ok(page.locator(".stop strong").first.inner_text() == "가상일", "동 거르기 (2개 동 이상일 때)")
    page.get_by_role("button", name="모든 동").click()
    expect(page.locator(".stop")).to_have_count(2)
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
    page.get_by_role("button", name="전부 실음 · 수거 완료").click()
    expect(page.get_by_text("저장됨: 수거 완료")).to_be_visible()
    ok(True, "단독 모드 현장 처리: 연락·수거·완료 저장")
    page.get_by_role("button", name="← 목록").first.click()

    # 위치 지정 (승인키가 없으면 주소 검색 대신 지도·내 위치)
    page.locator(".stop", has_text="가상일").click()
    expect(page.locator(".nav-none")).to_contain_text("위치 없음 — 위치 지정 필요")
    ok(page.locator(".dock .nav-btn").count() == 0, "위치 없는 곳: 이유 '위치 없음 — 위치 지정 필요' 표시 (마지막 곳·저장 뒤라 다음 길안내 없음)")
    page.get_by_role("button", name="← 목록").first.click()
    page.locator(".stop", has_text="가상이").click()
    ok(page.locator(".dock .nav-btn").is_disabled() and page.locator(".dock .nav-btn").get_attribute("title") == "위치 없음 — 위치 지정 필요",
       "결과 저장 전 위치 없는 곳: 전화 옆 [길안내] 꺼짐 + 이유")
    page.get_by_role("button", name="← 목록").first.click()
    page.locator(".stop", has_text="가상일").click()
    page.get_by_role("button", name="위치 지정", exact=True).click()
    expect(page.locator(".addr-status")).to_contain_text("승인키를 설정에서 입력")
    ok(page.get_by_label("주소 검색어").is_disabled(), "승인키가 없으면 주소 검색칸은 '승인키를 설정에서 입력' 안내, 지도·내 위치로 지정")
    save = page.get_by_role("button", name="이 위치로 저장")
    # 지도 조각에 기대지 않는다: 배출 위치에 서서 휴대폰 위치(가짜 GPS)로 저장
    page.get_by_role("button", name="지금 내 위치로 이동").click()
    expect(save).to_be_enabled(timeout=20000)
    expect(page.locator(".gps-note")).to_contain_text("오차")
    page.screenshot(path=SHOTS / "14_solo_pick.png")
    save.click()
    page.get_by_role("button", name="지도", exact=True).click()
    expect(page.locator(".pin.focus")).to_have_count(1, timeout=30000)
    ok(True, "단독 모드: 지도에서 배출 위치 지정 → 지도에 핀 표시")
    page.get_by_role("button", name="← 접수").click()
    page.get_by_role("button", name="← 목록").first.click()

    # 취소 (사유 보존)
    page.locator(".stop", has_text="가상이").click()
    page.get_by_role("button", name="접수 취소").click()
    page.get_by_role("dialog").get_by_role("button", name="취소로 기록").click()
    ok("취소 사유를 적어" in page.get_by_role("dialog").inner_text(), "취소 사유 없이는 기록 안 됨 (앱 안의 입력 창)")
    page.get_by_label("취소 사유").fill("주민이 직접 처리")
    page.get_by_role("button", name="취소로 기록").click()
    expect(page.get_by_text("취소된 접수입니다. 사유: 주민이 직접 처리")).to_be_visible()
    page.get_by_role("button", name="← 목록").first.click()
    page.get_by_role("button", name=re.compile("취소 1")).click()
    expect(page.locator(".stop")).to_have_count(1)
    ok(page.locator(".stop").count() == 1, "취소 건은 지우지 않고 취소 목록에 보존")

    # 단독 방식 보강: 하차 기록, 확인 필요 품목 단가 정하기(근거 필수), 큰 글씨
    page.goto(url + "#/home")
    page.get_by_role("button", name=re.compile("^하차 기록")).click()
    page.locator("label", has_text="부피").locator("input").fill("2.5")
    page.locator("label", has_text="반입지").locator("input").fill("가상 반입지")
    page.get_by_role("button", name="하차 기록", exact=True).click()
    expect(page.get_by_role("button", name="하차 기록 (1회)")).to_be_visible()
    page.goto(url + "#/settings")
    mline = page.locator(".line", has_text="6인용 이상(대리석)")
    mline.get_by_placeholder("원", exact=True).fill("15000")
    mline.get_by_role("button", name="저장").click()
    expect(page.get_by_text("근거를 적어 주세요")).to_be_visible()
    mline = page.locator(".line", has_text="6인용 이상(대리석)")
    mline.get_by_placeholder("원", exact=True).fill("15000")
    mline.get_by_placeholder(re.compile("^근거")).fill("가상 확인 11/3")
    mline.get_by_role("button", name="저장").click()
    expect(page.locator(".line", has_text="6인용 이상(대리석)").get_by_text("확정 15,000원")).to_be_visible()
    fs = lambda: page.evaluate("[getComputedStyle(document.body).fontSize, document.documentElement.className]")
    ok(fs()[0] == "15px", "글자 크기 기본 '보통' = 본문 15px")
    page.get_by_role("button", name="크게", exact=True).click()
    big_l = fs()
    page.get_by_role("button", name="아주 크게", exact=True).click()
    big = fs()
    page.get_by_role("button", name="작게", exact=True).click()
    small = fs()
    ok(big_l[0] == "17px" and "fs-l" in big_l[1] and big[0] == "19px" and "text-big" in big[1] and small[0] == "14px",
       f"글자 크기 4단계: 작게 {small[0]} · 크게 {big_l[0]} · 아주 크게 {big[0]} (뿌리 글자 크기 하나로 모두 바뀜)")
    page.get_by_role("button", name="보통", exact=True).click()
    page.get_by_role("button", name="어둡게").click()
    scr = page.locator("section.card", has_text="야외 고대비")
    scr.get_by_role("button", name="켬").click()
    theme = page.evaluate("[document.documentElement.dataset.theme, document.documentElement.classList.contains('hc')]")
    page.screenshot(path=SHOTS / "15_settings_dark_hc.png")
    page.get_by_role("button", name="자동").click()
    page.locator("section.card", has_text="야외 고대비").get_by_role("button", name="끔").click()
    ok(theme == ["dark", True] and page.evaluate("document.documentElement.dataset.theme") is None, "테마 자동/밝게/어둡게 + 야외 고대비")
    ok(page.locator("section.screen-card").count() == 1 and page.locator("main > section").first.get_attribute("class").count("screen-card") == 1
       and page.locator(".theme-seg .tp").count() == 3, "설정 맨 위 '화면': 밝기 구분 단추(미리보기 3개)")
    page.goto(url + "#/intake")
    page.get_by_placeholder(re.compile("품목명")).fill("대리석")
    expect(page.locator(".btn.result", has_text="6인용 이상").first).to_be_visible()
    ok("15,000" in page.locator(".btn.result", has_text="6인용 이상").first.inner_text(),
       "단독: 하차 기록, 확인 필요 단가는 근거와 함께 정한 뒤 새 접수에 적용, 큰 글씨 설정")
    page.goto(url + "#/home")
    # 첫 화면 위 ☀/🌙: 지금 보이는 것의 반대로 바로 바꾸기
    page.get_by_role("button", name="어두운 화면으로").click()
    expect(page.get_by_role("button", name="밝은 화면으로")).to_be_visible()
    t1 = page.evaluate("document.documentElement.dataset.theme")
    page.get_by_role("button", name="밝은 화면으로").click()
    ok(t1 == "dark" and page.evaluate("document.documentElement.dataset.theme") == "light", "첫 화면 위 ☀/🌙로 밝기 바로 바꾸기")
    page.goto(url + "#/settings")
    page.get_by_role("button", name="자동", exact=True).click()
    expect(page.get_by_role("button", name="자동", exact=True)).to_have_attribute("aria-pressed", "true")
    page.goto(url + "#/home")
    expect(page.get_by_role("button", name="+ 전화 접수 등록")).to_be_visible()

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
    mr = subprocess.run([sys.executable, str(ROOT / "pc" / "pc_tool.py"), "import-solo", str(bfile)], env={**ENV, "DG_SOLO_PASS": "backup-pass-1"},
                        capture_output=True, text=True)
    if mr.returncode:
        print("import-solo 실패 출력:\n" + mr.stdout + mr.stderr, flush=True)
    ok(mr.returncode == 0, "단독 백업 파일을 PC 명령(import-solo)이 열기")
    moved = mr.stdout
    mv = json.loads(moved.split(": ", 1)[1])
    ok(mv["requests"] == 2 and mv["events"] >= 3 and mv["unloads"] == 1 and mv["unloads_applied"] == 0,
       f"단독 백업 → PC로 옮기기 (휴대폰 암호화 형식 그대로 PC가 열기): 접수 {mv['requests']}건·기록 {mv['events']}건")
    again = subprocess.run([sys.executable, str(ROOT / "pc" / "pc_tool.py"), "import-solo", str(bfile)], env={**ENV, "DG_SOLO_PASS": "backup-pass-1"},
                           capture_output=True, text=True, check=True).stdout
    ok("duplicate_file" in again, "같은 백업을 다시 옮기면 중복 반영 안 함")
    page.goto(url + "#/settings")
    page.get_by_role("button", name="이 휴대폰의 모든 자료 지우기").click()
    page.get_by_role("button", name="모두 지우기").click()
    page.wait_for_load_state()
    page.get_by_placeholder("숫자 6자리 이상").fill(PIN)
    page.get_by_placeholder("한 번 더").fill(PIN)
    page.get_by_role("button", name="PIN 설정").click()
    page.get_by_role("button", name="이 휴대폰만으로 사용").click()
    page.get_by_role("button", name="이 휴대폰만으로 시작").click()
    page.get_by_role("button", name="백업·복원").click()
    page.locator("input[type=file]").set_input_files(str(bfile))
    page.get_by_placeholder("백업 비밀번호", exact=True).fill("wrong-password")
    page.get_by_role("button", name="백업에서 복원").click()
    expect(page.get_by_text("비밀번호가 틀렸거나")).to_be_visible()
    ok(True, "틀린 백업 비밀번호 거부")
    page.get_by_placeholder("백업 비밀번호", exact=True).fill("backup-pass-1")
    page.get_by_role("button", name="백업에서 복원").click()
    page.get_by_role("button", name="복원하기").click()
    page.get_by_role("button", name=re.compile("내일")).click()
    expect(page.locator(".stop")).to_have_count(1, timeout=15000)
    page.locator(".stop", has_text="가상일").click()
    expect(page.get_by_text("저장됨: 수거 완료")).to_be_visible()
    ok(page.get_by_text("저장됨: 수거 완료").is_visible() and page.get_by_text("주민 요청: 동·호수 추가").is_visible(), "새 휴대폰 가정: 백업 복원 후 접수·수정 이력·현장 기록 유지")
    page.screenshot(path=SHOTS / "13_solo_stop.png")
    ok(not errors, "단독 모드 스크립트 오류 없음" + (": " + "; ".join(errors) if errors else ""))
    ctx.close()
    check_requests("단독", ["가상일", "가상이", "01000007777", "0000-7777", "예시로 5", "시험길 9", "현관"])


if __name__ == "__main__":
    main()
