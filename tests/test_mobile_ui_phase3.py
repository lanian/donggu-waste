"""휴대폰 화면 개선 3단계 "예외 처리 줄이기" 회귀 (docs/reviews/2026-11-15-mobile-ui-expert-review.md P1 4, P2 10·11·12).

실행: python tests/test_mobile_ui_phase3.py [시험 이름 ...] (CHROME=Chromium 실행 파일, 이름 없으면 전부)
실제 PIN·암호화 IndexedDB를 쓰고, 자료는 모두 가상 자료다. 사유 시트·부재 다음 할 일도 지금과 같은
ITEM_RESULT(사유·수량) → ATTEMPT 기록만 남기는지(이벤트 규격 그대로)까지 확인한다.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from test_review_ui import PIN, mobile_server, unlock
from test_workflow_mobile import daylist, event, open_mobile, seed, stop

passed = 0
ROOT = Path(__file__).resolve().parents[1]
ICON = str(ROOT / "web/icons/icon-192.png")
SHOTS = Path(os.environ.get("DG_TEST_DIR") or tempfile.mkdtemp(prefix="dg-mobile-ui-phase3-")) / "shots"
REASONS = ["배출 안 됨", "접수와 다름", "적재 불가", "주민 요청 연기", "스티커·필증 없음·불일치", "추가 금액 거부", "기타"]


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


def events(page, kind=None):
    return page.evaluate("""async kind => (await import('./core.js')).st.events
        .filter(e => !kind || e.kind === kind).map(e => ({ id: e.event_id, rid: e.request_id, kind: e.kind, body: e.body }))""", kind)


def settle(page):
    page.evaluate("async () => { const C = await import('./core.js'); await C.flushSteps(); await C.saving; }")


def ui_value(page, key):
    return page.evaluate("async k => { const v = (await import('./core.js')).ui[k]; return v === undefined ? null : v; }", key)


def text_size(page, size):
    page.evaluate("async t => { const C = await import('./core.js'); C.st.textSize = t; C.applyTextSize(); await C.render(); }", size)


def before(page, a, b):
    return page.evaluate("""([a, b]) => { const x = document.querySelector(a), y = document.querySelector(b);
        return !!x && !!y && !!(x.compareDocumentPosition(y) & Node.DOCUMENT_POSITION_FOLLOWING); }""", [a, b])


def probe(page, var):
    return page.evaluate("""v => { const e = document.createElement('i'); e.style.color = `var(${v})`; document.body.append(e);
        const c = getComputedStyle(e).color; e.remove(); return c; }""", var)


def set_theme(page, theme, hc=False):
    page.evaluate("async p => { const C = await import('./core.js'); await C.setPrefs(p); await C.render(); }", {"theme": theme, "hc": hc})


def two_items(number, date, qty=(1, 1)):
    s = stop(number, date)
    s["items"] = [{"line_id": f"line-{number}{c}", "name": f"가상 물건 {c}", "spec": "시험용", "unit": "EA", "unit_label": "개",
                   "qty": q, "price_status": "CONFIRMED", "unit_price": 1000, "amount": 1000 * q} for c, q in zip("ab", qty)]
    return s


def open_at(browser, url, when):
    """휴대폰 시계를 정해 둔 채로 연다(마감 점검의 END_OF_DAY_H 시험용). 그 밖은 open_mobile과 같다."""
    ctx = browser.new_context(service_workers="block", viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.clock.install(time=when)
    page.goto(url)
    page.get_by_placeholder("숫자 6자리 이상").fill(PIN)
    page.get_by_placeholder("한 번 더", exact=True).fill(PIN)
    page.get_by_role("button", name="PIN 설정", exact=True).click()
    expect(page.get_by_role("button", name="이 휴대폰만으로 사용")).to_be_visible()
    page.evaluate("""async () => {
        const P = await import('./proto.js');
        const keys = await P.generateDeviceKeys(), pc = await P.generateDeviceKeys();
        window.__workflowDevice = { id: P.randomBytes(16), ...keys };
        window.__workflowPc = { pcKexPub: await P.exportRaw(pc.kex.publicKey), pcSignPub: await P.exportRaw(pc.sign.publicKey) };
    }""")
    return ctx, page


def result_button(page, name):
    return page.locator("#st-result").get_by_role("button", name=name, exact=True)


def fail_state_writes(page, on):
    if on:
        page.evaluate("""() => { window.__put = IDBObjectStore.prototype.put;
            IDBObjectStore.prototype.put = function (value, key) {
                if (key === 's:state') throw new DOMException('가상 저장 공간 부족', 'QuotaExceededError');
                return window.__put.apply(this, arguments); }; }""")
    else:
        page.evaluate("() => { IDBObjectStore.prototype.put = window.__put; }")


# ------------------------------------------------------------ A1. 사유가 빠지면 하단 사유 시트
def reason_sheet(browser, url):
    print("\n— A1. [일부 수거]·[미수거]에서 사유가 빠지면 하단 사유 시트(큰 칩, 모든 품목 같은 사유) —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        seed(page, {"daylist": daylist(today, [two_items(1, today), stop(2, today)]), "events": [event(1, "CONTACT", result="REACHED")]})
        route(page, "#/stop/workflow-1")
        result_button(page, "미수거").click()
        dialog = page.get_by_role("dialog", name="1번 미수거 사유", exact=True)
        expect(dialog).to_be_visible()
        ok(page.locator("#st-result .msg.err").inner_text().strip() == "", "오류 문구 대신 하단 시트 '1번 미수거 사유'")
        everyone = dialog.get_by_role("group", name="모든 품목 같은 사유", exact=True)
        ok(everyone.count() == 1 and before(page, "dialog .reason-all", "dialog .reason-item"), "맨 위에 '모든 품목 같은 사유' 칩 줄")
        groups = [dialog.get_by_role("group", name=f"가상 물건 {c} 사유 고르기", exact=True) for c in "ab"]
        ok(all(g.count() == 1 for g in groups), "사유가 필요한 품목마다 사유 칩 묶음")
        for g in [everyone, *groups]:
            names = [b.inner_text().strip() for b in g.get_by_role("button").all()]
            ok(names == REASONS, f"칩 7개는 기존 사유 코드 이름 그대로 ({names})")
        heights = dialog.locator(".reason-chip").evaluate_all("els => els.map(e => e.getBoundingClientRect().height)")
        ok(len(heights) == 21 and min(heights) >= 56, f"사유 칩 21개, 누르는 곳 56px 이상 (최소 {min(heights):.0f}px)")
        save = dialog.get_by_role("button", name="이 사유로 미수거 저장", exact=True)
        ok(save.is_disabled() and dialog.get_by_role("button", name="취소", exact=True).count() == 1, "[이 사유로 미수거 저장]은 처음엔 꺼짐, [취소] 있음")
        groups[0].get_by_role("button", name="적재 불가", exact=True).click()
        ok(save.is_disabled() and groups[0].get_by_role("button", name="적재 불가", exact=True).get_attribute("aria-pressed") == "true",
           "한 품목만 고르면 아직 꺼짐 (고른 칩 aria-pressed)")
        everyone.get_by_role("button", name="주민 요청 연기", exact=True).click()
        ok(all(g.get_by_role("button", name="주민 요청 연기", exact=True).get_attribute("aria-pressed") == "true" for g in groups)
           and groups[0].get_by_role("button", name="적재 불가", exact=True).get_attribute("aria-pressed") == "false" and save.is_enabled(),
           "'모든 품목 같은 사유'를 누르면 모든 품목에 같은 사유, 저장 단추 켜짐")
        groups[1].get_by_role("button", name="기타", exact=True).click()
        page.screenshot(path=str(SHOTS / "phase3-reason-sheet.png"))
        before_ids = {e["id"] for e in events(page)}
        save.click()
        expect(page.locator(".saved-band")).to_contain_text("1번 · 미수거로 저장됨")
        new = [e for e in events(page) if e["id"] not in before_ids]
        ok([e["kind"] for e in new] == ["ITEM_RESULT", "ITEM_RESULT", "ATTEMPT"], f"ITEM_RESULT(사유) → ATTEMPT 순서 ({[e['kind'] for e in new]})")
        ok({e["body"]["line_id"]: (e["body"]["reason"], e["body"]["qty_confirmed"], e["body"]["qty_collected"]) for e in new[:2]}
           == {"line-1a": ("POSTPONED", 1, 0), "line-1b": ("OTHER", 1, 0)} and new[2]["body"]["outcome"] == "NOT_COLLECTED",
           "품목별 사유(수량 그대로)와 미수거 결과를 기록")

        # 취소하면 아무것도 저장하지 않고 품목 칸 사유 표시(aria-invalid)로 돌아감
        route(page, "#/stop/workflow-2")
        result_button(page, "미수거").click()
        dialog = page.get_by_role("dialog", name="2번 미수거 사유", exact=True)
        dialog.get_by_role("button", name="취소", exact=True).click()
        expect(dialog).to_have_count(0)
        settle(page)
        ok(not [e for e in events(page) if e["rid"] == "workflow-2"], "시트에서 [취소] → 기록 없음")
        expect(page.locator("#st-result .msg.err")).to_contain_text("사유를 골라")
        ok(page.get_by_label("가상 의자 못 실은 사유", exact=True).get_attribute("aria-invalid") == "true", "취소하면 품목 칸 사유 강조(aria-invalid)로 안내")

        # 일부 수거: 사유가 필요한 품목만, 한 품목이면 '모든 품목' 줄 없음
        seed(page, {"daylist": daylist(today, [two_items(1, today)]),
                    "events": [event(1, "ITEM_RESULT", line_id="line-1a", qty_confirmed=1, qty_collected=1)]})
        route(page, "#/stop/workflow-1")
        result_button(page, "일부 수거").click()
        dialog = page.get_by_role("dialog", name="1번 일부 수거 사유", exact=True)
        expect(dialog).to_be_visible()
        ok(dialog.get_by_role("group", name="가상 물건 a 사유 고르기", exact=True).count() == 0
           and dialog.get_by_role("group", name="가상 물건 b 사유 고르기", exact=True).count() == 1
           and dialog.locator(".reason-all").count() == 0, "실은 품목은 빼고 사유가 필요한 품목만 (한 품목이면 '모든 품목' 줄 없음)")
        dialog.get_by_role("button", name="적재 불가", exact=True).click()
        before_ids = {e["id"] for e in events(page)}
        dialog.get_by_role("button", name="이 사유로 일부 수거 저장", exact=True).click()
        expect(page.locator(".saved-band")).to_contain_text("1번 · 일부 수거로 저장됨")
        new = [e for e in events(page) if e["id"] not in before_ids]
        ok([(e["kind"], e["body"].get("line_id"), e["body"].get("reason"), e["body"].get("outcome")) for e in new]
           == [("ITEM_RESULT", "line-1b", "CANNOT_LOAD", None), ("ATTEMPT", None, None, "PARTIAL")], "일부 수거: 사유 → ATTEMPT PARTIAL")

        # 저장 실패: 기존 저장 대기·다시 시도 그대로
        seed(page, {"daylist": daylist(today, [two_items(1, today)])})
        route(page, "#/stop/workflow-1")
        fail_state_writes(page, True)
        result_button(page, "미수거").click()
        dialog = page.get_by_role("dialog", name="1번 미수거 사유", exact=True)
        dialog.get_by_role("group", name="모든 품목 같은 사유", exact=True).get_by_role("button", name="적재 불가", exact=True).click()
        dialog.get_by_role("button", name="이 사유로 미수거 저장", exact=True).click()
        expect(page.locator("#st-result .callout.bad")).to_contain_text("수거 기록 저장 대기")
        expect(page.locator(".dock-next")).to_have_text("저장 다시 시도")
        ok(page.locator(".saved-band").count() == 0, "저장 실패면 결과 저장 띠 대신 저장 대기·[저장 다시 시도]")
        fail_state_writes(page, False)
        page.locator(".dock-next").click()
        expect(page.locator("#save-fail")).to_have_count(0)
        expect(page.locator("#st-result .callout.bad")).to_have_count(0)
        ok([e["kind"] for e in events(page)] == ["ITEM_RESULT", "ITEM_RESULT", "ATTEMPT"], "다시 시도하면 같은 기록(사유 2 → 미수거)을 저장")

        # 아주 큰 글씨 360×640
        seed(page, {"daylist": daylist(today, [two_items(1, today)])})
        page.set_viewport_size({"width": 360, "height": 640})
        route(page, "#/stop/workflow-1")
        result_button(page, "미수거").click()
        dialog = page.get_by_role("dialog", name="1번 미수거 사유", exact=True)
        page.screenshot(path=str(SHOTS / "phase3-reason-sheet-360x640.png"))
        dialog.get_by_role("button", name="취소", exact=True).click()
        text_size(page, "xl")
        result_button(page, "미수거").click()
        dialog = page.get_by_role("dialog", name="1번 미수거 사유", exact=True)
        width = page.evaluate("() => document.querySelector('dialog.sheet').scrollWidth <= document.querySelector('dialog.sheet').clientWidth + 1")
        heights = dialog.locator(".reason-chip").evaluate_all("els => els.map(e => e.getBoundingClientRect().height)")
        ok(width and min(heights) >= 56, "아주 큰 글씨 360px: 시트 가로 넘침 없음, 칩 56px 이상")
        page.screenshot(path=str(SHOTS / "phase3-reason-sheet-xl-360x640.png"))
        dialog.get_by_role("button", name="취소", exact=True).click()
        text_size(page, "")
    finally:
        ctx.close()


# ------------------------------------------------------------ A2. 증거 사진: 카메라 바로 열기 → [미수거로 저장]
def photo_direct(browser, url):
    print("\n— A2. 증거 사진 창의 [📷 사진 찍으러 가기]는 카메라를 바로 열고, 저장 뒤 [미수거로 저장] 한 번 —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        evs = [event(1, "CONTACT", result="NO_ANSWER"), event(1, "ITEM_RESULT", line_id="line-1", qty_confirmed=1, qty_collected=0, reason="NOT_PUT_OUT")]
        seed(page, {"daylist": daylist(today, [stop(1, today), stop(2, today)]), "events": evs})
        route(page, "#/stop/workflow-1")
        page.evaluate("() => scrollTo(0, 0)")
        result_button(page, "미수거").click()
        dialog = page.get_by_role("dialog", name="증거 사진이 필요합니다", exact=True)
        expect(dialog).to_be_visible()
        with page.expect_file_chooser() as chooser:
            dialog.get_by_role("button", name="📷 사진 찍으러 가기", exact=True).click()
        fc = chooser.value
        ok(fc.element.get_attribute("id") == "cam-BEFORE", "카메라 입력(#cam-BEFORE)을 바로 엶 (스크롤만 하지 않음)")
        ok(ui_value(page, "photoThen") == {"id": "workflow-1", "outcome": "NOT_COLLECTED"}, "'미수거로 저장하려던 것'을 ui.photoThen 에 기억")
        settle(page)
        ok(not events(page, "ATTEMPT"), "카메라를 여는 것만으로는 결과를 저장하지 않음")
        fc.set_files(ICON)
        then = page.locator(".photo-then")
        expect(then).to_contain_text("사진 저장됨")
        ok(then.get_attribute("role") == "status" and then.get_by_role("button", name="미수거로 저장", exact=True).count() == 1,
           "사진 저장 뒤 '사진 저장됨 — [미수거로 저장]'(role=status)")
        page.screenshot(path=str(SHOTS / "phase3-photo-then.png"))
        then.get_by_role("button", name="미수거로 저장", exact=True).click()
        expect(page.locator(".saved-band")).to_contain_text("1번 · 미수거로 저장됨")
        attempts = events(page, "ATTEMPT")
        ok(len(attempts) == 1 and attempts[0]["body"]["outcome"] == "NOT_COLLECTED" and "no_photo_reason" not in attempts[0]["body"],
           "[미수거로 저장] 한 번으로 ATTEMPT 미수거(사진이 있으니 사진 불가 사유 없음)")
        ok(ui_value(page, "photoThen") is None and page.locator(".photo-then").count() == 0, "저장하면 기억을 지움")

        # 잠그면 기억을 지운다
        route(page, "#/stop/workflow-2")
        page.get_by_role("button", name="부재", exact=True).click()
        result_button(page, "미수거").click()
        sheet = page.get_by_role("dialog", name="2번 미수거 사유", exact=True)
        sheet.get_by_role("button", name="배출 안 됨", exact=True).click()
        sheet.get_by_role("button", name="이 사유로 미수거 저장", exact=True).click()
        with page.expect_file_chooser():
            page.get_by_role("dialog", name="증거 사진이 필요합니다", exact=True).get_by_role("button", name="📷 사진 찍으러 가기", exact=True).click()
        ok(ui_value(page, "photoThen") == {"id": "workflow-2", "outcome": "NOT_COLLECTED"}, "사유 시트 → 증거 사진 → 카메라도 같은 흐름")
        page.evaluate("async () => { await (await import('./core.js')).lockNow(); }")
        unlock(page)
        ok(ui_value(page, "photoThen") is None, "잠그면(onLock) 기억을 지움")
    finally:
        ctx.close()


# ------------------------------------------------------------ A3. 부재 다음 할 일
def absent_next(browser, url):
    print("\n— A3. 연락 결과 '부재' → 다음 할 일 세 개 —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        seed(page, {"daylist": daylist(today, [stop(1, today), stop(2, today), stop(3, today)])})
        route(page, "#/stop/workflow-1")
        ok(page.get_by_role("group", name="부재 다음 할 일").count() == 0, "부재를 고르기 전에는 없음")
        page.get_by_role("button", name="부재", exact=True).click()
        group = page.locator("#st-contact").get_by_role("group", name="부재 다음 할 일", exact=True)
        expect(group).to_be_visible()
        names = [b.inner_text().strip() for b in group.get_by_role("button").all()]
        ok(names == ["물건 나와 있음 → 수거 진행", "물건 없음 → 사진 찍고 미수거", "나중에 다시 → 보류"], f"연락 카드 아래 다음 할 일 세 개 ({names})")
        page.screenshot(path=str(SHOTS / "phase3-absent-next.png"))
        group.get_by_role("button", name="물건 나와 있음 → 수거 진행", exact=True).click()
        page.wait_for_function("() => document.activeElement?.tagName === 'H2' && !!document.activeElement.closest('#st-items')")
        ok(True, "[물건 나와 있음 → 수거 진행] → 품목 칸 제목에 초점")

        group.get_by_role("button", name="물건 없음 → 사진 찍고 미수거", exact=True).click()
        sheet = page.get_by_role("dialog", name="1번 미수거 사유", exact=True)
        expect(sheet).to_be_visible()
        ok(sheet.get_by_role("button", name="배출 안 됨", exact=True).get_attribute("aria-pressed") == "true"
           and sheet.get_by_role("button", name="이 사유로 미수거 저장", exact=True).is_enabled(), "사유 시트가 '배출 안 됨'을 미리 고른 채로 열림")
        sheet.get_by_role("button", name="이 사유로 미수거 저장", exact=True).click()
        evidence = page.get_by_role("dialog", name="증거 사진이 필요합니다", exact=True)
        expect(evidence).to_be_visible()
        ok(evidence.get_by_role("button", name="📷 사진 찍으러 가기", exact=True).count() == 1, "사진이 없으면 증거 사진 단계(A2와 같은 창)")
        evidence.get_by_role("button", name="사진 불가: 어두워 촬영 불가", exact=True).click()
        expect(page.locator(".saved-band")).to_contain_text("1번 · 미수거로 저장됨")
        mine = [e for e in events(page) if e["rid"] == "workflow-1"]
        ok([e["kind"] for e in mine] == ["CONTACT", "ITEM_RESULT", "ATTEMPT"] and mine[1]["body"]["reason"] == "NOT_PUT_OUT"
           and mine[2]["body"] == {**mine[2]["body"], "outcome": "NOT_COLLECTED", "no_photo_reason": "DARK"},
           "CONTACT 부재 → ITEM_RESULT 배출 안 됨 → ATTEMPT 미수거(사진 불가 사유)")
        ok(page.get_by_role("group", name="부재 다음 할 일").count() == 0, "결과를 저장하면 다음 할 일은 숨김")

        route(page, "#/stop/workflow-2")
        page.get_by_role("button", name="부재", exact=True).click()
        page.get_by_role("button", name="나중에 다시 → 보류", exact=True).click()
        hold = page.get_by_role("dialog", name="보류로 저장", exact=True)
        expect(hold).to_contain_text("2번을 보류로 저장합니다. 남은 품목은 재방문 대상이 됩니다.")
        hold.get_by_role("button", name="보류로 저장", exact=True).click()
        expect(page.locator(".saved-band")).to_contain_text("2번 · 보류로 저장됨")
        ok([e["body"]["outcome"] for e in events(page, "ATTEMPT") if e["rid"] == "workflow-2"] == ["ON_HOLD"], "[나중에 다시 → 보류] → 1단계와 같은 확인 창 → 보류")

        # 360×640 기본·아주 큰 글씨 캡처
        page.set_viewport_size({"width": 360, "height": 640})
        route(page, "#/stop/workflow-3")
        page.get_by_role("button", name="부재", exact=True).click()
        expect(page.get_by_role("group", name="부재 다음 할 일", exact=True)).to_be_visible()
        page.evaluate("() => document.querySelector('.absent-next').scrollIntoView({ block: 'center' })")
        page.screenshot(path=str(SHOTS / "phase3-absent-next-360x640.png"))
        text_size(page, "xl")
        page.evaluate("() => document.querySelector('.absent-next').scrollIntoView({ block: 'center' })")
        heights = page.get_by_role("group", name="부재 다음 할 일", exact=True).get_by_role("button").evaluate_all(
            "els => els.map(e => e.getBoundingClientRect().height)")
        ok(min(heights) >= 56 and page.evaluate("() => document.documentElement.scrollWidth <= innerWidth"),
           "아주 큰 글씨 360px: 다음 할 일 단추 56px 이상, 가로 넘침 없음")
        page.screenshot(path=str(SHOTS / "phase3-absent-next-xl-360x640.png"))
        text_size(page, "")
    finally:
        ctx.close()


# ------------------------------------------------------------ B. 상세 화면 순서와 길이
def detail_order(browser, url):
    print("\n— B. 상세: [도착][수거 전][문자], 정보 → 연락 → 품목 → 사진 → 결과 → 계산 → 메모 → 변경 요청, 계산은 한 줄 —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        seed(page, {"daylist": daylist(today, [stop(1, today), stop(2, today)])})
        route(page, "#/stop/workflow-1")
        acts = page.locator(".who .field-acts > *").evaluate_all("els => els.map(e => e.textContent.trim())")
        ok(acts == ["📍 도착", "📷 수거 전", "✉ 문자"], f"정보 카드 동작 줄 세 칸 ({acts})")
        ok(page.locator(".who").get_by_role("button", name="PC 입력 도움", exact=True).count() == 0, "정보 카드에 'PC 입력 도움' 큰 단추 없음")
        with page.expect_file_chooser() as chooser:
            page.locator(".who .field-acts").get_by_text("📷 수거 전", exact=True).click()
        ok(chooser.value.element.get_attribute("id") == "cam-BEFORE", "[📷 수거 전]은 수거 전 사진 카메라를 바로 엶")
        chooser.value.set_files(ICON)
        expect(page.locator(".thumbs img")).to_have_count(1, timeout=15000)
        ok([e["body"]["phase"] for e in events(page, "PHOTO_META")] == ["BEFORE"], "찍은 사진은 수거 전(BEFORE)으로 저장")
        order = [".who", "#st-contact", "#st-items", "#st-photos", "#st-result", ".fee", ".memo-card", ".manage-card"]
        ok(all(before(page, a, b) for a, b in zip(order, order[1:])), "순서: 정보 → 연락 → 품목 → 사진 → 결과 → 계산 → 메모 → 변경 요청")
        manage = page.locator(".manage-card")
        ok(manage.get_by_role("heading", name="접수 변경 요청", exact=True).count() == 1
           and manage.get_by_role("button", name="PC 입력 도움", exact=True).count() == 1, "PC 입력 도움은 맨 아래 '접수 변경 요청' 카드 안")
        ok(page.locator(".memo-card").get_by_label("현장 메모", exact=True).count() == 1 and page.locator("#st-result textarea").count() == 0,
           "현장 메모는 결과 카드에서 빠져 따로")
        labels = [a.get_attribute("aria-label") for a in page.locator(".steps .step").all()]
        ok(labels == ["1단계 연락, 남음", "2단계 품목, 남음", "3단계 사진, 완료", "4단계 결과, 남음"], f"처리 단계 표시 그대로 ({labels})")
        for i, sid in enumerate(["st-contact", "st-items", "st-photos", "st-result"]):
            page.locator(".steps .step").nth(i).click()
            page.wait_for_function("sid => document.activeElement?.tagName === 'H2' && !!document.activeElement.closest('#' + sid)", arg=sid)
        ok(True, "단계 링크 4개가 새 순서의 같은 구역 제목으로 이동·초점")

        fee = page.locator("details.fee")
        summary = fee.locator("summary")
        ok(fee.count() == 1 and fee.get_attribute("open") is None, "접수 대비 증감·대기·추가 금액 안내가 없으면 계산 내역은 접힘")
        st = summary.inner_text().replace("\n", " ")
        ok("총 수수료 1,000원 · 접수 대비 +0원" in st and "자세히" in st, f"한 줄 요약 '총 수수료 1,000원 · 접수 대비 +0원 ▸ 자세히' ({st})")
        h = summary.evaluate("e => e.getBoundingClientRect().height")
        ok(h >= 56, f"요약 줄 높이 ≥ 누르는 곳 (--tap) ({h:.0f}px)")
        ok(not fee.locator(".total-box").is_visible(), "접힌 동안 자세한 표는 안 보임")
        summary.click()
        expect(fee.locator(".total-box")).to_be_visible()
        ok(fee.get_attribute("open") is not None, "요약 줄을 누르면 펼침")
        page.screenshot(path=str(SHOTS / "phase3-detail-order.png"), full_page=True)

        # 증감·대기·추가 금액 안내가 있으면 처음부터 펼침
        add = event(1, "FIELD_ITEM_ADD", line_id="field-1", name="가상 추가 물건", spec="", unit="EA", unit_label="개",
                    price_status="CONFIRMED", unit_price=3000, qty=1)
        seed(page, {"daylist": daylist(today, [stop(1, today)]), "events": [add]})
        route(page, "#/stop/workflow-1")
        ok(page.locator("details.fee").get_attribute("open") is not None and "접수 대비 +3,000원" in page.locator("details.fee summary").inner_text()
           and page.get_by_role("button", name="추가 금액 3,000원 안내 기록", exact=True).is_visible(), "접수 대비 +3,000원(추가 금액 안내 필요) → 처음부터 펼침")
        pend = stop(1, today)
        pend["items"][0].update(price_status="PENDING", unit_price=None, amount=0)
        seed(page, {"daylist": daylist(today, [pend])})
        route(page, "#/stop/workflow-1")
        ok(page.locator("details.fee").get_attribute("open") is not None and "미확정" in page.locator("details.fee summary").inner_text(),
           "금액 확인 대기가 있으면 펼침, 요약은 '미확정'")

        # 단독 방식: PC 입력 도움은 아래 '접수 관리' 카드 안(기존처럼 개인정보 정리 전이면 보임)
        seed(page, {"mode": "SOLO", "requests": [stop(1, today)], "lastBackupAt": None}, registered=False)
        route(page, "#/stop/workflow-1")
        ok(page.locator(".manage-card").get_by_role("heading", name="접수 관리", exact=True).count() == 1
           and page.locator(".manage-card").get_by_role("button", name="PC 입력 도움", exact=True).count() == 1
           and page.locator(".who").get_by_role("button", name="PC 입력 도움").count() == 0, "단독: PC 입력 도움은 '접수 관리' 카드 안")
    finally:
        ctx.close()


# ------------------------------------------------------------ C. 품목 수량 한 줄
def qty_one_line(browser, url):
    print("\n— C. 품목 수량: '실은 수량 [− n +] / 현장 m개' 한 줄, 현장 수량은 '수량이 접수와 다름'으로 펼침 —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        seed(page, {"daylist": daylist(today, [stop(1, today), two_items(2, today, (2, 1))])})
        route(page, "#/stop/workflow-1")
        line = page.locator(".line").first
        got = line.locator(".row.got")
        ok("실은 수량" in got.inner_text() and got.locator(".qty-of").inner_text().strip() == "/ 현장 1개"
           and got.get_by_role("group", name="가상 의자 실은 수량", exact=True).count() == 1, "기본 한 줄: '실은 수량 [− 0 +] / 현장 1개'")
        confirmed = line.get_by_role("group", name="가상 의자 현장 수량", exact=True)
        ok(not confirmed.is_visible() and line.get_by_text("접수 수량", exact=True).count() == 0, "현장 수량 스테퍼·접수 수량 줄은 기본으로 안 보임")
        toggle = line.get_by_role("button", name="수량이 접수와 다름", exact=True)
        ok(toggle.get_attribute("aria-expanded") == "false", "작은 단추 '수량이 접수와 다름'(aria-expanded=false)")
        page.screenshot(path=str(SHOTS / "phase3-qty-one-line.png"))
        toggle.click()
        expect(confirmed).to_be_visible()
        ok(toggle.get_attribute("aria-expanded") == "true", "누르면 현장 수량 스테퍼를 펼침")
        line.get_by_role("button", name="가상 의자 현장 수량 더하기", exact=True).click()
        page.wait_for_function("async () => (await import('./core.js')).st.events.some(e => e.kind === 'ITEM_RESULT' && e.body.qty_confirmed === 2)")
        settle(page)
        line = page.locator(".line").first
        expect(line.get_by_role("group", name="가상 의자 현장 수량", exact=True)).to_be_visible()
        of = line.locator(".qty-of")
        ok(of.inner_text().strip() == "/ 현장 2개(접수 1)" and "diff" in of.get_attribute("class").split()
           and of.evaluate("e => getComputedStyle(e).color") == probe(page, "--warn"), "다르면 '현장 2개(접수 1)' 경고색 강조")
        e = events(page, "ITEM_RESULT")[-1]["body"]
        ok(e["qty_confirmed"] == 2 and e["qty_collected"] == 0 and "2,000원" in line.locator(".amt").inner_text(), "이벤트·금액 계산 그대로 (2개 × 1,000원)")
        route(page, "#/home")
        route(page, "#/stop/workflow-1")
        ok(page.locator(".line").first.get_by_role("group", name="가상 의자 현장 수량", exact=True).is_visible()
           and page.locator(".line").first.get_by_role("button", name="수량이 접수와 다름").count() == 0, "접수와 이미 다르면 처음부터 펼침(접기 단추 없음)")
        # 실은 수량·못 실은 사유·스티커는 그대로
        line = page.locator(".line").first
        line.get_by_role("button", name="가상 의자 실은 수량 더하기", exact=True).click()
        settle(page)
        page.wait_for_function("async () => (await import('./core.js')).st.events.some(e => e.kind === 'ITEM_RESULT' && e.body.qty_collected === 1)")
        line = page.locator(".line").first
        ok(line.locator(".row.left .val").inner_text().strip() == "1 개" and line.get_by_label("가상 의자 못 실은 사유", exact=True).count() == 1
           and page.locator("#st-result .sticker-options:not([open]) .chk").count() == 1, "못 실은 수량·사유 칸 유지, 스티커는 결과 아래 기본 접힘")
        # 현장 추가 품목은 접수 수량이 없어 현장 수량 스테퍼를 늘 보임
        add = event(1, "FIELD_ITEM_ADD", line_id="field-1", name="가상 추가 물건", spec="", unit="EA", unit_label="개",
                    price_status="CONFIRMED", unit_price=3000, qty=1)
        seed(page, {"daylist": daylist(today, [stop(1, today)]), "events": [add]})
        route(page, "#/stop/workflow-1")
        field = page.locator(".line.field")
        ok(field.get_by_role("group", name="가상 추가 물건 현장 수량", exact=True).is_visible()
           and field.get_by_role("button", name="수량이 접수와 다름").count() == 0, "현장 추가 품목: 현장 수량 스테퍼 그대로 보임")

        # 상자 중첩 줄이기: 실은 수량 줄 바탕·스테퍼 바탕/고리·품목 테두리 없음, 품목 사이 구분선 하나
        def boxes():
            return page.evaluate("""() => { const line = document.querySelector('.line.orig'), got = line.querySelector('.row.got'), stp = got.querySelector('.stepper');
                const L = getComputedStyle(line), G = getComputedStyle(got), S = getComputedStyle(stp);
                return { gotBg: G.backgroundColor, stepBg: S.backgroundColor, stepRing: S.boxShadow,
                         lineTop: parseFloat(L.borderTopWidth), lineLeft: parseFloat(L.borderLeftWidth), lineBottom: parseFloat(L.borderBottomWidth),
                         lineBottomColor: L.borderBottomColor, lineBg: L.backgroundColor }; }""")
        seed(page, {"daylist": daylist(today, [two_items(1, today)])})
        for theme, hc in (("light", False), ("dark", False), ("light", True), ("dark", True)):
            set_theme(page, theme, hc)
            route(page, "#/stop/workflow-1")
            b = boxes()
            ok(b["gotBg"] == "rgba(0, 0, 0, 0)" and b["stepBg"] == "rgba(0, 0, 0, 0)" and b["stepRing"] == "none"
               and b["lineTop"] == 0 and b["lineLeft"] == 0 and b["lineBottom"] >= 1 and b["lineBottomColor"] not in ("rgba(0, 0, 0, 0)", b["lineBg"]),
               f"{theme}{' 고대비' if hc else ''}: 수량 영역 상자 없이 품목 사이 구분선 하나 ({b})")
        set_theme(page, "light")
        page.set_viewport_size({"width": 360, "height": 640})
        route(page, "#/stop/workflow-1")
        page.evaluate("() => document.querySelector('.line .row.got').scrollIntoView({ block: 'center' })")
        page.screenshot(path=str(SHOTS / "phase3-qty-one-line-360x640.png"))
        text_size(page, "xl")
        page.evaluate("() => document.querySelector('.line .row.got').scrollIntoView({ block: 'center' })")
        ok(page.evaluate("() => document.documentElement.scrollWidth <= innerWidth"), "아주 큰 글씨 360px: 가로 넘침 없음")
        page.screenshot(path=str(SHOTS / "phase3-qty-one-line-xl-360x640.png"))
        text_size(page, "")
    finally:
        ctx.close()


def fee_folded_shots(browser, url):
    print("\n— B. 접힌 계산 내역 캡처 (360×640 기본·아주 큰 글씨) —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        seed(page, {"daylist": daylist(today, [stop(1, today)])})
        page.set_viewport_size({"width": 360, "height": 640})
        for size, name in (("", "phase3-fee-folded-360x640.png"), ("xl", "phase3-fee-folded-xl-360x640.png")):
            text_size(page, size)
            route(page, "#/stop/workflow-1")
            page.evaluate("() => document.querySelector('details.fee').scrollIntoView({ block: 'center' })")
            h = page.locator("details.fee summary").evaluate("e => e.getBoundingClientRect().height")
            ok(page.locator("details.fee").get_attribute("open") is None and h >= 56, f"{size or '보통'}: 계산 내역 한 줄로 접힘 ({h:.0f}px)")
            page.screenshot(path=str(SHOTS / name))
        text_size(page, "")
    finally:
        ctx.close()


# ------------------------------------------------------------ D1. 결과 보내기 단계 카드
def export_steps(browser, url):
    print("\n— D1. 결과 보내기: 카드 ①~⑤가 단계와 1:1, 지금 단계만 펼침 —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        evs = [event(1, "ATTEMPT", outcome="DONE"), event(1, "FIELD_MEMO", text="가상 현장 메모")]
        seed(page, {"daylist": daylist(today, [stop(1, today)]), "events": evs})
        route(page, "#/export")
        cards = page.locator("details.step-card")
        ok(cards.count() == 5, "단계 카드 다섯 장")
        nums = cards.locator("summary .stepno").evaluate_all("els => els.map(e => e.textContent.trim())")
        ok(nums == ["1", "2", "3", "4", "5"], f"카드 번호 1·2·3·4·5 ({nums})")
        heads = [c.locator("summary h2").inner_text().replace("\n", " ") for c in cards.all()]
        ok("결과 보내기" in heads[0] and "D-코드 넣기" in heads[1] and "사진 보내기" in heads[2] and "P-코드 넣기" in heads[3] and "정리" in heads[4],
           f"① 결과 보내기 ② D-코드 넣기 ③ 사진 보내기 ④ P-코드 넣기 ⑤ 정리 ({heads})")
        opened = cards.evaluate_all("els => els.map(e => e.open)")
        ok(opened == [True, False, False, False, False], f"지금 단계(①)만 펼침 ({opened})")
        states = cards.locator(".step-state").evaluate_all("els => els.map(e => e.textContent.trim())")
        ok(states[0] == "지금" and states[1:] == ["대기"] * 4, f"접힌 카드는 한 줄 요약(대기) ({states})")
        first = cards.nth(0)
        ok(first.locator(".send-lead").inner_text().strip() == "PC만 열 수 있게 암호화해 업무 메일로 보냅니다."
           and first.locator(".send-lead + .small").inner_text().strip() == "메모에 이름·연락처 등 개인정보를 적지 마세요.",
           "①의 설명은 한 줄 + 작은 글씨 개인정보 안내")
        more = first.locator("details.send-more")
        ok(more.count() == 1 and more.get_attribute("open") is None and more.locator("summary").inner_text().strip() == "자세히",
           "나머지 설명은 [자세히] 안 (접힘)")
        expect(first.locator(".callout.warn.free-text")).to_contain_text("1건")
        ok(True, "이번 파일의 직접 쓴 글 건수 알림은 그대로 보임")
        more.locator("summary").click()
        expect(more).to_contain_text("지금까지 결과")
        ok(cards.nth(3).get_by_label("사진 회수 코드 (P-)", exact=True).count() == 1 and cards.nth(2).get_by_label("사진 회수 코드 (P-)").count() == 0,
           "P-코드 입력은 ③에서 ④로 분리")
        ok("primary" in page.locator(".footbar .btn").get_attribute("class").split(), "① 단계에서는 하단 [결과 파일 만들어 보내기]가 주 단추")
        cards.nth(3).locator("summary").click()
        expect(cards.nth(3).get_by_label("사진 회수 코드 (P-)", exact=True)).to_be_visible()
        ok(True, "접힌 카드도 눌러 펼칠 수 있음")
        page.set_viewport_size({"width": 360, "height": 640})
        route(page, "#/export")
        page.evaluate("() => scrollTo(0, 0)")
        page.screenshot(path=str(SHOTS / "phase3-export-steps-360x640.png"))
        text_size(page, "xl")
        ok(page.evaluate("() => document.documentElement.scrollWidth <= innerWidth"), "아주 큰 글씨 360px: 가로 넘침 없음")
        page.evaluate("() => scrollTo(0, 0)")
        page.screenshot(path=str(SHOTS / "phase3-export-steps-xl-360x640.png"))
        text_size(page, "")
        page.set_viewport_size({"width": 390, "height": 844})

        # 보낸 뒤 D-코드 대기: ② 만 펼침, ① ✓ 완료
        exp = [{"from": 1, "to": 2, "code": "ABCDEFGH", "name": "DG_R_가상_1-2.dgr", "at": "2026-01-01T00:00:00Z", "confirmed": False}]
        seed(page, {"daylist": daylist(today, [stop(1, today)]), "events": evs, "exports": exp})
        route(page, "#/export")
        cards = page.locator("details.step-card")
        ok(cards.evaluate_all("els => els.map(e => e.open)") == [False, True, False, False, False]
           and cards.nth(0).locator(".step-state").inner_text().strip() == "✓ 완료"
           and cards.nth(1).get_by_label("결과 반영 코드 (D-)", exact=True).is_visible(), "D-코드 대기: ②만 펼침, ①은 '✓ 완료'")
        ok("primary" not in page.locator(".footbar .btn").get_attribute("class").split(), "① 단계가 아니면 하단 결과 파일 단추는 주 단추가 아님")

        # 모두 반영·사진 없음: ⑤ 정리만 펼치고, 조건을 채우면 [오늘 자료 정리]가 주 단추
        seed(page, {"daylist": daylist(today, [stop(1, today)]), "events": [event(1, "ATTEMPT", outcome="DONE")], "releasedSeq": 1,
                    "exports": [{"from": 1, "to": 1, "code": "TEST0001", "confirmed": True}]})
        route(page, "#/export")
        cards = page.locator("details.step-card")
        clean = page.get_by_role("button", name="오늘 자료 정리", exact=True)
        expect(clean).to_be_enabled()
        ok(cards.evaluate_all("els => els.map(e => e.open)") == [False, False, False, False, True]
           and "primary" in clean.get_attribute("class").split(), "정리 단계만 펼침, 조건을 채운 [오늘 자료 정리]는 주 단추")

        # 미회수 사진을 남긴 채 정리할 수 있으면: 지금 단계(③ 사진 보내기)와 함께 ⑤ 정리도 펼침
        photo = {"photo_id": "keep-photo", "request_id": "workflow-1", "phase": "BEFORE", "sha256": "fixture", "bytes": 3}
        seed(page, {"daylist": daylist(today, [stop(1, today)]), "events": [event(1, "ATTEMPT", outcome="DONE")], "releasedSeq": 1,
                    "exports": [{"from": 1, "to": 1, "code": "TEST0001", "confirmed": True}], "keptPhotos": [photo]})
        route(page, "#/export")
        cards = page.locator("details.step-card")
        expect(page.get_by_role("button", name="오늘 자료 정리", exact=True)).to_be_enabled()
        ok(cards.evaluate_all("els => els.map(e => e.open)") == [False, False, True, False, True]
           and cards.nth(4).locator(".step-state").inner_text().strip() == "정리 가능", "사진을 남긴 채 정리할 수 있으면 ③과 함께 ⑤ '정리 가능'도 펼침")
    finally:
        ctx.close()


# ------------------------------------------------------------ D2. 마감 점검
def close_check(browser, url):
    print("\n— D2. 결과 보내기 맨 위 마감 점검(막지 않음): 결과 미기록·하차 0회·저장 대기 사진 —")
    # 18:30 KST (END_OF_DAY_H 17 이후) → 펼쳐 둠
    ctx, page = open_at(browser, url, "2026-11-16T09:30:00Z")
    try:
        today = today_of(page)
        s1 = stop(1, today, unload_after={"label": "가상 반입지", "eta": "11:00", "volume_m3": 3, "minutes": 20})
        evs = [event(1, "ITEM_RESULT", line_id="line-1", qty_confirmed=1, qty_collected=1), event(1, "ATTEMPT", outcome="DONE")]
        seed(page, {"daylist": daylist(today, [s1, stop(2, today), stop(3, today)]), "events": evs})
        page.evaluate("""async () => { const C = await import('./core.js');
            C.pendingPhotos.push({ stopId: 'workflow-2', phase: 'BEFORE', file: null, at: new Date().toISOString() }); }""")
        route(page, "#/export")
        box = page.locator(".close-check")
        ok(box.count() == 1 and before(page, ".close-check", "details.step-card"), "결과 보내기 맨 위에 마감 점검")
        ok(box.evaluate("e => e.tagName") == "DETAILS" and box.get_attribute("open") is not None and "callout" in box.get_attribute("class").split(),
           "END_OF_DAY_H 이후에는 펼쳐 둠 (.callout)")
        text = box.inner_text()
        ok("결과 미기록 2곳" in text and "하차 기록 0회" in text and "저장 대기 사진 1장" in text, f"결과 미기록 2곳 · 하차 기록 0회 · 저장 대기 사진 1장 ({text})")
        jumps = box.locator(".close-jumps").get_by_role("button")
        ok([b.inner_text().strip() for b in jumps.all()] == ["2번", "3번"] and jumps.nth(0).get_attribute("aria-label") == "2번 결과 기록하러 가기",
           "번호별 바로 가기 [2번][3번]")
        ok(page.get_by_role("button", name="결과 파일 만들어 보내기 (2건)", exact=True).is_enabled(), "막지 않는 안내: 결과 파일은 그대로 만들 수 있음")
        page.screenshot(path=str(SHOTS / "phase3-close-check.png"))
        page.set_viewport_size({"width": 360, "height": 640})
        route(page, "#/export")
        page.evaluate("() => scrollTo(0, 0)")
        page.screenshot(path=str(SHOTS / "phase3-close-check-360x640.png"))
        text_size(page, "xl")
        ok(page.evaluate("() => document.documentElement.scrollWidth <= innerWidth"), "아주 큰 글씨 360px: 가로 넘침 없음")
        page.evaluate("() => scrollTo(0, 0)")
        page.screenshot(path=str(SHOTS / "phase3-close-check-xl-360x640.png"))
        text_size(page, "")
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator(".close-check .close-jumps").get_by_role("button", name="2번 결과 기록하러 가기", exact=True).click()
        expect(page).to_have_url(re.compile(r"#/stop/workflow-2$"))
        route(page, "#/export")
        page.locator(".close-check").get_by_role("button", name="하차 기록", exact=True).click()
        expect(page).to_have_url(re.compile(r"#/unload$"))
        page.evaluate("async () => { (await import('./core.js')).pendingPhotos.length = 0; }")

        # 모두 이상 없음: 한 줄
        done = evs + [event(n, "ATTEMPT", outcome="DONE") for n in (2, 3)] + [{"request_id": "", "kind": "UNLOAD", "body": {"site": "가상 반입지"}}]
        seed(page, {"daylist": daylist(today, [s1, stop(2, today), stop(3, today)]), "events": done})
        route(page, "#/export")
        box = page.locator(".close-check")
        ok(box.inner_text().strip() == "✓ 마감 점검 이상 없음" and "ok" in box.get_attribute("class").split(), "모두 문제 없으면 '✓ 마감 점검 이상 없음' 한 줄")
        # 하차 계획도 수거도 없으면 하차 점검 안 함
        seed(page, {"daylist": daylist(today, [stop(1, today)]), "events": [event(1, "ATTEMPT", outcome="NOT_COLLECTED")]})
        route(page, "#/export")
        ok(page.locator(".close-check").inner_text().strip() == "✓ 마감 점검 이상 없음", "하차 계획이 없고 실은 곳이 없으면 하차 점검 안 함")

        # 단독 방식: 백업 화면 맨 위, 해당하는 것만(결과 미기록·하차·대기 사진)
        solo = [stop(1, today), stop(2, today)]
        seed(page, {"mode": "SOLO", "requests": solo, "events": evs, "lastBackupAt": None}, registered=False)
        route(page, "#/backup")
        box = page.locator(".close-check")
        text = box.inner_text()
        ok(box.count() == 1 and "결과 미기록 1곳" in text and "하차 기록 0회" in text and "사진" not in text,
           f"단독: 백업 화면 맨 위 마감 점검(결과 미기록 1곳 · 하차 기록 0회) ({text})")
    finally:
        ctx.close()

    # 10:00 KST: 접어 두고 요약만
    ctx, page = open_at(browser, url, "2026-11-16T01:00:00Z")
    try:
        today = today_of(page)
        seed(page, {"daylist": daylist(today, [stop(1, today), stop(2, today)])})
        route(page, "#/export")
        box = page.locator(".close-check")
        ok(box.get_attribute("open") is None and "결과 미기록 2곳" in box.locator("summary").inner_text(),
           f"END_OF_DAY_H 전에는 접어 두고 요약만 ({box.locator('summary').inner_text()})")
    finally:
        ctx.close()


# ------------------------------------------------------------ D3·D4. 홈: 하차 줄 [하차 기록], 빈 상태 단추 중복
def home_unload_empty(browser, url):
    print("\n— D3·D4. 홈 하차 줄에 [하차 기록], 목록이 없을 때 [오늘 목록 받기] 중복 줄이기 —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        s1 = stop(1, today, unload_after={"label": "가상 반입지", "eta": "11:00", "volume_m3": 3, "minutes": 20, "lat": 35.1, "lon": 126.9})
        seed(page, {"daylist": daylist(today, [s1, stop(2, today)])})
        row = page.locator(".unload-row")
        expect(row.locator(".btn")).to_have_count(2)
        names = [b.inner_text().strip() for b in row.locator(".btn").all()]
        ok(names == ["길안내", "하차 기록"], f"하차 줄: [길안내] 옆에 [하차 기록] ({names})")
        log = row.get_by_role("button", name="가상 반입지 하차 기록", exact=True)
        h = log.evaluate("e => e.getBoundingClientRect().height")
        ok(log.count() == 1 and h >= 56, f"하차 줄 [하차 기록] 이름 '가상 반입지 하차 기록', 56px 이상 ({h:.0f}px)")
        page.set_viewport_size({"width": 360, "height": 640})
        row.scroll_into_view_if_needed()
        box, page_w = row.bounding_box(), page.evaluate("() => document.documentElement.scrollWidth")
        ok(box["x"] + box["width"] <= 360 + 0.5 and page_w <= 360, "360px: 하차 줄 단추 두 개가 가로로 넘치지 않음")
        page.screenshot(path=str(SHOTS / "phase3-home-unload-360x640.png"))
        page.set_viewport_size({"width": 390, "height": 844})
        log.click()
        expect(page).to_have_url(re.compile(r"#/unload$"))
        ok(True, "[하차 기록] → 하차 기록 화면")

        seed(page)
        expect(page.locator(".next-action .btn.primary")).to_have_text("오늘 목록 받기")
        expect(page.locator(".empty")).to_have_text("오늘 목록이 없습니다. PC 담당자의 QR을 받으세요.")
        count = page.get_by_role("button", name="오늘 목록 받기", exact=True).count()
        ok(page.locator(".empty-actions").count() == 0 and count == 2, f"지금 할 일이 같은 동작이면 빈 상태 단추는 빼고 문구만 (보이는 단추 {count}개)")
        seed(page, registered=False)
        expect(page.locator(".next-action .btn.primary")).to_have_text("배차 QR로 업무 시작")
        expect(page.get_by_role("button", name="기기 등록", exact=True)).to_have_count(0)
        expect(page.get_by_role("button", name="오늘 목록 받기", exact=True)).to_have_count(0)
        guest_name = "배차 QR로 업무 시작"
        expect(page.get_by_role("button", name=guest_name, exact=True)).to_have_count(1)
        guest = page.locator(".next-action").get_by_role("button", name=guest_name, exact=True)
        expect(guest).to_be_enabled()
        guest.click()
        expect(page).to_have_url(url + "#/guest-list")
        expect(page.get_by_text("열쇠 QR을 비추세요", exact=True)).to_be_visible()
        ok(True, "개인폰 홈에서 기기 등록 없이 현장 업무용 배차 QR을 받는다")
    finally:
        ctx.close()


CASES = (reason_sheet, photo_direct, absent_next, detail_order, qty_one_line, fee_folded_shots, export_steps, close_check, home_unload_empty)


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
