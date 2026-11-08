"""첫 화면 파일이 응답하지 않을 때 실제 단계·경과·수동 복구를 표시한다."""
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_review_ui import pc_server


def main():
    with pc_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path, headless=True)
        context = browser.new_context(service_workers="block", viewport={"width": 980, "height": 680})
        page = context.new_page()
        held = []
        try:
            page.clock.install()
            context.route("**/core.js", lambda route: held.append(route))
            page.goto(url, wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="업무 화면을 준비하고 있습니다", exact=True)).to_be_visible()
            expect(page.locator("#startup-detail")).to_contain_text("화면 파일")
            page.clock.fast_forward(16000)
            expect(page.get_by_role("alert")).to_contain_text("지연")
            expect(page.locator("#startup-elapsed")).to_contain_text("16초")
            expect(page.get_by_role("button", name="화면 다시 불러오기", exact=True)).to_be_enabled()
            shots = Path(os.environ.get("DG_TEST_DIR") or tempfile.mkdtemp(prefix="dg-startup-ui-")) / "shots"
            shots.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(shots / "pc-startup-delayed.png"), full_page=True)
            for route in held:
                route.abort()
            held.clear()
            context.unroute("**/core.js")
            page.get_by_role("button", name="화면 다시 불러오기", exact=True).click()
            expect(page.get_by_placeholder("아이디", exact=True)).to_be_visible()
            expect(page.get_by_role("alert")).to_have_count(0)
            print("브라우저 시작 단계·실제 경과·지연 안내·사용자 재시도 통과")
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
