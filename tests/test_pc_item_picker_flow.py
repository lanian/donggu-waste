"""실제 SQLCipher/브라우저의 전화접수 물건 선택·초안·저장, 선택적으로 실제 EXE 검증."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_dong_work_e2e import api
from test_review_ui import pc_server
from test_reports_labels_bundle import frozen_server

ROOT = Path(__file__).resolve().parent.parent


def main(exe=None):
    output = Path(os.environ.get("DG_ITEM_FLOW_DIR") or tempfile.mkdtemp(prefix="dg-item-flow-"))
    shots = output / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    fixture = frozen_server(exe) if exe else pc_server()
    with fixture as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path, headless=True)
        context = browser.new_context(service_workers="block", viewport={"width": 1440, "height": 900})
        # 실제 지도 서비스 운영은 이 품목 선택 시험의 범위가 아니다.
        context.route("https://tiles.openfreemap.org/styles/**", lambda route: route.fulfill(json={"version": 8, "sources": {}, "layers": [{"id": "fixture", "type": "background", "paint": {"background-color": "#edf2f6"}}]}))
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            api(context, url, "setup", {"username": "picker_flow", "display_name": "가상 접수담당", "password": "picker-flow-fixture-123"})
            page.goto(url + "#/request/new", wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="전화 접수 등록", exact=True)).to_be_visible()
            expect(page.locator(".calday").first).to_be_visible()
            page.get_by_label("이름", exact=True).fill("가상 물건선택 주민")
            page.get_by_label("연락처", exact=True).fill("01000005555")
            page.get_by_label("배출 주소", exact=True).fill("가상구 시험로 25")
            page.get_by_label("수거 예정일", exact=True).fill("2099-01-05")
            picker = page.get_by_role("region", name="버릴 물건 선택", exact=True)
            page.set_viewport_size({"width": 1440, "height": 1100})
            picker.evaluate("el => window.scrollTo(0, scrollY + el.getBoundingClientRect().top - 100)")
            page.evaluate("() => document.fonts.ready")
            picker.screenshot(path=str(shots / "catalog.png"))
            page.set_viewport_size({"width": 1440, "height": 900})
            picker.get_by_role("button", name="가구류", exact=True).click()
            picker.get_by_role("button", name="장롱 · 규격 3개", exact=True).click()
            picker.locator(".results button").filter(has_text="90cm 미만").click()
            picker.get_by_label("추가 수량", exact=True).fill("2")
            picker.get_by_role("button", name="품목 추가", exact=True).click()
            search = picker.get_by_label("품목명·규격 검색", exact=True)
            search.fill("3인용 쇼파")
            search.press("Enter")
            expect(page.locator(".items .it")).to_have_count(2)
            search.fill("의자")
            search.press("Enter")
            expect(page.locator(".items .it")).to_have_count(2)
            picker.locator(".results button").filter(has_text="회전식 의자").click()
            picker.get_by_label("추가 수량", exact=True).fill("3")
            picker.get_by_role("button", name="품목 추가", exact=True).click()
            picker.get_by_text("목록에 없는 품목 넣기 (금액 확인 대기)", exact=True).click()
            picker.get_by_placeholder("목록에 없는 품목 이름", exact=True).fill("가상 미등록 물건")
            picker.get_by_role("button", name="확인 대기로 추가", exact=True).click()
            expect(page.locator(".items .it")).to_have_count(4)
            expect(page.locator(".intake-total")).to_contain_text("금액 확인 대기")
            page.goto(url + "#/requests")
            expect(page.get_by_role("heading", name="접수", exact=True)).to_be_visible()
            page.goto(url + "#/request/new")
            expect(page.get_by_label("이름", exact=True)).to_have_value("가상 물건선택 주민")
            expect(page.locator(".items .it")).to_have_count(4)
            picker = page.get_by_role("region", name="버릴 물건 선택", exact=True)
            picker.get_by_label("품목명·규격 검색", exact=True).fill("매트리스")
            picker.locator(".results button").first.click()
            layouts = []
            for theme, font in (("light", 15), ("dark", 17)):
                page.evaluate("async p => (await import('/core.js')).applyPrefs(p)", {"theme": theme, "font": font})
                for width, height in ((1440, 900), (980, 680), (390, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    picker.locator(".results button").first.click()
                    add = picker.get_by_role("button", name="품목 추가", exact=True)
                    metrics = add.evaluate("""button => {
                      const r=button.getBoundingClientRect(),at=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
                      return {width:innerWidth,documentWidth:document.documentElement.scrollWidth,
                        buttonRect:{x:r.x,y:r.y,bottom:r.bottom},occluder:at?.outerHTML.slice(0,240),
                        buttonInViewport:r.x>=0&&r.y>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1,
                        buttonUncovered:!!at&&(at===button||button.contains(at))};
                    }""")
                    layouts.append({"theme": theme, "font": font, **metrics})
                    picker.screenshot(path=str(shots / f"items-{theme}-{font}-{width}.png"))
                    assert metrics["documentWidth"] <= width + 1, metrics
                    assert metrics["buttonInViewport"] and metrics["buttonUncovered"], metrics
            page.set_viewport_size({"width": 1440, "height": 900})
            # Ctrl+Enter는 이미 담은 접수를 저장하며 선택 중인 5번째 품목을 임의로 추가하지 않는다.
            picker.get_by_label("추가 수량", exact=True).focus()
            page.keyboard.press("Control+Enter")
            page.wait_for_function("() => location.hash.startsWith('#/request/') && location.hash !== '#/request/new'")
            rid = page.evaluate("location.hash.split('/')[2]")
            saved = api(context, url, "requests/" + rid)
            items = saved["items"]
            assert len(items) == 4, items
            expected = [("장롱", "90cm 미만", 2, "쪽"), ("응접세트", "3인용 소파", 1, "개"), ("의자", "회전식 의자", 3, "개")]
            for name, spec, qty, unit in expected:
                row = next(item for item in items if item["name"] == name and spec in item["spec"])
                assert row["qty"] == qty and row["unit_label"] == unit and row["price_status"] == "CONFIRMED", row
            pending = next(item for item in items if item["name"] == "가상 미등록 물건")
            assert pending["price_status"] == "PENDING" and pending["unit_price"] is None, pending
            assert not errors, errors
            response = context.request.get(url + "widgets.js")
            assert response.body() == (ROOT / "pc/ui/widgets.js").read_bytes(), "이전 위젯 자산"
            (output / "report.json").write_text(json.dumps({"actual_exe": bool(exe), "layouts": layouts, "saved_items": len(items), "errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")
            print("물건 선택 실HTTP: 분류·규격·동의어·수량·확인 대기·초안 복구·Ctrl+Enter 저장·6화면 주 동작 통과", flush=True)
            print("화면:", shots, flush=True)
        except BaseException:
            page.screenshot(path=str(shots / "failure.png"), full_page=True)
            raise
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path)
    args = parser.parse_args()
    main(args.exe.resolve() if args.exe else None)
