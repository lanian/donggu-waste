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
        return P.open_envelope(c.assemble(bid), self.pc_sign, self.kex, self.id)

    def ev(self, stop, kind, body):
        self.seq += 1
        return {"event_id": str(uuid.uuid4()), "seq": self.seq, "request_id": stop["request_id"], "assign_ver": stop["assign_ver"],
                "base_req_ver": stop["req_ver"], "kind": kind, "body": body, "device_ts": f"2026-11-03T02:{self.seq:02d}:00Z"}

    def seal(self, payload, btype):
        head = P.Header(btype, uuid.uuid4().bytes, self.id, 1, 1, P.key_id(P.pub_raw(self.sign)))
        return P.seal(payload, head, self.pc_kex, self.sign)[0]


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
        page.get_by_placeholder("업무 비밀번호").fill(PW)
        page.get_by_placeholder("한 번 더").fill(PW)
        page.get_by_role("button", name="비밀번호 설정").click()
        expect(page.get_by_role("heading", name="접수")).to_be_visible()
        ok(True, "최초 업무 비밀번호 설정 후 로그인")

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
        expect(page.get_by_text(re.compile("미배정 1건"))).to_be_visible(timeout=20000)
        ok(page.get_by_text(re.compile("직선거리 추정\\(도로 아님\\)")).is_visible() and page.get_by_text(re.compile("좌표가 없음")).is_visible(),
           "노선 계산: 직선거리 추정 표시, 위치 없는 건은 미배정 사유와 함께 표시")
        ok(page.locator(".stopline").filter(has_text="도착 ").count() == 2, "위치가 있는 2건 방문 순서·도착 예상 시각 추천")
        page.wait_for_timeout(2500)
        page.screenshot(path=SHOTS / "pc_03_dispatch.png")
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

        # 마감
        page.goto(URL + "#/close")
        page.get_by_label("수거일").fill(today)
        expect(page.get_by_text(re.compile("결과 미회수 \\(배정했으나 결과 없음\\) 1건"))).to_be_visible()
        page.get_by_role("button", name="마감 기록").click()
        expect(page.get_by_text("마감 기록됨")).to_be_visible()
        ok(True, "일일 마감: 결과 미회수 1건 이월 표시 후 마감 기록")
        page.screenshot(path=SHOTS / "pc_06_close.png", full_page=True)
        ok(not errors, "화면 스크립트 오류 없음" + (": " + "; ".join(errors) if errors else ""))
        b.close()

    external = [u for u, _ in REQUESTS if not u.startswith(("http://127.0.0.1", "data:", "blob:"))]
    hosts = sorted({u.split("/")[2] for u in external})
    ok(all(h == "tiles.openfreemap.org" for h in hosts), f"PC 화면의 외부 요청은 지도 서버만 ({', '.join(hosts) or '없음'})")
    secrets = ["가상하나", "가상둘", "가상셋", "01000005001", "01000005003", "시험로"]
    ok(not [s for s in secrets if any(s in urllib.parse.unquote(u) for u in external)], "외부 요청에 이름·연락처·주소 없음")


DIALOG = {"text": ""}
import urllib.parse  # noqa: E402

if __name__ == "__main__":
    main()
