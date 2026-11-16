"""휴대폰 화면 개선 1단계 "현장 실수 막기" 회귀 (docs/reviews/2026-11-15-mobile-ui-expert-review.md P1 1·2·3·9, P2 13).

실행: python tests/test_mobile_ui_phase1.py [시험 이름 ...] (CHROME=Chromium 실행 파일, 이름 없으면 전부)
실제 PIN·암호화 IndexedDB를 쓰고, 자료는 모두 가상 자료다. 결과 바꾸기는 새 ATTEMPT를 덧붙이는 것(기록 삭제 없음)까지 확인한다.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from test_review_ui import mobile_server, unlock
from test_workflow_mobile import daylist, event, open_mobile, seed, stop

passed = 0
SHOTS = Path(os.environ.get("DG_TEST_DIR") or tempfile.mkdtemp(prefix="dg-mobile-ui-phase1-")) / "shots"


def ok(condition, message):
    global passed
    if not condition:
        raise AssertionError("실패: " + message)
    passed += 1
    print("  ✓", message, flush=True)


def today_of(page):
    return page.evaluate("async () => (await import('./core.js')).todayKST()")


def route(page, hash_):
    page.evaluate("async h => { location.hash = h; await (await import('./core.js')).render(); }", hash_)


def card(page, number):
    return page.locator(f'.stop[href="#/stop/workflow-{number}"]')


def events(page, kind=None):
    return page.evaluate("""async kind => (await import('./core.js')).st.events
        .filter(e => !kind || e.kind === kind).map(e => ({ id: e.event_id, rid: e.request_id, kind: e.kind, body: e.body }))""", kind)


def settle(page):
    page.evaluate("async () => { await (await import('./core.js')).saving; }")


def probe(page, var):
    """CSS 변수 색을 계산된 rgb 값으로."""
    return page.evaluate("""v => { const e = document.createElement('i'); e.style.color = `var(${v})`; document.body.append(e);
        const c = getComputedStyle(e).color; e.remove(); return c; }""", var)


def rgb(text):
    return tuple(float(x) for x in re.findall(r"[\d.]+", text)[:3])


def contrast(a, b):
    def lum(c):
        def ch(v):
            v /= 255
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        r, g, bl = (ch(x) for x in c)
        return 0.2126 * r + 0.7152 * g + 0.0722 * bl
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def capture_external(page):
    page.evaluate("() => { window.__opened = []; window.__dgExternal = (u) => { window.__opened.push(u); }; }")


# ------------------------------------------------------------ 1. 결과 미저장 표시
def fixtures_unsaved(today):
    s1 = stop(1, today)
    s1["items"][0].update(qty=3, amount=3000)
    s4 = stop(4, today)
    s4["items"].append({"line_id": "line-4b", "name": "가상 책상", "spec": "시험용", "unit": "SET", "unit_label": "세트",
                        "qty": 2, "price_status": "CONFIRMED", "unit_price": 1000, "amount": 2000})
    records = [s1, stop(2, today), stop(3, today), s4, stop(5, today, revisit=True), stop(6, today)]
    evs = [
        event(1, "ITEM_RESULT", line_id="line-1", qty_confirmed=3, qty_collected=1),   # 수량만: 진행 PARTIAL
        event(2, "ITEM_RESULT", line_id="line-2", qty_confirmed=1, qty_collected=1),   # 수량만: 진행 DONE
        event(3, "ITEM_RESULT", line_id="line-3", qty_confirmed=2, qty_collected=1),
        event(3, "ATTEMPT", outcome="PARTIAL"),                                         # 저장한 일부 수거
        event(4, "ITEM_RESULT", line_id="line-4", qty_confirmed=1, qty_collected=1),
        event(4, "ITEM_RESULT", line_id="line-4b", qty_confirmed=2, qty_collected=1),  # 단위가 섞인 수량만
    ]
    return records, evs


def unsaved_state(browser, url):
    print("\n— 1. 결과 없이 수량만 넣은 곳은 '결과 미저장' + 실은 양 —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        records, evs = fixtures_unsaved(today)
        for mode in ("PC", "SOLO"):
            patch = {"mode": mode, "events": evs, "releasedSeq": len(evs), "lastBackupAt": "2099-01-01T00:00:00Z"}
            patch.update({"daylist": daylist(today, records)} if mode == "PC" else {"requests": records})
            seed(page, patch, registered=mode == "PC")
            expect(card(page, 1).locator(".stop-state")).to_have_text("결과 미저장")
            ok("warn" in card(page, 1).locator(".stop-state").get_attribute("class").split(),
               f"{mode}: 진행 PARTIAL·결과 없음 → '결과 미저장'(경고색), '일부 수거'로 보이지 않음")
            expect(card(page, 1).locator(".stop-loaded")).to_have_text("1/3 실음")
            ok(True, f"{mode}: 실은 양 요약 '1/3 실음'")
            expect(card(page, 2).locator(".stop-state")).to_have_text("결과 미저장")
            expect(card(page, 2).locator(".stop-loaded")).to_have_text("1/1 실음")
            ok(True, f"{mode}: 진행 DONE·결과 없음도 '결과 미저장 · 1/1 실음' (기존 처리 유지)")
            expect(card(page, 3).locator(".stop-state")).to_have_text("일부 수거")
            expect(card(page, 3).locator(".stop-loaded")).to_have_count(0)
            ok(True, f"{mode}: '일부 수거'는 저장한 결과(outcome PARTIAL)에만")
            expect(card(page, 4).locator(".stop-loaded")).to_have_text("1/1개 · 1/2세트 실음")
            ok(True, f"{mode}: 단위가 섞이면 단위별 '1/1개 · 1/2세트 실음'")
            expect(card(page, 6).locator(".stop-state")).to_have_text("방문 전")
            # 남음: 본문색 굵게, 일부 수거·재방문만 경고색
            ink, warn = probe(page, "--ink"), probe(page, "--warn")
            colors = {n: card(page, n).locator(".stop-quantity").evaluate("e => getComputedStyle(e).color") for n in (1, 3, 5, 6)}
            weight = card(page, 6).locator(".stop-quantity").evaluate("e => Number(getComputedStyle(e).fontWeight)")
            ok(colors[6] == ink and colors[1] == ink and weight >= 700, f"{mode}: '남음'은 완료색이 아니라 본문색 굵게 ({colors}, ink {ink})")
            ok(colors[3] == warn and colors[5] == warn, f"{mode}: 일부 수거·재방문인 곳의 '남음'만 경고색 ({colors}, warn {warn})")
            labels = page.evaluate("""async () => {
                const C = await import('./core.js'), F = await import('./fees.js'), L = await import('./screens/stoplist.js');
                return (C.st.daylist?.stops || C.st.requests).map(s => L.stopState(s, F.deriveStop(s, C.st.events)).label);
            }""")
            ok(labels == ["결과 미저장", "결과 미저장", "일부 수거", "결과 미저장", "방문 전", "방문 전"],
               f"{mode}: 판정 함수 stopState 하나가 같은 글자를 냄 ({labels})")
            route(page, "#/find")
            expect(card(page, 1).locator(".stop-state")).to_have_text("결과 미저장")
            expect(card(page, 1).locator(".stop-loaded")).to_have_text("1/3 실음")
            ok(True, f"{mode}: 날짜·접수 찾기 목록도 같은 판정")
            route(page, "#/stop/workflow-1")
            chip = page.locator("main > .bar .state")
            expect(chip).to_contain_text("결과 미저장")
            expect(chip).to_contain_text("1/3 실음")
            ok("warn" in chip.get_attribute("class").split(), f"{mode}: 상세 머리 칩도 '결과 미저장 · 1/3 실음'(경고색)")
            if mode == "PC":
                page.set_viewport_size({"width": 360, "height": 640})
                page.screenshot(path=str(SHOTS / "phase1-unsaved-detail-360.png"))
                page.set_viewport_size({"width": 390, "height": 844})
            route(page, "#/stop/workflow-3")
            expect(page.locator("main > .bar .state")).to_have_text("일부 수거")
            ok(True, f"{mode}: 저장한 일부 수거는 상세 머리 칩에서도 '일부 수거'")
            route(page, "#/home")
            if mode == "PC":
                page.screenshot(path=str(SHOTS / "phase1-unsaved-list.png"), full_page=True)
    finally:
        ctx.close()


# ------------------------------------------------------------ 2. 다음 지점과 하단 바
def dock_texts(page):
    return [b.inner_text().strip() for b in page.locator(".dock .btn").all()]


def next_stop_dock(browser, url):
    print("\n— 2. 다음 지점은 홈 '지금 할 일'과 같은 기준, 하단 바는 모두 다음 지점 —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        records = [stop(n, today, phone=f"0100000000{n}") for n in (1, 2, 3, 4)]
        records[1].update(lat=35.1, lon=126.9)
        evs = [event(1, "ATTEMPT", outcome="NOT_COLLECTED"), event(4, "ATTEMPT", outcome="ON_HOLD")]
        seed(page, {"daylist": daylist(today, records), "events": evs})
        capture_external(page)
        route(page, "#/stop/workflow-3")
        ok(dock_texts(page) == ["←", "전화", "길안내", "수거 결과 선택 ↓"], f"저장 전 하단 바는 지금 지점 그대로 ({dock_texts(page)})")
        page.get_by_role("button", name="전부 실음 · 수거 완료", exact=True).click()
        expect(page.locator(".dock-next")).to_have_text("다음 2번 ▶")
        ok(True, "결과를 저장한 곳(1번 미수거·4번 보류)은 건너뛰고, 마지막 쪽에서 없으면 앞 번호(2번)로 돌아감")
        ok(dock_texts(page) == ["←", "2번 전화", "2번 길안내", "다음 2번 ▶"], f"저장 뒤 하단 바는 모두 다음 지점 ({dock_texts(page)})")
        page.screenshot(path=str(SHOTS / "phase1-dock-next.png"))
        page.locator(".dock").get_by_role("button", name="2번 전화", exact=True).click()
        page.wait_for_function("() => window.__opened.length > 0")
        dial = [e for e in events(page, "DIAL_INTENT")]
        ok(page.evaluate("window.__opened")[-1] == "tel:01000000002" and dial and dial[-1]["rid"] == "workflow-2",
           "[2번 전화]는 기존 dial()로 2번 주민에게 (기록도 2번 접수)")
        page.locator(".dock .dock-nav").click()
        page.wait_for_function("async () => (await import('./core.js')).st.events.some(e => e.kind === 'NAV_INTENT' && e.request_id === 'workflow-2')")
        ok(True, "[2번 길안내]는 2번 지점 좌표로")
        route(page, "#/home")
        expect(page.locator(".next-action").get_by_role("button")).to_have_text("2번 수거 이어가기")
        ok(True, "홈 '지금 할 일'과 상세 '다음 N번'이 같은 지점")
        route(page, "#/stop/workflow-3")
        page.locator(".dock-next").click()
        expect(page).to_have_url(url + "#/stop/workflow-2")
        ok(True, "[다음 2번 ▶]로 2번 상세")

        # 다음 지점이 없으면 [목록으로], 전화는 지금 지점
        seed(page, {"daylist": daylist(today, records[:2]), "events": [event(1, "ATTEMPT", outcome="DONE")]})
        capture_external(page)
        route(page, "#/stop/workflow-2")
        page.get_by_role("button", name="전부 실음 · 수거 완료", exact=True).click()
        expect(page.locator(".dock-next")).to_have_text("목록으로")
        ok(dock_texts(page)[:2] == ["←", "전화"] and page.locator(".dock .dock-nav").count() == 0,
           f"다음 지점이 없으면 [목록으로]이고 전화는 지금 지점 ({dock_texts(page)})")
        page.locator(".dock").get_by_role("button", name="전화", exact=True).click()
        page.wait_for_function("() => window.__opened.length > 0")
        ok(page.evaluate("window.__opened")[-1] == "tel:01000000002", "다음 지점이 없을 때 [전화]는 지금 지점 주민")

        # 다음 지점 번호가 없으면 전화 비활성
        no_phone = [stop(1, today), stop(2, today, phone="")]
        seed(page, {"daylist": daylist(today, no_phone)})
        route(page, "#/stop/workflow-1")
        page.get_by_role("button", name="전부 실음 · 수거 완료", exact=True).click()
        expect(page.locator(".dock-next")).to_have_text("다음 2번 ▶")
        expect(page.locator(".dock").get_by_role("button", name="2번 전화", exact=True)).to_be_disabled()
        ok(True, "다음 지점에 연락처가 없으면 [2번 전화] 비활성")

        # 단독 방식도 같은 기준
        seed(page, {"mode": "SOLO", "requests": records, "events": evs, "lastBackupAt": "2099-01-01T00:00:00Z"}, registered=False)
        route(page, "#/stop/workflow-3")
        page.get_by_role("button", name="전부 실음 · 수거 완료", exact=True).click()
        expect(page.locator(".dock-next")).to_have_text("다음 2번 ▶")
        ok(dock_texts(page) == ["←", "2번 전화", "2번 길안내", "다음 2번 ▶"], f"단독 방식도 같은 다음 지점·하단 바 ({dock_texts(page)})")
    finally:
        ctx.close()


# ------------------------------------------------------------ 3. 저장된 결과·보류 확인·결과 바꾸기
RESULT_NAMES = ("전부 실음 · 수거 완료", "일부 수거", "미수거", "보류")


def result_buttons(page):
    section = page.locator("#st-result")
    return {name: section.get_by_role("button", name=name, exact=True) for name in RESULT_NAMES}


def saved_result_visible(browser, url):
    print("\n— 3. 저장된 결과가 보이고, 보류는 확인, 저장 뒤 [결과 바꾸기] 띠 —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        seed(page, {"daylist": daylist(today, [stop(1, today), stop(2, today)])})
        route(page, "#/stop/workflow-1")
        buttons = result_buttons(page)
        ok("primary" in buttons["전부 실음 · 수거 완료"].get_attribute("class").split()
           and page.locator('#st-result [aria-pressed="true"]').count() == 0, "저장 전에는 지금처럼 큰 주 단추, 선택 표시 없음")

        buttons["보류"].click()
        dialog = page.get_by_role("dialog", name="보류로 저장", exact=True)
        expect(dialog).to_contain_text("1번을 보류로 저장합니다. 남은 품목은 재방문 대상이 됩니다.")
        ok(dialog.get_by_role("button", name="보류로 저장", exact=True).count() == 1, "보류는 저장 전에 확인 창 '보류로 저장'")
        dialog.get_by_role("button", name="취소", exact=True).click()
        expect(dialog).to_have_count(0)
        settle(page)
        ok(not events(page, "ATTEMPT"), "확인 창에서 [취소]하면 보류를 저장하지 않음")
        buttons["보류"].click()
        page.get_by_role("dialog", name="보류로 저장", exact=True).get_by_role("button", name="보류로 저장", exact=True).click()
        band = page.locator(".saved-band")
        expect(band).to_contain_text("1번 · 보류로 저장됨")
        ok(band.get_attribute("role") == "status", "저장 뒤 하단 바 위 띠 '1번 · 보류로 저장됨'(role=status)")
        ok([e["body"]["outcome"] for e in events(page, "ATTEMPT")] == ["ON_HOLD"], "[보류로 저장]을 눌러야 ATTEMPT 보류 저장")
        dock = page.locator(".dock").bounding_box()
        box = band.bounding_box()
        ok(box["y"] + box["height"] <= dock["y"] + 0.5, "띠는 하단 바 바로 위 (겹치지 않음)")

        buttons = result_buttons(page)
        pressed = {k: b.get_attribute("aria-pressed") for k, b in buttons.items()}
        ok(pressed == {"전부 실음 · 수거 완료": "false", "일부 수거": "false", "미수거": "false", "보류": "true"},
           f"저장 뒤 결과 단추 4개는 선택형, 저장된 '보류'만 aria-pressed ({pressed})")
        ok(buttons["보류"].text_content().startswith("✓ ") and not buttons["일부 수거"].text_content().startswith("✓"),
           "저장된 결과 앞머리에 '✓ '")
        style = buttons["보류"].evaluate("e => { const s = getComputedStyle(e); return [s.backgroundColor, s.borderTopColor, parseFloat(s.borderTopWidth)]; }")
        ok(style[0] == probe(page, "--brand-soft") and style[1] == probe(page, "--brand") and style[2] >= 2,
           f"선택 모양: 연초록 바탕 + 2px 브랜드 테두리 ({style})")
        ok(page.locator("#st-result .btn.primary:visible").count() == 0 and page.locator(".dock-next.primary").count() == 1,
           "저장 뒤 결과 카드에 큰 초록 주 단추 없음 — 주 행동은 하단 바 '다음'만")
        hint = page.locator("#st-result h2 .hint")
        ok("ok" in hint.get_attribute("class").split() and "저장됨: 보류" in hint.inner_text()
           and hint.evaluate("e => getComputedStyle(e).color") == probe(page, "--ok-ink"), "'— 저장됨: …'은 완료색(.hint.ok)")
        page.screenshot(path=str(SHOTS / "phase1-saved-band.png"))

        band.get_by_role("button", name="결과 바꾸기", exact=True).click()
        focused = page.evaluate("""() => { const a = document.activeElement, r = a.getBoundingClientRect();
            return { inResult: !!a.closest('#st-result'), tag: a.tagName, top: r.top, bottom: r.bottom, h: innerHeight }; }""")
        ok(focused["inResult"] and focused["tag"] == "H2" and 0 <= focused["top"] and focused["bottom"] <= focused["h"],
           f"[결과 바꾸기]는 결과 카드로 이동해 제목에 초점 ({focused})")
        before = {e["id"] for e in events(page)}
        result_buttons(page)["전부 실음 · 수거 완료"].click()
        expect(page.locator(".saved-band")).to_contain_text("1번 · 수거 완료로 저장됨")
        after = events(page)
        ok(before <= {e["id"] for e in after} and [e["body"]["outcome"] for e in after if e["kind"] == "ATTEMPT"] == ["ON_HOLD", "DONE"],
           "결과 바꾸기는 새 ATTEMPT를 덧붙임 (기존 기록 삭제 없음)")
        ok(result_buttons(page)["전부 실음 · 수거 완료"].get_attribute("aria-pressed") == "true"
           and result_buttons(page)["보류"].get_attribute("aria-pressed") == "false", "바꾼 결과로 선택 표시가 옮겨 감")

        # 저장 실패 띠와 겹치지 않음
        page.evaluate("""async () => {
            const C = await import('./core.js');
            window.__put = IDBObjectStore.prototype.put;
            IDBObjectStore.prototype.put = function (value, key) {
                if (key === 's:state') throw new DOMException('가상 저장 공간 부족', 'QuotaExceededError');
                return window.__put.apply(this, arguments);
            };
            await C.addEvent(C.st.daylist.stops[1], 'CONTACT', { result: 'REACHED' });
        }""")
        expect(page.locator("#save-fail")).to_be_visible()
        fail_box, band_box = page.locator("#save-fail").bounding_box(), page.locator(".saved-band").bounding_box()
        ok(band_box["y"] + band_box["height"] <= fail_box["y"] + 0.5, f"저장 실패 띠(#save-fail)와 겹치지 않음 ({band_box}, {fail_box})")
        page.screenshot(path=str(SHOTS / "phase1-saved-band-with-save-fail.png"))
        page.evaluate("async () => { IDBObjectStore.prototype.put = window.__put; await (await import('./core.js')).commit(); }")
        expect(page.locator("#save-fail")).to_have_count(0)

        page.wait_for_timeout(5300)
        expect(page.locator(".saved-band")).to_have_count(0)
        ok(True, "띠는 5초 뒤 사라짐")

        # 화면을 옮기거나 잠그면 사라짐
        result_buttons(page)["미수거"].click()  # 실은 품목이 있어 막힘 — 띠를 만들지 않음
        expect(page.locator(".saved-band")).to_have_count(0)
        ok(True, "저장하지 못한 선택은 띠를 만들지 않음")
        result_buttons(page)["보류"].click()
        page.get_by_role("dialog", name="보류로 저장", exact=True).get_by_role("button", name="보류로 저장", exact=True).click()
        expect(page.locator(".saved-band")).to_be_visible()
        route(page, "#/home")
        route(page, "#/stop/workflow-1")
        expect(page.locator(".saved-band")).to_have_count(0)
        ok(True, "화면을 옮기면 띠가 사라짐 (돌아와도 다시 뜨지 않음)")
        result_buttons(page)["전부 실음 · 수거 완료"].click()
        expect(page.locator(".saved-band")).to_be_visible()
        page.evaluate("async () => { await (await import('./core.js')).lockNow(); }")
        expect(page.locator(".saved-band")).to_have_count(0)
        unlock(page)
        route(page, "#/stop/workflow-1")
        expect(page.locator(".saved-band")).to_have_count(0)
        ok(True, "잠그면 띠가 사라지고 잠금 해제 뒤에도 다시 뜨지 않음")

        # 결과 재확인 필요: 경고색, 선택형이 아님
        page.evaluate("""async () => { const C = await import('./core.js');
            await C.addEvent(C.st.daylist.stops[0], 'ITEM_RESULT', { line_id: 'line-1', qty_confirmed: 1, qty_collected: 0 }); await C.render(); }""")
        hint = page.locator("#st-result h2 .hint")
        ok("warn" in hint.get_attribute("class").split() and "결과 재확인 필요" in hint.inner_text()
           and hint.evaluate("e => getComputedStyle(e).color") == probe(page, "--warn"), "'결과 재확인 필요'는 경고색(.hint.warn)")
        ok("primary" in result_buttons(page)["전부 실음 · 수거 완료"].get_attribute("class").split()
           and page.locator('#st-result [aria-pressed="true"]').count() == 0, "재확인이 필요하면 저장된 것처럼 선택 표시하지 않음")
    finally:
        ctx.close()


# ------------------------------------------------------------ 4. 고정 영역의 초점 가림 (WCAG 2.4.11)
def tab_obscured(page, presses=45):
    page.evaluate("() => { scrollTo(0, 0); document.activeElement?.blur(); }")
    bad, seen = [], 0
    for _ in range(presses):
        page.keyboard.press("Tab")
        r = page.evaluate("""() => {
            const a = document.activeElement;
            if (!a || a === document.body || a.closest('.dock, .bar, .steps, dialog, .save-fail, .saved-band')) return null;
            const r = a.getBoundingClientRect();
            if (!r.width || !r.height) return null;
            const dock = document.querySelector('.dock')?.getBoundingClientRect().top ?? innerHeight;
            const steps = document.querySelector('.steps');
            const sticky = steps && getComputedStyle(steps).position === 'sticky';
            const top = Math.max(document.querySelector('.bar').getBoundingClientRect().bottom, sticky ? steps.getBoundingClientRect().bottom : 0);
            const text = a.tagName === 'TEXTAREA' || a.tagName === 'INPUT' && !['button', 'checkbox', 'radio', 'file'].includes(a.type);
            return { name: (a.getAttribute('aria-label') || a.textContent || a.tagName).trim().slice(0, 30), text, top: r.top, bottom: r.bottom, limitTop: top, limitBottom: dock };
        }""")
        if not r:
            continue
        seen += 1
        if r["bottom"] - r["top"] > r["limitBottom"] - r["limitTop"]:
            continue
        if r["text"]:
            # 글자 입력칸은 브라우저가 칸 전체가 아니라 입력 위치(첫 줄)를 보이게 한다: 첫 줄 높이만큼은 고정 영역 밖에 있어야 한다.
            visible = min(r["bottom"], r["limitBottom"]) - max(r["top"], r["limitTop"])
            if visible < min(40, r["bottom"] - r["top"]) - 0.5:
                bad.append(r)
        elif r["bottom"] > r["limitBottom"] + 0.5 or r["top"] < r["limitTop"] - 0.5:
            bad.append(r)
    return seen, bad


def focus_not_obscured(browser, url):
    print("\n— 4. 짧은 화면·아주 큰 글씨: 단계 표시 고정 해제, 키보드 초점이 고정 바에 가리지 않음 —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        s1 = stop(1, today)
        s1["items"] += [{"line_id": f"line-1{c}", "name": f"가상 물건 {c}", "spec": "시험용", "unit": "EA", "unit_label": "개",
                         "qty": 2, "price_status": "CONFIRMED", "unit_price": 1000, "amount": 2000} for c in "ab"]
        seed(page, {"daylist": daylist(today, [s1, stop(2, today)])})
        for width, height, text in ((360, 640, "xl"), (360, 720, "xl"), (390, 844, ""), (360, 640, "")):
            page.set_viewport_size({"width": width, "height": height})
            page.evaluate("""async t => { const C = await import('./core.js'); C.st.textSize = t; C.applyTextSize(); }""", text)
            route(page, "#/stop/workflow-1")
            position = page.locator(".steps").evaluate("e => getComputedStyle(e).position")
            if text == "xl":
                ok(position == "static", f"{width}×{height} 아주 큰 글씨: 처리 단계 표시를 고정하지 않음 ({position})")
            elif height > 720:
                ok(position == "sticky", f"{width}×{height} 보통 글씨: 처리 단계 표시는 그대로 고정")
            if (width, height, text) == (360, 640, "xl"):
                page.screenshot(path=str(SHOTS / "phase1-xl-360x640.png"))
            seen, bad = tab_obscured(page)
            ok(seen >= 10 and not bad, f"{width}×{height} 글씨 '{text or '보통'}': Tab 초점 {seen}곳이 앱 바·단계·하단 바에 가리지 않음 ({bad[:3]})")
        page.evaluate("async () => { const C = await import('./core.js'); C.st.textSize = ''; C.applyTextSize(); }")
    finally:
        ctx.close()


# ------------------------------------------------------------ 5. 단추 테두리 대비
def control_contrast(browser, url):
    print("\n— 5. 단추 테두리 --control-edge, 사유 칸 강조와 aria-invalid —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        s1 = stop(1, today)
        s1["items"][0].update(qty=2, amount=2000)
        seed(page, {"daylist": daylist(today, [s1]),
                    "events": [event(1, "ITEM_RESULT", line_id="line-1", qty_confirmed=2, qty_collected=1)]})
        route(page, "#/stop/workflow-1")
        plain = page.get_by_role("button", name="통화 완료", exact=True)

        def edge():
            return plain.evaluate("e => getComputedStyle(e).borderTopColor")

        def set_theme(theme, hc=False):
            page.evaluate("async p => { const C = await import('./core.js'); await C.setPrefs(p); await C.render(); }", {"theme": theme, "hc": hc})

        set_theme("light")
        ok(edge() == "rgb(117, 127, 140)", f"밝게: 고르는 단추 테두리 #757f8c ({edge()})")
        ok(contrast(rgb(edge()), rgb(probe(page, "--card"))) >= 3.0, "밝게: 단추 테두리와 카드 바탕 대비 3:1 이상")
        set_theme("dark")
        ok(edge() == "rgb(125, 135, 148)", f"어둡게: 고르는 단추 테두리 #7d8794 ({edge()})")
        page.emulate_media(color_scheme="dark")
        set_theme("auto")
        ok(edge() == "rgb(125, 135, 148)", f"자동(휴대폰 어둡게): 같은 다크 토큰 ({edge()})")
        page.emulate_media(color_scheme="light")
        set_theme("light", hc=True)
        ok(edge() == "rgb(0, 0, 0)", f"고대비는 기존 검정 테두리 유지 ({edge()})")
        set_theme("light")
        warn_edge = page.evaluate("""async () => { const C = await import('./core.js');
            const b = C.btn('가상 경고 단추', null, 'warn'); document.querySelector('main').append(b);
            const c = getComputedStyle(b).borderTopColor; b.remove(); return c; }""")
        ok(warn_edge == probe(page, "--warn"), f".btn.warn 테두리는 --warn ({warn_edge})")
        ghost = page.locator(".bar .btn.ghost").first.evaluate("e => getComputedStyle(e).borderTopColor")
        ok(ghost == "rgba(0, 0, 0, 0)", f"ghost 단추 테두리는 그대로 투명 ({ghost})")

        select = page.get_by_label("가상 의자 못 실은 사유", exact=True)
        ok(select.get_attribute("aria-invalid") is None, "사유를 고르라고 하기 전에는 aria-invalid 없음")
        page.get_by_role("button", name="일부 수거", exact=True).click()
        # 3단계: 사유가 빠지면 사유 시트가 먼저 열린다. 시트를 닫으면 품목 칸 사유 강조로 안내
        page.get_by_role("dialog", name="1번 일부 수거 사유", exact=True).get_by_role("button", name="취소", exact=True).click()
        expect(page.locator("#st-result .msg.err")).to_contain_text("사유를 골라")
        select = page.get_by_label("가상 의자 못 실은 사유", exact=True)
        ok(select.get_attribute("aria-invalid") == "true", "사유가 빠지면 사유 칸에 aria-invalid=true")
        described = select.get_attribute("aria-describedby")
        ok(bool(described) and page.locator("#" + described).inner_text().strip() == "사유를 고르세요",
           "사유 칸 바로 아래 '사유를 고르세요'를 aria-describedby로 연결")
        need = select.evaluate("e => { const s = getComputedStyle(e); return [s.borderTopColor, parseFloat(s.borderTopWidth)]; }")
        ratio = contrast(rgb(need[0]), rgb(probe(page, "--card")))
        ok(need[0] == probe(page, "--warn") and need[1] >= 2 and ratio >= 3.0,
           f"사유 칸 강조 테두리 --warn 두껍게, 흰 바탕 대비 {ratio:.2f}:1 (3:1 이상)")
        page.screenshot(path=str(SHOTS / "phase1-reason-need.png"))
        select.select_option("CANNOT_LOAD")
        settle(page)
        expect(page.get_by_label("가상 의자 못 실은 사유", exact=True)).not_to_have_attribute("aria-invalid", "true")
        ok(True, "사유를 고르면 aria-invalid가 풀림")
    finally:
        ctx.close()


CASES = (unsaved_state, next_stop_dock, saved_result_visible, focus_not_obscured, control_contrast)


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), headless=True)
        try:
            for case in CASES:
                if not sys.argv[1:] or case.__name__ in sys.argv[1:]:
                    case(browser, url)
        finally:
            browser.close()
    print(f"\n통과 {passed}개 · 화면: {SHOTS}")


if __name__ == "__main__":
    main()
