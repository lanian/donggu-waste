"""업무 이어하기: 기준일 이동과 기존 배정 초안 보존 회귀."""
from __future__ import annotations

import os
from playwright.sync_api import expect, sync_playwright
from test_review_ui import pc_server


def main():
    with pc_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), headless=True)
        context = browser.new_context()
        try:
            response = context.request.post(url + "api/setup", headers={"X-DG-CSRF": "1"}, data={
                "username": "workflow_admin", "display_name": "가상관리자", "password": "workflow-pass-123"})
            assert response.ok, response.text()
            page = context.new_page()
            day = "2026-11-12"
            page.goto(url + "#/dispatch?date=" + day)
            expect(page.locator("#dp-date")).to_have_value(day)
            page.evaluate("""async () => {
                const { screenDispatch: D } = await import('/screens/dispatch.js');
                D.s.plan = { date: '2026-11-12', res: { requests: {}, matrix_source: 'ESTIMATE' }, routes: [], unassigned: [] };
                D.s.edit = { date: '2026-11-12', reason: '가상 편집 초안' };
                D.s.crews.add('draft-crew');
                window.__workflowDraft = { plan: D.s.plan, edit: D.s.edit, crews: D.s.crews, seen: D.s.seen };
            }""")
            page.evaluate("location.hash = '/dispatch?date=2026-11-13'")
            expect(page.locator("#dp-date")).to_have_value("2026-11-13")
            assert page.evaluate("""async () => {
                const { screenDispatch: D } = await import('/screens/dispatch.js'), s = window.__workflowDraft;
                return D.s.plan === s.plan && D.s.edit === s.edit && D.s.crews === s.crews && D.s.seen === s.seen;
            }"""), "날짜 이동이 기존 계산·편집·팀 선택 초안을 삭제함"
            page.evaluate("location.hash = '/send?date=2026-11-12'")
            expect(page.get_by_label("수거일")).to_have_value(day)
            page.evaluate("location.hash = '/close?date=2026-11-12'")
            expect(page.get_by_label("수거일", exact=True)).to_have_value(day)
            # 배정 API는 이미 별도 로직 시험이 있으므로 응답만 대체하고 확정 후 이동을 확인한다.
            assigned = []
            def assign_reply(route):
                assigned.append(route.request.post_data_json)
                route.fulfill(json=[])
            page.route("**/api/assign", assign_reply)
            page.evaluate("""async () => {
                const { screenDispatch: D } = await import('/screens/dispatch.js');
                const { screenSend: S } = await import('/screens/send.js');
                S.s = { date: '2026-11-13' };
                D.s.edit = null;
                location.hash = '/dispatch?date=2026-11-12';
            }""")
            page.get_by_role("button", name="배정 확정", exact=True).click()
            expect(page.get_by_role("heading", name="목록 전송 (QR)", exact=True)).to_be_visible()
            expect(page.get_by_label("수거일", exact=True)).to_have_value(day)
            assert assigned and assigned[0]["date"] == day
            created = context.request.post(url + "api/users", headers={"X-DG-CSRF": "1"}, data={
                "username": "first_date", "display_name": "가상 첫 사용자", "password": "first-date-pass-123", "role": "INTAKE"})
            assert created.ok, created.text()
            first = browser.new_context()
            try:
                logged = first.request.post(url + "api/login", headers={"X-DG-CSRF": "1"}, data={
                    "username": "first_date", "password": "first-date-pass-123"})
                assert logged.ok, logged.text()
                p = first.new_page()
                p.goto(url + "#/dispatch?date=" + day)
                expect(p.get_by_role("heading", name="내 계정", exact=True)).to_be_visible()
                changed = first.request.post(url + "api/password", headers={"X-DG-CSRF": "1"}, data={
                    "old": "first-date-pass-123", "password": "first-date-new-pass-123"})
                assert changed.ok, changed.text()
                await_render = "async () => { const C = await import('/core.js'); C.setBoot(await C.api('bootstrap')); await C.render(true); }"
                p.evaluate(await_render)
                expect(p.locator("#dp-date")).to_have_value(day)
            finally:
                first.close()
            print("업무 기준일·초안·확정 후 이동·첫 비밀번호 변경 뒤 날짜 유지 6개 통과")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
