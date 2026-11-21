"""단독 마감 설정 전환, 기존 회차의 확인자, 팀장 화면과 마감 배지를 실제 PC 화면에서 확인한다."""
import os
import unittest

from playwright.sync_api import expect, sync_playwright
from test_dong_work_e2e import api
from test_review_ui import pc_server


class SoloCloseUI(unittest.TestCase):
    def test_solo_and_reviewed_closes_across_setting_changes(self):
        with (pc_server() as url, sync_playwright() as pw,
              pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path) as browser):
            admin = browser.new_context(service_workers="block")
            lead = browser.new_context(service_workers="block")
            password = "solo-close-test-123"
            api(admin, url, "setup", {"username": "solo_admin", "password": password, "display_name": "가상 담당"})
            api(admin, url, "users", {"username": "solo_lead", "password": password, "display_name": "가상 팀장", "role": "LEAD"})
            api(lead, url, "login", {"username": "solo_lead", "password": password})
            api(lead, url, "password", {"old": password, "password": password + "-new"})
            today = api(admin, url, "bootstrap")["today"]
            page, reviewer = admin.new_page(), lead.new_page()
            page.goto(url + "#/settings/basic")
            toggle = page.get_by_role("checkbox", name="일일 마감 시 팀장 확인 필요", exact=True)
            expect(toggle).to_be_checked(timeout=3000)
            save = page.get_by_role("button", name="마감 설정 저장", exact=True)

            api(admin, url, "close", {"date": today, "note": "팀장 확인한 첫 회차"})
            reviewer.goto(url + "#/close")
            reviewer.get_by_role("button", name="팀장 확인", exact=True).click()
            reviewer.get_by_role("dialog").get_by_role("button", name="확인 기록", exact=True).click()
            expect(reviewer.get_by_text("팀장 확인 (solo_lead)", exact=True)).to_be_visible()
            api(admin, url, "close", {"date": today, "note": "확인 전 두 번째 회차"})
            page.get_by_label("문의처", exact=True).fill("저장 전 문의처")
            toggle.uncheck()
            with page.expect_response(lambda r: r.request.method == "POST" and r.url == url + "api/settings"):
                save.click()
            expect(page.get_by_label("문의처", exact=True)).to_have_value("저장 전 문의처")
            page.reload()
            expect(toggle).not_to_be_checked()
            reviewer.goto(url + "#/home")
            expect(reviewer.locator('.workflow-primary')).to_contain_text("마감 내역 보기")
            expect(reviewer.locator('.workflow')).not_to_contain_text("팀장 확인 대기")
            expect(reviewer.locator('[data-key="close_approve"], nav.side a[data-k="close"] .nav-badge')).to_have_count(0)
            reviewer.goto(url + "#/close")
            expect(reviewer.get_by_text("확인 전 두 번째 회차", exact=True)).to_be_visible()
            expect(reviewer.get_by_role("button", name="팀장 확인", exact=True)).to_have_count(0)
            expect(reviewer.get_by_role("row").filter(has_text="팀장 확인한 첫 회차")).to_contain_text("solo_lead")

            page.goto(url + "#/close")
            page.get_by_placeholder("마감 메모").fill("혼자 마친 세 번째 회차")
            page.get_by_role("button", name="마감 다시 기록", exact=True).click()
            dialog = page.get_by_role("dialog")
            expect(dialog).not_to_contain_text("팀장 확인을 받아야")
            dialog.get_by_role("button", name="마감 기록", exact=True).click()
            history = page.get_by_role("row").filter(has_text="혼자 마친 세 번째 회차")
            expect(history).to_contain_text("단독 마감 완료")
            expect(history).not_to_contain_text("solo_lead")
            page.goto(url + "#/home")
            expect(page.locator('[data-step="close"]')).to_contain_text("단독 마감 완료")
            page.goto(url + "#/settings/basic")
            toggle.check()
            with page.expect_response(lambda r: r.request.method == "POST" and r.url == url + "api/settings"):
                save.click()
            reviewer.reload()
            expect(reviewer.get_by_role("row").filter(has_text="혼자 마친 세 번째 회차")).to_contain_text("단독 마감 완료")
            expect(reviewer.get_by_role("button", name="팀장 확인", exact=True)).to_have_count(0)
            api(admin, url, "close", {"date": today, "note": "두 사람이 처리할 새 회차"})
            reviewer.reload()
            expect(reviewer.get_by_role("button", name="팀장 확인", exact=True)).to_be_visible()


if __name__ == "__main__":
    unittest.main()
