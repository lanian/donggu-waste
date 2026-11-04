"""PC 업무 화면 종단 테스트 (가상 자료).

PC 서버를 띄우고 헤드리스 Chrome으로 실제 화면을 조작한다. 휴대폰은 Python으로 흉내 낸다.
  비밀번호 설정 → 휴대폰 등록·지문 → 팀 → 전화 접수 3건(중복 경고·지도 위치·수정 이력) → 노선 계산·미배정 사유 →
  배정 확정 → QR 표시·수신 확인 → 결과 파일·사진 파일 가져오기 → 현장 결과 확인 → 마감, 그리고 서버 보안 검사.
실행: python3 tests/e2e_pc.py
"""
from __future__ import annotations

import base64
import contextlib
import datetime as dt
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
        try:
            uri, _ = pc_tool.qr_png_data_uri(f, "M")
            im = Image.open(_io.BytesIO(base64.b64decode(uri.split(",")[1]))).convert("RGB")
        except ValueError:  # 영숫자 방식으로 못 담는 글(기기 코드 base64url) → 바이트 방식
            import qrcode
            q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
            q.add_data(f.encode()); q.make(fit=True)
            im = q.make_image(fill_color="black", back_color="white").get_image().convert("RGB")
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


# 행안부 주소 API·카카오모빌리티 가짜 서버 (이 PC 안). PC 서버가 DG_JUSO_BASE·DG_KAKAO_NAVI_BASE 로 여기에 묻는다.
FAKE: dict = {"juso": [], "kakao": []}


