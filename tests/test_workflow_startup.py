"""PC 첫 모듈 로딩 실패 안내와 사용자가 선택한 다시 불러오기 회귀."""
import os
from pathlib import Path
import tempfile
from playwright.sync_api import expect, sync_playwright
from test_review_ui import pc_server


def main():
    with pc_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path, headless=True)
        context = browser.new_context(service_workers="block", viewport={"width": 980, "height": 680})
        try:
            page = context.new_page()
            context.route("**/screens/login.js", lambda route: route.abort("connectionrefused"))
            page.goto(url)
            expect(page.get_by_role("alert")).to_contain_text("PC 업무 화면을 불러오지 못했습니다")
            expect(page.get_by_role("button", name="화면 다시 불러오기", exact=True)).to_be_enabled()
            shots = Path(os.environ.get("DG_TEST_DIR") or tempfile.mkdtemp(prefix="dg-startup-shots-")) / "shots"
            shots.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(shots / "pc-startup-recovery.png"), full_page=True)
            assert context.request.get(url + "api/state", headers={"X-DG-CSRF": "1"}).json() == {
                "setup_needed": True, "logged_in": False}
            context.unroute("**/screens/login.js")
            page.get_by_role("button", name="화면 다시 불러오기", exact=True).click()
            expect(page.get_by_placeholder("아이디", exact=True)).to_be_visible()
            expect(page.get_by_role("alert")).to_have_count(0)
            print("PC 초기 모듈 오류 안내·수동 재시도·초기 계정 보존 3개 통과")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
