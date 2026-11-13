"""실제 SQLCipher/HTTP/Chromium으로 동 순서의 입력 보존·재시도·화면 전파를 확인한다."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_review_ui import pc_server

passed = 0
HOLD_ORDER = """() => {
  const originalFetch = window.__orderNativeFetch || window.fetch;
  window.__orderNativeFetch = originalFetch;
  window.__orderCalls = 0;
  window.fetch = (path, options) => {
    if (String(path).endsWith('/api/dongs/order') && options?.method === 'POST') {
      window.__orderCalls++;
      return new Promise((resolve, reject) => {
        window.__releaseOrder = () => { originalFetch(path, options).then(resolve, reject); };
      });
    }
    return originalFetch(path, options);
  };
}"""


def ok(value, label):
    global passed
    assert value, label
    passed += 1
    print("  OK", label, flush=True)


def api(context, url, path, body=None):
    options = {"headers": {"X-DG-CSRF": "1"}, "timeout": 30000}
    response = (context.request.get(url + "api/" + path, **options) if body is None else
                context.request.post(url + "api/" + path, data=body, **options))
    assert response.status == 200, (path, response.status, response.text())
    return response.json()


def row_order(page):
    return page.locator("table.dongedit tbody tr[data-dong-id]").evaluate_all("rows => rows.map(row => row.dataset.dongId)")


def main():
    shots = Path(os.environ.get("DG_TEST_DIR") or tempfile.mkdtemp(prefix="dg-dong-order-ui-")) / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    with pc_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path, headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block")
        page = context.new_page()
        page.set_default_timeout(10000)
        try:
            api(context, url, "setup", {"username": "order_ui_admin", "display_name": "가상 순서 관리자", "password": "order-ui-password-123"})
            a_name, b_name = "순서가상A동", "순서가상B동"
            a = api(context, url, "dongs", {"name": a_name, "code": "A1", "keywords": ["가상 보존로"]})["id"]
            b = api(context, url, "dongs", {"name": b_name})["id"]
            original = api(context, url, "dongs")
            base = [d["id"] for d in original]
            wanted = base[:-2] + [b, a]
            page.goto(url + "#/settings/dongs")
            expect(page.get_by_role("heading", name="행정동 관리", exact=True)).to_be_visible()
            save = page.get_by_role("button", name="순서 저장", exact=True)
            reset = page.get_by_role("button", name="순서 되돌리기", exact=True)
            expect(save).to_be_disabled()
            expect(page.get_by_role("button", name=original[0]["name"] + " 위로 이동", exact=True)).to_be_disabled()
            expect(page.get_by_role("button", name=b_name + " 아래로 이동", exact=True)).to_be_disabled()
            ok(row_order(page) == base, "현재 직제순과 맨 위·아래 경계를 표시")

            draft_name = a_name + " 초안"
            page.get_by_label(a_name + " 이름", exact=True).fill(draft_name)
            page.get_by_label(a_name + " 부과번호 코드", exact=True).fill("ZZ")
            up = page.get_by_role("button", name=b_name + " 위로 이동", exact=True)
            up.click()
            expect(up).to_be_focused()
            ok(row_order(page) == wanted and [d["id"] for d in api(context, url, "dongs")] == base,
               "위·아래 이동은 저장 전 화면에서만 반영하고 초점을 유지")
            reset.click()
            ok(row_order(page) == base and page.get_by_label(a_name + " 이름", exact=True).input_value() == draft_name,
               "되돌리기는 순서만 복원하고 입력 중인 동 이름을 유지")
            up.click()
            expect(page.get_by_label(a_name + " 부과번호 코드", exact=True)).to_have_value("ZZ")
            page.route("**/api/dongs/order", lambda route: route.fulfill(status=503, content_type="application/json",
                                                                        body=json.dumps({"error": "가상 순서 저장 실패"})))
            save.click()
            expect(page.get_by_text("가상 순서 저장 실패", exact=True)).to_be_visible()
            ok(row_order(page) == wanted and page.get_by_label(a_name + " 이름", exact=True).input_value() == draft_name,
               "순서 저장 실패 뒤 이동 배치와 다른 입력을 보존")
            page.unroute("**/api/dongs/order")
            page.evaluate(HOLD_ORDER)
            save.click()
            expect(save).to_be_disabled()
            expect(reset).to_be_disabled()
            expect(up).to_be_disabled()
            save.evaluate("button => button.click()")
            page.get_by_label(a_name + " 부과번호 코드", exact=True).fill("QQ")
            ok(page.evaluate("window.__orderCalls") == 1, "저장 대기 중 반복 클릭과 순서 이동을 막음")
            page.evaluate("window.__releaseOrder()")
            expect(page.get_by_text("동 순서를 저장했습니다.", exact=True)).to_be_visible()
            expect(save).to_be_disabled()
            saved = api(context, url, "dongs")
            ok([d["id"] for d in saved] == wanted and next(d for d in saved if d["id"] == a)["code"] == "A1",
               "순서 저장은 순서만 반영하고 저장하지 않은 다른 입력은 서버에 보내지 않음")
            expect(page.get_by_label(a_name + " 이름", exact=True)).to_have_value(draft_name)
            expect(page.get_by_label(a_name + " 부과번호 코드", exact=True)).to_have_value("QQ")
            ok(True, "저장 성공 뒤에도 이름·코드 입력 초안을 유지")
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=shots / "dong_order_saved.png", full_page=True)

            a_row = page.get_by_label(a_name + " 이름", exact=True).locator("xpath=ancestor::tr")
            page.evaluate("""() => {
              const originalFetch = window.fetch;
              let hold = true;
              window.fetch = (path, options) => {
                if (hold && String(path).endsWith('/api/dongs') && (!options?.method || options.method === 'GET')) {
                  hold = false;
                  return new Promise((resolve, reject) => {
                    window.__releaseDongs = () => { originalFetch(path, options).then(resolve, reject); };
                  });
                }
                return originalFetch(path, options);
              };
            }""")
            a_row.get_by_role("button", name="저장", exact=True).click()
            page.wait_for_function("() => typeof window.__releaseDongs === 'function'")
            expect(up).to_be_disabled()
            ok(True, "동 항목 저장 뒤 목록을 다시 읽는 동안에도 순서 조작을 잠금")
            page.evaluate("window.__releaseDongs()")
            expect(page.get_by_label(draft_name + " 이름", exact=True)).to_have_value(draft_name)
            ok([d["id"] for d in api(context, url, "dongs")] == wanted,
               "동의 일반 항목을 저장해도 방금 저장한 순서를 되돌리지 않음")
            page.goto(url + "#/dongs")
            selector = page.get_by_label("행정동 범위", exact=True)
            expect(selector).to_be_visible()
            values = selector.evaluate("el => [...el.options].map(o => o.value).filter(v => !['all','-'].includes(v))")
            ok(values == wanted, "동별 관리의 행정동 범위가 저장 순서를 따름")
            page.goto(url + "#/request/new")
            expect(page.get_by_role("heading", name="전화 접수 등록", exact=True)).to_be_visible()
            intake = page.get_by_label("행정동")
            expect(intake).to_be_visible()
            names = intake.evaluate("el => [...el.options].map(o => o.value).filter(Boolean)")
            expected_names = [d["name"] for d in api(context, url, "dongs")]
            assert names == expected_names, ("전화 접수 동 선택 순서", names, expected_names)
            ok(True, "전화 접수의 동 선택도 저장 순서를 따름")
            page.goto(url + "#/users")
            page.get_by_label("새 계정 역할", exact=True).select_option("DONG")
            checks = page.get_by_role("group", name="담당 동 (여러 개 선택 가능)").locator("input[type=checkbox]")
            ok(checks.evaluate_all("els => els.map(el => el.value)") == wanted, "계정의 담당 동 선택도 저장 순서를 따름")
            page.reload()
            page.get_by_label("새 계정 역할", exact=True).select_option("DONG")
            checks = page.get_by_role("group", name="담당 동 (여러 개 선택 가능)").locator("input[type=checkbox]")
            ok(checks.evaluate_all("els => els.map(el => el.value)") == wanted, "새로 읽은 화면에서도 저장 순서를 유지")

            page.goto(url + "#/settings/dongs")
            page.get_by_role("button", name=b_name + " 아래로 이동", exact=True).click()
            page.evaluate(HOLD_ORDER)
            page.get_by_role("button", name="순서 저장", exact=True).evaluate("button => { window.__savedOrderButton = button; }")
            page.get_by_role("button", name="순서 저장", exact=True).click()
            page.goto(url + "#/request/new")
            expect(page.get_by_role("heading", name="전화 접수 등록", exact=True)).to_be_visible()
            page.get_by_label("이름").fill("가상 이동후 입력")
            with page.expect_response("**/api/dongs/order") as reply:
                page.evaluate("window.__releaseOrder()")
            assert reply.value.status == 200
            page.wait_for_function("() => !window.__savedOrderButton.hasAttribute('aria-busy')")
            ok(page.evaluate("async () => (await import('/core.js')).boot?.dongs.map(d => d.id)") == base
               and page.get_by_label("이름").input_value() == "가상 이동후 입력",
               "늦은 저장 응답이 이동한 화면의 계정 정보와 입력 초안을 지우지 않음")

            page.goto(url + "#/settings/dongs")
            page.get_by_role("button", name=b_name + " 위로 이동", exact=True).click()
            page.evaluate("""() => {
              const originalFetch = window.__orderNativeFetch;
              let hold = true;
              window.fetch = async (path, options) => {
                const response = await originalFetch(path, options);
                if (hold && String(path).endsWith('/api/dongs/order') && options?.method === 'POST') {
                  hold = false;
                  await new Promise(resolve => { window.__releaseCommittedOrder = resolve; });
                }
                return response;
              };
            }""")
            page.get_by_role("button", name="순서 저장", exact=True).evaluate("button => { window.__oldOrderButton = button; }")
            page.get_by_role("button", name="순서 저장", exact=True).click()
            page.wait_for_function("() => typeof window.__releaseCommittedOrder === 'function'")
            assert [d["id"] for d in api(context, url, "dongs")] == wanted
            page.goto(url + "#/dongs")
            expect(page.get_by_label("행정동 범위", exact=True)).to_be_visible()
            page.evaluate("async () => { const C = await import('/core.js'); C.setBoot(await C.api('bootstrap')); }")
            page.goto(url + "#/settings/dongs")
            page.get_by_role("button", name=b_name + " 아래로 이동", exact=True).click()
            page.get_by_role("button", name="순서 저장", exact=True).click()
            expect(page.get_by_text("동 순서를 저장했습니다.", exact=True)).to_be_visible()
            page.evaluate("window.__releaseCommittedOrder()")
            page.wait_for_function("() => !window.__oldOrderButton.hasAttribute('aria-busy')")
            ok([d["id"] for d in api(context, url, "dongs")] == base
               and page.evaluate("async () => (await import('/core.js')).boot.dongs.map(d => d.id)") == base,
               "먼저 반영됐지만 늦게 도착한 응답이 더 최근에 저장한 순서를 덮어쓰지 않음")
            print(f"동 순서 실제 HTTP/UI {passed}개 통과 · 캡처 {shots}", flush=True)
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