def start_fake_external() -> int:
    import http.server
    import threading
    from dgpc import geo
    ent = geo.wgs84_to_utmk(35.1555, 126.9311)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, obj, code=200):
            b = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            if u.path.startswith("/addrlink/"):
                FAKE["juso"].append({"path": u.path, "q": q, "headers": dict(self.headers)})
                if u.path.endswith("addrLinkApi.do"):
                    return self.reply({"results": {"common": {"errorCode": "0", "errorMessage": "정상", "totalCount": "1"}, "juso": [
                        {"roadAddr": "가상광역시 동구 서남로 1 (가상동)", "roadAddrPart1": "가상광역시 동구 서남로 1", "jibunAddr": "가상광역시 동구 가상동 1-1", "bdNm": "가상청사",
                         "zipNo": "61400", "emdNm": "가상동", "hemdNm": "가상1동", "admCd": "2911010800", "rnMgtSn": "291103166047", "udrtYn": "0", "buldMnnm": "1", "buldSlno": "0"}]}})
                return self.reply({"results": {"common": {"errorCode": "0", "totalCount": "1"}, "juso": [{"entX": f"{ent[0]:.6f}", "entY": f"{ent[1]:.6f}"}]}})
            if u.path == "/v1/directions":
                FAKE["kakao"].append({"path": u.path, "q": q, "auth": self.headers.get("Authorization"), "body": None})
                return self.reply({"routes": [{"result_code": 0, "summary": {"distance": 900, "duration": 240}}]})
            self.reply({"error": "없음"}, 404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            FAKE["kakao"].append({"path": self.path, "q": {}, "auth": self.headers.get("Authorization"), "body": body})
            if self.headers.get("Authorization") != "KakaoAK E2EKAKAOKEY-777777":
                return self.reply({"msg": "unauthorized"}, 401)
            return self.reply({"trans_id": "e2e", "routes": [{"result_code": 0, "key": d["key"], "summary": {"distance": 1200, "duration": 420}} for d in body.get("destinations", [])]})

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def raw_get(path, headers=None):
    req = urllib.request.Request(URL.rstrip("/") + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


LAST_PAGE: list = []  # 실패 시 화면 캡처용 (SHOTS/fail.png)


def main():
    print("작업 폴더:", WORK)
    subprocess.run([sys.executable, str(ROOT / "pc" / "pc_tool.py"), "init"], env=ENV, check=True, capture_output=True)
    fake = f"http://127.0.0.1:{start_fake_external()}"
    ENV.update(DG_JUSO_BASE=fake, DG_KAKAO_NAVI_BASE=fake)
    FAKE["base"] = fake
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
    with playwright_with_fail_shot() as pw:
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
        LAST_PAGE.append(page)
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
        page.goto(URL + "#/settings/devices")
        expect(page.get_by_alt_text("기기 등록 QR")).to_have_attribute("src", re.compile("^data:image/png"))
        page.get_by_text("웹캠이 없을 때: 기기 코드 붙여 넣기").click()
        page.get_by_placeholder(re.compile("기기 코드")).fill(phone.code())
        page.get_by_placeholder(re.compile("기기 이름")).fill("1팀 업무폰")
        page.get_by_role("button", name="기기 등록").click()
        expect(page.locator(".big")).to_contain_text("지문")
        fp_pc = re.sub(r"\D", "", page.locator(".big").inner_text())
        fp_phone = P.fingerprint(P.pub_raw(phone.pc_sign), P.pub_raw(phone.pc_kex), phone.id, P.pub_raw(phone.kex), P.pub_raw(phone.sign))
        ok(fp_pc == fp_phone, f"화면에서 휴대폰 등록, 지문 9자리 일치 ({fp_pc})")
        page.wait_for_timeout(2800)
        page.get_by_role("button", name="지문 같음 · 사용 시작").click()
        expect(page.locator("tr", has_text="1팀 업무폰")).to_contain_text("사용")
        ok(S.list_devices(store.connect())[0]["confirmed_at"], "지문 확인을 눌러야 사용 시작(그 전에는 목록을 보낼 수 없음)")
        page.wait_for_timeout(2800)
        page.goto(URL + "#/settings/crews")
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
            # 화면이 다 그려진 뒤(예약 달력까지) 입력한다: 늦게 끝난 다시 그리기가 입력한 양식을 덮지 않게
            expect(page.locator(".calday").first).to_be_visible()
            quiet(page)
            page.get_by_label("이름").fill(name)
            page.get_by_label("연락처").fill(phone_no)
            if name == "가상하나":
                ok(page.locator(".addr-setup").get_by_text("주소 검색 자료가 아직 없습니다").is_visible(),
                   "주소 자료·행안부 승인키가 없으면 접수 화면에 '주소 검색 자료가 아직 없습니다'와 설정 위치 안내")
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
            for label, val in (("이름", name), ("연락처", phone_no), ("배출 주소", addr)):  # 혹시 다시 그려져 비었으면 채운다
                if not page.get_by_label(label).input_value():
                    page.get_by_label(label).fill(val)
            expect(page.get_by_label("이름")).to_have_value(name)
            page.get_by_role("button", name="접수 등록").click()

        intake("가상하나", "010-0000-5001", "가상시 동구 시험로 1", "3인용 소파", 1, click_map={"x": 300, "y": 180}, appt=("09:00", "11:00"))
        expect(page.get_by_role("heading", name=re.compile("접수 DG-"))).to_be_visible()
        r1_url = page.url
        r1_head = page.get_by_role("heading", name=re.compile("접수 DG-")).inner_text()
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
        # 같은 상세 화면(#/request/…)끼리는 주소 뒷부분만 바뀌므로, 앞 접수 화면이 아니라 이 접수가 그려졌는지 먼저 확인
        expect(page.get_by_role("heading", name=re.compile("접수 DG-"))).to_have_text(r1_head)
        settled(page)
        page.get_by_label("상세 배출 위치").fill("101동 현관 앞")
        expect(page.get_by_label("상세 배출 위치")).to_have_value("101동 현관 앞")
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
        expect(page.locator(".day-load")).to_contain_text(re.compile(r"3건 · 적재 추정 [\d.]+m³ / 고른 팀 [\d.]+m³"))
        ok(page.locator(".stopline .stop-items").count() == 2 and page.locator(".day-load").get_by_text(re.compile("무게 추정")).is_visible(),
           "하루 물량 점검(건수·적재·무게·작업 시간 ↔ 팀 용량)과 지점별 품목 표시")
        page.wait_for_timeout(2500)
        page.screenshot(path=SHOTS / "pc_03_dispatch.png")
        page.get_by_role("button", name="1번 아래로").first.click()
        expect(page.locator(".route .stopline").nth(1)).to_contain_text("누적 적재(추정) 4.2m³")
        first_load = page.locator(".route .stopline").nth(0).inner_text()
        ok(("1.4m³" in first_load or "2.8m³" in first_load) and page.locator(".over-note").count() == 0,
           "순서를 바꾸면 누적 적재(추정)를 화면에서 바로 다시 셈 (용량 안이면 초과 경고 없음)")
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
                  phone.ev(s1, "PHOTO_META", {"photo_id": pid, "phase": "AFTER", "sha256": hashlib.sha256(photo_raw).hexdigest(), "bytes": len(photo_raw), "taken_at": "2026-11-03T02:10:00Z", "lat": 35.1, "lon": 126.9, "acc": 9}),
                  phone.ev(s1, "ARRIVE", {"lat": 35.1, "lon": 126.9, "acc": 14, "worker": "김수거"}),
                  phone.ev(s1, "STICKER_CHECK", {"line_id": s1["items"][0]["line_id"], "result": "MISMATCH", "number": "2611-0042", "decision": "COLLECT", "worker": "김수거"}),
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
        ok(page.locator("dd").first.inner_text() == "6", "결과 파일 가져오기: 6건 반영(도착·스티커 확인 포함), 보관 종료 확인 코드 표시")
        page.locator("input[type=file]").nth(1).set_input_files(str(pfile))
        page.get_by_role("button", name="가져오기").nth(1).click()
        expect(page.get_by_text("사진 회수 확인 코드")).to_be_visible()
        ok(True, "사진 파일 가져오기: 해시 대조 후 사진 회수 확인 코드 표시")
        page.screenshot(path=SHOTS / "pc_05_results.png", full_page=True)

        # v0.12 휴대폰(PC 연결 방식) 현장 접수·정정 요청 → 충돌함(지금 값 → 요청 값) 반영, 현장 접수 확인 완료
        s2 = payload["stops"][1]
        fid, fline = str(uuid.uuid4()), str(uuid.uuid4())
        fev = phone.ev({"request_id": fid, "assign_ver": 0, "req_ver": 1}, "FIELD_INTAKE", {"temp_no": "현장-1103-1", "name": "가상현장", "phone": "010-0000-5061",
              "address": "가상시 동구 현장길 9", "planned_date": "2026-12-28", "items": [{"line_id": fline, "fee_rate_id": s1["items"][0]["fee_rate_id"], "qty": 1}]})
        fev["assign_ver"] = 0
        cev = phone.ev(s2, "CORRECTION", {"fields": {"spot_note": "뒷문 옆 화단"}, "reason": "주민이 배출 위치를 바꿨다고 함"})
        f2 = WORK / "DG_R_test_field.dgr"
        f2.write_bytes(phone.seal({"schema": "dg.result/1", "work_date": today, "events": [fev, cev]}, P.TYPE_RESULT))
        page.goto(URL + "#/results")
        page.locator("input[type=file]").nth(0).set_input_files(str(f2))
        page.get_by_role("button", name="가져오기").nth(0).click()
        expect(page.get_by_text("현장 접수 만듦")).to_be_visible()
        page.locator(".conf-tabs .qchip", has_text="충돌").click()
        expect(page.locator("#conflicts tr", has_text="정정 요청")).to_have_count(0)
        page.locator(".conf-tabs .qchip", has_text="현장 요청").click()
        expect(page.locator(".conf-tabs .qchip.on")).to_contain_text("현장 요청")
        expect(page.locator("#conflicts tr", has_text="정정 요청")).to_have_count(1)
        ok(True, "충돌함 탭: 현장 요청 / 충돌을 나눠 보기")
        row = page.locator("#conflicts tr", has_text="정정 요청")
        expect(row).to_contain_text("뒷문 옆 화단")
        old_spot = s2.get("spot_note") or "(없음)"
        ok(old_spot in row.inner_text() and "현장 정정 요청" in row.inner_text(), f"충돌함: 정정 요청을 '지금 값({old_spot}) → 요청 값'으로 보여 줌")
        page.screenshot(path=SHOTS / "pc_05b_field_requests.png", full_page=True)
        row.get_by_label("판단 사유").fill("주민 통화로 확인")
        row.get_by_role("button", name="반영").click()
        expect(page.locator("#conflicts tr", has_text="정정 요청")).to_have_count(0)
        ok(S.get_request(store.connect(), s2["request_id"])["spot_note"] == "뒷문 옆 화단", "정정 요청 [반영] → PC 원본 상세 위치 변경(이전 내용은 버전으로)")
        page.goto(URL + f"#/request/{fid}")
        expect(page.get_by_text("휴대폰 현장 접수")).to_be_visible()
        page.get_by_role("button", name="확인 완료").click()
        expect(page.get_by_text(re.compile("사무실 확인 \\("))).to_be_visible()
        ok(S.get_request(store.connect(), fid)["field_checked_at"] and S.get_request(store.connect(), fid)["channel"] == "현장", "현장 접수 상세: 안내 → [확인 완료] (경로 '현장')")
        page.goto(URL + "#/home")
        expect(page.locator("h1", has_text="오늘 현황")).to_be_visible()
        # 시범 운영 비교: 기간·기존 방식 수치 저장 → 비교표(100건당, 좋아짐/나빠짐)
        page.goto(URL + "#/pilot")
        expect(page.get_by_text("시범 운영 기간을 정하고 저장하면")).to_be_visible()
        page.get_by_label("시범 운영 시작").fill(today)
        page.get_by_label("시범 운영 끝").fill(today)
        page.get_by_label("접수 건수").fill("100")
        page.get_by_label("누락 건수(약속한 날 처리 못 하고 넘어간 건)").fill("10")
        page.get_by_label("접수 1건 처리 시간(분)").fill("6")
        page.get_by_role("button", name="저장").click()
        expect(page.locator("table.pilot")).to_contain_text("누락(약속한 날 처리 못 함)")
        ok("10" in page.locator("table.pilot tr", has_text="누락").inner_text() and page.locator("table.pilot tr", has_text="누락").locator(".chip").count() == 1
           and "6분" in page.locator("table.pilot tr", has_text="접수 1건 처리 시간").inner_text(),
           "시범 운영 비교: 기존 방식 수치 저장 → 비교표(누락 100건당·판단, 접수 1건 처리 시간 기존 값)")
        page.screenshot(path=SHOTS / "pc_05c_pilot.png", full_page=True)

        # 현장 결과 확인
        page.goto(URL + f"#/request/{s1['request_id']}")
        expect(page.get_by_role("heading", name="현장 결과")).to_be_visible()
        ok(page.get_by_text("수거 완료").first.is_visible() and page.locator(".thumbs img").count() == 1, "접수 상세에 현장 결과(수거 완료)와 회수된 사진 표시")
        fc = page.locator(".card", has=page.get_by_role("heading", name="현장 결과"))
        ok("위치 정확도 ±14m" in fc.inner_text() and "김수거" in fc.inner_text() and "번호 불일치" in fc.inner_text() and "촬영" in fc.inner_text(),
           "현장 결과: 도착 시각·위치 정확도, 작업자, 스티커 번호 불일치, 사진 촬영 시각 (휴대폰 v0.10 기록)")
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
        expect(page.get_by_role("heading", name="마감 이월 대상 2건")).to_be_visible()
        carry_ids = [r["id"] for r in S.rows(store.connect(), "SELECT id FROM request WHERE planned_date=? AND status NOT IN ('DONE','CANCELLED')", (today,))]
        page.get_by_role("button", name="마감 기록").click()
        expect(page.get_by_role("dialog")).to_contain_text("미처리 2건")
        page.locator(".dlg").get_by_role("button", name="마감 기록").click()
        expect(page.get_by_text("마감 기록됨")).to_be_visible()
        from dgpc import area as A
        c_ = store.connect()
        moved = [S.get_request(c_, i) for i in carry_ids]
        ok(len(moved) == 2 and all(m["planned_date"] == A.next_working_day(c_, today, m["dong"]) and m["versions"][0]["reason"].startswith("마감 이월") for m in moved)
           and page.get_by_text("팀장 확인 전").is_visible(), f"마감 이월: 결과 없음·미배정 2건 → 다음 업무일({moved[0]['planned_date']}), 팀장 확인 전 표시")
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
        page.goto(URL + "#/settings/crews")
        vrow = page.locator("tr", has=page.get_by_placeholder("보정값")).first
        expect(vrow).to_be_visible()
        vrow.get_by_placeholder("보정값").fill("1.8")
        vrow.get_by_role("button", name="저장").click()
        expect(page.get_by_text("보정값을 저장했습니다")).to_be_visible()
        ok(abs(list(S.volume_overrides(store.connect()).values())[0] - 1.8) < 1e-9, "적재량 보정값 저장 (추정치와 별도)")
        # 주소 위치 자료 가져오기 (가상 위치정보요약DB) → 접수 화면에서 주소로 위치 찾기
        from dgpc import geo
        def aline(road, main, lat, lon, bname=""):
            x, y = geo.wgs84_to_utmk(lat, lon)
            return "|".join(["29110", "1", "2911010100", "가상광역시", "동구", "가상동", "1", road, "0", str(main), "0", bname, "61400", "주택", "0", "가상동", f"{x:.6f}", f"{y:.6f}"])
        addr_file = WORK / "addr.txt"
        page.goto(URL + "#/settings/addr")
        addr_file.write_bytes("\n".join([aline("시험로", 9, 35.1472, 126.9251, "가상빌라")] + [aline(f"가상{i}길", i, 35.14 + i * 1e-4, 126.92) for i in range(1, 30)]).encode("cp949"))
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
        page.get_by_label("배출 주소").fill("")
        page.get_by_label("주소 찾기").fill("ㄱㅅㅂㄹ")
        page.locator(".addr-sug button", has_text="가상빌라").first.click()
        expect(page.get_by_label("배출 주소")).to_have_value(re.compile("시험로 9 \\(가상동, 가상빌라\\)"))
        ok(abs(float(page.locator("input[readonly]").first.input_value()) - 35.1472) < 1e-5, "주소 찾기(건물명 초성 ㄱㅅㅂㄹ) → 제안 고르면 표준 주소·좌표 채움")
        ok(page.locator(".addr-setup").count() == 0, "주소 자료를 가져온 뒤에는 '주소 검색 자료가 없습니다' 안내가 사라짐")
        page.get_by_label("주소 찾기").fill("시험")
        page.locator(".addr-sug button", has_text="건물번호").first.click()
        expect(page.get_by_label("주소 찾기")).to_have_value("시험로 ")
        expect(page.locator(".addr-sug button", has_text="시험로 9").first).to_be_visible()
        ok(page.locator(".addr-sug button", has_text="시험로 9").count() >= 1, "도로명 앞부분('시험') → 도로 제안 고르면 '시험로 '로 채우고 번호별 주소 제안")
        page.goto(URL + "#/requests")
        page.goto(URL + "#/request/new")
        page.get_by_role("button", name="비우고 새로 쓰기").click()

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
        page.goto(URL + "#/settings/backup")
        page.get_by_placeholder(re.compile("^백업 비밀번호 \\(12자")).fill("backup-pass-e2e-0001")
        page.get_by_placeholder("한 번 더").fill("backup-pass-e2e-0001")
        page.get_by_role("button", name="지금 백업").click()
        expect(page.get_by_text(re.compile("백업했습니다: donggu-backup-"))).to_be_visible()
        brow = page.locator("tr", has_text="donggu-backup-").first
        brow.get_by_placeholder("백업 비밀번호").fill("backup-pass-e2e-0001")
        brow.get_by_role("button", name="열어 확인").click()
        expect(page.get_by_text(re.compile("정상: 접수 \\d+건"))).to_be_visible()
        ok(True, "설정: 암호화 백업 만들기 → 백업 비밀번호로 열어 확인")

        # 행정동 관리 → 접수 화면에서 주소로 동 자동 선택 → 목록 동 필터
        page.goto(URL + "#/settings/dongs")
        dc = page.locator(".card", has=page.get_by_role("heading", name="행정동 관리"))
        dc.get_by_placeholder(re.compile("^새 동 이름")).fill("가상1동")
        dc.get_by_placeholder(re.compile("^법정동·도로명")).fill("예시로, 가상법정동")
        dc.get_by_label("새 동 월요일").check()
        dc.get_by_role("button", name="추가").click()
        expect(dc.get_by_label("가상1동 이름")).to_have_value("가상1동")
        ok(True, "설정 → 행정동 관리: 가상1동(판별 낱말·수거 요일) 추가")
        page.goto(URL + "#/request/new")
        page.get_by_label("이름").fill("가상넷")
        page.get_by_label("연락처").fill("01000005004")
        page.get_by_label("배출 주소").fill("가상시 동구 예시로 5")
        page.get_by_label("배출 주소").press("Tab")
        expect(page.get_by_label("행정동")).to_have_value("가상1동")
        expect(page.locator(".calday").first).to_be_visible()
        ok(page.locator(".calday").count() == 14 and page.get_by_label("접수 경로").input_value() == "전화", "접수 화면: 주소 낱말로 행정동 자동 선택, 다음 업무일 14일 예약 현황, 접수 경로 기본 '전화'")
        page.get_by_role("button", name="가장 빠른 가능일").click()
        ok(page.get_by_label("수거 예정일").input_value() != "", "가장 빠른 가능일 단추로 예정일 채움")
        page.get_by_placeholder(re.compile("품목명·규격")).fill("3인용 소파")
        page.locator(".results button").first.click()
        page.get_by_role("button", name="품목 추가", exact=True).click()
        # 세션이 끝난 뒤 저장 → 다시 로그인 대화 상자 → 같은 요청 이어서 보냄 (입력 내용 유지)
        page.evaluate("""async () => fetch('/api/logout', {method:'POST', headers:{'X-DG-CSRF':'1'}})""")
        page.get_by_role("button", name="접수 등록").click()
        dlg = page.get_by_role("dialog", name="다시 로그인")
        expect(dlg).to_be_visible()
        dlg.get_by_label("비밀번호").fill(PW)
        dlg.get_by_role("button", name="로그인하고 계속").click()
        expect(page.get_by_role("heading", name=re.compile("접수 DG-"))).to_be_visible()
        ok(page.get_by_label("행정동").input_value() == "가상1동", "세션 만료 뒤 저장 → 다시 로그인 대화 상자 → 작성한 접수 그대로 등록")
        page.goto(URL + "#/request/new")
        page.get_by_label("이름").fill("가상다섯")
        page.get_by_label("연락처").fill("01000005005")
        page.get_by_label("배출 주소").fill("가상시 동구 예시로 6")
        page.goto(URL + "#/requests")
        page.goto(URL + "#/request/new")
        expect(page.get_by_text("작성 중이던 접수를 불러왔습니다")).to_be_visible()
        ok(page.get_by_label("이름").input_value() == "가상다섯", "작성 중인 새 접수는 다른 화면에 다녀와도 남음(메모리)")
        page.get_by_placeholder(re.compile("품목명·규격")).fill("장농")
        page.locator(".results button").first.click()
        page.get_by_role("button", name="품목 추가", exact=True).click()
        page.get_by_role("button", name="접수 등록").click()
        expect(page.get_by_role("heading", name=re.compile("접수 DG-"))).to_be_visible()
        page.goto(URL + "#/requests")
        page.locator("#rq-status").select_option("")
        page.locator("#rq-dong").select_option("가상1동")
        expect(page.locator("tbody tr.click")).to_have_count(2)
        ok("가상넷" in page.locator("tbody").inner_text() and "가상하나" not in page.locator("tbody").inner_text(), "접수 목록: 동 열·동 조회조건으로 거르기")
        # 일괄 예정일 변경
        later = (dt.date.fromisoformat(today) + dt.timedelta(days=30)).isoformat()
        page.get_by_label("이 쪽 모두 고르기").check()
        expect(page.get_by_text("2건 선택")).to_be_visible()
        page.get_by_role("button", name="예정일 변경").click()
        bd = page.get_by_role("dialog")
        bd.get_by_label("새 수거 예정일").fill(later)
        bd.locator("select").select_option("RESIDENT")
        bd.get_by_role("button", name="변경").click()
        expect(page.get_by_text("2건 예정일을 바꿨습니다")).to_be_visible()
        c_ = store.connect()
        ok(all(r["planned_date"] == later for r in S.rows(c_, "SELECT planned_date FROM request WHERE dong='가상1동'"))
           and len(S.list_audit(c_, entity="request", action="RESCHEDULE")) == 2, f"일괄 예정일 변경: 2건 → {later}, 사유 코드·건별 이력")
        page.keyboard.press("Escape")

        # 외부 서비스(v0.11): 행안부 주소 검색·카카오모빌리티 — PC 서버가 대신 묻고(가짜 서버), 브라우저는 직접 연결하지 않음
        page.goto(URL + "#/settings/addr")
        xc = page.locator(".card", has=page.get_by_role("heading", name=re.compile("^외부 서비스")))
        xc.get_by_label("주소 검색 승인키").fill("E2EJUSOKEY-123456")
        xc.get_by_label("좌표 승인키").fill("E2ECOORDKEY-654321")
        xc.get_by_label("카카오모빌리티 REST 키").fill("E2EKAKAOKEY-777777")
        xc.get_by_label("이동시간 계산 방식").select_option("KAKAO")
        xc.get_by_role("button", name="저장", exact=True).click()
        expect(xc.get_by_text("저장됨 ••••3456").first).to_be_visible()
        boot_txt = page.evaluate("async () => (await fetch('/api/bootstrap', {headers: {'X-DG-CSRF': '1'}})).text()")
        ok("E2EJUSOKEY-123456" not in boot_txt and "E2EKAKAOKEY-777777" not in boot_txt and "••••7777" in boot_txt and xc.get_by_label("주소 검색 승인키").input_value() == ""
           and xc.get_by_label("이동시간 계산 방식").input_value() == "KAKAO", "설정 → 주소·외부 서비스: 승인키·REST 키 저장 후 화면(브라우저)에는 가린 값만, 이동시간 방식 카카오모빌리티")
        xc.get_by_role("button", name="주소 검색 연결 시험").click()
        expect(xc.get_by_text(re.compile("✓ 주소 검색 정상.*좌표 정상"))).to_be_visible()
        xc.get_by_role("button", name="카카오모빌리티 연결 시험").click()
        expect(xc.get_by_text(re.compile("✓ 카카오모빌리티 정상"))).to_be_visible()
        ok(True, "설정 [연결 시험]: 행안부 주소(검색·좌표)·카카오모빌리티 (개인정보 없는 고정 값)")
        later2 = (dt.date.fromisoformat(today) + dt.timedelta(days=45)).isoformat()
        page.goto(URL + "#/request/new")
        expect(page.locator(".calday").first).to_be_visible()
        quiet(page)
        page.get_by_label("이름").fill("가상주소")
        page.get_by_label("연락처").fill("01000005009")
        n_juso = len(FAKE["juso"])
        page.get_by_label("주소 찾기").fill("010-1234-5678")
        expect(page.locator(".addr-sug .jusonote")).to_contain_text("전화번호")
        page.get_by_label("주소 찾기").fill("가상주소")
        expect(page.locator(".addr-sug .jusonote")).to_contain_text("이름")
        ok(len(FAKE["juso"]) == n_juso, "주소 찾기 검색어에 전화번호·신청인 이름이 있으면 PC 서버가 행안부로 보내지 않고 안내")
        page.get_by_label("주소 찾기").fill("서남로 1")
        jb = page.locator(".addr-sug button", has_text="행안부")
        expect(jb.first).to_be_visible()
        page.screenshot(path=SHOTS / "pc_11_juso.png")
        jb.first.click()
        expect(page.get_by_label("배출 주소")).to_have_value(re.compile("서남로 1 \\(가상동, 가상청사\\)"))
        expect(page.locator("input[readonly]").first).not_to_have_value("")
        ok(abs(float(page.locator("input[readonly]").first.input_value()) - 35.1555) < 1e-5 and page.get_by_label("행정동").input_value() == "가상1동",
           "주소 찾기: 이 PC 자료 다음에 '행안부' 결과 → 고르면 주소·좌표(좌표 API, UTM-K→WGS84)·동(행정동) 채움")
        kws = [c["q"].get("keyword") for c in FAKE["juso"] if c["path"].endswith("addrLinkApi.do")]
        ok(set(kws) <= {"서남로 1", "동구청"} and all(set(c["q"]) <= {"confmKey", "currentPage", "countPerPage", "keyword", "resultType", "admCd", "rnMgtSn", "udrtYn", "buldMnnm", "buldSlno"} for c in FAKE["juso"])
           and not any(k.lower() in ("cookie", "referer") for c in FAKE["juso"] for k in c["headers"]), f"행안부로 간 것: 검색어({', '.join(sorted(set(kws)))})·승인키·건물 식별 번호뿐, 쿠키·Referer 없음")
        page.get_by_label("수거 예정일").fill(later2)
        page.get_by_placeholder(re.compile("품목명·규격")).fill("3인용 소파")
        page.locator(".results button").first.click()
        page.get_by_role("button", name="품목 추가", exact=True).click()
        page.get_by_role("button", name="접수 등록").click()
        expect(page.get_by_role("heading", name=re.compile("접수 DG-"))).to_be_visible()
        dismiss(page)
        page.goto(URL + "#/dispatch")
        page.get_by_label("수거일").fill(later2)
        page.get_by_role("button", name="노선 계산").click()
        expect(page.locator(".matrix-note")).to_contain_text("도로 시간: 카카오모빌리티", timeout=30000)
        km = page.locator("a.maplink").first
        href = km.get_attribute("href")
        ok(href.startswith("https://map.kakao.com/link/to/") and urllib.parse.unquote(href).startswith("https://map.kakao.com/link/to/1번 수거지,35.1555") and "가상주소" not in urllib.parse.unquote(href)
           and km.get_attribute("target") == "_blank" and "noreferrer" in km.get_attribute("rel"), f"배정·노선: 지점마다 카카오맵 링크(좌표와 'N번 수거지'만) {urllib.parse.unquote(href)}")
        kb = [c for c in FAKE["kakao"] if c["body"] or c["q"]]
        ok(kb and all(c["auth"] == "KakaoAK E2EKAKAOKEY-777777" for c in kb) and not re.search(r"[가-힣]", json.dumps([c["body"] or c["q"] for c in kb], ensure_ascii=False)),
           f"카카오모빌리티로 간 것: 좌표·키 머리글뿐 ({len(kb)}회)")
        page.screenshot(path=SHOTS / "pc_12_dispatch_kakao.png")
        slog = (WORK / "state" / "logs" / "server.log").read_text(encoding="utf-8")
        ok("/api/juso-search" in slog and "서남로" not in slog and "가상주소" not in slog and "E2EJUSOKEY" not in slog, "서버 로그: 주소 검색 경로만 남고 검색어·이름·키 없음")
        acc = S.rows(store.connect(), "SELECT filter, count FROM access_log WHERE filter LIKE '%행안부%'")
        ok(acc and not any("서남로" in (a["filter"] or "") for a in acc), "접속기록: 행안부 주소 검색은 건수만 (검색어 없음)")

        # 오늘 현황
        page.goto(URL + "#/home")
        expect(page.locator(".kpi").first).to_be_visible()
        page.screenshot(path=SHOTS / "pc_10_home.png", full_page=True)
        ok(page.locator(".kpi", has_text="수거 완료").inner_text().find("1건") >= 0 and page.locator(".crew").count() == 1, "오늘 현황: 완료 1건, 팀별 진행 표시")
        ok(page.locator(".todo-a", has_text="이번 달 접속기록 점검").count() == 1, "오늘 현황(관리자): 이번 달 접속기록 점검 알림")

        # 화면 설정: 어두운 테마·글자 크기 (사용자별 서버 저장, 다시 열어도 유지)
        page.get_by_label("화면 테마").select_option("dark")
        page.wait_for_function("() => document.documentElement.dataset.theme === 'dark'")
        bg = lambda: page.evaluate("() => getComputedStyle(document.body).backgroundColor")
        lum = lambda c: sum(int(x) for x in re.findall(r"\d+", c)[:3])
        page.get_by_label("글자 크기").select_option("16")
        page.wait_for_function("() => getComputedStyle(document.documentElement).fontSize === '16px'")
        page.reload()
        expect(page.get_by_label("화면 테마")).to_have_value("dark")
        ok(lum(bg()) < 150 and page.evaluate("() => getComputedStyle(document.documentElement).fontSize") == "16px" and page.get_by_label("글자 크기").input_value() == "16",
           f"테마 어둡게·글자 크게 → 바탕 어두움({bg()}), 다시 열어도 유지(계정에 저장)")
        page.screenshot(path=SHOTS / "pc_20_dark.png", full_page=True)
        page.get_by_label("화면 테마").select_option("light")
        page.get_by_label("글자 크기").select_option("15")
        page.wait_for_function("() => document.documentElement.dataset.theme === 'light'")
        ok(lum(bg()) > 600, "테마 밝게로 되돌림")
        # 좁은 화면: 메뉴 단추로 열고 닫기, 표는 카드 안에서 가로 스크롤
        page.set_viewport_size({"width": 900, "height": 900})
        expect(page.locator("nav.side")).to_be_hidden()
        page.get_by_role("button", name="메뉴 열기·닫기").click()
        expect(page.locator("nav.side")).to_be_visible()
        page.screenshot(path=SHOTS / "pc_21_narrow_menu.png")
        page.locator("nav.side a", has_text="접수").first.click()
        expect(page.get_by_role("heading", name="접수", exact=True)).to_be_visible()
        sw = page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
        page.set_viewport_size({"width": 1400, "height": 950})
        ok(sw, "1000px 아래: 메뉴 단추로 여는 메뉴, 화면 가로 넘침 없음(표는 카드 안에서 스크롤)")

        # 수수료 감면 → 부과 금액 → 과납 → 자동 환불 건, 접수증·배출 표지 인쇄
        c_ = store.connect()
        r1id = s1["request_id"]
        page.goto(URL + f"#/request/{r1id}")
        dismiss(page)
        rc = page.locator(".card", has=page.get_by_role("heading", name=re.compile("^수수료 감면")))
        rc.get_by_label("감면 사유").select_option("NEAR_POOR")
        rc.get_by_label("증빙 확인 방법").select_option("서류 확인")
        rc.get_by_role("button", name="감면 저장").click()
        expect(rc.get_by_text("감면 변경 사유를 적어 주세요")).to_be_visible()  # 마감한 날의 접수: 사유 필수
        rc.get_by_label("감면 변경 사유").fill("증빙 늦게 제출(가상)")
        rc.get_by_role("button", name="감면 저장").click()
        expect(page.locator(".feeflow")).to_contain_text("차상위계층 50%")
        dismiss(page)
        pay_card = page.locator(".card", has=page.locator("h2", has_text="수납 기록"))
        ps1 = S.payment_status(c_, r1id)
        ok(ps1["reduction_rate"] == 50 and ps1["levied"] == ps1["calculated"] - ps1["reduction"] and "과납" in pay_card.inner_text(),
           f"감면(차상위 50%) 적용 → 계산 {ps1['calculated']:,} → 감면 {ps1['reduction']:,} → 부과 {ps1['levied']:,}원, 이미 낸 금액은 과납 확인 필요")
        with ctx.expect_page() as rp_:
            page.get_by_role("button", name="접수증 인쇄").click()
        rcp = rp_.value; rcp.wait_for_load_state()
        rtxt = rcp.locator("body").inner_text()
        ok(s1["receipt_no"] in rtxt and S.get_request(c_, r1id)["levy_no"] in rtxt and "부과 금액" in rtxt and "문의처" in rtxt and "배출 장소 안내" in rtxt and "가상하나" not in rtxt,
           "접수증(신청인 보관용): 접수번호·부과번호·품목·계산→감면→부과 금액·배출일·안내·문의처")
        rcp.screenshot(path=SHOTS / "pc_22_receipt.png", full_page=True)
        rcp.close()
        with ctx.expect_page() as lp_:
            page.get_by_role("button", name="배출 표지 인쇄").click()
        lbl = lp_.value; lbl.wait_for_load_state()
        ok(lbl.locator(".label").count() >= 1 and lbl.locator(".label img.lqr").first.get_attribute("src").startswith("data:image/png") and s1["receipt_no"] in lbl.locator(".label").first.inner_text()
           and "가상하나" not in lbl.locator("body").inner_text(), f"배출 표지: A4 여러 장({lbl.locator('.label').count()}장), 접수번호 QR·동·배출일·품목, 이름·주소 없음")
        lbl.screenshot(path=SHOTS / "pc_23_labels.png", full_page=True)
        lbl.close()
        prints = S.list_audit(c_, entity="request", action="PRINT_RECEIPT") + S.list_audit(c_, entity="request", action="PRINT_LABEL")
        ok(len(prints) >= 2 and S.rows(c_, "SELECT COUNT(*) AS n FROM access_log WHERE action='PRINT'")[0]["n"] >= 2, "인쇄마다 변경 이력·접속기록(출력) 남음")
        # 수납: 새 납부 방법·영수번호
        pay_card.get_by_label("납부 방법").select_option("가상계좌")
        pay_card.get_by_label("금액", exact=True).fill("500")
        pay_card.get_by_label("영수번호·거래번호").fill("VA-E2E-1")
        pay_card.get_by_label("마감 후 수정 사유").fill("늦게 확인한 입금(가상)")
        pay_card.get_by_role("button", name="수납 기록").click()
        expect(page.locator(".card", has=page.locator("h2", has_text="수납 기록"))).to_contain_text("VA-E2E-1")
        ok(True, "수납 기록: 납부 방법(가상계좌)·영수번호")
        # 환불: 자동 생성(과납) → 신청(관리자) → 결재(팀장, 아래) → 지급(접수 담당자, 아래)
        page.goto(URL + "#/refunds")
        rrow = page.locator("tbody tr", has_text=s1["receipt_no"]).first
        expect(rrow).to_contain_text("자동 생성")
        rrow.get_by_role("button", name="신청").click()
        page.locator(".dlg").get_by_role("button", name="신청").click()
        expect(page.locator("tbody tr", has_text=s1["receipt_no"]).first).to_contain_text("결재 대기")
        ok(True, "환불 목록: 과납 → 자동 환불 건 → 신청(결재 대기)")

        # 세입 일계표·대사
        page.goto(URL + "#/revenue")
        expect(page.get_by_text("세외수입시스템 연계 형식은 기관 확인 필요")).to_be_visible()
        expect(page.locator(".card", has=page.get_by_role("heading", name=re.compile("^납부 방법별"))).locator("tbody")).to_contain_text("가상계좌")
        r2 = S.one(c_, "SELECT id, levy_no FROM request WHERE name='가상둘'")
        due2 = S.payment_status(c_, r2["id"])["levied"]
        recon = WORK / "recon.csv"
        recon.write_text(f"부과번호,금액,납부일,영수번호\n{r2['levy_no']},{due2},{today},R-E2E-1\n2099-99-99999,1000,{today},R-E2E-2\n", encoding="utf-8")
        page.get_by_label("대사 CSV 파일").set_input_files(str(recon))
        page.get_by_role("button", name="미리보기").click()
        expect(page.locator(".notice", has_text="일치 1")).to_be_visible()
        page.get_by_role("button", name=re.compile("일치 1건 납부로 기록")).click()
        page.locator(".dlg").get_by_role("button", name="기록").click()
        expect(page.get_by_text("1건을 납부로 기록했습니다")).to_be_visible()
        ok(S.payment_status(store.connect(), r2["id"])["records"][-1]["source"] == "대사", "세입·대사: 방법별 일계표, 대사 CSV 미리보기(일치·부과번호 없음) → 일치 건만 '대사' 납부로 기록")
        with page.expect_download() as dlr:
            page.get_by_role("button", name="명세").click()
        rtxt = Path(dlr.value.path()).read_text(encoding="utf-8-sig")
        ok(rtxt.startswith("납부일,부과번호") and r2["levy_no"] in rtxt and "가상둘" not in rtxt, "세입 명세 CSV(부과번호·방법·금액, 개인정보 없음)")

        # 통계: 집계 기준·월보 출력
        page.goto(URL + "#/stats")
        page.get_by_label("집계 기준").select_option("received")
        expect(page.get_by_role("heading", name=re.compile("접수일 기준"))).to_be_visible()
        dismiss(page)
        with ctx.expect_page() as mp_:
            page.get_by_role("button", name="월보 출력").click()
        mo = mp_.value; mo.wait_for_load_state()
        ok("대형폐기물 수거 월보" in mo.locator("h1").inner_text() and "접수일" in mo.locator("body").inner_text() and "가상하나" not in mo.locator("body").inner_text(),
           "통계: 집계 기준(접수일) 바꾸기, 월보 출력(개인정보 없음)")
        mo.close()

        # 자동 백업: 백업 키(개인키는 한 번만 내려받음) → 지금 자동 백업
        page.goto(URL + "#/settings/backup")
        settled(page)
        # 화면이 다 그려진 뒤(백업 키 상태가 보인 뒤) 누른다 — 늦은 다시 그리기가 대화 상자와 겹치지 않게
        expect(page.get_by_text("없음 — 자동 백업 안 함")).to_be_visible()
        dismiss(page)
        page.get_by_role("button", name="백업 키 만들기").click()
        kd = page.locator(".dlg")
        expect(kd).to_be_visible()
        kd.get_by_label("개인키 보호 비밀번호 (12자 이상)").fill("backup-key-e2e-01")
        kd.get_by_label("한 번 더").fill("backup-key-e2e-01")
        try:
            with page.expect_download(timeout=60000) as kdl:
                kd.get_by_role("button", name="만들고 개인키 내려받기").click()
        except Exception:
            print("백업 키 내려받기 실패 — 대화 상자:", kd.all_inner_texts(), "알림:", page.locator(".toast").all_inner_texts(), flush=True)
            raise
        pem = Path(kdl.value.path()).read_bytes()
        page.get_by_role("button", name="지금 자동 백업").click()
        expect(page.get_by_text(re.compile("자동 백업: donggu-auto-"))).to_be_visible()
        page.reload()
        arow = page.locator("tr", has_text="donggu-auto-").first
        arow.get_by_label(re.compile("개인키 파일")).set_input_files(files=[{"name": "k.pem", "mimeType": "application/x-pem-file", "buffer": pem}])
        arow.get_by_label(re.compile("비밀번호")).fill("backup-key-e2e-01")
        arow.get_by_role("button", name="열어 확인").click()
        expect(page.get_by_text(re.compile("정상: 접수 \\d+건"))).to_be_visible()
        ok(b"ENCRYPTED PRIVATE KEY" in pem and "백업 키" in page.locator(".card", has=page.get_by_role("heading", name="자동 백업")).inner_text(),
           "자동 백업: 백업 키 만들기(암호화된 개인키 내려받기) → 공개키로 자동 백업 → 개인키+비밀번호로 열어 확인")

        # 개인정보 보유기간·파기 (오래된 완료 건을 가상으로 만들어)
        c_.execute("UPDATE request SET status='DONE', completed_at='2020-01-05T00:00:00Z' WHERE name='가상다섯'")
        page.goto(URL + "#/settings/privacy")
        expect(page.get_by_text("정책 값 · 기관 결정 필요")).to_be_visible()
        page.get_by_role("button", name="파기 실행").click()
        page.locator(".dlg").get_by_role("button", name="파기").click()
        expect(page.get_by_text("1건 파기했습니다")).to_be_visible()
        g3 = S.one(store.connect(), "SELECT name, receipt_no, pii_purged_at FROM request WHERE receipt_no=(SELECT receipt_no FROM request WHERE pii_purged_at IS NOT NULL LIMIT 1)")
        ok(g3["name"] == "(파기)" and g3["pii_purged_at"], "개인정보 파기: 보유기간 지난 완료 건 가명 처리 (접수번호 유지)")
        page.goto(URL + "#/users")
        expect(page.locator(".card", has=page.get_by_role("heading", name="개인정보 파기 대장")).locator("tbody")).to_contain_text("admin")
        ok(True, "사용자·이력 화면에 파기 대장(일시·건수·작업자·기준)")

        # 개인정보 접속기록: 목록·무결성 검사·이번 달 점검
        page.goto(URL + "#/access")
        expect(page.locator("table.audit tbody tr").first).to_be_visible()
        tb = page.locator("table.audit tbody")
        expect(tb).to_contain_text("목록 조회")
        expect(tb).to_contain_text("상세 조회")
        page.get_by_label("작업", exact=True).select_option("PRINT")
        expect(tb).to_contain_text("배출 표지")
        expect(tb).to_contain_text("접수증")
        ok("가상하나" not in tb.inner_text(), "접속기록: 상세·목록·출력(접수증·배출 표지) 기록, 작업별 거르기 (내용·검색어 없음)")
        page.get_by_label("작업", exact=True).select_option("")
        page.get_by_role("button", name="무결성 검사").click()
        expect(page.get_by_text("무결성 검사 정상")).to_be_visible()
        page.get_by_label("점검 메모").fill("이상 없음(가상)")
        page.get_by_role("button", name="점검 완료").click()
        expect(page.locator(".chip", has_text="점검 완료")).to_be_visible()
        with page.expect_download() as adl:
            page.get_by_role("button", name="CSV 내보내기").click()
        ok(Path(adl.value.path()).read_text(encoding="utf-8-sig").startswith("번호,시각"), "무결성 검사(해시 사슬) 정상 → 이번 달 점검 완료 기록, 접속기록 CSV")
        page.goto(URL + "#/home")
        expect(page.locator(".kpi").first).to_be_visible()
        ok(page.locator(".todo-a", has_text="이번 달 접속기록 점검").count() == 0, "점검 완료 뒤 오늘 현황 알림 사라짐")
        page.goto(URL + "#/settings/basic")
        ok(page.locator("nav.tabs a").count() == 10 and page.locator("nav.tabs a.on").inner_text() == "기본", "설정: 기본·수거팀·기기·행정동·한도·휴무일·수수료/감면·주소·외부 서비스·백업·개인정보 탭")
        page.screenshot(path=SHOTS / "pc_24_settings.png", full_page=True)

        # 통계·보고서 + 배차표 인쇄
        page.goto(URL + "#/stats")
        dismiss(page)
        expect(page.locator("svg.chart g.bar").first).to_be_visible()
        page.locator("svg.chart g.bar").first.hover()
        expect(page.locator(".tip")).to_contain_text("예정")
        with page.expect_download() as dlc:
            page.get_by_role("button", name="접수별 CSV").click()
        csv_txt = Path(dlc.value.path()).read_text(encoding="utf-8-sig")
        ok(csv_txt.startswith("접수번호") and "가상하나" not in csv_txt and "0100000500" not in csv_txt and "상태 코드" in csv_txt and "부과 금액(원)" in csv_txt,
           "통계 화면: 날짜별 막대·툴팁, 접수별 CSV(이름·연락처 없음, 상태 한글+코드, 부과 금액) 내려받기")
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
        page.get_by_placeholder("아이디 (영문)").fill("lead1")
        page.get_by_placeholder("이름", exact=True).fill("가상 팀장")
        page.locator("select", has=page.locator("option[value=LEAD]")).select_option("LEAD")
        page.get_by_placeholder("임시 비밀번호 (10자 이상)", exact=True).fill("temp-pass-e2e-03")
        page.get_by_role("button", name="사용자 추가").click()
        expect(page.get_by_role("cell", name="lead1", exact=True).first).to_be_visible()
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
        p3.locator("#rq-status").select_option("")
        expect(p3.locator("tr.click").first).to_be_visible()
        names = p3.locator("tbody").inner_text()
        other = p3.evaluate("""async (id) => (await fetch('/api/requests/' + id, {headers:{'X-DG-CSRF':'1'}})).status""", S.rows(store.connect(), "SELECT id FROM request WHERE name='가상셋'")[0]["id"])
        ok("가상셋" not in names and "가상하나" in names and other == 400, "수거팀 계정: 내 팀에 배정된 접수만 보임, 다른 접수 열기 거부")
        c3.close()
        # 팀장(검토자): 마감 확인(2단계), 설정·사용자 관리 메뉴 없음
        c4 = b2.new_context(viewport={"width": 1400, "height": 950})
        p4 = c4.new_page()
        p4.goto(URL)
        p4.get_by_placeholder("아이디").fill("lead1")
        p4.get_by_placeholder("비밀번호").fill("temp-pass-e2e-03")
        p4.get_by_role("button", name="로그인").click()
        p4.locator("label", has_text="현재 비밀번호").locator("input").fill("temp-pass-e2e-03")
        p4.locator("label", has_text="새 비밀번호 (10자").locator("input").fill("lead-pass-0004")
        p4.locator("label", has_text="새 비밀번호 확인").locator("input").fill("lead-pass-0004")
        p4.get_by_role("button", name="비밀번호 바꾸기").click()
        expect(p4.get_by_role("heading", name="접수", exact=True)).to_be_visible()
        p4.goto(URL + "#/close")
        p4.get_by_label("수거일").fill(today)
        p4.get_by_role("button", name="팀장 확인").click()
        p4.get_by_role("dialog").get_by_role("button", name="확인 기록").click()
        expect(p4.get_by_text("팀장 확인 (lead1)")).to_be_visible()
        p4.goto(URL + "#/refunds")
        p4.locator("tbody tr", has_text=s1["receipt_no"]).first.get_by_role("button", name="결재").click()
        p4.locator(".dlg").get_by_role("button", name="승인").click()
        expect(p4.locator("tbody tr", has_text=s1["receipt_no"]).first).to_contain_text("결재 완료")
        ok(True, "환불 결재: 팀장(신청하지 않은 사람)이 승인")
        nav4 = p4.locator("nav.side").inner_text()
        st4 = p4.evaluate("""async () => (await fetch('/api/close', {method:'POST', headers:{'X-DG-CSRF':'1','Content-Type':'application/json'}, body:JSON.stringify({date:'2026-01-01'})})).status""")
        ok("설정" not in nav4 and "변경 이력" in nav4 and st4 == 403 and S.close_info(store.connect(), today)["approved_by"] == "lead1",
           "팀장: 담당자가 기록한 마감을 확인(2단계), 변경 이력 조회 가능, 설정 메뉴·마감 기록 API 없음")
        c4.close()
        # 지급 기록: 접수 담당자 (계좌번호는 저장하지 않고 확인 여부·끝 4자리만)
        p2.goto(URL + "#/refunds")
        p2.locator("tbody tr", has_text=s1["receipt_no"]).first.get_by_role("button", name="지급 기록").click()
        d2 = p2.locator(".dlg")
        d2.get_by_label("예금주·계좌를 확인함").check()
        d2.get_by_label("계좌 끝 4자리").fill("1234")
        d2.get_by_role("button", name="지급 기록").click()
        expect(p2.get_by_text("지급을 기록했습니다")).to_be_visible()
        p2.goto(URL + "#/home")
        p2.goto(URL + "#/refunds")
        p2.locator(".qchip", has_text="지급 완료").click()
        expect(p2.locator("tbody tr", has_text=s1["receipt_no"]).first).to_contain_text("지급 완료")
        ps_end = S.payment_status(store.connect(), s1["request_id"])
        ok(ps_end["status"] in ("PAID", "UNDERPAID") and any(x["kind"] == "REFUND" and x["source"] == "환불 결재" for x in ps_end["records"]),
           f"환불 지급 기록(접수 담당자) → 환불 납부 기록, 수납 상태 {ps_end['status']}")
        b2.close()

        # 기기 코드 QR(휴대폰 화면) → PC 웹캠으로 읽어 바로 등록 (가짜 카메라 영상)
        phone2 = Phone(S.registration_text(store.connect()))
        write_y4m(WORK / "devcode.y4m", [phone2.code()])
        b4 = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), headless=True,
                                args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", f"--use-file-for-fake-video-capture={WORK / 'devcode.y4m'}"])
        p4 = b4.new_context(viewport={"width": 1400, "height": 950}, permissions=["camera"]).new_page()
        p4.goto(URL)
        p4.get_by_placeholder("아이디").fill("admin")
        p4.get_by_placeholder("비밀번호").fill(PW)
        p4.get_by_role("button", name="로그인").click()
        expect(p4.get_by_role("heading", name="오늘 현황")).to_be_visible()
        p4.goto(URL + "#/settings/devices")
        p4.get_by_placeholder(re.compile("기기 이름")).fill("2팀 업무폰")
        p4.get_by_role("button", name="휴대폰 QR 웹캠으로 읽기").click()
        expect(p4.locator(".big")).to_contain_text("지문", timeout=30000)
        fp4 = re.sub(r"\D", "", p4.locator(".big").inner_text())
        ok(fp4 == P.fingerprint(P.pub_raw(phone2.pc_sign), P.pub_raw(phone2.pc_kex), phone2.id, P.pub_raw(phone2.kex), P.pub_raw(phone2.sign))
           and any(d["label"] == "2팀 업무폰" for d in S.list_devices(store.connect())),
           f"휴대폰 기기 코드 QR을 PC 웹캠으로 읽어 바로 등록 (붙여 넣기 없이), 지문 일치 ({fp4})")
        b4.close()

    external = [u for u, _ in REQUESTS if not u.startswith(("http://127.0.0.1", "data:", "blob:"))]
    hosts = sorted({u.split("/")[2] for u in external})
    ok(all(h == "tiles.openfreemap.org" for h in hosts), f"PC 화면(브라우저)의 외부 요청은 지도 서버만 ({', '.join(hosts) or '없음'}) — 행안부·카카오는 PC 서버가 물음")
    ok(FAKE.get("base") and not any(u.startswith(FAKE["base"]) for u, _ in REQUESTS) and FAKE["juso"] and FAKE["kakao"],
       "브라우저는 주소 API·길찾기 API(가짜 서버)에 직접 연결하지 않음 (PC 서버만 연결)")
    secrets = ["가상하나", "가상둘", "가상셋", "01000005001", "01000005003", "시험로"]
    ok(not [s for s in secrets if any(s in urllib.parse.unquote(u) for u in external)], "외부 요청에 이름·연락처·주소 없음")


@contextlib.contextmanager
def playwright_with_fail_shot():
    """실패하면 Playwright를 닫기 전에 마지막 화면을 SHOTS/fail.png로 남긴다(닫힌 뒤에는 캡처할 수 없음)."""
    with sync_playwright() as pw:
        try:
            yield pw
        except BaseException:
            for pg in LAST_PAGE[-1:]:
                try:
                    pg.screenshot(path=SHOTS / "fail.png", full_page=True)
                    print("실패 화면:", SHOTS / "fail.png", flush=True)
                except Exception as e:  # noqa: BLE001
                    print("실패 화면 캡처 못 함:", e, flush=True)
            raise


def quiet(pg, ms=8000):
    """네트워크가 조용해질 때까지 기다리되, 지도 조각을 계속 받는 경우(CI) 시간이 지나면 그냥 진행한다."""
    try:
        pg.wait_for_load_state("networkidle", timeout=ms)
    except Exception:  # noqa: BLE001
        pg.wait_for_timeout(500)


def settled(pg):
    """화면이 다 그려질 때까지(늦게 오는 예약 현황 등) 기다린다 — 그 전에 입력하면 늦은 다시 그리기가 지울 수 있다."""
    quiet(pg)
    expect(pg.locator("main h1")).to_be_visible()
    if pg.get_by_role("group", name="예약 현황").count():
        expect(pg.get_by_role("group", name="예약 현황").get_by_role("button").first).to_be_visible()
    quiet(pg)


def dismiss(pg):
    """오른쪽 위 오류·경고 알림(눌러서 닫기)을 닫는다 — 그 아래 단추를 누르기 전에."""
    for t in pg.locator(".toast.bad").all():
        try:
            t.click(timeout=2000)
        except Exception:
            pass


DIALOG = {"text": ""}
import urllib.parse  # noqa: E402

if __name__ == "__main__":
    main()
