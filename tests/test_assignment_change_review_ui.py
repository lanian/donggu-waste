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
            print("실HTTP UI 통과: 검토 전 미저장·취소·다른 곳의 배정 변경·재조회·확정·변경 팀만 전송·QR 수신 확인")
            ctx.close()
            browser.close()
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
