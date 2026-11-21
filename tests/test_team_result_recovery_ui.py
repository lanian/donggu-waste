"""실제 로컬 PC 서버에서 오늘 현황과 마감 점검의 팀 상태를 확인."""
import os

from playwright.sync_api import expect, sync_playwright
from test_team_result_recovery import DAY, TeamRecoveryTest
from dgpc import server as SV, service as S


def main():
    fixture = TeamRecoveryTest()
    fixture.setUp()
    try:
        # 이 화면과 시험 자료 작성기가 같은 서버 쓰기 연결을 사용한다.
        with SV.LOCK:
            crew, phone = fixture.team("가상 현장 회수팀")
            receipts = [fixture.request()[0] for _ in range(2)]
        root = f"http://127.0.0.1:{fixture.port}/"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path, headless=True)
            ctx = browser.new_context(service_workers="block", viewport={"width": 1440, "height": 900})
            ctx.route("**/*", lambda route: route.continue_() if route.request.url.startswith(root) else route.abort())
            name, value = fixture.admin_cookie.split("=", 1)
            ctx.add_cookies([{"name": name, "value": value, "url": root}])
            page = ctx.new_page()

            def home(stage):
                page.goto(root + "#/home", wait_until="domcontentloaded")
                date = page.get_by_label("기준일", exact=True)
                date.fill(DAY)
                date.press("Tab")
                card = page.locator(".today-crews")
                expect(card.get_by_text(stage, exact=True)).to_be_visible()
                return card

            home("배정 전")
            with SV.LOCK:
                batch = fixture.plan(DAY, crew, *receipts)
            home("목록 미전송")
            with SV.LOCK:
                bundle, stops, receipt = fixture.send(DAY, batch, phone, confirmed=False)
            card = home("수신 미확인")
            expect(card.get_by_text("현장 결과 기다림", exact=True)).to_be_visible()
            with SV.LOCK:
                fixture.upload(DAY, phone, fixture.event(DAY, phone, stops[0], body={"outcome": "PARTIAL"}))
            card = home("부분 회수")
            expect(card.get_by_text("수신 미확인", exact=True)).to_be_visible()
            expect(card.get_by_text("현장 반영 1건 · 미회수 1건", exact=False)).to_be_visible()
            expect(card.get_by_text("마지막 관련 반영", exact=False)).to_contain_text("10:00")

            page.goto(root + "#/close?date=" + DAY, wait_until="domcontentloaded")
            table = page.get_by_role("region", name="팀별 결과 회수")
            expect(table.get_by_text("부분 회수", exact=True)).to_be_visible()
            expect(table.get_by_text("가상 현장 회수팀", exact=False)).to_be_visible()
            expect(table.get_by_text("수신 미확인", exact=True)).to_be_visible()
            page.get_by_role("button", name="마감 기록", exact=True).click()
            dialog = page.get_by_role("dialog", name="마감 기록")
            expect(dialog.get_by_text("미회수 1건", exact=False)).to_be_visible()
            expect(dialog.get_by_role("button", name="그래도 마감 기록")).to_be_enabled()
            dialog.get_by_role("button", name="취소", exact=True).click()

            with SV.LOCK:
                S.confirm_receipt(fixture.con, bundle["bundle_id"], receipt)
                fixture.upload(DAY, phone, fixture.event(DAY, phone, stops[1]), stamp=DAY + "T02:00:00Z")
            # PC 라우터는 기준일 쿼리를 한 번 해석한 뒤 주소에서 지운다.
            page.goto(root + "#/close?date=" + DAY, wait_until="domcontentloaded")
            expect(table.get_by_text("회수 완료", exact=True)).to_be_visible()
            expect(table.get_by_text("수신 확인 · ", exact=False)).to_be_visible()
            expect(table.get_by_text("현장 반영 2건 · 미회수 0건", exact=False)).to_be_visible()
            ctx.close()
            browser.close()
            print("실HTTP UI 통과: 배정 전 · 미전송 · 수신 미확인 · 결과 기다림 · 부분 회수 · 회수 완료 · 마감 경고")
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
