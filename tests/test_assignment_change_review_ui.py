"""실제 PC 서버에서 확정 배정의 변경 검토·충돌·변경 팀 전송 화면을 검증."""
import json
import os

from playwright.sync_api import expect, sync_playwright
from test_assignment_change_review import DAY, AssignmentChangeReviewTest
from test_pc import Phone
from dgpc import service as S


def main():
    fixture = AssignmentChangeReviewTest()
    fixture.setUp()
    try:
        a, b, _ = fixture.crews
        phone = Phone(S.registration_text(fixture.con))
        dev = S.register_device(fixture.con, phone.code(), "가상 변경팀 휴대폰", confirm=True)["device_id"]
        S.save_crew(fixture.con, {"id": a, "name": "가상 재계산 1팀", "device_id": dev, "shift_end": "18:00"})
        one = fixture.receipt("가상 변경팀 기존 접수")
        extra = fixture.receipt("가상 추가 접수")
        keep = fixture.receipt("가상 미변경팀 접수")
        fixture.plan(c0=[one], c1=[keep])
        root = f"http://127.0.0.1:{fixture.port}/"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path, headless=True)
            ctx = browser.new_context(service_workers="block")
            ctx.route("**/*", lambda route: route.continue_() if route.request.url.startswith(root) else route.abort())
            name, value = fixture.admin_cookie.split("=", 1)
            ctx.add_cookies([{"name": name, "value": value, "url": root}])
            page = ctx.new_page()
            page.goto(root + "#/dispatch?date=" + DAY, wait_until="domcontentloaded")
            page.get_by_role("button", name="현재 배정 불러오기", exact=True).click()
            expect(page.get_by_label("재계산 시작 시각 (선택)")).to_be_visible()
            expect(page.get_by_role("region", name="노선 조정안")).not_to_contain_text("null")
            expect(page.get_by_role("link", name="변경 팀 목록 전송", exact=False)).to_have_count(0)
            add = page.get_by_role("button", name="가상 재계산 1팀에 넣기", exact=True)
            team = page.locator(".edit-cols .route").filter(has=page.get_by_role("heading", name="가상 재계산 1팀", exact=False))
            dialog = page.get_by_role("dialog", name="배정 변경 검토")

            def review_add():
                page.get_by_label("가상 재계산 1팀에 넣을 접수", exact=True).select_option(extra)
                add.click()
                expect(dialog.get_by_text("이동", exact=False)).to_be_visible(timeout=20000)
                expect(dialog.get_by_text("누적 적재", exact=False).first).to_be_visible()

            review_add()
            before = fixture.current()
            expect(team).to_contain_text("갱신본 1")
            dialog.get_by_role("button", name="취소", exact=True).click()
            assert before == fixture.current()

            review_add()
            fixture.plan(c0=[one])
            dialog.get_by_role("button", name="변경 배정 확정", exact=True).click()
            expect(page.get_by_text("배정이나 계산 자료가 바뀌었습니다.", exact=False)).to_be_visible()
            page.get_by_role("button", name="다시 불러오기", exact=True).click()
            expect(team).to_contain_text("갱신본 2")
            review_add()
            dialog.get_by_role("button", name="변경 배정 확정", exact=True).click()
            expect(team).to_contain_text("갱신본 3")
            assert fixture.current()[b]["assign_ver"] == 1
            page.get_by_role("link", name="변경 팀 목록 전송 →").click()
            expect(page.get_by_text("변경 팀의 최신 목록", exact=False)).to_be_visible()
            expect(page.get_by_role("heading", name="가상 재계산 1팀", exact=False)).to_be_visible()
            expect(page.get_by_role("heading", name="가상 재계산 2팀", exact=False)).to_have_count(0)
            qr = page.get_by_role("button", name="QR 표시", exact=True)
            expect(qr).to_have_count(1)
            with page.expect_response(lambda r: r.url == root + "api/bundles" and r.request.method == "POST") as shown:
                qr.click()
            bundle_id = shown.value.json()["bundle_id"]
            transfer = S.one(fixture.con, "SELECT batch_id,frames_json FROM transfer_bundle WHERE id=?", (bundle_id,))
            assert transfer["batch_id"] == fixture.current()[a]["batch_id"]
            _, payload, receipt = phone.receive(json.loads(transfer["frames_json"]))
            assert [s["request_id"] for s in payload["stops"]] == [one, extra]
            page.get_by_label("수신 코드", exact=True).fill(receipt)
            page.get_by_role("button", name="수신 확인", exact=True).click()
            expect(page.get_by_text("휴대폰 수신 확인됨", exact=True)).to_be_visible()
            page.get_by_role("button", name="전체 팀 보기", exact=True).click()
            expect(page.get_by_role("heading", name="가상 재계산 2팀", exact=False)).to_be_visible()
            assert fixture.current()[a]["assign_ver"] == 3

            page.goto(root + "#/dispatch?date=" + DAY, wait_until="domcontentloaded")
            page.get_by_role("button", name="현재 배정 불러오기", exact=True).click()
            page.get_by_label("재계산 시작 시각 (선택)", exact=True).fill("10:00")
            team.get_by_text("현재 위치·적재량", exact=True).click()
            lat = team.get_by_label("가상 재계산 1팀 현재 위도", exact=True)
            lon = team.get_by_label("가상 재계산 1팀 현재 경도", exact=True)
            lat.fill("35.149")
            recalculate = team.get_by_role("button", name="가상 재계산 1팀 남은 노선 재계산", exact=True)
            recalculate.click()
            expect(lon).to_be_focused()
            expect(page.get_by_text("가상 재계산 1팀의 현재 위도와 경도를 모두 입력하세요.", exact=True)).to_be_visible()
            lon.fill("126.91")
            team.get_by_label("가상 재계산 1팀 현재 적재 부피(m³)", exact=True).fill("0.5")
            held = []
            route_url = root + 'api/route'
            ctx.route(route_url, lambda route: held.append(route))
            with page.expect_request(route_url):
                recalculate.click()
            expect(page.get_by_text('변경 팀의 남은 노선을 계산하는 중입니다…', exact=True)).to_be_visible()
            lat.fill('35.1498')
            held.pop().continue_()
            expect(page.get_by_text('차량 정보나 시작 시각이 바뀌었습니다.', exact=False)).to_be_visible(timeout=20000)
            expect(dialog).not_to_be_visible()
            expect(lat).to_have_value('35.1498')
            ctx.unroute(route_url)
            recalculate.click()
            expect(dialog.get_by_text("차량 현재 위치 · 10:00 출발", exact=False)).to_be_visible(timeout=20000)
            expect(dialog.get_by_text("입력한 현재 적재 0.5m³", exact=True)).to_be_visible()
            dialog.get_by_role("button", name="변경 배정 확정", exact=True).click()
            expect(team).to_contain_text("갱신본 4")
            assert [s["request_id"] for s in fixture.current()[a]["stops"]] == [one, extra]
            assert fixture.current()[b]["assign_ver"] == 1
            print("실HTTP UI 통과: 검토·취소·충돌·확정·QR 수신·현재 차량 정보로 순서 유지 재계산")
            ctx.close()
            browser.close()
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
