"""외형 저장의 계정 전환·늦은 401·여러 선택 입력 순서 회귀."""
import argparse
import json
import os

from playwright.sync_api import expect, sync_playwright
from test_review_ui import pc_server
from test_dong_order_ui import api
from test_pc_theme import KEY, appearance, login, logout

HOLD = """mode => {
  const original = window.fetch;
  window.__appearanceCalls = 0;
  window.__appearanceCommitted = false;
  window.fetch = async (path, options) => {
    if (String(path).endsWith('/api/prefs') && options?.method === 'POST') {
      window.__appearanceCalls++;
      if (window.__appearanceCalls === 1) {
        const response = mode === 'before' ? null : mode === '401' ? new Response(JSON.stringify({login:true, error:'가상 세션 만료'}), {
          status:401, headers:{'Content-Type':'application/json'}
        }) : await original(path, options);
        window.__appearanceCommitted = true;
        return new Promise(resolve => { window.__releaseAppearance = () => mode === 'before' ? original(path, options).then(resolve) : resolve(response); });
      }
    }
    if (String(path).endsWith('/api/bootstrap') && window.__holdAppearanceBootstrap) {
      window.__holdAppearanceBootstrap = false;
      const response = await original(path, options);
      window.__appearanceBootstrapReady = true;
      return new Promise(resolve => { window.__releaseAppearanceBootstrap = () => resolve(response); });
    }
    if (String(path).endsWith('/api/login') && window.__holdAppearanceLogin) {
      window.__holdAppearanceLogin = false;
      const response = await original(path, options);
      window.__appearanceLoginReady = true;
      return new Promise(resolve => { window.__releaseAppearanceLogin = () => resolve(response); });
    }
    return original(path, options);
  };
}"""


