"""휴대폰 검토 결함 회귀: 두 창 전환·잠금 뒤 화면 상태·결과 파일 메모 안내·정리 뒤 임시번호/초안.

실행: python tests/test_mobile_review_fixes.py [시험 이름 ...] (CHROME=Chromium 실행 파일, 이름 없으면 전부)
실제 PIN·암호화 IndexedDB·BroadcastChannel을 쓰고, 자료는 모두 가상 자료다.
"""
from __future__ import annotations

import json
import os
import sys

from playwright.sync_api import expect, sync_playwright

from test_review_ui import PIN, mobile_server, unlock
from test_workflow_mobile import daylist, event, open_mobile, seed, stop

passed = 0


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


def disk_state(page):
    return page.evaluate("async () => (await import('./store.js')).getSecure('state')")


# ------------------------------------------------------------ 1. 두 창 전환
def two_window_handoff(browser, url):
    print("\n— 두 창 전환: 기존 창의 마지막 기록 —")
    ctx, a = open_mobile(browser, url)
    try:
        today = today_of(a)
        seed(a, {"daylist": daylist(today, [stop(1, today)]), "storagePersisted": True})
        # A창: 마지막 수량 기록이 아직 모아 둔 상태(stepFlush). 잠그기 전에 저장되며, 그 저장을 일부러 늦춘다.
        a.evaluate("""async () => {
            const C = await import('./core.js');
            const key = Symbol('slow-final-write');
            C.stepFlush.set(key, async () => {
                C.stepFlush.delete(key);
                await new Promise(resolve => setTimeout(resolve, 800));
                await C.addEvent(C.st.daylist.stops[0], 'ITEM_RESULT', { line_id: 'line-1', qty_confirmed: 1, qty_collected: 1 });
            });
        }""")
        b = ctx.new_page()
        b.goto(url)
        unlock(b)
        expect(a.get_by_placeholder("PIN", exact=True)).to_be_visible(timeout=15000)
        kinds = b.evaluate("async () => (await import('./core.js')).st.events.map(e => e.kind)")
        ok("ITEM_RESULT" in kinds, f"새 창은 기존 창이 잠그며 저장한 마지막 기록까지 읽은 뒤 시작 ({kinds})")
        b.evaluate("""async () => {
            const C = await import('./core.js');
            await C.addEvent(C.st.daylist.stops[0], 'CONTACT', { result: 'REACHED' });
            await C.saving;
        }""")
        stored = [e["kind"] for e in disk_state(b)["events"]]
        ok("ITEM_RESULT" in stored and "CONTACT" in stored, f"새 창의 다음 저장이 기존 창의 마지막 기록을 덮어쓰지 않음 ({stored})")
        ok(not b.evaluate("async () => (await import('./core.js')).saveFailed"), "정상 전환에서는 충돌로 표시하지 않음")
    finally:
        ctx.close()


def normal_path_conflict(browser, url):
    print("\n— 정상 저장 경로의 충돌 감지 —")
    ctx, a = open_mobile(browser, url)
    try:
        today = today_of(a)
        seed(a, {"daylist": daylist(today, [stop(1, today)])})
        # 앱 창이 아닌 같은 출처의 빈 문서에서 저장소에 직접 쓴다(잠금 방송 없이 끼어든 다른 창).
        other = ctx.new_page()
        other.route("**/review-fixes-storage.html", lambda r: r.fulfill(content_type="text/html", body="<main></main>"))
        other.goto(url + "review-fixes-storage.html")
        other.evaluate("""async pin => {
            const S = await import('./store.js');
            await S.unlock(pin);
            const state = await S.getSecure('state');
            state.events.push({ event_id: 'other-window-event', seq: 99, request_id: 'workflow-1', kind: 'CONTACT', body: { result: 'NO_ANSWER' } });
            await S.putSecure('state', state);
        }""", PIN)
        a.evaluate("""async () => {
            const C = await import('./core.js');
            await C.addEvent(C.st.daylist.stops[0], 'CONTACT', { result: 'REACHED' });
        }""")
        stored = [e["event_id"] for e in disk_state(other)["events"]]
        ok("other-window-event" in stored, f"이 창이 읽은 뒤 디스크가 바뀌었으면 덮어쓰지 않음 ({stored})")
        own = a.evaluate("""async () => {
            const C = await import('./core.js'), S = await import('./store.js');
            return { saveFailed: C.saveFailed, pending: S.hasPendingSecure('state'), events: C.st.events.length };
        }""")
        ok(own["saveFailed"] and own["pending"] and own["events"] == 1, f"충돌한 창은 자기 기록을 메모리·대기 암호문으로 보존하고 저장 실패로 표시 ({own})")
        expect(a.locator("#save-fail")).to_contain_text("다른 창에서 기록이 바뀌었습니다")
        ok(True, "충돌 안내(다른 창에서 기록이 바뀌었습니다) 표시")
    finally:
        ctx.close()


