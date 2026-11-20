"""모바일 최종 하차 기록과 복귀 길안내의 저장 경계."""
import os

from playwright.sync_api import expect, sync_playwright

from test_mobile_add_save import disk_state, release, writes
from test_review_ui import mobile_server, unlock
from test_workflow_mobile import daylist, event, open_mobile, seed, stop


def final_case(browser, url, *, recorded_result=True, prior_day=False):
    context, page = open_mobile(browser, url)
    day = page.evaluate("import('./core.js').then(C=>C.todayKST(" + ('-1' if prior_day else '0') + '))')
    item = stop(1, day, unload_after={
        'eta': '14:10', 'label': '가상 최종 반입지', 'minutes': 20, 'volume_m3': 0.5,
        'lat': 35.154308, 'lon': 126.926742, 'final': True,
        'stop_count': 1, 'depot': {'lat': 35.155915, 'lon': 126.92974},
    })
    events = [event(1, 'ITEM_RESULT', line_id='line-1', qty_confirmed=1, qty_collected=1),
              event(1, 'ATTEMPT', outcome='DONE')] if recorded_result else []
    seed(page, {'daylist': daylist(day, [item]), 'events': events, 'navApp': 'tmap'})
    page.evaluate("async()=>{window.__C=await import('./core.js');}")
    page.goto(url + '#/unload/final')
    return context, page, day


def save_and_retry(browser, url):
    context, page, day = final_case(browser, url)
    try:
        expect(page.get_by_role('heading', name='최종 하차 기록', exact=True)).to_be_visible(timeout=2000)
        expect(page.get_by_label('반입지', exact=True)).to_have_value('가상 최종 반입지')
        expect(page.get_by_role('button', name='차고지 복귀 길안내', exact=False)).to_have_count(0)
        page.get_by_label('시각', exact=True).fill('14:45')
        page.get_by_label('중량 (kg)', exact=True).fill('17')
        writes(page, fail_at=1)
        save = page.get_by_role('button', name='최종 하차 기록', exact=True)
        save.click()
        retry = page.get_by_role('button', name='저장 다시 시도', exact=True)
        expect(retry).to_be_enabled()
        expect(page.get_by_role('button', name='차고지 복귀 길안내', exact=False)).to_have_count(0)
        unsaved = disk_state(page)
        assert len(unsaved['events']) == 2 and not unsaved.get('finalUnloadReceipt')
        prepared = page.evaluate('()=>structuredClone(window.__C.st.events.at(-1))')
        assert prepared['kind'] == 'UNLOAD'
        page.evaluate('window.__failAt=0')
        retry.click()
        expect(page).to_have_url(url + '#/home')
        durable = disk_state(page)
        assert durable.get('finalUnloadReceipt')
        assert len(durable['events']) == 3 and durable['events'][-1] == prepared
        assert prepared['body'] == {'place': '가상 최종 반입지', 'at_time': '14:45',
            'volume_m3': None, 'weight_kg': 17, 'note': '', 'work_date': day, 'worker': '가상 작업자'}

        page.reload()
        unlock(page)
        page.goto(url + '#/unload/final')
        expect(page.get_by_role('heading', name='차고지 복귀', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='최종 하차 기록', exact=True)).to_have_count(0)
        page.evaluate("()=>{window.__dgExternal=(url)=>{window.__routeTarget=url;}}")
        page.get_by_role('button', name='차고지 복귀 길안내', exact=False).click()
        assert page.evaluate('window.__routeTarget') == (
            'tmap://route?goalx=126.929740&goaly=35.155915&goalname=%EC%B0%A8%EA%B3%A0%EC%A7%80')
        assert disk_state(page)['events'][-1] == prepared
    finally:
        context.close()


def reject_unready_route(browser, url):
    for fixture in ({'recorded_result': False}, {'prior_day': True}):
        context, page, _ = final_case(browser, url, **fixture)
        try:
            expect(page.get_by_role('heading', name='최종 하차 기록', exact=True)).to_be_visible()
            expect(page.get_by_role('button', name='수거 목록으로', exact=True)).to_be_visible()
            expect(page.get_by_role('button', name='최종 하차 기록', exact=True)).to_have_count(0)
            assert not disk_state(page).get('finalUnloadReceipt')
        finally:
            context.close()


def keep_receipt_pending_until_commit(browser, url):
    context, page, day = final_case(browser, url)
    try:
        page.get_by_label('중량 (kg)', exact=True).fill('12')
        writes(page, hold_at=1)
        page.get_by_role('button', name='최종 하차 기록', exact=True).click()
        page.wait_for_function('()=>window.__releases.length===1')
        assert page.evaluate("""async () => {
            const U = await import('./screens/unload.js');
            const state = window.__C.st;
            return U.finalUnloadPending(state, state.finalUnloadReceipt);
        }""")
        # 저장소에는 커밋됐어도 oncomplete가 돌아오기 전에는 완료 안내를 보류한다.
        assert disk_state(page).get('finalUnloadReceipt')
        page.goto(url + '#/home')
        expect(page.get_by_role('button', name='차고지 복귀 길안내', exact=False)).to_have_count(0)
        page.goto(url + '#/unload/final')
        expect(page.get_by_role('button', name='저장 중…', exact=True)).to_be_disabled()
        page.goto(url + '#/settings')
        expect(page.get_by_role('heading', name='설정', exact=True)).to_be_visible()
        release(page)
        page.wait_for_function("""async () => {
            const U = await import('./screens/unload.js');
            const state = window.__C.st;
            return !U.finalUnloadPending(state, state.finalUnloadReceipt);
        }""")
        expect(page).to_have_url(url + '#/settings')
        durable = disk_state(page)
        assert durable.get('finalUnloadReceipt') and [e['kind'] for e in durable['events']].count('UNLOAD') == 1
        # 이미 저장한 최종 하차 이후 새 현장 수거를 마치면 같은 계획이라도 새 하차를 기록한다.
        page.evaluate("""async (visit) => {
            const C = window.__C;
            C.st.fieldStops.push(visit);
            await C.addEvent(visit, 'ITEM_RESULT', { line_id: 'line-2', qty_confirmed: 1, qty_collected: 1 });
            await C.addEvent(visit, 'ATTEMPT', { outcome: 'DONE' });
        }""", stop(2, day, field=True))
        page.goto(url + '#/unload/final')
        expect(page.get_by_role('button', name='최종 하차 기록', exact=True)).to_be_enabled()
        expect(page.get_by_label('중량 (kg)', exact=True)).to_have_value('')
        assert [e['kind'] for e in disk_state(page)['events']].count('UNLOAD') == 1
    finally:
        context.close()


if __name__ == '__main__':
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path)
        try:
            save_and_retry(browser, url)
            reject_unready_route(browser, url)
            keep_receipt_pending_until_commit(browser, url)
        finally:
            browser.close()
    print('PASS: 최종 하차 실제 저장·재시도·재실행과 차고지 복귀 길안내, 미완료·지난 목록 차단')
