"""PC 업무 화면 종단 테스트 (가상 자료).

PC 서버를 띄우고 헤드리스 Chrome으로 실제 화면을 조작한다. 휴대폰은 Python으로 흉내 낸다.
  비밀번호 설정 → 휴대폰 등록·지문 → 팀 → 전화 접수 3건(중복 경고·지도 위치·수정 이력) → 노선 계산·미배정 사유 →
  배정 확정 → QR 표시·수신 확인 → 결과 파일·사진 파일 가져오기 → 현장 결과 확인 → 마감, 그리고 서버 보안 검사.
실행: python3 tests/e2e_pc.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
WORK = Path(os.environ.get("DG_TEST_DIR") or tempfile.mkdtemp(prefix="dg-pc-e2e-"))
SHOTS = WORK / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)
ENV = {**os.environ, "DG_STATE": str(WORK / "state"), "DG_DB_KEY": "test-db-key-not-for-production"}
os.environ.update(DG_STATE=ENV["DG_STATE"], DG_DB_KEY=ENV["DG_DB_KEY"])
sys.path.insert(0, str(ROOT / "pc"))
import dgproto as P  # noqa: E402
from dgpc import service as S, store  # noqa: E402

PORT = 8899
URL = f"http://127.0.0.1:{PORT}/"
PW = "test-password-123"
PROXY = os.environ.get("HTTPS_PROXY", "")
REQUESTS: list[tuple[str, str]] = []
passed = 0


def ok(c, m):
    global passed
    if not c:
        raise AssertionError("실패: " + m)
    passed += 1
    print("  ✓", m)


class Phone:
    def __init__(self, reg_text):
        raw = P.b45decode(reg_text)
        self.pc_sign, self.pc_kex = P.pub_from_raw(raw[4:69]), P.pub_from_raw(raw[69:134])
        self.kex, self.sign, self.id, self.seq = P.gen_key(), P.gen_key(), uuid.uuid4().bytes, 0

    def code(self):
        d = self.id + P.pub_raw(self.kex) + P.pub_raw(self.sign)
        return "DGD1." + base64.urlsafe_b64encode(d + hashlib.sha256(d).digest()[:4]).decode().rstrip("=")

    def receive(self, frames):
        c = P.Collector()
        for f in frames:
            bid, *_ = c.add(f)
        head, payload, receipt = P.open_envelope(c.assemble(bid), self.pc_sign, self.kex, self.id)
        return head, P.expand_daylist(payload, S.FEE_ROWS), receipt

    def ev(self, stop, kind, body):
        self.seq += 1
        return {"event_id": str(uuid.uuid4()), "seq": self.seq, "request_id": stop["request_id"], "assign_ver": stop["assign_ver"],
                "base_req_ver": stop["req_ver"], "kind": kind, "body": body, "device_ts": f"2026-11-03T02:{self.seq:02d}:00Z"}

    def seal(self, payload, btype):
        head = P.Header(btype, uuid.uuid4().bytes, self.id, 1, 1, P.key_id(P.pub_raw(self.sign)))
        return P.seal(payload, head, self.pc_kex, self.sign)[0]


def write_y4m(path, frames, hold=4, loops=4, w=1280, h=720):
    import io as _io
    from PIL import Image
    sys.path.insert(0, str(ROOT / "pc"))
    import pc_tool
    out = []
    for f in frames:
        uri, _ = pc_tool.qr_png_data_uri(f, "M")
        im = Image.open(_io.BytesIO(base64.b64decode(uri.split(",")[1]))).convert("RGB")
        canvas = Image.new("RGB", (w, h), "white")
        side = h - 40
        canvas.paste(im.resize((side, side), Image.NEAREST), ((w - side) // 2, 20))
        y, cb, cr = canvas.convert("YCbCr").split()
        out.append(y.tobytes() + cb.resize((w // 2, h // 2)).tobytes() + cr.resize((w // 2, h // 2)).tobytes())
    with open(path, "wb") as fh:
        fh.write(f"YUV4MPEG2 W{w} H{h} F10:1 Ip A1:1 C420jpeg\n".encode())
        for _ in range(loops):
            for fr in out:
                for _ in range(hold):
                    fh.write(b"FRAME\n" + fr)


def raw_get(path, headers=None):
    req = urllib.request.Request(URL.rstrip("/") + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def main():
    print("작업 폴더:", WORK)
    subprocess.run([sys.executable, str(ROOT / "pc" / "pc_tool.py"), "init"], env=ENV, check=True, capture_output=True)
    srv = subprocess.Popen([sys.executable, str(ROOT / "pc" / "pc_tool.py"), "serve", "--port", str(PORT)], env=ENV,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(URL, timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        run()
    finally:
        srv.terminate()
        out = srv.communicate(timeout=5)[0]
        if "Traceback" in out:
            print(out)
            raise AssertionError("서버 오류 발생")
    print(f"\n통과 {passed}개, 화면 캡처: {SHOTS}")


def run():
    expect.set_options(timeout=15000)
    # 서버 보안
    no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    urllib.request.install_opener(no_proxy)
    ok(raw_get("/api/state", {"Host": "evil.example:8899", "X-DG-CSRF": "1"}) == 403, "다른 Host 이름으로 접근 거부 (DNS 리바인딩 방지)")
    ok(raw_get("/api/state") == 403, "CSRF 헤더 없는 API 요청 거부")
    ok(raw_get("/api/requests", {"X-DG-CSRF": "1"}) == 401, "로그인 없이 업무 자료 요청 거부")

    con = store.connect()
    phone = Phone(S.registration_text(con))
    today = S.today_kst()
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), headless=True,
                               args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader", *([f"--proxy-server={PROXY}", "--proxy-bypass-list=127.0.0.1"] if PROXY else [])])
        ctx = b.new_context(viewport={"width": 1400, "height": 950}, ignore_https_errors=True)
        def rec(r):
            try:
                body = (r.post_data_buffer or b"").decode("utf8", "ignore")
            except Exception:
                body = ""
            REQUESTS.append((r.url, body))
        ctx.on("request", rec)
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept(DIALOG["text"]) if d.type == "prompt" else d.accept())

        # 비밀번호 설정
        page.goto(URL)
        page.get_by_placeholder(re.compile("^이름")).fill("시험 관리자")
        page.get_by_placeholder("비밀번호", exact=True).fill(PW)
        page.get_by_placeholder("비밀번호 한 번 더").fill(PW)
        page.get_by_role("button", name="관리자 계정 만들기").click()
        expect(page.get_by_role("heading", name="오늘 현황")).to_be_visible()
        ok(True, "최초 운영 관리자 계정(admin) 만들기 후 로그인 → 오늘 현황")

        # 휴대폰 등록·팀
        page.goto(URL + "#/settings")
        expect(page.get_by_alt_text("기기 등록 QR")).to_have_attribute("src", re.compile("^data:image/png"))
        page.get_by_placeholder(re.compile("기기 코드")).fill(phone.code())
        page.get_by_placeholder(re.compile("기기 이름")).fill("1팀 업무폰")
        page.get_by_role("button", name="기기 등록").click()
        expect(page.locator(".big")).to_contain_text("지문")
        fp_pc = re.sub(r"\D", "", page.locator(".big").inner_text())
        fp_phone = P.fingerprint(P.pub_raw(phone.pc_sign), P.pub_raw(phone.kex), P.pub_raw(phone.sign))
        ok(fp_pc == fp_phone, f"화면에서 휴대폰 등록, 지문 일치 ({fp_pc})")
        page.wait_for_timeout(2800)
        new_row = page.locator("tr", has=page.get_by_placeholder("새 팀 이름"))
        new_row.get_by_placeholder("새 팀 이름").fill("가상 1팀")
        new_row.locator("select").select_option(index=1)
        new_row.get_by_role("button", name="추가").click()
        expect(page.locator("td input").first).to_have_value("가상 1팀")
        ok(True, "수거팀 추가·휴대폰 연결")

        # 접수 3건
        def intake(name, phone_no, addr, query, qty=1, click_map=None, appt=None):
            page.goto(URL + "#/request/new")
            expect(page.get_by_role("heading", name="전화 접수 등록")).to_be_visible()
            page.get_by_label("이름").fill(name)
            page.get_by_label("연락처").fill(phone_no)
            page.get_by_label("배출 주소").fill(addr)
            page.get_by_label("상세 배출 위치").fill("현관 앞")
            page.get_by_label("수거 예정일").fill(today)
            if appt:
                page.get_by_label("방문 약속 시작").fill(appt[0]); page.get_by_label("방문 약속 끝").fill(appt[1])
            page.get_by_placeholder(re.compile("품목명·규격")).fill(query)
            page.locator(".results button").first.click()
            page.locator(".row", has=page.get_by_placeholder(re.compile("품목명·규격"))).locator("input[type=number]").fill(str(qty))
            page.get_by_role("button", name="품목 추가", exact=True).click()
            if click_map:
                canvas = page.locator(".mapbox canvas")
                expect(canvas).to_be_visible(timeout=30000)
                page.wait_for_timeout(1500)
                canvas.click(position=click_map)
                expect(page.locator("input[readonly]").first).not_to_have_value("")
            page.get_by_role("button", name="접수 등록").click()

        intake("가상하나", "010-0000-5001", "가상시 동구 시험로 1", "3인용 소파", 1, click_map={"x": 300, "y": 180}, appt=("09:00", "11:00"))
        expect(page.get_by_role("heading", name=re.compile("접수 DG-"))).to_be_visible()
        r1_url = page.url
        page.screenshot(path=SHOTS / "pc_01_request.png", full_page=True)
        intake("가상둘", "01000005001", "가상시 동구 시험로 2", "장농", 2, click_map={"x": 420, "y": 260})
        expect(page.get_by_text(re.compile("비슷한 미완료 접수 1건"))).to_be_visible()
        ok(True, "같은 연락처 접수 → 중복 후보 경고 (자동 병합 없음)")
        page.get_by_role("button", name="접수 등록").click()
        expect(page.get_by_role("heading", name=re.compile("접수 DG-"))).to_be_visible()
        intake("가상셋", "01000005003", "가상시 동구 시험로 3", "대리석")
        expect(page.get_by_role("heading", name=re.compile("접수 DG-"))).to_be_visible()
        ok(page.get_by_text("금액 확인 대기").first.is_visible(), "대리석 식탁(원문 규격 겹침) → 금액 확인 대기")

        # 수정 이력
        page.goto(r1_url)
        page.get_by_label("상세 배출 위치").fill("101동 현관 앞")
        page.get_by_role("button", name="수정 저장").click()
        expect(page.get_by_text("수정 사유를 적어 주세요.")).to_be_visible()
        page.get_by_placeholder("수정 사유 (필수)").fill("주민 요청: 위치 보완")
        page.get_by_role("button", name="수정 저장").click()
        expect(page.get_by_text(re.compile("주민 요청: 위치 보완"))).to_be_visible()
        ok(True, "수정 사유 필수, 버전 이력 표시")

        page.goto(URL + "#/requests")
        expect(page.locator("tbody tr")).to_have_count(3)
        page.screenshot(path=SHOTS / "pc_02_list.png")

        # 노선·배정
        page.goto(URL + "#/dispatch")
        page.get_by_label("수거일").fill(today)
        page.get_by_role("button", name="노선 계산").click()
        expect(page.get_by_role("heading", name=re.compile("미배정 1건"))).to_be_visible(timeout=20000)
        ok(page.get_by_text(re.compile("직선거리 추정\\(도로 아님\\)")).is_visible() and page.get_by_text(re.compile("좌표가 없음")).is_visible(),
           "노선 계산: 직선거리 추정 표시, 위치 없는 건은 미배정 사유와 함께 표시")
        ok(page.locator(".stopline").filter(has_text="도착 ").count() == 2, "위치가 있는 2건 방문 순서·도착 예상 시각 추천")
        page.wait_for_timeout(2500)
        page.screenshot(path=SHOTS / "pc_03_dispatch.png")
        page.get_by_role("button", name="1번 아래로").first.click()
        page.get_by_role("button", name="2번 위로").first.click()  # 바꿨다 되돌림 → 조정한 것으로 보고 사유 요구
        page.get_by_role("button", name="배정 확정").click()
        expect(page.get_by_text("조정 사유를 적어 주세요")).to_be_visible()
        page.get_by_placeholder(re.compile("^조정 사유")).fill("수거팀 의견으로 순서 확인(가상)")
        page.get_by_role("button", name="배정 확정").click()
        expect(page.get_by_role("heading", name="목록 전송 (QR)")).to_be_visible()

        # QR 표시 → 휴대폰 수신 → 수신 확인
        with page.expect_popup() as pop:
            page.get_by_role("button", name="QR 표시").click()
        qr = pop.value
        expect(qr.locator("img#q")).to_have_attribute("src", re.compile("^data:image/png"))
        qr.screenshot(path=SHOTS / "pc_04_qr.png")
        frames = json.loads(S.rows(con, "SELECT frames_json FROM transfer_bundle ORDER BY displayed_at DESC LIMIT 1")[0]["frames_json"])
        head, payload, receipt = phone.receive(frames)
        ok(len(payload["stops"]) == 2 and payload["stops"][0]["phone"].startswith("010"), f"휴대폰이 목록 2건 수신 (QR {len(frames)}장)")
        qr.close()
        page.get_by_placeholder(re.compile("수신 확인 코드")).fill(receipt)
        page.get_by_role("button", name="수신 확인").click()
        expect(page.get_by_text("휴대폰 수신 확인됨")).to_be_visible()
        ok(True, f"수신 확인 코드 입력 → 수신 확인 ({receipt})")

        # 결과·사진 파일
        s1 = payload["stops"][0]
        photo_raw = Path(ROOT / "web/icons/icon-192.png").read_bytes()
        pid = str(uuid.uuid4())
        events = [phone.ev(s1, "CONTACT", {"result": "REACHED"}),
                  phone.ev(s1, "ITEM_RESULT", {"line_id": s1["items"][0]["line_id"], "line_kind": "RECEIVED", "qty_confirmed": s1["items"][0]["qty"], "qty_collected": s1["items"][0]["qty"], "reason": ""}),
                  phone.ev(s1, "PHOTO_META", {"photo_id": pid, "phase": "AFTER", "sha256": hashlib.sha256(photo_raw).hexdigest(), "bytes": len(photo_raw)}),
                  phone.ev(s1, "ATTEMPT", {"outcome": "DONE"})]
        rfile = WORK / "DG_R_test_1-4.dgr"
        rfile.write_bytes(phone.seal({"schema": "dg.result/1", "work_date": today, "events": events}, P.TYPE_RESULT))
        pfile = WORK / "DG_P_test.dgp"
        pfile.write_bytes(phone.seal({"schema": "dg.photos/1", "photos": [{"photo_id": pid, "request_id": s1["request_id"], "phase": "AFTER",
                                     "sha256": hashlib.sha256(photo_raw).hexdigest(), "data": base64.b64encode(photo_raw).decode()}]}, S.TYPE_PHOTOS))
        page.goto(URL + "#/results")
        page.locator("input[type=file]").nth(0).set_input_files(str(rfile))
        page.get_by_role("button", name="가져오기").nth(0).click()
        expect(page.get_by_text("보관 종료 확인 코드")).to_be_visible()
        ok(page.locator("dd").first.inner_text() == "4", "결과 파일 가져오기: 4건 반영, 보관 종료 확인 코드 표시")
        page.locator("input[type=file]").nth(1).set_input_files(str(pfile))
        page.get_by_role("button", name="가져오기").nth(1).click()
        expect(page.get_by_text("사진 회수 확인 코드")).to_be_visible()
        ok(True, "사진 파일 가져오기: 해시 대조 후 사진 회수 확인 코드 표시")
        page.screenshot(path=SHOTS / "pc_05_results.png", full_page=True)

        # 현장 결과 확인
        page.goto(URL + f"#/request/{s1['request_id']}")
        expect(page.get_by_role("heading", name="현장 결과")).to_be_visible()
        ok(page.get_by_text("수거 완료").first.is_visible() and page.locator(".thumbs img").count() == 1, "접수 상세에 현장 결과(수거 완료)와 회수된 사진 표시")
        expect(page.locator("h2", has_text="수납 기록")).to_contain_text("미납")
        page.get_by_role("button", name="수납 기록").click()
        expect(page.locator("h2", has_text="수납 기록")).to_contain_text("납부 확인")
        ok(True, "수납: 미납 → 계산 금액 납부 기록 → 납부 확인")

        # 마감
        page.goto(URL + "#/close")
        page.get_by_label("수거일").fill(today)
        expect(page.get_by_text(re.compile("결과 미회수 \\(배정했으나 결과 없음\\) 1건"))).to_be_visible()
        uc = page.locator(".card", has=page.get_by_role("heading", name=re.compile("하차 기록")))
        uc.get_by_placeholder("반입지").fill("가상 반입지")
        uc.get_by_placeholder("m³").fill("3.5")
        uc.get_by_role("button", name="하차 기록").click()
        expect(page.get_by_role("heading", name=re.compile("하차 기록 · 합계 3.5m³"))).to_be_visible()
        ok(True, "하차 기록 → 마감 화면 합계 반영")
        page.get_by_role("button", name="마감 기록").click()
        expect(page.get_by_text("마감 기록됨")).to_be_visible()
        ok(True, "일일 마감: 결과 미회수 1건 이월 표시 후 마감 기록")
        page.screenshot(path=SHOTS / "pc_06_close.png", full_page=True)
        # QR 실측
        page.goto(URL + "#/qrtest")
        with page.expect_popup() as pop2:
            page.get_by_role("button", name="실측 QR 표시").click()
        expect(pop2.value.locator("img#q")).to_have_attribute("src", re.compile("^data:image/png"))
        pop2.value.close()
        chk = S.rows(con, "SELECT check_code FROM qr_test ORDER BY created_at DESC LIMIT 1")[0]["check_code"]
        row = page.locator("tbody tr").first
        row.get_by_placeholder("초").fill("4.2"); row.get_by_placeholder("인식 횟수").fill("31"); row.get_by_placeholder("확인 4자리").fill(chk)
        row.get_by_role("button", name="기록").click()
        expect(page.get_by_text("4.2초 · 인식 31회 · 복원 일치")).to_be_visible()
        ok(True, "QR 실측: 가상 목록 QR 표시 → 휴대폰 결과 입력, 확인 코드로 복원 일치 기록")

        # 적재량 보정
        page.goto(URL + "#/settings")
        vrow = page.locator("tr", has=page.get_by_placeholder("보정값")).first
        expect(vrow).to_be_visible()
        vrow.get_by_placeholder("보정값").fill("1.8")
        vrow.get_by_role("button", name="저장").click()
        expect(page.get_by_text("보정값을 저장했습니다")).to_be_visible()
        ok(abs(list(S.volume_overrides(store.connect()).values())[0] - 1.8) < 1e-9, "적재량 보정값 저장 (추정치와 별도)")
        # 주소 위치 자료 가져오기 (가상 위치정보요약DB) → 접수 화면에서 주소로 위치 찾기
        from dgpc import geo
        def aline(road, main, lat, lon):
            x, y = geo.wgs84_to_utmk(lat, lon)
            return "|".join(["29110", "1", "2911010100", "가상광역시", "동구", "가상동", "1", road, "0", str(main), "0", "", "61400", "주택", "0", "가상동", f"{x:.6f}", f"{y:.6f}"])
        addr_file = WORK / "addr.txt"
        addr_file.write_bytes("\n".join([aline("시험로", 9, 35.1472, 126.9251)] + [aline(f"가상{i}길", i, 35.14 + i * 1e-4, 126.92) for i in range(1, 30)]).encode("cp949"))
        page.locator(".card", has=page.get_by_role("heading", name="주소 위치 자료")).locator("input[type=file]").set_input_files(str(addr_file))
        page.get_by_role("button", name="미리보기").click()
        expect(page.get_by_text(re.compile("시험로 9 → 35.1472"))).to_be_visible()
        page.get_by_role("button", name="이대로 가져오기").click()
        expect(page.get_by_text(re.compile("30건 가져옴"))).to_be_visible()
        page.goto(URL + "#/request/new")
        page.get_by_label("배출 주소").fill("가상광역시 동구 시험로 9, 2층")
        page.get_by_role("button", name="주소로 위치 찾기").click()
        expect(page.get_by_text(re.compile("시험로 9 — 건물 출입구 기준"))).to_be_visible()
        ok(abs(float(page.locator("input[readonly]").first.input_value()) - 35.1472) < 1e-5, "주소 자료 미리보기·가져오기 → 접수 화면 '주소로 위치 찾기' (이 PC 안에서만)")

        # 수수료표 개정안 (관리자 작성 → 다른 담당자 승인은 아래 접수 담당자 화면에서)
        page.goto(URL + "#/fees")
        mrow = page.locator("tr", has_text="6인용 이상(대리석)")
        mrow.get_by_placeholder("원", exact=True).fill("15000")
        mrow.get_by_placeholder(re.compile("^근거")).fill("가상 회신: 6인용은 이상으로 적용")
        page.get_by_placeholder(re.compile("원문 대조 확인자")).fill("가상 확인자")
        page.locator("input[type=date]").first.fill(today)
        page.get_by_role("button", name="개정안 저장").click()
        expect(page.get_by_text(re.compile("승인 대기: 개정 1번"))).to_be_visible()
        page.screenshot(path=SHOTS / "pc_12_fees.png", full_page=True)
        ok(page.get_by_text("작성한 사람은 승인할 수 없습니다").first.is_visible(), "수수료표 개정안 저장 → 작성자는 승인 불가 표시")

        # 백업·사진 보관
        page.goto(URL + "#/settings")
        page.get_by_placeholder(re.compile("^백업 비밀번호 \\(12자")).fill("backup-pass-e2e-0001")
        page.get_by_placeholder("한 번 더").fill("backup-pass-e2e-0001")
        page.get_by_role("button", name="지금 백업").click()
        expect(page.get_by_text(re.compile("백업했습니다: donggu-backup-"))).to_be_visible()
        brow = page.locator("tr", has_text="donggu-backup-").first
        brow.get_by_placeholder("백업 비밀번호").fill("backup-pass-e2e-0001")
        brow.get_by_role("button", name="열어 확인").click()
        expect(page.get_by_text(re.compile("정상: 접수 \\d+건"))).to_be_visible()
        ok(True, "설정: 암호화 백업 만들기 → 백업 비밀번호로 열어 확인")

        # 오늘 현황
        page.goto(URL + "#/home")
        expect(page.locator(".kpi").first).to_be_visible()
        page.screenshot(path=SHOTS / "pc_10_home.png", full_page=True)
        ok(page.locator(".kpi", has_text="수거 완료").inner_text().find("1건") >= 0 and page.locator(".crew").count() == 1, "오늘 현황: 완료 1건, 팀별 진행 표시")

        # 통계·보고서 + 배차표 인쇄
        page.goto(URL + "#/stats")
        expect(page.locator("svg.chart g.bar").first).to_be_visible()
        page.locator("svg.chart g.bar").first.hover()
        expect(page.locator(".tip")).to_contain_text("예정")
        with page.expect_download() as dlc:
            page.get_by_role("button", name="접수별 CSV").click()
        csv_txt = Path(dlc.value.path()).read_text(encoding="utf-8-sig")
        ok(csv_txt.startswith("접수번호") and "가상하나" not in csv_txt and "010" not in csv_txt, "통계 화면: 날짜별 막대·툴팁, 접수별 CSV(이름·연락처 없음) 내려받기")
        page.screenshot(path=SHOTS / "pc_13_stats.png", full_page=True)
        page.goto(URL + "#/send")
        with ctx.expect_page() as pp:
            page.get_by_role("button", name="배차표 인쇄").first.click()
        pr = pp.value
        pr.wait_for_load_state()
        ok(pr.locator("tbody tr").count() >= 2 and "파쇄" in pr.locator(".warn").inner_text() and pr.locator("#print").is_visible(), "인쇄용 배차표: 순서·신청인·품목, 파쇄 안내")
        pr.screenshot(path=SHOTS / "pc_14_print.png", full_page=True)
        pr.close()

        # 사용자 추가 (접수 담당자) + 변경 이력
        page.goto(URL + "#/users")
        page.get_by_placeholder("아이디 (영문)").fill("intake1")
        page.get_by_placeholder("이름", exact=True).fill("접수 담당")
        page.get_by_placeholder("임시 비밀번호 (10자 이상)", exact=True).fill("temp-pass-e2e-01")
        page.get_by_role("button", name="사용자 추가").click()
        expect(page.get_by_role("cell", name="intake1", exact=True).first).to_be_visible()
        page.get_by_placeholder("아이디 (영문)").fill("crew1")
        page.get_by_placeholder("이름", exact=True).fill("수거 팀원")
        page.locator(".card", has=page.get_by_role("heading", name="사용자")).locator("select").first.select_option("FIELD")
        page.locator(".card", has=page.get_by_role("heading", name="사용자")).locator("select").nth(1).select_option(label="가상 1팀")
        page.get_by_placeholder("임시 비밀번호 (10자 이상)", exact=True).fill("temp-pass-e2e-02")
        page.get_by_role("button", name="사용자 추가").click()
        expect(page.get_by_role("cell", name="crew1", exact=True).first).to_be_visible()
        expect(page.locator("table.audit td", has_text="admin").first).to_be_visible()
        page.screenshot(path=SHOTS / "pc_11_users.png", full_page=True)
        ok(page.locator("table.audit tr", has_text="등록").count() >= 1 and page.locator("table.audit tr", has_text="수정").count() >= 1,
           "사용자·변경 이력: 접수 담당자 추가, 작업자(admin)별 등록·수정 이력 표시")
        ok(not errors, "화면 스크립트 오류 없음" + (": " + "; ".join(errors) if errors else ""))
        b.close()

        # 역방향 QR: 휴대폰 화면의 결과 QR → PC 웹캠 (가짜 카메라 영상)
        s2 = payload["stops"][1]
        ev2 = [phone.ev(s2, "CONTACT", {"result": "NO_ANSWER"}), phone.ev(s2, "ATTEMPT", {"outcome": "NOT_COLLECTED"})]
        rq = phone.seal({"schema": "dg.result/1", "work_date": today, "events": ev2}, P.TYPE_RESULT)
        frames2 = P.split_frames(rq, uuid.uuid4().bytes, 600)
        write_y4m(WORK / "result.y4m", frames2)
        b2 = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), headless=True,
                                args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", f"--use-file-for-fake-video-capture={WORK / 'result.y4m'}"])
        c2 = b2.new_context(viewport={"width": 1400, "height": 950}, permissions=["camera"])
        p2 = c2.new_page()
        p2.goto(URL)
        p2.get_by_placeholder("아이디").fill("intake1")
        p2.get_by_placeholder("비밀번호").fill("temp-pass-e2e-01")
        p2.get_by_role("button", name="로그인").click()
        expect(p2.get_by_role("heading", name="내 계정")).to_be_visible()
        p2.locator("label", has_text="현재 비밀번호").locator("input").fill("temp-pass-e2e-01")
        p2.locator("label", has_text="새 비밀번호 (10자").locator("input").fill("intake-pass-0002")
        p2.locator("label", has_text="새 비밀번호 확인").locator("input").fill("intake-pass-0002")
        p2.get_by_role("button", name="비밀번호 바꾸기").click()
        expect(p2.get_by_role("heading", name="접수")).to_be_visible()
        nav = p2.locator("nav.side").inner_text()
        st403 = p2.evaluate("""async () => (await fetch('/api/settings', {method:'POST', headers:{'X-DG-CSRF':'1','Content-Type':'application/json'}, body:'{"osrm_url":""}'})).status""")
        ok("설정" not in nav and "사용자" not in nav and st403 == 403, "접수 담당자: 임시 비밀번호 변경 강제, 설정·사용자 메뉴 없음, 설정 API 403")
        p2.goto(URL + "#/fees")
        p2.get_by_role("button", name="원문과 대조했고 승인").click()
        expect(p2.get_by_text(re.compile("적용 버전"))).to_be_visible()
        expect(p2.locator("dd", has_text=re.compile("-r1$"))).to_be_visible()
        ok(S.fee_state(store.connect())["approved_by"] == "intake1", "다른 담당자(접수 담당자)가 승인 → 개정 버전 적용 (2인 대조)")
        # 유사 품목 적용 (확인 대기 품목 → 확정된 행, 근거·확인자)
        p2.goto(URL + "#/requests")
        p2.locator("tr.click", has_text="가상셋").click()
        p2.get_by_role("button", name="유사 품목 적용").first.click()
        p2.get_by_placeholder(re.compile("비슷한 품목 찾기")).fill("대리석 6인용 이상")
        p2.locator(".linkbox .results button").first.click()
        p2.get_by_placeholder(re.compile("^적용 근거")).fill("담당 부서 확인(가상)")
        p2.locator(".linkbox").get_by_role("button", name="적용").click()
        expect(p2.get_by_text(re.compile("유사 품목 적용: 식탁"))).to_be_visible()
        ok("15,000원" in p2.locator(".card", has=p2.get_by_role("heading", name="현장 결과")).inner_text(), "유사 품목 적용: 확인 대기 품목에 근거·확인자와 함께 확정 단가 연결")
        p2.goto(URL + "#/results")
        p2.get_by_role("button", name="웹캠으로 읽기").click()
        expect(p2.get_by_text("보관 종료 확인 코드")).to_be_visible(timeout=60000)
        code2 = p2.locator(".big").inner_text()
        ok(len(code2) == 8 and S.get_request(store.connect(), s2["request_id"])["status"] == "NOT_COLLECTED",
           f"역방향 QR {len(frames2)}장 웹캠 수신 → 결과 반영(미수거), 확인 코드 {code2}")
        ok(S.rows(store.connect(), "SELECT channel FROM result_import ORDER BY imported_at DESC LIMIT 1")[0]["channel"] == "QR", "회수 경로가 'QR'로 기록됨")
        # 수거팀 계정: 자기 팀 배정만 보임
        c3 = b2.new_context(viewport={"width": 1400, "height": 950})
        p3 = c3.new_page()
        p3.goto(URL)
        p3.get_by_placeholder("아이디").fill("crew1")
        p3.get_by_placeholder("비밀번호").fill("temp-pass-e2e-02")
        p3.get_by_role("button", name="로그인").click()
        p3.locator("label", has_text="현재 비밀번호").locator("input").fill("temp-pass-e2e-02")
        p3.locator("label", has_text="새 비밀번호 (10자").locator("input").fill("crew-pass-0003")
        p3.locator("label", has_text="새 비밀번호 확인").locator("input").fill("crew-pass-0003")
        p3.get_by_role("button", name="비밀번호 바꾸기").click()
        p3.goto(URL + "#/requests")
        p3.locator("main select").select_option("")
        expect(p3.locator("tr.click").first).to_be_visible()
        names = p3.locator("tbody").inner_text()
        other = p3.evaluate("""async (id) => (await fetch('/api/requests/' + id, {headers:{'X-DG-CSRF':'1'}})).status""", S.rows(store.connect(), "SELECT id FROM request WHERE name='가상셋'")[0]["id"])
        ok("가상셋" not in names and "가상하나" in names and other == 400, "수거팀 계정: 내 팀에 배정된 접수만 보임, 다른 접수 열기 거부")
        c3.close()
        b2.close()

    external = [u for u, _ in REQUESTS if not u.startswith(("http://127.0.0.1", "data:", "blob:"))]
    hosts = sorted({u.split("/")[2] for u in external})
    ok(all(h == "tiles.openfreemap.org" for h in hosts), f"PC 화면의 외부 요청은 지도 서버만 ({', '.join(hosts) or '없음'})")
    secrets = ["가상하나", "가상둘", "가상셋", "01000005001", "01000005003", "시험로"]
    ok(not [s for s in secrets if any(s in urllib.parse.unquote(u) for u in external)], "외부 요청에 이름·연락처·주소 없음")


DIALOG = {"text": ""}
import urllib.parse  # noqa: E402

if __name__ == "__main__":
    main()
