"""방문하지 않은 화면의 연결 지연이 PC 첫 화면을 막지 않는 회귀."""
import os
import time

from playwright.sync_api import expect, sync_playwright
from test_review_ui import pc_server


def main():
    with pc_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path, headless=True)
        context = browser.new_context(service_workers="block")
        page = context.new_page()
        held, requests, failures = [], [], []
        page.on("request", lambda request: requests.append(request.url))
        page.on("requestfailed", lambda request: failures.append((request.url, request.failure)))
        context.route("**/screens/stats.js", lambda route: held.append(route))
        try:
            page.clock.install()
            started = time.monotonic()
            page.goto(url, wait_until="domcontentloaded")
            expect(page.get_by_placeholder("아이디", exact=True)).to_be_visible(timeout=4000)
            login_seconds = time.monotonic() - started
            login_modules = {value for value in requests if value.endswith('.js') and not value.endswith('/start.js')}
            assert not held, "로그인 전에 통계 모듈을 요청해 첫 화면을 함께 기다림"
            response = context.request.post(url + "api/setup", headers={"X-DG-CSRF": "1"}, data={
                "username": "lazy_admin", "display_name": "가상 시작검증", "password": "lazy-start-pass-123"})
            assert response.ok, response.text()
            page.reload(wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="오늘 현황", exact=True)).to_be_visible(timeout=15000)
            assert not held, "오늘 현황이 방문하지 않은 통계 모듈을 기다림"
            page.evaluate("location.hash = '#/requests?quick=NODATE'")
            expect(page.locator(".qchip.on")).to_contain_text("예정일 없음", timeout=4000)
            expect(page.get_by_text("불러오는 중…", exact=True)).to_have_count(0)
            with page.expect_request("**/screens/stats.js"):
                page.get_by_role("link", name="통계·보고서", exact=True).click()
            page.clock.fast_forward(10050)
            expect(page.get_by_role("alert")).to_contain_text("화면 파일")
            for route in held:
                route.continue_()
            held.clear()
            context.unroute("**/screens/stats.js")
            page.get_by_role("button", name="다시 시도", exact=True).click()
            expect(page.get_by_role("heading", name="통계·보고서", exact=True)).to_be_visible()
            page.evaluate("location.hash = '#/requests?quick=NODATE'")
            expect(page.locator(".qchip.on")).to_contain_text("예정일 없음", timeout=4000)
            expect(page.get_by_text("불러오는 중…", exact=True)).to_have_count(0)
            print(f"필요 화면만 로드·비방문 모듈 지연 격리·실제 통계 이동 통과 (로그인 {login_seconds:.3f}초, 모듈 {len(login_modules)}개)")
        except Exception:
            print(f"시작 진단: 통계 보류 {len(held)}개 / 요청 {len(requests)}개 / 실패 {failures}")
            raise
        finally:
            for route in held:
                try:
                    route.abort()
                except Exception:
                    pass
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
