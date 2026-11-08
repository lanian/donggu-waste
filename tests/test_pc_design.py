"""PC 디자인의 실제 임시 서버·브라우저 레이아웃 및 업무 동작 점검.

실행: python tests/test_pc_design.py [--baseline] [--label before|after]
사진·측정값은 DG_DESIGN_DIR 또는 시스템 임시 폴더의 dg-design-*/shots에 둔다.
--baseline은 현재 레이아웃 문제를 보고서에 남기고 동작 회귀만 엄격히 검사한다.
외부 지도 양식만 테스트 context에서 정적인 배경으로 대체하며 제품 파일은 바꾸지 않는다.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import urllib.parse
import urllib.request

from playwright.sync_api import expect, sync_playwright

from test_review_ui import pc_server


PASSWORD = "design-fixture-password-123"
VIEWPORTS = ((1440, 900), (1280, 800), (980, 680), (390, 844))
VARIANTS = (("light", 15), ("dark", 15), ("light", 17))
SCREENS = {
    "home": ("#/home", "오늘 현황", ".workflow-primary"),
    "requests": ("#/requests", "접수", '.requests-create'),
    "newrequest": ("#/request/new", "전화 접수 등록", '[data-save="1"]'),
}


def post(context, url, path, body):
    response = context.request.post(url + "api/" + path, headers={"X-DG-CSRF": "1"}, data=body)
    assert response.ok, f"fixture {path}: {response.status} {response.text()}"
    return response.json()


def refresh_boot(page):
    # Hash 이동은 문서를 새로 읽지 않는다. API fixture의 설정을 기존 앱 경로로 다시 읽힌다.
    page.evaluate("""async () => {
        const C = await import('/core.js');
        C.setBoot(await C.api('bootstrap'));
        await C.render(true);
    }""")


def attach_evidence(context, errors, report, url, current_case):
    outstanding = set()
    context.on("request", lambda request: outstanding.add(request.url))
    context.on("requestfinished", lambda request: outstanding.discard(request.url))
    def failed(request):
        entry = {"type": "network", "url": request.url, "message": request.failure,
                 "case": current_case.get("name"), "outstanding": sorted(outstanding)}
        outstanding.discard(request.url)
        # 다른 화면으로 이동하며 아직 진행 중인 외부 지도 양식 읽기를 취소한 경우는 별도 기록한다.
        if request.url.startswith("https://tiles.openfreemap.org/") and request.failure == "net::ERR_ABORTED":
            report.setdefault("navigation_cancellations", []).append(entry)
            return
        if request.url.startswith(url):
            probe = urllib.request.Request(url + "api/state", headers={"X-DG-CSRF": "1"})
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(probe, timeout=3) as response:
                    entry["http_probe"] = {"status": response.status, "body": response.read().decode("utf-8")}
            except Exception as error:
                entry["http_probe"] = repr(error)
            port = urllib.parse.urlsplit(url).port
            with socket.socket() as sk:
                sk.settimeout(2)
                entry["tcp_connect"] = sk.connect_ex(("127.0.0.1", port))
            if os.name == "nt":
                command = (f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue | "
                           "Select-Object LocalAddress,LocalPort,State,OwningProcess | ConvertTo-Json -Compress")
                result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                        capture_output=True, text=True, encoding="utf-8", timeout=10)
                entry["tcp_listener"] = {"exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        errors.append(entry)
    context.on("weberror", lambda error: errors.append({"type": "pageerror", "message": str(error.error)}))
    context.on("console", lambda msg: errors.append({"type": "console", "message": msg.text}) if msg.type == "error" else None)
    context.on("requestfailed", failed)
    # 실제 MapLibre 컨트롤·레이아웃을 유지하되 외부 네트워크 성공 여부와 사진을 분리한다.
    context.route("https://tiles.openfreemap.org/**", lambda route: route.fulfill(json={
        "version": 8, "sources": {}, "layers": [{"id": "fixture-background", "type": "background",
                                                   "paint": {"background-color": "#dde6e8"}}]}))


def layout_snapshot(page, target):
    return page.evaluate("""selector => {
        const primary = document.querySelector(selector);
        const rect = primary?.getBoundingClientRect();
        const vw = innerWidth, vh = innerHeight;
        const visible = rect && rect.width > 0 && rect.height > 0;
        const center = rect && document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
        const contained = visible && rect.left >= -1 && rect.right <= vw + 1 && rect.top >= -1 && rect.bottom <= vh + 1;
        return { width: vw, height: vh, bodyWidth: document.body.scrollWidth,
            documentWidth: document.documentElement.scrollWidth,
            effectiveFont: getComputedStyle(document.documentElement).fontSize,
            effectiveTheme: document.documentElement.dataset.theme,
            primaryText: primary?.textContent.trim(), primaryRect: rect?.toJSON(),
            primaryInViewport: !!contained, primaryUncovered: !!(center && (primary === center || primary.contains(center))),
            calendarClips: [...document.querySelectorAll('.calday')].filter(el => el.scrollWidth > el.clientWidth + 1).length,
            filterControlClips: [...document.querySelectorAll('.cond-v input, .cond-v select')].filter(el => {
                const field = el.getBoundingClientRect(), cell = el.closest('.cond-v').getBoundingClientRect();
                return field.left < cell.left - 1 || field.right > cell.right + 1;
            }).map(el => el.id),
            overflowElements: [...document.body.querySelectorAll('*')].filter(el => {
                const s = getComputedStyle(el), r = el.getBoundingClientRect();
                if (s.position === 'absolute' || s.position === 'fixed' || s.visibility === 'hidden') return false;
                if (el.closest('.tablecard, .results, .mapbox, nav.side')) return false;
                return r.width > 0 && (r.right > vw + 1 || r.left < -1);
            }).slice(0, 10).map(el => ({ tag: el.tagName, class: el.className, text: el.textContent.slice(0, 70) })) };
    }""", target)


def keyboard_snapshot(page, target):
    action = page.locator(target).first
    action.focus()
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    return action.evaluate("""el => {
        const s = getComputedStyle(el), r = el.getBoundingClientRect();
        const at = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
        return { focused: document.activeElement === el, focusVisible: el.matches(':focus-visible'),
            indicator: (s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0) || s.boxShadow !== 'none',
            reachable: !!at && (at === el || el.contains(at)) };
    }""")


def visit(page, url, screen):
    route, heading, _ = SCREENS[screen]
    page.goto(url + route)
    expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
    if screen == "requests":
        expect(page.locator("tbody tr.click")).to_have_count(2)
    if screen == "newrequest":
        expect(page.locator(".calday").first).to_be_visible()
    page.evaluate("document.fonts.ready")


def seed(context, url):
    post(context, url, "setup", {"username": "designadmin", "display_name": "가상 디자인담당", "password": PASSWORD})
    post(context, url, "dongs", {"name": "가상동", "code": "QA", "active": True})
    post(context, url, "crews", {"name": "가상 수거팀"})
    today = context.request.get(url + "api/bootstrap", headers={"X-DG-CSRF": "1"}).json()["today"]
    for i in range(2):
        post(context, url, "requests", {"name": f"가상 주민{i + 1}", "phone": f"0100000200{i}",
             "address": f"가상구 예시로 {i + 1}", "dong": "가상동", "planned_date": today,
             "lat": 35.1462 + i * 0.001, "lon": 126.9233,
             "items": [{"name": "가상 의자", "qty": i + 1, "unit": "EA", "unit_label": "개"}]})


def behavior_regressions(page, url):
    # 목록 조회·선택은 업무 이동 후에도 유지되어야 한다.
    visit(page, url, "requests")
    page.locator("#rq-q").fill("2000")
    page.locator("#rq-q").press("Enter")
    expect(page.locator("tbody tr.click")).to_have_count(1)
    page.get_by_label("이 쪽 모두 고르기", exact=True).check()
    expect(page.get_by_text("1건 선택", exact=True)).to_be_visible()
    page.evaluate("location.hash = '/request/new'")
    expect(page.get_by_role("heading", name="전화 접수 등록", exact=True)).to_be_visible()
    page.get_by_role("button", name="← 목록", exact=True).click()
    expect(page.locator("#rq-q")).to_have_value("2000")
    expect(page.get_by_text("1건 선택", exact=True)).to_be_visible()
    page.get_by_role("button", name="선택 해제", exact=True).click()
    expect(page.get_by_text("1건 선택", exact=True)).to_have_count(0)
    # 새 접수 초안은 브라우저 저장소 없이 같은 앱의 다른 업무 이동에서 보존한다.
    page.evaluate("location.hash = '/request/new'")
    expect(page.get_by_role("heading", name="전화 접수 등록", exact=True)).to_be_visible()
    page.get_by_label("이름", exact=True).fill("가상 작성중")
    page.get_by_label("배출 주소", exact=True).fill("가상구 초안로 99")
    page.get_by_role("button", name="← 목록", exact=True).click()
    page.evaluate("location.hash = '/request/new'")
    expect(page.get_by_label("이름", exact=True)).to_have_value("가상 작성중")
    expect(page.get_by_label("배출 주소", exact=True)).to_have_value("가상구 초안로 99")
    page.get_by_role("button", name="비우고 새로 쓰기", exact=True).click()
    expect(page.get_by_label("이름", exact=True)).to_have_value("")
    return ["접수 목록 검색·선택 유지", "선택 해제", "신규 접수 초안 보존", "명시적 새로 쓰기"]


def run(browser, output, label, baseline, quick=False, behaviors_only=False):
    shots = output / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    rows, errors, problems = [], [], []
    report = {"label": label, "evidence": "실제 임시 SQLCipher 서버·가상 계정 / 외부 지도 양식만 정적 fixture",
              "screens": rows, "errors": errors, "problems": problems}
    with pc_server() as url:
        context = browser.new_context(service_workers="block", color_scheme="light")
        current_case = {}
        attach_evidence(context, errors, report, url, current_case)
        login_context = browser.new_context(service_workers="block", color_scheme="light")
        attach_evidence(login_context, errors, report, url, current_case)
        page, login = context.new_page(), login_context.new_page()
        try:
            seed(context, url)
            for theme, font in (() if behaviors_only else VARIANTS):
                post(context, url, "prefs", {"theme": theme, "font": font})
                if page.url.startswith(url):
                    refresh_boot(page)
                for width, height in VIEWPORTS:
                    if quick and ((theme, font) != ("light", 15) and width != 1440 or width == 1280):
                        continue
                    size = {"width": width, "height": height}
                    page.set_viewport_size(size)
                    login.set_viewport_size(size)
                    login.emulate_media(color_scheme=theme)
                    login.goto(url)
                    expect(login.get_by_role("button", name="로그인", exact=True)).to_be_visible()
                    login.evaluate("async p => (await import('/core.js')).applyPrefs(p)", {"theme": theme, "font": font})
                    for screen in ("login", *SCREENS):
                        if quick and (theme, font) != ("light", 15) and screen != "home":
                            continue
                        current = login if screen == "login" else page
                        target = ".login button.primary" if screen == "login" else SCREENS[screen][2]
                        name = f"{label}-{screen}-{width}x{height}-{theme}-font{font}"
                        current_case["name"] = name
                        if screen != "login":
                            visit(current, url, screen)
                        current.evaluate("scrollTo(0, 0)")
                        metrics = layout_snapshot(current, target)
                        current.screenshot(path=str(shots / (name + ".png")), full_page=True)
                        metrics["keyboard"] = keyboard_snapshot(current, target)
                        metrics.update(screen=screen, theme=theme, font=font, screenshot=name + ".png")
                        rows.append(metrics)
                        print("  화면: " + name, flush=True)
                        if metrics["effectiveFont"] != f"{font}px" or metrics["effectiveTheme"] != theme:
                            problems.append(name + ": 요청한 글자 크기·테마와 실제 화면 설정이 다름")
                        if metrics["documentWidth"] > width + 1 or metrics["bodyWidth"] > width + 1:
                            problems.append(name + ": 본문 수평 넘침")
                        if not metrics["primaryInViewport"]:
                            problems.append(name + ": 주 동작이 첫 화면 밖에 있음")
                        elif not metrics["primaryUncovered"]:
                            problems.append(name + ": 주 동작이 다른 요소에 가려짐")
                        if metrics["calendarClips"]:
                            problems.append(name + ": 예약 날짜 텍스트 잘림")
                        if metrics["filterControlClips"]:
                            problems.append(name + ": 조회조건 입력이 셀 밖으로 넘침 " + ", ".join(metrics["filterControlClips"]))
                        if not all(metrics["keyboard"].values()):
                            problems.append(name + ": 키보드 주 동작 초점/가림 확인 필요")
            page.set_viewport_size({"width": 1440, "height": 900})
            post(context, url, "prefs", {"theme": "light", "font": 15})
            if page.url.startswith(url):
                refresh_boot(page)
            current_case["name"] = "behavior-regressions"
            report["behaviors"] = behavior_regressions(page, url)
            assert not errors, f"브라우저 오류: {errors}"
        finally:
            (output / f"{label}-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            login_context.close()
            context.close()
    print(f"화면 {len(rows)}개 / 레이아웃 관찰 {len(problems)}개 / 브라우저 오류 {len(errors)}개", flush=True)
    print("사진: " + str(shots), flush=True)
    print("보고서: " + str(output / f"{label}-report.json"), flush=True)
    if not baseline:
        assert not problems, "\n".join(problems)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--label", default="after")
    parser.add_argument("--quick", action="store_true", help="대표 14화면만 캡처")
    parser.add_argument("--behaviors-only", action="store_true", help="사진 없이 실제 업무 동작만 검사")
    args = parser.parse_args()
    output = Path(os.environ.get("DG_DESIGN_DIR") or tempfile.mkdtemp(prefix="dg-design-"))
    expect.set_options(timeout=15000)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path, headless=True)
        try:
            run(browser, output, args.label, args.baseline, args.quick, args.behaviors_only)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
