"""휴대폰 다음 행동 안내 회귀: 실제 PIN·IndexedDB·암호화 파일 UI.

실행: python tests/test_workflow_mobile.py (CHROME=Chromium 실행 파일)
가상 상태만 사용하며 결과/사진 코드는 실제 암호화 파일을 만든 뒤 화면에서 입력한다.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from test_review_ui import PIN, mobile_server

passed = 0


def ok(condition, message):
    global passed
    if not condition:
        raise AssertionError("실패: " + message)
    passed += 1
    print("  ✓", message, flush=True)


def open_mobile(browser, url):
    ctx = browser.new_context(service_workers="block", viewport={"width": 390, "height": 844})
    page = ctx.new_page()
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


def seed(page, patch=None, registered=True):
    """PIN으로 열린 실제 저장소에 가상 업무 상태를 저장하고 앱 라우터로 그린다."""
    return page.evaluate("""async ({ patch, registered }) => {
        const C = await import('./core.js'), S = await import('./store.js'), F = await import('./field.js');
        if (registered) {
            await S.putRaw('device', window.__workflowDevice);
            await S.putRaw('pc', window.__workflowPc);
        } else { await S.delRaw('device'); await S.delRaw('pc'); }
        const state = { ...C.emptyState(), mode: 'PC', setupHidden: true, storagePersisted: true,
            mapEnabled: false, worker: '가상 작업자', ...patch };
        F.ensureGen(state);
        state.events = state.events.map((e, i) => ({ event_id: crypto.randomUUID(), gen: state.gen,
            seq: i + 1, ts: new Date().toISOString(), req_ver: 1, assign_ver: 1, body: {}, ...e }));
        state.seq = Math.max(state.seq, ...state.events.map(e => e.seq));
        C.setSt(state);
        C.pendingPhotos.length = 0;
        Object.assign(C.ui, { filter: 'ALL', soloView: 'TODAY', stopQ: '', dong: '', reorder: false, receipt: null });
        await C.commit();
        location.hash = '#/home';
        await C.render();
        return { today: C.todayKST(), yesterday: C.todayKST(-1), tomorrow: C.todayKST(1) };
    }""", {"patch": patch or {}, "registered": registered})


def stop(number, date, **extra):
    return {"request_id": f"workflow-{number}", "receipt_no": f"가상-{number}", "seq": number,
            "req_ver": 1, "assign_ver": 1, "status": "ASSIGNED", "planned_date": date,
            "name": f"가상 주민 {number}", "phone": "01000000000", "address": f"가상시 가상동 시험길 {number}",
            "dong": "가상동", "items": [{"line_id": f"line-{number}", "name": "가상 의자", "spec": "시험용",
            "unit": "EA", "unit_label": "개", "qty": 1, "price_status": "CONFIRMED", "unit_price": 1000,
            "amount": 1000}], **extra}


def daylist(date, stops):
    return {"work_date": date, "crew": "가상 수거팀", "assign_ver": 1, "stops": stops}


def event(number, kind="ATTEMPT", **body):
    return {"request_id": f"workflow-{number}", "kind": kind, "body": body or {"outcome": "PARTIAL"}}


def action(page, label):
    card = page.locator(".next-action")
    expect(card).to_have_count(1, timeout=2000)
    expect(card.get_by_role("heading", name="지금 할 일", exact=True)).to_be_visible()
    expect(card.get_by_role("button")).to_have_count(1)
    expect(card.get_by_role("button", name=label, exact=True)).to_be_visible()
    return card.get_by_role("button", name=label, exact=True)


def go_home(page):
    page.evaluate("async () => { location.hash = '#/home'; await (await import('./core.js')).render(); }")


def mobile_workflow_regressions(browser, shots):
    with mobile_server() as url:
        ctx, page = open_mobile(browser, url)
        try:
            dates = seed(page)
            today, yesterday, tomorrow = (dates[k] for k in ("today", "yesterday", "tomorrow"))
            action(page, "배정 목록 수신")
            ok(page.get_by_role("button", name="오늘 목록 받기", exact=True).count() == 1,
               "PC 새 기기의 다음 행동은 목록 수신이며 기존 버튼 이름을 중복하지 않음")

            seed(page, registered=False)
            action(page, "PC 연결 등록하기").click()
            expect(page).to_have_url(url + "#/register")
            go_home(page)
            ok(page.get_by_role("button", name="+ 접수 등록 (현장·전화)", exact=True).is_enabled(),
               "미등록 기기는 등록 안내로 이동하고 현장·전화 접수는 계속 허용")

            stops = [stop(1, today), stop(2, today, dong="가상2동", address="가상시 가상2동 시험길 2")]
            seed(page, {"daylist": daylist(today, stops), "events": [event(1, "CONTACT", result="REACHED")],
                        "localOrder": {"work_date": today, "assign_ver": 1, "ids": ["workflow-2", "workflow-1"]}})
            page.evaluate("""async () => {
                const C = await import('./core.js');
                Object.assign(C.ui, { filter: 'HOLD', stopQ: '가상', dong: '가상동' });
                window.__workflowBefore = JSON.stringify(C.st); await C.render();
            }""")
            action(page, "2번 수거 이어가기").click()
            expect(page).to_have_url(url + "#/stop/workflow-2")
            go_home(page)
            unchanged = page.evaluate("""async () => {
                const C = await import('./core.js');
                return JSON.stringify(C.st) === window.__workflowBefore && C.ui.filter === 'HOLD'
                    && C.ui.stopQ === '가상' && C.ui.dong === '가상동';
            }""")
            ok(unchanged, "미전송 연락 기록이 있어도 재정렬한 다음 수거를 안내하며 검색·필터·업무 상태 보존")
            page.evaluate("""async () => {
                const C = await import('./core.js'); Object.assign(C.ui, { filter: 'ALL', stopQ: '', dong: '' });
                await C.render();
            }""")
            ok(page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "390px 일반 화면의 다음 행동·목록에 가로 넘침 없음")
            page.screenshot(path=str(shots / "mobile-next-collection-390.png"), full_page=True)

            seed(page, {"daylist": daylist(today, stops), "lastReceipt": {"work_date": today,
                        "told": False, "code": "ABCDEFGH", "count": 2, "ver": 1}})
            page.evaluate("async () => { (await import('./core.js')).ui.receipt = { code: '잘못된 임시 코드' }; }")
            action(page, "수신 코드 확인하기").click()
            expect(page.locator(".fp")).to_contain_text("ABCD EFGH")
            go_home(page)
            ok(page.locator(".receipt-card").is_visible() and not page.evaluate("async () => (await import('./core.js')).st.lastReceipt.told"),
               "수신 코드 안내는 같은 R-코드를 다시 열고 PC에 알렸음을 자동 처리하지 않음")

            seed(page, {"daylist": daylist(yesterday, [stop(1, yesterday)]), "events": [event(1)]})
            action(page, "결과 전달·반영 확인").click()
            expect(page).to_have_url(url + "#/export")
            go_home(page)
            ok(page.locator(".day-banner").is_visible() and page.locator(".summary").count() == 0,
               "지난 목록의 미반영 결과부터 안내하고 오늘 진행 표시를 만들지 않음")
            seed(page, {"daylist": daylist(yesterday, [stop(1, yesterday)])})
            action(page, "배정 목록 수신")
            seed(page, {"daylist": daylist(tomorrow, [stop(1, tomorrow)]), "events": [event(1)]})
            action(page, "배정 목록 수신")
            ok(page.locator(".next-action").get_by_text("수거 이어가기", exact=False).count() == 0,
               "결과 없는 지난 목록·미래 목록은 오늘 목록 수신으로 안내하며 미래 수거로 보내지 않음")

            seed(page, {"fieldStops": [stop(1, tomorrow, field=True), stop(2, today, field=True)]})
            action(page, "2번 수거 이어가기")
            ok(True, "PC 배정 목록이 없어도 오늘 현장 접수는 다음 수거로 안내")
            seed(page, {"daylist": daylist(today, [stop(1, today)]), "events": [event(1, "ITEM_RESULT",
                        line_id="line-1", qty_confirmed=1, qty_collected=1)]})
            action(page, "1번 수거 이어가기")
            ok(True, "수량을 모두 입력했어도 방문 결과를 저장하기 전에는 수거 이어가기 유지")
            seed(page, {"daylist": daylist(today, [stop(1, today, removed=True), stop(2, today, status="CANCELLED"),
                        stop(3, today, pii_purged="가상 정리 시각"), stop(4, today)]),
                        "exports": [{"from": 1, "to": 1, "code": "ABCDEFGH", "confirmed": False}]})
            action(page, "4번 수거 이어가기")
            ok(True, "빠진·취소·개인정보 정리 지점은 건너뛰고 미확인 결과 파일이 있어도 남은 오늘 방문부터 안내")

            seed(page, {"daylist": daylist(today, [stop(1, today)]), "events": [event(1)]})
            action(page, "결과 전달·반영 확인").click()
            ok("수거 완료" not in page.locator("main").inner_text(), "일부 수거 결과를 수거 완료로 단정하지 않음")
            with page.expect_download():
                page.get_by_role("button", name="결과 파일 만들어 보내기 (1건)", exact=True).click()
            code = page.evaluate("async () => (await import('./core.js')).st.exports[0].code")
            go_home(page)
            action(page, "결과 전달·반영 확인")
            ok(page.evaluate("async () => (await import('./core.js')).st.releasedSeq") == 0,
               "실제 암호화 결과 파일 생성 뒤에도 D-코드 전에는 PC 반영 확인을 유지")
            action(page, "결과 전달·반영 확인").click()
            page.get_by_placeholder("결과 반영 코드 8자리").fill("R-" + code)
            page.get_by_role("button", name="결과 반영 코드 입력", exact=True).click()
            ok(page.evaluate("async () => (await import('./core.js')).st.releasedSeq") == 0,
               "다른 종류의 코드는 결과 반영으로 처리하지 않음")
            page.locator(".toast.bad").click()  # 오류 안내는 사용자가 닫을 때까지 화면에 남는다.
            page.get_by_placeholder("결과 반영 코드 8자리").fill("D-" + code)
            page.get_by_role("button", name="결과 반영 코드 입력", exact=True).click()
            expect(page.get_by_role("button", name="오늘 자료 정리", exact=True)).to_be_enabled()
            go_home(page)
            action(page, "정리 단계 확인하기")
            ok(page.evaluate("async () => !!(await import('./core.js')).st.daylist"),
               "D-코드 입력 뒤 정리 경로만 안내하고 목록을 자동 삭제하지 않음")

            # 정리 뒤 보존한 사진도 실제 사진 파일·P-코드 확인이 끝날 때까지 안내한다.
            seed(page, {"daylist": daylist(today, []), "keptPhotos": [{"photo_id": "workflow-photo",
                        "request_id": "workflow-1", "phase": "BEFORE", "sha256": "가상 해시"}]})
            page.evaluate("""async () => {
                const C = await import('./core.js'), S = await import('./store.js'), P = await import('./proto.js');
                const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
                const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg'));
                const bytes = new Uint8Array(await blob.arrayBuffer());
                C.st.keptPhotos[0].sha256 = P.toHex(await P.sha256(bytes));
                C.st.keptPhotos[0].bytes = bytes.length;
                await S.putSecureBytes('ph:workflow-photo', bytes); await C.commit();
            }""")
            action(page, "사진 전달·회수 확인").click()
            with page.expect_download():
                page.get_by_role("button", name="사진 파일 만들기 (1장)", exact=True).click()
            pcode = page.evaluate("async () => (await import('./core.js')).st.photoExports[0].code")
            go_home(page)
            action(page, "사진 전달·회수 확인")
            ok(page.evaluate("async () => !(await import('./core.js')).st.recoveredPhotos?.length"),
               "사진 파일 생성만으로 사진 회수나 정리 단계로 넘어가지 않음")
            action(page, "사진 전달·회수 확인").click()
            page.get_by_placeholder("사진 회수 코드 8자리").fill("P-" + pcode)
            page.get_by_role("button", name="사진 회수 코드 입력", exact=True).click()
            go_home(page)
            action(page, "정리 단계 확인하기")
            ok(True, "실제 P-코드 입력 뒤에만 사진 회수 안내를 마치고 기존 정리 단계로 연결")

            seed(page, {"daylist": daylist(today, [stop(1, today)]), "events": [event(1)], "releasedSeq": 1})
            page.evaluate("""async () => {
                const C = await import('./core.js');
                C.pendingPhotos.push({ stopId: 'workflow-1', phase: 'BEFORE', file: new File([], '가상.jpg') });
                await C.render();
            }""")
            action(page, "대기 사진 저장 안내").click()
            expect(page.locator(".pending-photo").get_by_role("button", name="사진 다시 저장", exact=True)).to_be_focused()
            ok(page.evaluate("async () => (await import('./core.js')).pendingPhotos.length") == 1,
               "사진 저장 대기 때 정리·완료를 안내하지 않고 기존 사진 다시 저장 수단 보존")

            for mode, registered in (("PC", True), ("PC", False), ("SOLO", False)):
                seed(page, {"mode": mode, "daylist": daylist(today, [stop(1, today)]) if mode == "PC" else None}, registered)
                page.evaluate("""async () => {
                    const C = await import('./core.js');
                    window.__workflowPut = IDBObjectStore.prototype.put;
                    IDBObjectStore.prototype.put = function(value, key) {
                        if (key === 's:state') throw new DOMException('가상 저장 공간 부족', 'QuotaExceededError');
                        return window.__workflowPut.apply(this, arguments);
                    };
                    await C.addEvent({ request_id: 'workflow-1', req_ver: 1, assign_ver: 1 }, 'CONTACT', { result: 'NO_ANSWER' });
                    await C.render();
                }""")
                action(page, "기록 보호용 내보내기" if mode == "PC" and registered else "기록 보호용 백업").click()
                expect(page).to_have_url(url + ("#/export" if mode == "PC" and registered else "#/backup"))
                ok(page.locator("#save-fail").is_visible(), f"{mode} 등록={registered}: 실제 저장 실패는 기록 보호 경로를 우선하고 실패 알림 유지")
                page.evaluate("""async () => {
                    IDBObjectStore.prototype.put = window.__workflowPut;
                    await (await import('./core.js')).commit();
                }""")

            seed(page, {"mode": "SOLO"}, registered=False)
            action(page, "새 전화 접수 시작").click()
            expect(page).to_have_url(url + "#/intake")
            ok(True, "단독 새 기기는 PC 등록 없이 기존 전화 접수 화면으로 이동")
            seed(page, {"mode": "SOLO", "requests": [stop(1, today), stop(2, today, status="CANCELLED")]}, False)
            action(page, "1번 수거 이어가기").click()
            expect(page).to_have_url(url + "#/stop/workflow-1")
            ok(True, "단독 오늘 수거는 취소 접수를 제외한 상세로 이어짐")
            seed(page, {"mode": "SOLO", "requests": [stop(1, yesterday)]}, False)
            action(page, "미처리 접수 이어가기").click()
            expect(page).to_have_url(url + "#/stop/workflow-1")
            ok(True, "단독 지난 미처리 접수를 오늘 날짜로 바꾸지 않고 원래 상세로 연결")
            seed(page, {"mode": "SOLO", "requests": [stop(1, today)], "events": [event(1)]}, False)
            action(page, "오늘 기록 백업하기")
            ok("PC 반영" not in page.locator(".next-action").inner_text(), "단독 결과 기록 후에는 PC 전송 대신 암호화 백업 안내")
            page.evaluate("async () => { await (await import('./core.js')).setPrefs({ theme: 'light', hc: true }); }")
            expect(page.locator(".toast")).to_have_count(0)
            page.screenshot(path=str(shots / "mobile-next-solo-hc-390.png"), full_page=True)
            ok(page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "390px·야외 고대비 화면에서 가로 넘침 없음")
        finally:
            ctx.close()


def main():
    shots = Path(os.environ.get("DG_TEST_DIR") or tempfile.mkdtemp(prefix="dg-workflow-mobile-")) / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), headless=True)
        try:
            mobile_workflow_regressions(browser, shots)
        finally:
            browser.close()
    print(f"\n통과 {passed}개 · 화면: {shots}")


if __name__ == "__main__":
    main()
