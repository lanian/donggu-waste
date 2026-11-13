"""실제 SQLCipher/HTTP/Chromium에서 사용 장수·필터·오류·지연 응답을 확인한다."""
import os
from pathlib import Path
import tempfile
import unittest

from playwright.sync_api import expect, sync_playwright
import test_label_usage_api as fixture


class UsageUITests(fixture.UsageHTTPCase):
    def setUp(self):
        super().setUp()
        self.r, self.line = self.request()
        self.result(self.r, self.line, 3)
        other, other_line = self.request(dong="동명동")
        self.result(other, other_line, 0, "NOT_COLLECTED")
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(executable_path=os.environ.get("CHROME") or self.pw.chromium.executable_path, headless=True)
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
        self.url = f"http://127.0.0.1:{self.port}/"
        self.context.add_cookies([{"name": "dg_s", "value": self.admin_cookie.split("=", 1)[1], "url": self.url}])
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(self.url + "#/labels", wait_until="domcontentloaded")

    def tearDown(self):
        try:
            self.context.close()
            self.browser.close()
            self.pw.stop()
        finally:
            super().tearDown()

    def panel(self):
        panel = self.page.get_by_role("region", name="사용 장수 자동 집계", exact=True)
        expect(panel).to_be_visible(timeout=3000)
        return panel

    def test_real_quantity_auto_count_filters_navigation_and_ledger_independence(self):
        p = self.panel()
        expect(self.page.get_by_role("heading", name="배출표 관리", exact=True)).to_be_visible()
        expect(p.get_by_label("자동 집계 사용 장수", exact=True)).to_have_text("3장")
        expect(p.get_by_label("자동 집계 수거 접수", exact=True)).to_have_text("1건")
        expect(p.get_by_role("link", name=self.r["receipt_no"], exact=True)).to_be_visible()
        self.assertNotIn("가상비공개", p.inner_text())
        self.assertEqual(self.get("numbered-labels", self.admin_cookie)[1]["counts"]["USED"], 0)
        p.get_by_label("집계 행정동", exact=True).select_option(label="동명동")
        expect(p.get_by_label("자동 집계 사용 장수", exact=True)).to_have_text("0장")
        p.get_by_label("집계 행정동", exact=True).select_option(label="충장동")
        expect(p.get_by_label("자동 집계 사용 장수", exact=True)).to_have_text("3장")
        p.get_by_label("집계 접수번호", exact=True).fill("not-found")
        p.get_by_role("button", name="집계 조회", exact=True).click()
        expect(p.get_by_label("자동 집계 사용 장수", exact=True)).to_have_text("0장")
        p.get_by_label("집계 접수번호", exact=True).fill(self.r["receipt_no"])
        p.get_by_role("button", name="집계 조회", exact=True).click()
        expect(p.get_by_label("자동 집계 사용 장수", exact=True)).to_have_text("3장")
        p.get_by_role("link", name=self.r["receipt_no"], exact=True).click()
        self.page.wait_for_url("**/#/request/" + self.r["id"])
        self.page.get_by_role("link", name="배출표 관리", exact=True).click()
        p = self.panel()
        expect(p.get_by_label("집계 접수번호", exact=True)).to_have_value(self.r["receipt_no"])
        expect(p.get_by_label("자동 집계 사용 장수", exact=True)).to_have_text("3장")
        # 실제 서명된 수거 결과를 반영하면 별도 사용 등록 없이 다음 조회에서 갱신된다.
        seal, event = self.phone(self.r)
        corrected = [event("ITEM_RESULT", {"line_id": self.line, "qty_confirmed": 3, "qty_collected": 1}),
                     event("ATTEMPT", {"outcome": "PARTIAL"})]
        status, value, _ = self.post("import/result", seal(corrected), self.admin_cookie)
        self.assertEqual(status, 200, value)
        self.assertEqual(value["applied"], 2)
        self.assertEqual(self.get(cookie=self.admin_cookie)[1]["totals"]["used"], 1)
        p.get_by_role("button", name="집계 조회", exact=True).click()
        expect(p.get_by_label("자동 집계 사용 장수", exact=True)).to_have_text("1장")
        shots = Path(os.environ.get("DG_TEST_DIR") or tempfile.mkdtemp(prefix="dg-label-usage-ui-"))
        shots.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(shots / "label-usage.png"), full_page=True)
        print("사용 장수 화면: " + str(shots / "label-usage.png"))
        self.assertEqual(self.errors, [])

    def test_error_retry_and_stale_response_do_not_replace_changed_filters(self):
        p = self.panel()
        expect(p.get_by_label("자동 집계 사용 장수", exact=True)).to_have_text("3장")
        self.page.route("**/api/label-usage?*", lambda route: route.fulfill(status=503, json={"error": "가상 집계 실패"}), times=1)
        p.get_by_role("button", name="집계 조회", exact=True).click()
        expect(p.get_by_role("alert")).to_contain_text("가상 집계 실패")
        p.get_by_role("button", name="집계 다시 시도", exact=True).click()
        expect(p.get_by_label("자동 집계 사용 장수", exact=True)).to_have_text("3장")
        _, original = self.get(cookie=self.admin_cookie)
        held = []
        def hold(route):
            held.append(route)
        self.page.route("**/api/label-usage?*", hold)
        p.get_by_role("button", name="집계 조회", exact=True).click()
        self.page.wait_for_function("() => document.querySelector('[aria-label=\"사용 장수 자동 집계\"] [aria-busy=\"true\"]')")
        self.page.wait_for_timeout(50)
        self.assertEqual(len(held), 1)
        p.get_by_label("집계 접수번호", exact=True).fill("not-found")
        held.pop().fulfill(status=200, json=original)
        self.page.unroute("**/api/label-usage?*", hold)
        expect(p.get_by_text("조회조건을 바꿨습니다. [집계 조회]를 눌러 주세요.", exact=True)).to_be_visible()
        expect(p.get_by_role("link", name=self.r["receipt_no"], exact=True)).to_have_count(0)
        p.get_by_role("button", name="집계 조회", exact=True).click()
        expect(p.get_by_label("자동 집계 사용 장수", exact=True)).to_have_text("0장")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
