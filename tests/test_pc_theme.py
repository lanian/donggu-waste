"""실제 PC 로그인 전후 테마·글자 크기와 브라우저 외형 기억 회귀."""
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_review_ui import pc_server
from test_dong_order_ui import api

KEY = "dg.pc.appearance.v1"
passed = 0


def ok(value, label):
    global passed
    assert value, label
    passed += 1
    print("  OK", label, flush=True)


def appearance(page):
    return page.evaluate("() => ({theme: document.documentElement.dataset.theme || 'auto', font: document.documentElement.style.getPropertyValue('--fs'), scheme: getComputedStyle(document.documentElement).colorScheme})")


def login(page, username, password):
    page.get_by_placeholder("아이디", exact=True).fill(username)
    page.get_by_placeholder("비밀번호", exact=True).fill(password)
    page.get_by_role("button", name="로그인", exact=True).click()
    expect(page.get_by_role("banner").get_by_label("화면 테마", exact=True)).to_be_visible()


def logout(page):
    page.get_by_role("link", name="로그아웃", exact=True).click()
    expect(page.get_by_role("button", name="로그인", exact=True)).to_be_visible()


def check_login_focus(browser, url):
    context = browser.new_context(service_workers="block")
    context.add_init_script("""{
      const original = window.setTimeout;
      window.__loginFocusCallbacks = [];
      window.setTimeout = (callback, delay, ...args) => {
        if (delay === 30 && typeof callback === 'function') {
          window.__loginFocusCallbacks.push(() => callback(...args)); return 0;
        }
        return original(callback, delay, ...args);
      };
    }""")
    page = context.new_page()
    try:
        page.goto(url)
        password = page.get_by_placeholder("비밀번호", exact=True)
        expect(password).to_be_visible()
        assert page.evaluate("window.__loginFocusCallbacks.length") > 0
        password.focus()
        page.evaluate("() => window.__loginFocusCallbacks.forEach(callback => callback())")
        expect(password).to_be_focused()
        ok(True, "로그인 초기 초점 예약이 이미 입력 중인 비밀번호 칸의 초점을 빼앗지 않음")
    finally:
        context.close()


def main():
    shots = Path(os.environ.get("DG_TEST_DIR") or tempfile.mkdtemp(prefix="dg-login-appearance-")) / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    with pc_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path, headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block", color_scheme="light")
        page = context.new_page()
        try:
            api(context, url, "setup", {"username": "theme_admin", "display_name": "가상 외형관리자", "password": "theme-admin-test-123"})
            api(context, url, "users", {"username": "theme_worker", "display_name": "가상 외형접수", "role": "INTAKE", "password": "theme-worker-test-123"})
            page.goto(url)
            theme = page.get_by_role("banner").get_by_label("화면 테마", exact=True)
            expect(theme).to_be_visible()
            theme.select_option("dark")
            page.get_by_role("banner").get_by_label("글자 크기", exact=True).select_option("17")
            expect(page.locator('html')).to_have_attribute("data-theme", "dark")
            page.wait_for_function("() => document.documentElement.style.getPropertyValue('--fs') === '17px'")
            logout(page)
            before = appearance(page)
            page.reload()
            expect(page.get_by_role("button", name="로그인", exact=True)).to_be_visible()
            ok(appearance(page) == before, "로그아웃 뒤 새로고침해도 어둡게·아주 크게 유지")
            cached = page.evaluate("key => JSON.parse(localStorage.getItem(key))", KEY)
            ok(cached == {"theme": "dark", "font": 17}, "브라우저에는 외형 두 값만 저장")
            page.screenshot(path=str(shots / "login_dark_font17.png"))

            held = []
            page.route("**/core.js", lambda route: held.append(route))
            page.reload(wait_until="domcontentloaded")
            ok(appearance(page)["theme"] == "dark" and appearance(page)["font"] == "17px", "로그인 모듈을 기다리는 시작 단계에도 외형 복원")
            for route in held:
                route.continue_()
            page.unroute("**/core.js")
            expect(page.get_by_role("button", name="로그인", exact=True)).to_be_visible()
            login(page, "theme_admin", "theme-admin-test-123")
            expect(theme).to_have_value("dark")
            ok(appearance(page) == before, "같은 계정 로그인 전후 테마와 글자 크기가 같음")
            page.screenshot(path=str(shots / "signed_in_dark_font17.png"))

            theme.select_option("light")
            page.get_by_role("banner").get_by_label("글자 크기", exact=True).select_option("14")
            page.wait_for_function("() => document.documentElement.style.getPropertyValue('--fs') === '14px'")
            logout(page)
            page.emulate_media(color_scheme="dark")
            page.reload()
            expect(page.get_by_role("button", name="로그인", exact=True)).to_be_visible()
            ok(appearance(page) == {"theme": "light", "font": "14px", "scheme": "light"}, "시스템이 어두워도 명시한 밝게·작게 유지")
            page.screenshot(path=str(shots / "login_light_font14.png"))
            login(page, "theme_admin", "theme-admin-test-123")
            theme.select_option("auto")
            page.wait_for_function("() => !document.documentElement.hasAttribute('data-theme')")
            logout(page)
            page.reload()
            expect(page.get_by_role("button", name="로그인", exact=True)).to_be_visible()
            ok(appearance(page)["theme"] == "auto" and appearance(page)["scheme"] == "dark", "자동 모드는 로그인 전에도 시스템 테마를 따름")
            page.emulate_media(color_scheme="light")
            ok(appearance(page)["scheme"] == "light", "자동 모드에서 시스템 테마 변경을 반영")

            login(page, "theme_admin", "theme-admin-test-123")
            theme.select_option("dark")
            expect(page.locator('html')).to_have_attribute("data-theme", "dark")
            logout(page)
            login(page, "theme_worker", "theme-worker-test-123")
            expect(theme).to_have_value("auto")
            ok(appearance(page)["theme"] == "auto" and appearance(page)["font"] == "15px", "다른 계정은 그 계정의 서버 설정을 우선하고 기존 외형을 복사하지 않음")
            logout(page)

            invalid = [("not-json", "auto"), (json.dumps({"theme": "bad", "font": 999, "name": "저장하면안됨"}), "auto"),
                       (json.dumps({"theme": "dark", "font": {"toString": None}}), "dark"),
                       (json.dumps({"theme": "dark", "font": {"valueOf": 1, "toString": 2}}), "dark")]
            for raw, expected_theme in invalid:
                page.evaluate("([key, value]) => localStorage.setItem(key, value)", [KEY, raw])
                page.reload()
                expect(page.get_by_role("button", name="로그인", exact=True)).to_be_visible()
                ok(appearance(page)["theme"] == expected_theme and appearance(page)["font"] == "15px", "깨진/유효하지 않은 외형 값은 기본값으로 복구")

            blocked = browser.new_context(service_workers="block", color_scheme="light")
            blocked.add_init_script("Object.defineProperty(Storage.prototype, 'getItem', {value() {throw new DOMException('blocked', 'SecurityError');}}); Object.defineProperty(Storage.prototype, 'setItem', {value() {throw new DOMException('blocked', 'SecurityError');}});")
            blocked_page = blocked.new_page()
            blocked_page.goto(url)
            login(blocked_page, "theme_admin", "theme-admin-test-123")
            expect(blocked_page.locator('html')).to_have_attribute("data-theme", "dark")
            ok(True, "브라우저 저장소 차단 상태에도 로그인과 계정 테마 적용 가능")
            blocked.close()
            check_login_focus(browser, url)
            print(f"로그인 전후 외형 실제 HTTP/UI {passed}개 통과 · 캡처 {shots}", flush=True)
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
