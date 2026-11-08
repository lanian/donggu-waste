"""PC 업무 이어하기: 실제 임시 SQLCipher 서버와 브라우저의 화면·이동 회귀.

실행: python tests/test_workflow_pc.py
사진은 DG_WORKFLOW_SHOTS 또는 시스템 임시 폴더에 남긴다. 모든 자료는 가상 자료다.
"""
from __future__ import annotations

import base64
import hashlib
import os
import json
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from test_review_ui import pc_server

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))
import dgproto as P  # noqa: E402

PW = "workflow-password-123"
DATE = "2099-01-02"
SHOTS = Path(os.environ["DG_WORKFLOW_SHOTS"]) if os.environ.get("DG_WORKFLOW_SHOTS") else (
    Path(os.environ["DG_TEST_DIR"]) / "shots" if os.environ.get("DG_TEST_DIR") else Path(tempfile.mkdtemp(prefix="dg-workflow-pc-shots-")))
passed = 0


def ok(condition, message):
    global passed
    if not condition:
        raise AssertionError("실패: " + message)
    passed += 1
    print("  ✓", message, flush=True)


def post(page, path, body):
    response = page.request.post(urllib.parse.urljoin(page.url, "/api/" + path), headers={"X-DG-CSRF": "1"}, data=body)
    assert response.ok, f"fixture {path}: {response.status} {response.text()}"
    return response.json()


def refresh_boot(page):
    # API fixture의 설정·계정 변경을 앱의 기존 bootstrap/render 경로로 반영한다.
    page.evaluate("""async () => {
        const C = await import('/core.js');
        C.setBoot(await C.api('bootstrap'));
        await C.render(true);
    }""")