def scenario(browser, case):
    with pc_server() as url:
        context = browser.new_context(service_workers="block", color_scheme="light")
        page = context.new_page()
        page.set_default_timeout(7000)
        try:
            api(context, url, "setup", {"username": "race_admin", "display_name": "가상 외형관리", "password": "race-admin-test-123"})
            api(context, url, "users", {"username": "race_worker", "display_name": "가상 외형접수", "role": "INTAKE", "password": "race-worker-test-123"})
            page.goto(url)
            theme = page.get_by_role("banner").get_by_label("화면 테마", exact=True)
            expect(theme).to_be_visible()
            mode = "before" if case in ("refresh", "bootstrap") else "401" if case in ("late401", "reauth") else "200"
            page.evaluate(HOLD, mode)
            theme.select_option("dark")
            page.wait_for_function("() => window.__appearanceCommitted")
            if case == "queue":
                page.get_by_role("banner").get_by_label("글자 크기", exact=True).select_option("17")
                assert page.evaluate("window.__appearanceCalls") == 1, "이전 응답이 끝나기 전에 다음 외형 저장을 보내 역순 반영 가능"
                page.evaluate("async () => { window.__releaseAppearance(); await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))); }")
                expect(page.locator('html')).to_have_attribute("data-theme", "dark")
                page.wait_for_function("() => document.documentElement.style.getPropertyValue('--fs') === '17px'")
                page.wait_for_function("async () => (await import('/core.js')).pendingActions.size === 0")
                assert appearance(page)["theme"] == "dark" and appearance(page)["font"] == "17px", appearance(page)
                assert api(context, url, "bootstrap")["me"]["prefs"] == {"theme": "dark", "font": 17}
                assert page.evaluate("key => JSON.parse(localStorage.getItem(key))", KEY) == {"theme": "dark", "font": 17}
                print("  OK 테마/글자 크기 연속 변경을 순서대로 저장하고 DB·화면·캐시 일치", flush=True)
            elif case == "loginbody":
                page.get_by_role("banner").get_by_label("글자 크기", exact=True).select_option("17")
                api(context, url, "logout", {})
                page.evaluate("async () => { const C = await import('/core.js'); C.setBoot(null); await C.render(true); }")
                expect(page.get_by_role("button", name="로그인", exact=True)).to_be_visible()
                page.evaluate("window.__holdAppearanceLogin = true")
                page.get_by_placeholder("아이디", exact=True).fill("race_worker")
                page.get_by_placeholder("비밀번호", exact=True).fill("race-worker-test-123")
                page.get_by_role("button", name="로그인", exact=True).click()
                page.wait_for_function("() => window.__appearanceLoginReady")
                page.evaluate("async () => { window.__releaseAppearance(); await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))); }")
                assert page.evaluate("window.__appearanceCalls") == 1, "로그인 응답 본문 대기 중 이전 계정의 대기 저장이 새 쿠키로 전송됨"
                assert api(context, url, "bootstrap")["me"]["prefs"] == {"theme": "auto", "font": 15}
                page.evaluate("window.__releaseAppearanceLogin()")
                expect(theme).to_be_visible()
                expect(theme).to_have_value("auto")
                print("  OK 새 로그인 쿠키 적용/본문 대기 경계에서도 이전 계정 대기 저장 차단", flush=True)
            elif case == "refresh":
                page.evaluate("async () => { const C = await import('/core.js'); C.setBoot(null); C.setBoot(await C.api('bootstrap')); await C.render(true); }")
                theme.select_option("light")
                assert page.evaluate("window.__appearanceCalls") == 1, "같은 계정 bootstrap 뒤 저장 큐가 분리됨"
                page.evaluate("window.__releaseAppearance()")
                expect(page.locator('html')).to_have_attribute("data-theme", "light")
                page.wait_for_function("async () => (await import('/core.js')).pendingActions.size === 0")
                assert api(context, url, "bootstrap")["me"]["prefs"] == {"theme": "light", "font": 15}
                assert page.evaluate("key => JSON.parse(localStorage.getItem(key))", KEY) == {"theme": "light", "font": 15}
                print("  OK 같은 계정 bootstrap 갱신 전후에도 외형 저장 순서 유지", flush=True)
            elif case == "bootstrap":
                page.evaluate("() => { window.__holdAppearanceBootstrap = true; window.__appearanceRefresh = (async () => { const C = await import('/core.js'); C.setBoot(null); C.setBoot(await C.api('bootstrap')); await C.render(true); })(); }")
                page.wait_for_function("() => window.__appearanceBootstrapReady")
                page.evaluate("window.__releaseAppearance()")
                expect(page.locator('html')).to_have_attribute("data-theme", "dark")
                page.evaluate("async () => { window.__releaseAppearanceBootstrap(); await window.__appearanceRefresh; }")
                assert appearance(page)["theme"] == "dark", "오래된 bootstrap 외형이 이후 저장 결과를 덮음"
                assert page.evaluate("async () => (await import('/core.js')).boot.me.prefs.theme") == "dark"
                assert api(context, url, "bootstrap")["me"]["prefs"] == {"theme": "dark", "font": 15}
                assert page.evaluate("key => JSON.parse(localStorage.getItem(key))", KEY) == {"theme": "dark", "font": 15}
                print("  OK 데이터 재조회 대기 중 저장한 외형을 늦은 bootstrap이 덮지 않음", flush=True)
            elif case == "reauth":
                page.evaluate("window.__releaseAppearance()")
                dialog = page.get_by_role("dialog", name="다시 로그인", exact=True)
                expect(dialog).to_be_visible()
                dialog.get_by_label("비밀번호", exact=True).fill("race-admin-test-123")
                dialog.get_by_role("button", name="로그인하고 계속", exact=True).click()
                page.wait_for_function("async () => (await import('/core.js')).pendingActions.size === 0")
                expect(page.locator('html')).to_have_attribute("data-theme", "dark")
                assert api(context, url, "bootstrap")["me"]["prefs"] == {"theme": "dark", "font": 15}
                assert page.evaluate("window.__appearanceCalls") == 2
                print("  OK 같은 계정의 정상 재로그인 뒤 외형 저장은 한 번 재전송해 계속 진행", flush=True)
            else:
                logout(page)
                login(page, "race_worker", "race-worker-test-123")
                before = appearance(page)
                page.evaluate("async () => { window.__releaseAppearance(); await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))); }")
                page.wait_for_function("async () => (await import('/core.js')).pendingActions.size === 0")
                assert appearance(page) == before, "이전 계정의 응답이 현재 외형을 덮음"
                assert api(context, url, "bootstrap")["me"]["prefs"] == {"theme": "auto", "font": 15}
                assert page.evaluate("key => JSON.parse(localStorage.getItem(key))", KEY) == {"theme": "auto", "font": 15}
                expect(page.get_by_role("dialog")).to_have_count(0)
                print(f"  OK {case}: 이전 계정 응답/재인증·자동 재전송이 현재 계정을 바꾸지 않음", flush=True)
        finally:
            context.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=("all", "late200", "late401", "queue", "reauth", "refresh", "bootstrap", "loginbody"), default="all")
    args = parser.parse_args()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path, headless=True)
        try:
            for case in (("late200", "late401", "queue", "reauth", "refresh", "bootstrap", "loginbody") if args.case == "all" else (args.case,)):
                scenario(browser, case)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
