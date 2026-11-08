"""버릴 물건 선택: 실제 ES 모듈·수수료표/Chromium, 사용 빈도 API만 가상 제어."""
from __future__ import annotations

from contextlib import contextmanager
from functools import partial
import http.server
import json
import os
from pathlib import Path
from socketserver import TCPServer
import tempfile
import threading
import unittest

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = Path(os.environ.get("DG_ITEM_DIR") or tempfile.mkdtemp(prefix="dg-item-picker-")) / "shots"
FEES = json.loads((ROOT / "data/fee_table.json").read_text(encoding="utf-8"))["rows"]


@contextmanager
def source_server():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript", ".mjs": "text/javascript"}

        def log_message(self, *_):
            pass

        def translate_path(self, path):
            if path.startswith("/fonts/"):
                return str(ROOT / "pc/ui/fonts" / Path(path).name)
            return super().translate_path(path)

    class Server(http.server.ThreadingHTTPServer):
        request_queue_size = 128

        def server_bind(self):
            TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address[:2]

    server = Server(("127.0.0.1", 0), partial(Quiet, directory=str(ROOT)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


MOUNT = r"""async ({usagePath,overrides,holdUsage}) => {
  const C=await import('/pc/ui/core.js');
  C.setBoot({me:{username:'picker_fixture',role:usagePath?'ADMIN':'DONG',prefs:{}},settings:{},fee:{overrides}});
  window.__picked=[]; window.__apiCalls=[]; window.__releaseUsage=null;
  const original=window.fetch;
  window.fetch=async(url,options)=>{
    if(!String(url).startsWith('/api/'))return original(url,options);
    window.__apiCalls.push(String(url));
    if(holdUsage)await new Promise(resolve=>window.__releaseUsage=resolve);
    return new Response(JSON.stringify([C.FEES().rows.at(-1).id]),{status:200});
  };
  const {itemPicker}=await import('/pc/ui/widgets.js');
  window.__fees=C.FEES().rows;
  document.querySelector('#picker-host').append(itemPicker(item=>__picked.push(structuredClone(item)),{usagePath}));
}"""


class ItemPickerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server_context = source_server()
        cls.url = cls.server_context.__enter__()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=os.environ.get("CHROME") or cls.pw.chromium.executable_path, headless=True)
        SHOTS.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server_context.__exit__(None, None, None)

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)

    def tearDown(self):
        if self._outcome.result and (self._outcome.result.failures or self._outcome.result.errors):
            self.page.screenshot(path=str(SHOTS / (self._testMethodName + ".png")), full_page=True)
        self.context.close()

    def mount(self, usage_path=None, overrides=None, hold=False):
        self.page.route("**/picker-fixture.html", lambda route: route.fulfill(content_type="text/html", body="""<!doctype html><html lang='ko'>
          <meta charset='utf-8'><link rel='stylesheet' href='/pc/ui/pc.css'>
          <main id='app' style='padding:20px;max-width:620px;margin:auto'><section class='card' id='picker-host'><h2>버릴 물건</h2></section></main></html>"""))
        self.page.goto(self.url + "/picker-fixture.html")
        self.page.evaluate(MOUNT, {"usagePath": usage_path, "overrides": overrides or {}, "holdUsage": hold})
        self.search = self.page.get_by_placeholder("품목명·규격 검색", exact=False)

    def visible(self, locator):
        expect(locator).to_be_visible()
        return locator

    def test_legacy_callback_and_dong_usage_api_contract(self):
        self.mount()
        self.search.fill("3인용 소파")
        self.page.locator(".results button").first.click()
        self.page.get_by_role("button", name="품목 추가", exact=True).click()
        picked = self.page.evaluate("__picked")
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["qty"], 1)
        self.assertEqual(picked[0]["fee_rate_id"], picked[0]["_row"]["id"])
        self.assertIn("3인용", picked[0]["_row"]["spec"])
        self.assertEqual(self.page.evaluate("__apiCalls"), [])

    def test_catalog_explains_common_objects_behind_official_names(self):
        self.mount()
        expect(self.page.get_by_role("button", name="응접세트 · 규격 8개", exact=True)).to_contain_text("소파")
        expect(self.page.get_by_role("button", name="침대 · 규격 10개", exact=True)).to_contain_text("매트리스")

    def test_category_name_and_specification_without_search(self):
        self.mount()
        self.visible(self.page.get_by_role("button", name="가구류", exact=True)).click()
        self.page.get_by_role("button", name="장롱 · 규격 3개", exact=True).click()
        expect(self.page.locator(".results button")).to_have_count(3)
        self.page.locator(".results button").filter(has_text="120cm 이상").click()
        selected = self.page.get_by_role("region", name="선택한 물건", exact=True)
        expect(selected).to_contain_text("장롱")
        expect(selected).to_contain_text("쪽")
        self.page.get_by_label("추가 수량", exact=True).fill("2")
        expect(selected).to_contain_text("30,000원")
        self.page.get_by_role("button", name="품목 추가", exact=True).click()
        self.assertEqual(self.page.evaluate("__picked[0].qty"), 2)
        self.assertIn("120cm 이상", self.page.evaluate("__picked[0]._row.spec"))
        expect(self.search).to_be_focused()

    def test_ambiguous_enter_requires_specification_choice(self):
        self.mount()
        self.search.fill("의자")
        self.search.press("Enter")
        self.assertEqual(self.page.evaluate("__picked.length"), 0)
        expect(self.page.locator(".results button").first).to_be_focused()
        self.page.keyboard.press("Enter")
        expect(self.page.get_by_label("추가 수량", exact=True)).to_be_focused()
        self.page.get_by_label("추가 수량", exact=True).press("Enter")
        self.assertEqual(self.page.evaluate("__picked.length"), 1)

    def test_exact_search_enter_adds_once_and_returns_focus(self):
        self.mount()
        self.search.fill("3인용 쇼파")
        expect(self.page.locator(".results button")).to_have_count(1)
        self.search.press("Enter")
        self.assertEqual(self.page.evaluate("__picked.length"), 1)
        expect(self.search).to_have_value("")
        expect(self.search).to_be_focused()
        expect(self.page.get_by_role("status")).to_contain_text("추가했습니다")

    def test_search_keeps_all_matching_specifications(self):
        self.mount()
        self.search.fill("기타")
        expected = len([r for r in FEES if r["category"] == "기타"])
        self.assertGreater(expected, 40)
        expect(self.page.locator(".results button")).to_have_count(expected)

    def test_ime_confirmation_and_ctrl_enter_do_not_add_candidate(self):
        self.mount()
        self.search.fill("3인용 소파")
        self.search.dispatch_event("keydown", {"key": "Enter", "isComposing": True})
        self.assertEqual(self.page.evaluate("__picked.length"), 0)
        self.search.press("Control+Enter")
        self.assertEqual(self.page.evaluate("__picked.length"), 0)

    def test_quantity_validation_and_step_controls(self):
        self.mount()
        self.search.fill("3인용 소파")
        self.page.locator(".results button").first.click()
        qty = self.visible(self.page.get_by_label("추가 수량", exact=True))
        for value in ("0", "1.5"):
            qty.fill(value)
            self.page.get_by_role("button", name="품목 추가", exact=True).click()
            self.assertEqual(self.page.evaluate("__picked.length"), 0)
            expect(qty).to_have_attribute("aria-invalid", "true")
        qty.fill("2")
        self.page.get_by_role("button", name="수량 늘리기", exact=True).click()
        expect(qty).to_have_value("3")
        self.page.get_by_role("button", name="수량 줄이기", exact=True).click()
        expect(qty).to_have_value("2")
        self.page.get_by_role("button", name="품목 추가", exact=True).click()
        self.assertEqual(self.page.evaluate("__picked[0].qty"), 2)

    def test_quantity_caption_focuses_input_without_changing_number(self):
        self.mount()
        self.search.fill("3인용 소파")
        self.page.locator(".results button").first.click()
        qty = self.page.get_by_label("추가 수량", exact=True)
        qty.fill("3")
        self.page.locator(".wp-quantity").click(position={"x": 3, "y": 5})
        expect(qty).to_have_value("3")
        expect(qty).to_be_focused()

    def test_known_and_pending_quantity_use_server_upper_limit(self):
        self.mount()
        self.search.fill("3인용 소파")
        self.page.locator(".results button").first.click()
        qty = self.page.get_by_label("추가 수량", exact=True)
        qty.fill("10000")
        self.page.get_by_role("button", name="품목 추가", exact=True).click()
        self.assertEqual(self.page.evaluate("__picked.length"), 0)
        qty.fill("9999")
        expect(self.page.get_by_role("button", name="수량 늘리기", exact=True)).to_be_disabled()
        self.page.get_by_text("목록에 없는 품목 넣기 (금액 확인 대기)", exact=True).click()
        self.page.get_by_placeholder("목록에 없는 품목 이름", exact=True).fill("가상 물건")
        self.page.get_by_label("확인 대기 수량", exact=True).fill("10000")
        self.page.get_by_role("button", name="확인 대기로 추가", exact=True).click()
        self.assertEqual(self.page.evaluate("__picked.length"), 0)

    def test_review_price_and_unknown_item_remain_pending(self):
        self.mount()
        review = next(r for r in FEES if r["status"] == "REVIEW")
        self.search.fill(review["name"])
        self.visible(self.page.locator(f'[data-fee-id="{review["id"]}"]')).click()
        selected = self.page.get_by_role("region", name="선택한 물건", exact=True)
        expect(selected).to_contain_text("금액 확인 대기")
        self.assertNotIn("0원", selected.inner_text())
        self.page.get_by_role("button", name="품목 추가", exact=True).click()
        self.assertEqual(self.page.evaluate("__picked[0]._row.status"), "REVIEW")
        self.page.get_by_text("목록에 없는 품목 넣기 (금액 확인 대기)", exact=True).click()
        self.page.get_by_placeholder("목록에 없는 품목 이름", exact=True).fill("가상 미등록 물건")
        self.page.get_by_label("확인 대기 수량", exact=True).fill("3")
        self.page.get_by_role("button", name="확인 대기로 추가", exact=True).click()
        pending = self.page.evaluate("__picked[1]")
        self.assertEqual(pending["name"], "가상 미등록 물건")
        self.assertEqual(pending["qty"], 3)
        self.assertNotIn("fee_rate_id", pending)

    def test_approved_override_is_used_for_selection_amount(self):
        review = next(r for r in FEES if r["status"] == "REVIEW")
        self.mount(overrides={review["id"]: 12345})
        self.search.fill(review["name"])
        self.visible(self.page.locator(f'[data-fee-id="{review["id"]}"]')).click()
        expect(self.page.get_by_role("region", name="선택한 물건", exact=True)).to_contain_text("12,345원")
        self.page.get_by_role("button", name="품목 추가", exact=True).click()
        self.assertTrue(self.page.evaluate("__picked[0]._row.overridden"))

    def test_late_usage_does_not_reset_selection_or_quantity(self):
        self.mount(usage_path="fee-usage", hold=True)
        self.search.fill("3인용 소파")
        self.page.locator(".results button").first.click()
        self.visible(self.page.get_by_label("추가 수량", exact=True)).fill("3")
        self.page.evaluate("__releaseUsage()")
        expect(self.page.get_by_label("추가 수량", exact=True)).to_have_value("3")
        expect(self.page.get_by_role("region", name="선택한 물건", exact=True)).to_contain_text("3인용")
        self.page.get_by_role("button", name="품목 추가", exact=True).click()
        self.assertEqual(self.page.evaluate("__picked[0].qty"), 3)


if __name__ == "__main__":
    print("화면 증거:", SHOTS, flush=True)
    unittest.main(verbosity=2)
