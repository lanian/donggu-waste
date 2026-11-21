"""모바일 중간·최종 하차 기록과 다음 길안내의 저장 경계."""
import os

from playwright.sync_api import expect, sync_playwright

from test_mobile_add_save import disk_state, release, writes
from test_mobile_final_unload_home import open_nav, picked_up
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


def intermediate_unload_before_next_visit(browser, url):
    context, page = open_mobile(browser, url)
    try:
        today = seed(page)['today']
        first_site = {'label': '가상 첫 반입지', 'lat': 35.15, 'lon': 126.92, 'eta': '10:15',
                      'minutes': 20, 'volume_m3': 2}
        second_site = {**first_site, 'label': '가상 두 번째 반입지', 'lat': 35.16, 'lon': 126.93}
        route = daylist(today, [stop(1, today, unload_after=first_site), stop(2, today),
                                stop(3, today, unload_after=second_site), stop(4, today)])
        seed(page, {'daylist': route, 'events': picked_up(1), 'navApp': 'geo'})
        card = page.get_by_role('region', name='지금 할 일', exact=True)
        expect(card.get_by_role('button', name='중간 하차 기록', exact=True)).to_be_visible(timeout=1500)
        expect(card.get_by_role('button', name='2번 수거 이어가기', exact=True)).to_have_count(0)
        open_nav(page, card.get_by_role('button', name='반입지 길안내 (기본 지도 앱)', exact=True),
                 'geo:35.150000,126.920000?q=35.150000,126.920000(반입지)')

        page.goto(url + '#/stop/workflow-1')
        dock = page.get_by_role('navigation', name='빠른 동작')
        expect(dock.get_by_role('button', name='다음 2번 ▶', exact=True)).to_have_count(0)
        expect(dock.get_by_role('button', name='2번 전화', exact=True)).to_have_count(0)
        dock.get_by_role('button', name='중간 하차 기록', exact=True).click()
        expect(page.get_by_role('heading', name='중간 하차 기록', exact=True)).to_be_visible()
        expect(page.get_by_label('반입지', exact=True)).to_have_value('가상 첫 반입지')
        page.get_by_label('중량 (kg)', exact=True).fill('18')
        writes(page, fail_at=1)
        page.get_by_role('button', name='중간 하차 기록', exact=True).click()
        expect(page.get_by_role('button', name='저장 다시 시도', exact=True)).to_be_enabled()
        prepared = page.evaluate("async () => (await import('./core.js')).st.events.at(-1)")
        assert prepared['kind'] == 'UNLOAD' and len(disk_state(page)['events']) == 2

        page.goto(url + '#/home')
        card.get_by_role('button', name='중간 하차 저장 확인', exact=True).click()
        expect(page.get_by_label('중량 (kg)', exact=True)).to_have_value('18')
        page.evaluate('window.__failAt=0')
        page.get_by_role('button', name='저장 다시 시도', exact=True).click()
        expect(page).to_have_url(url + '#/home')
        expect(card.get_by_role('button', name='2번 수거 이어가기', exact=True)).to_be_visible()
        assert disk_state(page)['events'][-1] == prepared

        page.reload()
        unlock(page)
        expect(card.get_by_role('button', name='2번 수거 이어가기', exact=True)).to_be_visible()
        page.evaluate("""async () => {
            const C = await import('./core.js');
            for (const n of [2, 3]) {
                const s = C.findStop(`workflow-${n}`);
                await C.addEvent(s, 'ITEM_RESULT', {line_id: `line-${n}`, qty_confirmed: 1, qty_collected: 1});
                await C.addEvent(s, 'ATTEMPT', {outcome: 'DONE'});
            }
            await C.render();
        }""")
        open_nav(page, card.get_by_role('button', name='반입지 길안내 (기본 지도 앱)', exact=True),
                 'geo:35.160000,126.930000?q=35.160000,126.930000(반입지)')
        card.get_by_role('button', name='중간 하차 기록', exact=True).click()
        expect(page.get_by_label('반입지', exact=True)).to_have_value('가상 두 번째 반입지')
        assert [e['kind'] for e in disk_state(page)['events']].count('UNLOAD') == 1
        context.set_offline(True)
        page.get_by_label('부피 (m³, 추정)', exact=True).fill('1.2')
        writes(page, hold_at=1)
        page.get_by_role('button', name='중간 하차 기록', exact=True).click()
        page.wait_for_function('()=>window.__releases.length===1')
        page.goto(url + '#/home')
        expect(card.get_by_role('button', name='중간 하차 저장 확인', exact=True)).to_be_visible()
        expect(card.get_by_role('button', name='4번 수거 이어가기', exact=True)).to_have_count(0)
        release(page)
        expect(card.get_by_role('button', name='4번 수거 이어가기', exact=True)).to_be_visible()
        assert [e['kind'] for e in disk_state(page)['events']].count('UNLOAD') == 2
    finally:
        context.close()


def intermediate_unload_plan_bounds(browser, url):
    context, page = open_mobile(browser, url)
    try:
        dates = seed(page)
        today, yesterday = dates['today'], dates['yesterday']
        site = {'label': '가상 좌표 없는 반입지', 'eta': '13:45', 'minutes': 20, 'volume_m3': 1}
        first = stop(1, today, unload_after=site)
        route = daylist(today, [first, stop(2, today)])
        card = page.get_by_role('region', name='지금 할 일', exact=True)
        seed(page, {'daylist': route, 'events': picked_up(1)[:1]})
        expect(card.get_by_role('button', name='1번 수거 이어가기', exact=True)).to_be_visible()
        seed(page, {'daylist': route, 'events': [event(1, 'ATTEMPT', outcome='NOT_COLLECTED')]})
        expect(card.get_by_role('button', name='2번 수거 이어가기', exact=True)).to_be_visible()
        seed(page, {'daylist': route, 'events': [*picked_up(1), {**event(1, 'UNLOAD', place=site['label'],
                    work_date=today, weight_kg=20), 'request_id': ''}]})
        expect(card.get_by_role('button', name='2번 수거 이어가기', exact=True)).to_be_visible()
        seed(page, {'daylist': daylist(yesterday, [stop(1, yesterday, unload_after=site), stop(2, yesterday)]),
                    'events': picked_up(1)})
        expect(card.get_by_role('button', name='결과 보내기', exact=True)).to_be_visible()

        printed = {**daylist(today, [first]), 'printed_since_seq': 0}
        seed(page, {'daylist': printed, 'events': picked_up(1)})
        expect(card.get_by_text('반입지 좌표가 없습니다. PC 배차표에서 위치를 확인하세요.', exact=True)).to_be_visible()
        card.get_by_role('button', name='중간 하차 기록', exact=True).click()
        expect(page.get_by_label('반입지', exact=True)).to_have_value(site['label'])
        expect(page.get_by_role('button', name='중간 하차 기록', exact=True)).to_be_enabled()
    finally:
        context.close()


if __name__ == '__main__':
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path)
        try:
            intermediate_unload_before_next_visit(browser, url)
            intermediate_unload_plan_bounds(browser, url)
            save_and_retry(browser, url)
            reject_unready_route(browser, url)
            keep_receipt_pending_until_commit(browser, url)
        finally:
            browser.close()
    print('PASS: 중간·최종 하차 실제 저장·재시도·재실행, 반입지·차고지 길안내, 미완료·지난 목록 차단')
