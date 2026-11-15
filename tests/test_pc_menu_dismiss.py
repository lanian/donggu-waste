"""실제 임시 PC 서버/Chromium: 메뉴 바깥 클릭은 입력·원래 동작을 보존한다."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_dong_order_ui import api
from test_review_ui import pc_server

PASSED = 0


def ok(value, message):
    global PASSED
    assert value, message
    PASSED += 1
    print("  ✓", message, flush=True)


def closed(page):
    expect(page.locator(".shell")).not_to_have_class(re.compile(r"\bnav-open\b"))
    expect(page.get_by_role("button", name="메뉴 열기·닫기")).to_have_attribute("aria-expanded", "false")


def open_menu(page):
    trigger = page.get_by_role("button", name="메뉴 열기·닫기")
    trigger.click()
    expect(trigger).to_have_attribute("aria-expanded", "true")
    expect(page.locator("nav.side")).to_be_visible()
    return trigger


def main():
    output = Path(os.environ.get("DG_TEST_DIR") or tempfile.mkdtemp(prefix="dg-pc-menu-"))
    output.mkdir(parents=True, exist_ok=True)
    errors, writes = [], []
    with pc_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path, headless=True)
        context = browser.new_context(viewport={"width": 900, "height": 900}, service_workers="block")
        context.route("https://tiles.openfreemap.org/**", lambda route: route.fulfill(json={
            "version": 8, "sources": {}, "layers": [{"id": "background", "type": "background", "paint": {"background-color": "#dde6e8"}}]}))
        api(context, url, "setup", {"username": "menu_admin", "display_name": "가상 메뉴관리자", "password": "menu-fixture-password-123"})
        api(context, url, "prefs", {"theme": "dark", "font": 17})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(url + "#/request/new")
            expect(page.get_by_role("heading", name="전화 접수 등록", exact=True)).to_be_visible()
            expect(page.locator("html")).to_have_attribute("data-theme", "dark")
            ok(page.evaluate("getComputedStyle(document.documentElement).fontSize") == "17px", "어두운 테마·큰 글씨의 실제 사용자 설정 적용")
            page.get_by_label("이름", exact=True).fill("가상 작성중")
            address = page.get_by_label("배출 주소", exact=True)
            address.fill("가상구 메뉴시험로 99")
            page.evaluate("window.__menuDraftNode=document.querySelector('main input[id=name]') || [...document.querySelectorAll('main input')].find(x=>x.value==='가상 작성중')")
            page.on("request", lambda request: writes.append({"url": request.url, "method": request.method}) if request.url.startswith(url) and request.method not in {"GET", "HEAD"} else None)

            canvas = page.locator("main canvas.maplibregl-canvas")
            expect(canvas).to_be_visible()
            page.evaluate("document.fonts.ready")
            page.wait_for_function("async()=>{const C=await import('./core.js');return C.liveMaps.some(m=>m.getCanvas()===document.querySelector('main canvas'))}")
            canvas.scroll_into_view_if_needed()
            open_menu(page)
            point = page.evaluate("""async () => {
              const C=await import('./core.js'), map=C.liveMaps.find(m=>m.getCanvas()===document.querySelector('main canvas'));
              const r=map.getCanvas().getBoundingClientRect(), container=map.getCanvasContainer(), c=container.getBoundingClientRect();
              const x=Math.round(r.left+r.width*.7), y=Math.round(r.top+r.height*.5);
              const pixel={x:x-c.left-container.clientLeft,y:y-c.top-container.clientTop};
              map.once('click',e=>{window.__menuMapClick={lat:e.lngLat.lat,lng:e.lngLat.lng,projected:map.project(e.lngLat),point:e.point,
                client:{x:e.originalEvent.clientX,y:e.originalEvent.clientY},menuWasOpen:document.querySelector('.shell').classList.contains('nav-open')}});
              return {x,y,pixel,rect:{x:r.left,y:r.top,w:r.width,h:r.height}};
            }""")
            page.mouse.click(point["x"], point["y"])
            closed(page)
            page.wait_for_function("window.__menuMapClick")
            chosen = page.evaluate("window.__menuMapClick")
            expect(page.get_by_text("지도에서 지정한 위치", exact=True)).to_be_visible()
            ok(chosen["menuWasOpen"], "지도 클릭 처리는 메뉴의 원래 레이아웃 상태에서 먼저 수행")
            # Integer client coordinates can meet a fractional layout edge (observed drift: 1/32 CSS px).
            # Compare in input pixels; the separate menuWasOpen assertion still enforces handler order.
            assert chosen["client"] == {"x": point["x"], "y": point["y"]}, json.dumps({"point":point,"chosen":chosen})
            pixel_delta = math.hypot(chosen["projected"]["x"] - point["pixel"]["x"], chosen["projected"]["y"] - point["pixel"]["y"])
            assert pixel_delta <= 1, json.dumps({"pixel_delta":pixel_delta,"point":point,"chosen":chosen})
            ok(True, "지도 바깥 메뉴 닫기가 원래 클릭 위치의 좌표를 바꾸지 않음")

            for width in (900, 1000, 390):
                page.set_viewport_size({"width": width, "height": 900})
                open_menu(page)
                box = address.bounding_box()
                address.click(position={"x": box["width"] - 10, "y": box["height"] / 2})
                closed(page)
                expect(address).to_be_focused()
                expect(address).to_have_value("가상구 메뉴시험로 99")
                expect(page.get_by_label("이름", exact=True)).to_have_value("가상 작성중")
                ok(page.evaluate("window.__menuDraftNode.isConnected"), f"{width}px 바깥 입력 클릭으로 메뉴만 정리하고 초점·초안·기존 DOM 보존")

            page.set_viewport_size({"width": 900, "height": 900})
            trigger = open_menu(page)
            page.locator("nav.side .navgrp-t").first.click()
            expect(trigger).to_have_attribute("aria-expanded", "true")
            ok(True, "메뉴 안의 비탐색 영역을 클릭하면 열린 상태 유지")
            page.locator('nav.side a[data-k="requests"]').focus()
            page.keyboard.press("Escape")
            closed(page)
            expect(trigger).to_be_focused()
            ok(True, "Esc는 상태·aria를 함께 닫고 숨겨질 메뉴 초점을 버튼으로 복귀")

            open_menu(page)
            address.focus()
            page.keyboard.press("Escape")
            closed(page)
            expect(address).to_be_focused()
            ok(True, "Esc로 닫아도 이미 본문에 있는 입력 초점은 유지")

            trigger = open_menu(page)
            page.evaluate("dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',isComposing:true,bubbles:true}))")
            expect(trigger).to_have_attribute("aria-expanded", "true")
            page.evaluate("const e=new KeyboardEvent('keydown',{key:'Escape',cancelable:true,bubbles:true});e.preventDefault();dispatchEvent(e)")
            expect(trigger).to_have_attribute("aria-expanded", "true")
            ok(True, "IME와 이미 처리된 Escape는 메뉴 상태를 변경하지 않음")
            trigger.click()
            closed(page)

            for action in ("취소", "Escape"):
                page.evaluate("""async () => {
                  const C=await import('./core.js');
                  window.__menuDialog=C.modal({title:'가상 메뉴 시험',body:[C.h('input',{'aria-label':'메뉴 시험 입력'})]});
                }""")
                expect(page.get_by_role("dialog")).to_be_visible()
                # 실제 모달이 메뉴보다 위에 있으므로, 열린 메뉴 상태만 원래 앱 callback으로 만든다.
                page.evaluate("const b=document.querySelector('.menu-btn');if(b.getAttribute('aria-expanded')!=='true')b.click()")
                page.get_by_label("메뉴 시험 입력").click()
                expect(page.locator(".menu-btn")).to_have_attribute("aria-expanded", "true")
                if action == "Escape":
                    page.keyboard.press("Escape")
                else:
                    page.get_by_role("dialog").get_by_role("button", name="취소", exact=True).click()
                expect(page.get_by_role("dialog")).to_have_count(0)
                expect(page.locator(".menu-btn")).to_have_attribute("aria-expanded", "true")
                ok(True, f"실제 모달 입력·{action}가 뒤의 메뉴를 함께 닫지 않음")
            page.get_by_role("button", name="메뉴 열기·닫기").click()

            page.evaluate("""async () => {
              const C=await import('./core.js');
              document.querySelector('main').append(C.h('button',{type:'button',id:'menu-popup-probe',style:'display:block;margin-left:auto',onclick:()=>{void C.modal({title:'가상 외부 버튼 확인창'})}},'외부 확인창 열기'));
            }""")
            page.locator("#menu-popup-probe").scroll_into_view_if_needed()
            open_menu(page)
            page.locator("#menu-popup-probe").click()
            expect(page.get_by_role("dialog", name="가상 외부 버튼 확인창")).to_be_visible()
            closed(page)
            page.get_by_role("dialog").get_by_role("button", name="취소", exact=True).click()
            page.locator("#menu-popup-probe").evaluate("el=>el.remove()")
            ok(True, "바깥 버튼이 새 대화상자를 열어도 메뉴는 닫고 새 대화상자는 유지")

            open_menu(page)
            page.get_by_role("button", name="← 목록", exact=True).click()
            expect(page.get_by_role("heading", name="접수", exact=True)).to_be_visible()
            closed(page)
            ok(True, "바깥의 실제 목록 버튼은 한 번의 클릭으로 원래 화면 이동 수행")
            trigger = open_menu(page)
            page.locator('nav.side a[data-k="requests"]').click()
            closed(page)
            expect(trigger).to_be_focused()
            ok(True, "현재 메뉴를 다시 선택해도 hashchange 없이 즉시 닫힘")

            page.evaluate("location.hash='/request/new'")
            expect(page.get_by_label("이름", exact=True)).to_have_value("가상 작성중")
            expect(page.get_by_label("배출 주소", exact=True)).to_have_value("가상구 메뉴시험로 99")
            ok(True, "메뉴 닫기·원래 목록 이동 뒤 새 접수의 초안 유지")

            open_menu(page)
            page.set_viewport_size({"width": 1001, "height": 900})
            expect(page.locator(".shell")).not_to_have_class(re.compile(r"\bnav-open\b"))
            expect(page.locator(".menu-btn")).to_have_attribute("aria-expanded", "false")
            expect(page.locator("nav.side")).to_be_visible()
            page.locator(".rail-btn").click()
            expect(page.locator(".shell")).to_have_class(re.compile(r"\brail\b"))
            address.click(position={"x": address.bounding_box()["width"] - 10, "y": 10})
            expect(page.locator(".shell")).to_have_class(re.compile(r"\brail\b"))
            ok(page.evaluate("localStorage.getItem('dg-rail')") == "1", "넓은 화면 전환은 임시 메뉴만 정리하고 고정 rail 설정 유지")
            page.set_viewport_size({"width": 900, "height": 900})
            closed(page)
            page.screenshot(path=str(output / "menu-closed.png"))
            open_menu(page)
            page.screenshot(path=str(output / "menu-open.png"))
            page.keyboard.press("Escape")
            ok(not writes, "메뉴 동작에서 업무 저장·삭제 요청 없음")
            ok(not errors, "브라우저 오류 없음")
        finally:
            (output / "result.json").write_text(json.dumps({"passed": PASSED, "errors": errors, "writes": writes}, ensure_ascii=False, indent=2), encoding="utf-8")
            context.close()
            browser.close()
    print(f"PC menu outside-dismiss: {PASSED} checks passed", flush=True)


if __name__ == "__main__":
    main()
