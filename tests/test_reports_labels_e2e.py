"""실제 API·라우터에서 보고서와 숫자 배출표를 함께 사용하는 가상 업무 회귀."""
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_review_ui import pc_server

ROOT = Path(__file__).resolve().parent.parent


def main():
    with pc_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path, headless=True)
        context = browser.new_context(service_workers="block", viewport={"width": 1440, "height": 900})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        shots = Path(os.environ.get("DG_TEST_DIR") or tempfile.mkdtemp(prefix="dg-reports-labels-e2e-")) / "shots"
        shots.mkdir(parents=True, exist_ok=True)
        try:
            def post(path, body):
                response = context.request.post(url + "api/" + path, headers={"X-DG-CSRF": "1"}, data=body)
                assert response.ok, response.text()
                return response.json()
            post("setup", {"username": "report_ui", "display_name": "가상 보고담당", "password": "report-ui-fixture-123"})
            fees = json.loads((ROOT / "data/fee_table.json").read_text(encoding="utf-8"))
            fee = next(row["id"] for row in fees["rows"] if row["status"] == "CONFIRMED" and row.get("price", 0) > 0)
            created = post("requests", {"name": "가상비공개주민", "phone": "01000009987", "address": "가상 시험로", "planned_date": "2099-01-02", "items": [{"fee_rate_id": fee, "qty": 1}]})
            page.goto(url + "#/reports", wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="업무 보고서", exact=True)).to_be_visible()
            page.get_by_label("작업일", exact=True).fill("2099-01-02")
            page.get_by_role("button", name="조회", exact=True).click()
            expect(page.get_by_role("heading", name="일일 작업일지 · 2099-01-02", exact=True)).to_be_visible()
            with page.expect_popup() as opened:
                page.get_by_role("button", name="인쇄 화면 열기", exact=True).click()
            printed = opened.value
            printed.wait_for_load_state()
            expect(printed.get_by_role("heading", name="일일 작업일지", exact=True)).to_be_visible()
            assert "가상비공개주민" not in printed.content() and "01000009987" not in printed.content()
            printed.close()
            page.get_by_label("보고서 종류", exact=True).select_option("monthly")
            page.get_by_label("보고 월", exact=True).fill("2099-01")
            page.get_by_role("button", name="조회", exact=True).click()
            expect(page.get_by_role("heading", name="월보 · 2099-01", exact=True)).to_be_visible()
            with page.expect_download() as saved:
                page.get_by_role("button", name="CSV 내려받기", exact=True).click()
            assert saved.value.suggested_filename.endswith(".csv")
            page.screenshot(path=str(shots / "reports-monthly.png"), full_page=True)
            page.get_by_role("link", name="배출표 관리", exact=True).click()
            expect(page.get_by_role("heading", name="숫자 배출표", exact=True)).to_be_visible()
            page.get_by_role("button", name="번호 구간 입고", exact=True).click()
            dialog = page.get_by_role("dialog", name="번호 구간 입고", exact=True)
            dialog.get_by_label("시작 번호", exact=True).fill("000001")
            dialog.get_by_label("끝 번호", exact=True).fill("000002")
            dialog.get_by_role("button", name="입고 기록", exact=True).click()
            expect(dialog).to_have_count(0)
            page.get_by_role("button", name="000001", exact=True).click()
            detail = page.get_by_role("dialog", name="숫자 배출표 000001", exact=True)
            detail.get_by_label("접수 찾기", exact=True).fill("9987")
            detail.get_by_role("button", name="접수 검색", exact=True).click()
            detail.get_by_role("button", name=created["receipt_no"] + " 선택", exact=True).click()
            detail.get_by_role("button", name="접수에 연결하고 사용 기록", exact=True).click()
            expect(detail.get_by_role("button", name="접수에 연결하고 사용 기록", exact=True)).to_have_count(0)
            with page.expect_popup() as numbered:
                detail.get_by_role("button", name="인쇄 화면 열기", exact=True).click()
            label = numbered.value
            label.wait_for_load_state()
            expect(label.locator(".rno")).to_have_text("000001")
            label.close()
            detail.get_by_label("무효 사유", exact=True).fill("가상 번호표 훼손")
            detail.get_by_role("button", name="무효 기록", exact=True).click()
            expect(detail.get_by_role("button", name="무효 기록", exact=True)).to_have_count(0)
            detail.get_by_role("button", name="닫기", exact=True).click()
            page.screenshot(path=str(shots / "numbered-labels.png"), full_page=True)
            state = context.request.get(url + "api/numbered-labels", headers={"X-DG-CSRF": "1"}).json()
            assert state["counts"]["ALL"] == 2 and state["counts"]["VOID"] == 1
            longest = "9" * 30
            post("numbered-labels/receive", {"start": longest, "end": longest})
            token = post(f"numbered-labels/{longest}/print", {})["token"]
            label = context.new_page()
            label.goto(url + "print/doc/" + token)
            expect(label.locator(".rno")).to_have_text(longest)
            assert label.locator(".label").evaluate("el => el.scrollWidth <= el.clientWidth"), "30자리 번호표 잘림"
            for selector in (".rno", ".lday", ".lqr"):
                assert label.locator(selector).evaluate("el => el.getBoundingClientRect().right <= el.closest('.label').getBoundingClientRect().right"), selector + " 번호표 경계 벗어남"
            label.screenshot(path=str(shots / "numbered-label-30-digits.png"), full_page=True)
            label.close()
            assert not errors, errors
            print("실제 보고서 조회·인쇄·월보 CSV·배출표 입고·접수 연결·번호 인쇄·무효·앞0 보존 통과")
            print("화면: " + str(shots))
        except Exception:
            page.screenshot(path=str(shots / "failure.png"), full_page=True)
            print("오류 화면: " + str(shots))
            raise
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
