"""기본 보고서 표시 회귀: 실제 Chromium과 가상 집계만 사용한다."""
import os
import unittest

from playwright.sync_api import expect, sync_playwright

from test_reports_labels_ui import fixture_server, mount, ROOT
import sys

sys.path.insert(0, str(ROOT / "pc"))
from dgpc.operation_print import report_body


def report_fixture(kind="daily", empty=False):
    revenue = {
        "methods": [
            {"method": "현금", "pay_n": 2, "pay": 1000, "refund_n": 1, "refund": 200, "net": 800},
            {"method": "카드", "pay_n": 1, "pay": 500, "refund_n": 0, "refund": 0, "net": 500},
        ],
        "totals": {"pay_n": 3, "pay": 1500, "refund_n": 1, "refund": 200, "net": 1300},
    }
    if empty:
        revenue = {"methods": [], "totals": dict.fromkeys(revenue["totals"], 0)}
    return {
        "kind": kind, "start": "2026-11-07", "end": "2026-11-07", "basis_label": "수거 예정일",
        "generated_at": "2026-11-07 10:00", "source_note": "수거 수량은 현재 누적값입니다.",
        "totals": {"requests": 3, "received": 3, "by_status": {"DONE": 1, "ON_HOLD": 1},
                   "calculated": 3000, "reduction": 300, "levied": 2700, "paid": 4800,
                   "amount_pending": 0, "unpaid": 0, "unload_m3": 0, "unload_kg": 0},
        "days": [], "items": [], "categories": [],
        "dongs": [{"dong": "", "planned": 3, "done": 1, "revisit": 1, "cancelled": 1, "levied": 2700}],
        "crews": [{"crew": "가상 보류팀", "assigned": 1, "done": 0, "partial_or_not": 1}],
        "channels": [{"channel": "전화", "planned": 2, "done": 1, "cancelled": 0},
                     {"channel": "방문", "planned": 1, "done": 0, "cancelled": 1}],
        "reductions": [{"label": "가상 감면 <검토>", "rate": 50, "count": 1, "amount": 300}],
        "unload_summary": {"count": 0, "volume_records": 0, "weight_records": 0}, "revenue": revenue,
    }


class ReportPresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = fixture_server()
        cls.url = cls.server.__enter__()
        cls.playwright = sync_playwright().start()
        options = {"headless": True}
        if os.environ.get("CHROME"):
            options["executable_path"] = os.environ["CHROME"]
        cls.browser = cls.playwright.chromium.launch(**options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.__exit__(None, None, None)

    def screen(self, report):
        context, page = mount(self.browser, self.url, "reports")
        self.addCleanup(context.close)
        expect(page.get_by_text("가상 의자", exact=True)).to_be_visible()
        page.evaluate("""report => {
            const previous = window.fetch;
            window.fetch = (path, options) => String(path).startsWith('/api/reports/daily?')
              ? Promise.resolve(new Response(JSON.stringify(report), {headers:{'Content-Type':'application/json'}}))
              : previous(path, options);
        }""", report)
        page.get_by_role("button", name="조회", exact=True).click()
        expect(page.get_by_text(report["source_note"], exact=True)).to_be_visible()
        return page

    def document(self, report):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_content(report_body(report)[1])
        return page

    def screen_table(self, page, title):
        return page.locator("section.card").filter(has=page.get_by_role("heading", name=title, exact=True)).get_by_role("table")

    def print_table(self, page, title):
        return page.get_by_role("heading", name=title, exact=True).locator("+ table")

    def cells(self, row):
        return row.locator("th,td").all_text_contents()

    def test_screen_team_heading_includes_on_hold(self):
        page = self.screen(report_fixture())
        table = self.screen_table(page, "수거팀별")
        expect(table.get_by_role("columnheader", name="재방문·보류", exact=True)).to_be_visible()
        self.assertEqual(self.cells(table.get_by_role("row").last), ["가상 보류팀", "1", "0", "1"])

    def test_screen_revenue_shows_refund_counts_and_period_totals(self):
        page = self.screen(report_fixture())
        table = self.screen_table(page, "세입 · 납부일 기준")
        expect(table.get_by_role("columnheader", name="환불 건수", exact=True)).to_be_visible()
        self.assertEqual(self.cells(table.get_by_role("row").filter(has_text="현금")), ["현금", "2", "1,000원", "1", "200원", "800원"])
        self.assertEqual(self.cells(table.get_by_role("row").filter(has_text="합계")), ["합계", "3", "1,500원", "1", "200원", "1,300원"])

    def test_screen_empty_revenue_has_zero_totals(self):
        page = self.screen(report_fixture(empty=True))
        table = self.screen_table(page, "세입 · 납부일 기준")
        expect(table.get_by_role("cell", name="합계", exact=True)).to_be_visible()
        self.assertEqual(self.cells(table.get_by_role("row").last), ["합계", "0", "0원", "0", "0원", "0원"])

    def test_screen_report_cards_stay_within_viewport(self):
        report = report_fixture()
        report["dongs"] = [{"dong": "", "planned": 2, "done": 0, "revisit": 0, "cancelled": 0, "levied": 0}]
        report["crews"] = []
        page = self.screen(report)
        expect(page.get_by_role("cell", name="(동 미지정)", exact=True)).to_be_visible()
        crew_card = page.locator("section.card").filter(has=page.get_by_role("heading", name="수거팀별", exact=True))
        expect(crew_card.get_by_text("해당 기간에 기록이 없습니다.", exact=True)).to_be_visible()
        self.assertEqual(crew_card.locator("xpath=parent::*[contains(@class, 'grid2')]").count(), 1)
        for width in (1440, 1280, 980, 390):
            with self.subTest(width=width):
                page.set_viewport_size({"width": width, "height": 900})
                layout = page.evaluate("""() => ({
                    document: document.documentElement.scrollWidth,
                    outside: [...document.querySelectorAll('section.card')].filter(card => {
                        const rect = card.getBoundingClientRect();
                        return rect.left < 0 || rect.right > innerWidth + 1;
                    }).map(card => card.querySelector('h2')?.textContent)
                })""")
                self.assertEqual(layout["outside"], [], str(layout))
                self.assertLessEqual(layout["document"], width)

    def test_print_revenue_shows_refund_counts_and_period_totals(self):
        for kind in ("daily", "monthly"):
            with self.subTest(kind=kind):
                page = self.document(report_fixture(kind))
                table = self.print_table(page, "세입(납부일 기준)")
                expect(table.get_by_role("columnheader", name="환불 건수", exact=True)).to_be_visible()
                self.assertEqual(self.cells(table.get_by_role("row").filter(has_text="현금")), ["현금", "2", "1000", "1", "200", "800"])
                self.assertEqual(self.cells(table.get_by_role("row").last), ["합계", "3", "1500", "1", "200", "1300"])

    def test_print_empty_revenue_has_zero_totals(self):
        page = self.document(report_fixture(empty=True))
        table = self.print_table(page, "세입(납부일 기준)")
        expect(table.get_by_role("cell", name="합계", exact=True)).to_be_visible()
        self.assertEqual(self.cells(table.get_by_role("row").last), ["합계", "0", "0", "0", "0", "0"])

    def test_print_heading_pagination_style_works_with_content_security_policy(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        page.route("**/report-print.html", lambda route: route.fulfill(
            headers={"Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'"},
            content_type="text/html",
            body='<link rel="stylesheet" href="/pc/ui/doc.css">' + report_body(report_fixture())[1]))
        page.goto(self.url + "/report-print.html")
        page.emulate_media(media="print")
        self.assertTrue(page.locator(".report h2").count())
        self.assertEqual(page.locator(".report h2").evaluate_all(
            "headings => [...new Set(headings.map(heading => getComputedStyle(heading).breakAfter))]"), ["avoid"])

    def test_print_preserves_channel_breakdown(self):
        for kind in ("daily", "monthly"):
            with self.subTest(kind=kind):
                page = self.document(report_fixture(kind))
                table = self.print_table(page, "접수 경로별")
                expect(table).to_be_visible()
                self.assertEqual(self.cells(table.get_by_role("row").nth(1)), ["전화", "2", "1", "0"])
                self.assertEqual(self.cells(table.get_by_role("row").nth(2)), ["방문", "1", "0", "1"])

    def test_print_preserves_reduction_breakdown_and_escapes_labels(self):
        for kind in ("daily", "monthly"):
            with self.subTest(kind=kind):
                page = self.document(report_fixture(kind))
                table = self.print_table(page, "감면별")
                expect(table).to_be_visible()
                self.assertEqual(self.cells(table.get_by_role("row").last), ["가상 감면 <검토>", "50", "1", "300"])
                self.assertEqual(table.locator("검토").count(), 0)


if __name__ == "__main__":
    unittest.main()