# ------------------------------------------------------------ 2. 잠금 뒤 화면 상태
SECRETS = ["가상 잠금 초안", "가상 검색 주민", "가상 정정 이름", "가상 적던 메모", "가상 재방문 메모", "가상 주민 1", "01000000000", "시험길"]


def lock_clears_ui(browser, url):
    print("\n— 잠금 뒤 화면 상태(ui)에 개인정보 없음 —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        seed(page, {"daylist": daylist(today, [stop(1, today)])})
        route(page, "#/intake")
        page.get_by_label("이름", exact=True).fill("가상 잠금 초안")
        page.wait_for_function("import('./core.js').then(C => C.st.intakeDraft?.name === '가상 잠금 초안')")
        route(page, "#/stop/workflow-1")
        page.get_by_label("현장 메모", exact=True).fill("가상 적던 메모")
        route(page, "#/fix/workflow-1")
        page.get_by_label("이름", exact=True).fill("가상 정정 이름")
        page.evaluate("""async () => {
            const C = await import('./core.js');
            Object.assign(C.ui, { filter: 'HOLD', stopQ: '가상 검색 주민', revisit: { 'workflow-1': { note: '가상 재방문 메모' } } });
        }""")
        before = page.evaluate("async () => JSON.stringify((await import('./core.js')).ui)")
        ok(all(s in before for s in ("가상 잠금 초안", "가상 적던 메모", "가상 정정 이름")), "시험 준비: 잠그기 전 화면 상태에 입력 중인 글이 있음")
        page.evaluate("async () => { await (await import('./core.js')).lockNow(); }")
        expect(page.get_by_placeholder("PIN", exact=True)).to_be_visible()
        after = page.evaluate("async () => JSON.stringify((await import('./core.js')).ui)")
        left = [s for s in SECRETS if s in after]
        ok(not left, f"잠그면 화면 상태에서 이름·연락처·주소·메모·검색어를 지움 (남은 것: {left})")
        ok(json.loads(after).get("filter") == "HOLD", "개인정보 없는 표시 설정(목록 필터)은 유지")
        unlock(page)
        route(page, "#/intake")
        expect(page.get_by_label("이름", exact=True)).to_have_value("가상 잠금 초안")
        ok(True, "잠금 해제 뒤 접수 초안은 암호화 저장본에서 복구")
        route(page, "#/stop/workflow-1")
        expect(page.get_by_label("현장 메모", exact=True)).to_have_value("")
        ok(True, "남기지 않은 현장 메모는 잠그면 사라짐(주석대로)")
        route(page, "#/home")
        expect(page.get_by_label("목록에서 찾기", exact=True)).to_have_value("")
        ok(True, "목록 검색어는 잠금 뒤 되살아나지 않음")
    finally:
        ctx.close()


def heap_strings(page, needle):
    """힙 스냅샷(먼저 가비지 수집을 한다)에서 needle 을 담은 문자열 수."""
    cdp = page.context.new_cdp_session(page)
    chunks = []
    cdp.on("HeapProfiler.addHeapSnapshotChunk", lambda e: chunks.append(e["chunk"]))
    cdp.send("HeapProfiler.collectGarbage")
    cdp.send("HeapProfiler.takeHeapSnapshot", {"reportProgress": False})
    cdp.detach()
    return sum(needle in x for x in json.loads("".join(chunks))["strings"])


def lock_drops_failed_memo_text(browser, url):
    print("\n— 저장 실패한 현장 메모: 잠근 뒤 메모리에 원문을 두지 않음 —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        seed(page, {"daylist": daylist(today, [stop(1, today)])})
        route(page, "#/stop/workflow-1")
        page.evaluate("""() => { window.__put = IDBObjectStore.prototype.put;
            IDBObjectStore.prototype.put = function (value, key) { if (key === 's:state') throw new DOMException('가상 저장 공간 부족', 'QuotaExceededError'); return window.__put.apply(this, arguments); }; }""")
        memo = page.get_by_label("현장 메모", exact=True)
        memo.fill("잠금후남으면안되는메모원문")
        page.get_by_role("button", name="메모 남기기", exact=True).click()
        # 화면 글자 검색(get_by_text)은 쓰지 않는다: 브라우저가 마지막 정규식 대상(화면 전체 글)을 붙잡아 힙 검사를 흐린다
        page.wait_for_function("import('./core.js').then(C => C.saveFailed === true)")
        eid = page.evaluate("async () => (await import('./core.js')).st.events.find(e => e.kind === 'FIELD_MEMO').event_id")
        page.evaluate("async () => { await (await import('./core.js')).lockNow(); }")  # 저장은 계속 실패하는 채로 잠근다
        expect(page.get_by_placeholder("PIN", exact=True)).to_be_visible()
        # 떼어 낸 옛 화면 단추의 CSS 전환(누른 효과)이 끝날 때까지는 그 단추가 잠시 메모리에 남는다(브라우저가 곧 놓음)
        page.wait_for_timeout(1500)
        left = heap_strings(page, "잠금후남으면안되는메모원문")
        ok(left == 0, f"잠근 뒤 저장 실패한 메모의 원문이 메모리에 남지 않음 (힙 문자열 {left}개)")
        unlock(page)
        route(page, "#/stop/workflow-1")
        retry = page.get_by_role("button", name="메모 저장 다시 시도", exact=True)
        expect(retry).to_be_visible()
        ok(True, "잠금 해제 뒤에도 저장 대기 메모는 다시 시도할 수 있음")
        page.evaluate("() => { IDBObjectStore.prototype.put = window.__put; }")
        retry.click()
        expect(page.get_by_text("메모 저장 대기", exact=True)).to_have_count(0)
        stored = [e for e in disk_state(page)["events"] if e["kind"] == "FIELD_MEMO"]
        ok(len(stored) == 1 and stored[0]["event_id"] == eid and stored[0]["body"]["text"] == "잠금후남으면안되는메모원문",
           "다시 시도하면 같은 메모(같은 이벤트 ID) 한 건만 저장")
    finally:
        ctx.close()


# ------------------------------------------------------------ 3. 결과 파일 메모 안내
def export_memo_notice(browser, url):
    print("\n— 결과 파일 메모 안내 —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        seed(page, {"daylist": daylist(today, [stop(1, today)]), "events": [event(1, "FIELD_MEMO", text="가상 현장 메모")]})
        route(page, "#/export")
        text = page.locator("main").inner_text()
        ok("이름·연락처·주소·메모가 들어가지 않" not in text, "결과 파일에 메모가 안 들어간다고 잘못 안내하지 않음")
        ok("현장 메모" in text and "개인정보를 적지 마세요" in text, "현장 메모·취소 사유가 암호화되어 PC로 간다고 안내")
        expect(page.locator(".notice.free-text")).to_contain_text("1건")
        ok(True, "이번 파일에 직접 적은 글이 몇 건 들어가는지 알림")
        route(page, "#/stop/workflow-1")
        memo = page.get_by_label("현장 메모", exact=True)
        hint_id = memo.get_attribute("aria-describedby")
        ok(bool(hint_id) and "개인정보를 적지 마세요" in page.locator("#" + hint_id).inner_text(), "현장 메모 입력칸에 개인정보 금지 안내")
        page.get_by_role("button", name="취소 요청", exact=True).click()
        dialog = page.get_by_role("dialog", name="취소 요청", exact=True)
        expect(dialog).to_contain_text("개인정보를 적지 마세요")
        ok(True, "취소 요청 입력칸에 개인정보 금지 안내")
        dialog.get_by_role("button", name="돌아가기", exact=True).click()
    finally:
        ctx.close()


# ------------------------------------------------------------ 4. 정리 뒤 임시번호·초안
def cleanup_state(page, **extra):
    today = today_of(page)
    seed(page, {"daylist": daylist(today, [stop(1, today)]), "events": [event(1)], "releasedSeq": 1,
                "exports": [{"from": 1, "to": 1, "code": "TEST0001", "confirmed": True}], **extra})
    page.evaluate("document.querySelectorAll('.toast').forEach(node => node.remove())")
    route(page, "#/export")
    button = page.get_by_role("button", name="오늘 자료 정리", exact=True)
    expect(button).to_be_enabled()
    return today, button


def cleanup_keeps_counter_and_draft(browser, url):
    print("\n— 정리 뒤 임시번호·작성 중 초안 —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        _, button = cleanup_state(page, receiptCounter={today: 3})
        button.click()
        dialog = page.get_by_role("dialog", name="확인해 주세요", exact=True)
        dialog.get_by_role("button", name="정리하기", exact=True).click()
        expect(page.get_by_text("정리했습니다", exact=True)).to_be_visible()
        current = page.evaluate("async () => (await import('./core.js')).st.receiptCounter")
        ok(current.get(today) == 3 and disk_state(page)["receiptCounter"].get(today) == 3,
           f"오늘 자료 정리 뒤에도 현장 접수 임시번호 순번 유지 ({current})")

        draft = {"name": "가상 작성 중 초안", "phone": "01000000000", "items": []}
        _, button = cleanup_state(page, intakeDraft=draft)
        expect(page.get_by_text("작성 중인 접수 초안", exact=False)).to_be_visible()
        ok(True, "작성 중인 접수 초안이 있으면 정리 단계에 알림")
        button.click()
        dialog = page.get_by_role("dialog", name="확인해 주세요", exact=True)
        expect(dialog).to_contain_text("작성 중인 접수 초안")
        ok(dialog.get_by_role("button", name="정리하기", exact=True).count() == 0, "초안이 있으면 일반 [정리하기]로 조용히 지우지 않음")
        dialog.get_by_role("button", name="취소", exact=True).click()
        expect(dialog).to_have_count(0)
        ok(page.evaluate("async () => (await import('./core.js')).st.intakeDraft?.name") == "가상 작성 중 초안"
           and disk_state(page)["intakeDraft"]["name"] == "가상 작성 중 초안", "확인창을 닫으면 초안과 목록을 그대로 둠")
        button.click()
        dialog = page.get_by_role("dialog", name="확인해 주세요", exact=True)
        dialog.get_by_role("button", name="초안까지 지우고 정리", exact=True).click()
        expect(page.get_by_text("정리했습니다", exact=True)).to_be_visible()
        stored = disk_state(page)
        ok(stored["intakeDraft"] is None and stored["daylist"] is None, "명시적으로 확인했을 때만 초안까지 정리")
    finally:
        ctx.close()


CASES = (two_window_handoff, normal_path_conflict, lock_clears_ui, lock_drops_failed_memo_text, export_memo_notice, cleanup_keeps_counter_and_draft)


def main():
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), headless=True)
        try:
            for case in CASES:
                if not sys.argv[1:] or case.__name__ in sys.argv[1:]:
                    case(browser, url)
        finally:
            browser.close()
    print(f"\n통과 {passed}개")


if __name__ == "__main__":
    main()