def failure_evidence(page, url, failures):
    """helper가 서버를 정리하기 전에 실패한 출처의 HTTP·리스너·브라우저 증거를 남긴다."""
    evidence = {"url": page.url, "request_failures": failures}
    request = urllib.request.Request(url + "api/state", headers={"X-DG-CSRF": "1"})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=3) as response:
            evidence["http_probe"] = {"status": response.status, "body": response.read().decode("utf-8")}
    except Exception as error:
        evidence["http_probe"] = repr(error)
    if os.name == "nt":
        port = urllib.parse.urlsplit(url).port
        command = (f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue | "
                   "Select-Object LocalAddress,LocalPort,RemoteAddress,RemotePort,State,OwningProcess | ConvertTo-Json -Compress")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        evidence["tcp"] = {"exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    page.screenshot(path=str(SHOTS / "pc_workflow_failure.png"), full_page=True)
    (SHOTS / "pc_workflow_failure.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  실패 증거: " + str(SHOTS / "pc_workflow_failure.json"), flush=True)


def home(page, url, day=None):
    page.goto(url + "#/home")
    expect(page.get_by_role("heading", name="오늘 현황", exact=True)).to_be_visible()
    if day:
        page.get_by_label("기준일", exact=True).fill(day)
        page.get_by_label("기준일", exact=True).dispatch_event("change")
    # hashchange가 시작한 새 render를 기다려 이전 Home DOM에서 다음 동작을 하지 않는다.
    page.evaluate("async () => (await import('/core.js')).render(true)")
    expect(page.locator(".workflow")).to_contain_text(day or page.get_by_label("기준일").input_value())
    return page.locator(".workflow")


def run_suite(browser):
    SHOTS.mkdir(parents=True, exist_ok=True)
    with pc_server() as url:
        ctx = browser.new_context(viewport={"width": 1440, "height": 1050}, service_workers="block")
        failures = []
        ctx.on("requestfailed", lambda request: failures.append({"url": request.url, "error": request.failure}))
        ctx.on("weberror", lambda error: failures.append({"page_error": str(error.error)}))
        page = ctx.new_page()
        try:
            page.goto(url)
            expect(page.get_by_placeholder("아이디", exact=True)).to_be_visible()
            post(page, "setup", {"username": "workflowadmin", "display_name": "가상 업무관리자", "password": PW})
            refresh_boot(page)
            board = home(page, url)
            expect(board.locator(".workflow-step")).to_have_count(5)
            expect(board.locator(".workflow-primary")).to_have_text("전화 접수 등록 →")
            ordered = page.evaluate("""() => {
                const head = document.querySelector('.dash > .pagehead'), board = document.querySelector('.workflow');
                const ready = document.querySelector('.readiness'), kpis = document.querySelector('.kpis');
                return !!(head.compareDocumentPosition(board) & 4) && !!(board.compareDocumentPosition(kpis) & 4)
                    && !!(kpis.compareDocumentPosition(ready) & 4);
            }""")
            ok(ordered, "제목·날짜 → 업무 5단계 → 지표 → 운영 준비 점검 순서")
            board.locator(".workflow-primary").press("Enter")
            expect(page.get_by_role("heading", name="전화 접수 등록", exact=True)).to_be_visible()
            ok(True, "빈 날짜 주 동작은 키보드로 실제 접수 등록 화면으로 이동")

            # 기기·배차·접수는 기존 실제 API로 준비한다. DB와 키는 helper의 임시 폴더에만 있다.
            raw = uuid.uuid4().bytes + P.pub_raw(P.gen_key()) + P.pub_raw(P.gen_key())
            code = "DGD1." + base64.urlsafe_b64encode(raw + hashlib.sha256(raw).digest()[:4]).decode().rstrip("=")
            dev = post(page, "devices/register", {"code": code, "label": "가상 업무폰"})["device_id"]
            post(page, "devices/" + dev + "/confirm", {})
            crew = post(page, "crews", {"name": "가상 이어하기팀", "device_id": dev})["id"]
            fee = page.evaluate("async () => (await import('/web/fees-data.js')).BUILTIN_FEES.rows.find(r => r.status === 'CONFIRMED' && r.price > 0).id")
            request = post(page, "requests", {"name": "가상 이어하기", "phone": "01000004567", "address": "가상구 시험로 1",
                                                   "planned_date": DATE, "lat": 35.1, "lon": 126.9,
                                                   "items": [{"fee_rate_id": fee, "qty": 1}]})
            rid = request["id"]
            # API로 만든 설정 fixture를 실제 앱 bootstrap에 다시 읽힌다.
            refresh_boot(page)
            board = home(page, url, DATE)
            expect(board.locator('[data-step="dispatch"]')).to_contain_text("미배정 1건")
            expect(board.locator(".workflow-primary")).to_have_attribute("href", "#/dispatch?date=" + DATE)
            before = page.evaluate("async () => (await (await import('/core.js')).api('batches?date=' + '2099-01-02')).length")
            board.locator(".workflow-primary").click()
            expect(page.locator("#dp-date")).to_have_value(DATE)
            after = page.evaluate("async () => (await (await import('/core.js')).api('batches?date=' + '2099-01-02')).length")
            ok(before == after == 0, "미배정 CTA가 선택 날짜 배정 화면으로 이동하며 배정을 자동 확정하지 않음")

            post(page, "assign", {"date": DATE, "plan": {crew: [{"request_id": rid, "eta": "09:30"}]}})
            board = home(page, url, DATE)
            expect(board.locator('[data-step="send"]')).to_contain_text("미전송 1팀")
            expect(board.locator(".workflow-primary")).to_have_attribute("href", "#/send?date=" + DATE)
            board.locator(".workflow-primary").click()
            expect(page.locator("#send-date")).to_have_value(DATE)
            expect(page.get_by_text("아직 전송하지 않았습니다.", exact=True)).to_be_visible()
            ok(True, "첫 목록 전송 CTA는 선택 날짜 목록 전송 화면을 열고 QR 생성은 하지 않음")

            batch = page.evaluate("async d => (await (await import('/core.js')).api('batches?date=' + d)).find(b => b.status === 'ACTIVE').id", DATE)
            post(page, "bundles", {"batch_id": batch})
            post(page, "assign", {"date": DATE, "plan": {crew: [{"request_id": rid, "eta": "09:45"}]}})
            board = home(page, url, DATE)
            expect(board.locator('[data-step="send"]')).to_contain_text("미전송 1팀")
            expect(board.locator('[data-step="send"]')).not_to_contain_text("수신 미확인")
            ok(True, "이전 갱신본의 미확인 전송을 현재 배차 전달 수치에 더하지 않음")

            # 미배정 조회 실패는 완료나 마감 권유로 바뀌면 안 된다.
            page.route("**/api/requests?*", lambda route: route.abort("failed") if "UNASSIGNED" in route.request.url else route.continue_())
            board = home(page, url, DATE)
            expect(board.locator('[data-step="dispatch"]')).to_contain_text("확인 필요")
            expect(board.locator(".workflow-primary")).not_to_have_attribute("href", "#/close?date=" + DATE)
            ok(True, "미배정 조회 오류는 확인 필요로 남기고 완료로 단정하지 않음")
            page.unroute("**/api/requests?*")

            # 근무 중의 아직 없는 현장 결과는 누락 경고가 아닌 진행 상태다.
            today = page.evaluate("async () => (await (await import('/core.js')).api('bootstrap')).today")
            post(page, "assign", {"date": DATE, "plan": {crew: []}})
            post(page, "assign", {"date": today, "plan": {crew: [{"request_id": rid, "eta": "09:30"}]}})
            page.clock.install(time=today + "T01:00:00Z")
            board = home(page, url, today)
            expect(board.locator('[data-step="results"]')).to_contain_text("수거 진행 중")
            ok(True, "근무 중 결과 미회수는 수거 진행 중으로 안내")
            page.clock.resume()
            page.screenshot(path=str(SHOTS / "pc_workflow_desktop.png"), full_page=True)
            page.set_viewport_size({"width": 980, "height": 900})
            board = home(page, url, today)
            no_overflow = board.evaluate("el => el.scrollWidth <= el.clientWidth && [...el.querySelectorAll('.workflow-step, .workflow-primary')].every(x => x.scrollWidth <= x.clientWidth)")
            ok(no_overflow, "980px 폭에서도 단계와 날짜·주 동작 문구가 가로로 넘치지 않음")
            page.screenshot(path=str(SHOTS / "pc_workflow_980.png"), full_page=True)

            closed_date = "2026-01-06"
            post(page, "close", {"date": closed_date, "note": "가상 마감"})
            board = home(page, url, closed_date)
            expect(board.locator('[data-step="close"]')).to_contain_text("팀장 확인 대기")
            expect(board.locator(".workflow-primary")).to_have_attribute("href", "#/close?date=" + closed_date)
            board.locator(".workflow-primary").click()
            expect(page.get_by_text(closed_date + " 접수 0건", exact=True)).to_be_visible()
            ok(True, "접수 0건인 마감 날짜도 신규 접수 대신 기록된 마감으로 이어짐")

            post(page, "crews", {"id": crew, "name": "가상 이어하기팀", "device_id": None})
            refresh_boot(page)
            board = home(page, url, today)
            expect(board.locator(".workflow-primary")).to_have_attribute("href", "#/settings/crews")
            expect(board.locator('[data-step="send"]')).to_contain_text("휴대폰 연결 확인 1팀")
            ok(True, "연결 휴대폰이 없으면 관리자에게 연결 설정을 먼저 안내")

            for role in ("LEAD", "FIELD", "INTAKE"):
                username = "workflow" + role.lower()
                post(page, "users", {"username": username, "display_name": "가상 " + role, "role": role,
                                      "password": PW, "crew_id": crew if role == "FIELD" else None})
                other = browser.new_context(service_workers="block", viewport={"width": 1440, "height": 1050})
                other.on("requestfailed", lambda request: failures.append({"url": request.url, "error": request.failure}))
                other.on("weberror", lambda error: failures.append({"page_error": str(error.error)}))
                p = other.new_page()
                try:
                    p.goto(url)
                    expect(p.get_by_placeholder("아이디", exact=True)).to_be_visible()
                    post(p, "login", {"username": username, "password": PW})
                    post(p, "password", {"old": PW, "password": PW + "-new"})
                    refresh_boot(p)
                    b = home(p, url, closed_date if role == "LEAD" else today)
                    forbidden = b.locator('a[href^="#/settings"],a[href^="#/dispatch"],a[href^="#/send"]')
                    if role in ("LEAD", "FIELD"):
                        expect(forbidden).to_have_count(0)
                    if role == "LEAD":
                        expect(b.locator('a[href^="#/results"]')).to_have_count(0)
                        expect(b.locator(".workflow-primary")).to_have_attribute("href", "#/close?date=" + closed_date)
                        b.locator(".workflow-primary").click()
                        expect(p.get_by_text(closed_date + " 접수 0건", exact=True)).to_be_visible()
                    if role == "FIELD":
                        expect(b.locator(".workflow-primary")).to_have_attribute("href", "#/results")
                        b.locator(".workflow-primary").click()
                        expect(p.get_by_role("heading", name="결과 가져오기", exact=True)).to_be_visible()
                    if role == "INTAKE":
                        expect(b.locator(".workflow-primary")).to_have_attribute("href", "#/send?date=" + today)
                        expect(b.locator('a[href^="#/settings"]')).to_have_count(0)
                        expect(b.locator('[data-step="send"]')).to_contain_text("운영 관리자에게")
                        b.locator(".workflow-primary").click()
                        expect(p.locator("#send-date")).to_have_value(today)
                    ok(True, role + " 주 동작과 단계 링크가 실제 역할 권한을 지킴")
                except BaseException:
                    failure_evidence(p, url, failures)
                    raise
                finally:
                    other.close()
        except BaseException:
            failure_evidence(page, url, failures)
            raise
        finally:
            ctx.close()
    print("  화면 캡처: " + str(SHOTS), flush=True)


def main():
    expect.set_options(timeout=15000)
    with sync_playwright() as pw:
        chrome = os.environ.get("CHROME") or pw.chromium.executable_path
        browser = pw.chromium.launch(executable_path=chrome, headless=True)
        try:
            run_suite(browser)
        finally:
            browser.close()
    print(f"\nPC 업무 이어하기 통과 {passed}개", flush=True)


if __name__ == "__main__":
    main()
