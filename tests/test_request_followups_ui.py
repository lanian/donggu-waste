"""가상 접수와 실제 PC 서버로 오늘 현황·동별 후속조치 브라우저 흐름 확인."""
import json
import os
import uuid

from playwright.sync_api import expect, sync_playwright
from test_dong_work_e2e import ROOT, api
from test_review_ui import pc_server

PASSWORD = "followup-ui-test-only-123"


def open_queue(page, selector):
    queue = page.locator(selector).filter(has=page.locator("summary", has_text="접수 후속조치"))
    queue.locator("summary").click()
    return queue


def main():
    with pc_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path, headless=True)
        admin = browser.new_context(service_workers="block")
        scoped = browser.new_context(service_workers="block")
        for ctx in (admin, scoped):
            ctx.route("**/*", lambda route: route.continue_() if route.request.url.startswith(url) else route.abort())
        try:
            api(admin, url, "setup", {"username": "followup_admin", "display_name": "가상 관리자", "password": PASSWORD})
            dong = api(admin, url, "dongs", {"name": "후속가상동"})["id"]
            uid = api(admin, url, "users", {"username": "followup_dong", "display_name": "가상 동 담당", "role": "DONG",
                      "password": PASSWORD, "dong_ids": [dong]})["id"]
            fee = next(r for r in json.loads((ROOT / "data/fee_table.json").read_text(encoding="utf-8"))["rows"]
                       if r["status"] == "CONFIRMED" and r["price"] > 0)
            created = api(admin, url, "requests", {"name": "가상 신청인", "phone": "01000001122", "address": "가상구 검증로 10",
                          "dong": "후속가상동", "planned_date": "2098-01-01", "items": [{"fee_rate_id": fee["id"], "qty": 3}]})
            rid = created["id"]
            req = api(admin, url, f"requests/{rid}")
            api(admin, url, f"requests/{rid}/collection",
                {"req_ver": req["req_ver"], "basis": req["collection"]["basis"], "collected_on": api(admin, url, "bootstrap")["today"],
                 "items": [{"line_id": req["items"][0]["line_id"], "qty": 1}], "reason": "가상 수거 사유", "client_req_id": str(uuid.uuid4())})
            api(admin, url, f"requests/{rid}/contacts", {"kind": "독촉", "content": "가상 재방문 독촉"})

            page = admin.new_page()
            page.goto(url + "#/home", wait_until="domcontentloaded")
            queue = open_queue(page, ".dash-backlog details")
            queue.get_by_role("link", name=created["receipt_no"], exact=True).click()
            card = page.get_by_role("region", name="접수 후속조치")
            expect(card.get_by_text("가상 재방문 독촉", exact=False)).to_be_visible()
            expect(card.get_by_text("가상 수거 사유", exact=False)).to_be_visible()
            expect(card.get_by_role("list", name="남은 품목과 사유")).to_contain_text("남은 2")
            card.get_by_role("button", name="후속조치 수정").click()
            form = page.get_by_role("dialog", name="후속조치 저장")
            form.get_by_label("담당자").select_option(uid)
            form.get_by_label("처리기한").fill("2020-01-01")
            form.get_by_label("다음 조치").fill("가상 재방문 안내")
            form.get_by_role("button", name="저장", exact=True).click()
            expect(card.get_by_text("가상 재방문 안내", exact=False)).to_be_visible()
            memo = page.get_by_label("요청 메모", exact=True)
            memo.fill("아직 저장하지 않은 가상 메모")
            card.get_by_role("button", name="재방문 약속", exact=True).click()
            form = page.get_by_role("dialog", name="재방문 약속")
            form.get_by_label("재방문 날짜").fill("2098-01-03")
            form.get_by_label("방문 시작 (선택)").fill("10:00")
            form.get_by_label("방문 종료 (선택)").fill("11:00")
            form.get_by_role("button", name="약속 확정").click()
            expect(card.get_by_text("재방문 약속 확정", exact=False)).to_contain_text("2098-01-03 10:00 ~ 11:00")
            expect(memo).to_have_value("아직 저장하지 않은 가상 메모")
            expect(page.get_by_label("수거 예정일", exact=True)).to_have_value("2098-01-03")

            api(scoped, url, "login", {"username": "followup_dong", "password": PASSWORD})
            api(scoped, url, "password", {"old": PASSWORD, "password": "followup-ui-changed-123"})
            page = scoped.new_page()
            page.goto(url + "#/dongs", wait_until="domcontentloaded")
            queue = open_queue(page, ".dong-workspace details")
            queue.get_by_label("후속조치 행정동").select_option(dong)
            queue.get_by_label("후속조치 담당자").select_option(uid)
            queue.get_by_label("기한 초과만").check()
            expect(queue.get_by_text("기한 초과", exact=True)).to_be_visible()
            queue.get_by_role("link", name=created["receipt_no"], exact=True).click()
            card = page.get_by_role("region", name="접수 후속조치")
            expect(card.get_by_text("재방문 약속 확정", exact=False)).to_contain_text("2098-01-03")
            card.get_by_role("button", name="후속조치 수정").click()
            form = page.get_by_role("dialog", name="후속조치 저장")
            form.get_by_label("처리 상태").select_option("DONE")
            form.get_by_role("button", name="저장", exact=True).click()
            expect(card.get_by_text("조치 완료", exact=True)).to_be_visible()
            card.get_by_text("후속조치 이력", exact=True).click()
            expect(card.get_by_text("followup_dong · 완료", exact=False)).to_be_visible()
            expect(card.get_by_text("수거 상태 · 일부 수거")).to_be_visible()
            print("실HTTP UI 통과: 오늘 현황, 미수거 사유·잔량, 재방문 확정, 수정 초안 보존, 동·담당자·기한 초과 필터, 완료와 이력")
        finally:
            scoped.close()
            admin.close()
            browser.close()


if __name__ == "__main__":
    main()
