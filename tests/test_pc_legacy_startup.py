"""옛 정적 파일 허용 목록을 유지한 서버에서도 새 PC 진입점이 열린다."""
import os

from playwright.sync_api import expect, sync_playwright
from test_review_ui import pc_server


def main():
    with pc_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path, headless=True)
        context = browser.new_context(service_workers="block")
        blocked = []
        def legacy_missing_entry(route):
            blocked.append(route.request.url)
            route.fulfill(status=404, content_type="text/plain", body="not found")
        context.route("**/start.js", legacy_missing_entry)
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded")
            expect(page.get_by_placeholder("아이디", exact=True)).to_be_visible(timeout=4000)
            assert not blocked, "새 파일 start.js에 의존해 기존 서버의 허용 목록과 충돌함"
            response = context.request.post(url + "api/setup", headers={"X-DG-CSRF": "1"}, data={
                "username": "legacy_admin", "display_name": "가상 호환검증", "password": "legacy-test-pass-123"})
            assert response.ok, response.text()
            page.reload(wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="오늘 현황", exact=True)).to_be_visible(timeout=15000)
            assert not blocked
            print("기존 서버 허용 목록·동적 core의 로그인 후 상태 반영 통과")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
