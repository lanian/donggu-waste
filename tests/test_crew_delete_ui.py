"""임시 PC 서버와 Chrome에서 수거팀 삭제 확인·취소·재시도·다른 행 초안을 검증한다."""
import os
import unittest

from playwright.sync_api import expect, sync_playwright
from test_dong_work_e2e import api
from test_review_ui import pc_server


class CrewDeleteUI(unittest.TestCase):
    def test_delete_preserves_other_rows_and_removes_team_from_bootstrap(self):
        with (pc_server() as url, sync_playwright() as pw,
              pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path) as browser):
            ctx = browser.new_context(service_workers="block")
            api(ctx, url, "setup", {"username": "crew_admin", "password": "crew-delete-test-123", "display_name": "시험 관리자"})
            cid = api(ctx, url, "crews", {"name": "가상 A팀"})["id"]
            other_id = api(ctx, url, "crews", {"name": "가상 B팀"})["id"]
            dong_id = api(ctx, url, "dongs", {"name": "삭제시험동", "crew_id": cid})["id"]
            page = ctx.new_page()
            page.goto(url + "#/settings/crews")
            name = page.get_by_label("가상 A팀 이름", exact=True)
            other = page.get_by_label("가상 B팀 이름", exact=True)
            draft = page.get_by_label("새 팀 이름", exact=True)
            row = page.get_by_role("row").filter(has=name)
            new_row = page.get_by_role("row").filter(has=draft)
            remove = row.get_by_role("button", name="가상 A팀 삭제", exact=True)
            expect(remove).to_be_visible(timeout=3000)
            expect(new_row.get_by_role("button", name="삭제")).to_have_count(0)
            other.fill("수정 중인 B팀")
            draft.fill("등록 중인 팀")

            endpoint = url + f"api/crews/{cid}/delete"
            posts = []
            page.on("request", lambda req: posts.append(req.method) if req.url == endpoint else None)
            remove.click()
            dialog = page.get_by_role("dialog", name="수거팀 삭제")
            expect(dialog).to_contain_text("가상 A팀")
            dialog.get_by_role("button", name="취소").click()
            expect(remove).to_be_focused()
            self.assertEqual(posts, [])
            expect(other).to_have_value("수정 중인 B팀")

            remove.click()
            confirm = dialog.get_by_role("button", name="삭제", exact=True)
            confirm.click()
            expect(dialog.get_by_role("alert")).to_contain_text("동")
            expect(row).to_be_visible()
            expect(draft).to_have_value("등록 중인 팀")
            api(ctx, url, "dongs", {"id": dong_id, "name": "삭제시험동", "crew_id": None})
            confirm.click()
            expect(dialog).to_have_count(0)
            expect(row).to_have_count(0)
            expect(other).to_be_focused()
            expect(page.get_by_text("수거팀을 삭제했습니다.", exact=True)).to_be_visible()
            expect(other).to_have_value("수정 중인 B팀")
            expect(draft).to_have_value("등록 중인 팀")
            self.assertEqual(posts, ["POST", "POST"])
            self.assertFalse(page.evaluate("async id => (await import('/core.js')).boot.crews.some(c => c.id === id)", cid))
            page.reload()
            expect(other).to_have_value("가상 B팀")
            expect(row).to_have_count(0)

            api(ctx, url, "unloads", {"crew_id": other_id, "work_date": "2024-01-02", "place": "시험 반입지", "weight_kg": 50})
            page.get_by_role("button", name="가상 B팀 삭제", exact=True).click()
            confirm.click()
            expect(other).to_have_count(0)
            expect(page.get_by_text("팀을 목록에서 숨겼습니다. 기존 기록은 보존했습니다.", exact=True)).to_be_visible()
            self.assertEqual(api(ctx, url, "unloads?date=2024-01-02")[0]["crew_name"], "가상 B팀")
            page.reload()
            expect(draft).to_be_visible()
            expect(other).to_have_count(0)
            ctx.close()


if __name__ == "__main__":
    unittest.main()
