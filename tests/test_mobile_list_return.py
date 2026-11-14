"""가상 자료와 실제 PIN/IndexedDB로 목록 복귀 위치·초점·자료 경계를 확인한다."""
import os
import re
import tempfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from test_review_ui import PIN, mobile_server
from test_workflow_mobile import daylist, event, open_mobile, seed, stop


def prepare(page, mode, finder=False):
    today = page.evaluate("import('./core.js').then(C=>C.todayKST())")
    records = [stop(i, today) for i in range(1, 31)]
    patch = {'mode': mode, 'requests': records} if mode == 'SOLO' else {'daylist': daylist(today, records)}
    patch['events'] = [event(15, 'ITEM_RESULT', line_id='line-15', qty_confirmed=1, qty_collected=1),
                       event(15, 'ATTEMPT', outcome='DONE')]
    seed(page, patch, registered=mode == 'PC')
    if finder:
        page.get_by_role('button', name='날짜·접수 찾기', exact=True).click()
        page.get_by_label('찾을 접수', exact=True).fill('가상')
        expect(page.locator('.stops .stop')).to_have_count(30)
        # 검색의 지연 렌더를 마친 뒤 카드 위치를 측정한다.
        expect(page.get_by_label('찾을 접수', exact=True)).to_be_focused()
    return '#/find' if finder else '#/home'


def enter(page, number=15):
    card = page.locator(f'.stops .stop[href="#/stop/workflow-{number}"]')
    card.evaluate("el=>el.scrollIntoView({block:'center'})")
    before = card.evaluate('(el)=>({top:el.getBoundingClientRect().top,y:scrollY})')
    assert before['y'] > 500, before
    card.click()
    expect(page).to_have_url(re.compile(f'#/stop/workflow-{number}$'))
    expect(page.get_by_role('heading', name=f'{number}번', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='← 목록', exact=True)).to_be_visible()
    return before


def restored(page, route, before, number=15):
    expect(page).to_have_url(re.compile(re.escape(route) + '$'))
    card = page.locator(f'.stops .stop[href="#/stop/workflow-{number}"]')
    expect(card).to_be_focused()
    top = card.evaluate('el=>el.getBoundingClientRect().top')
    assert abs(top - before['top']) <= 4, (before, top)


def no_restore(page, route='#/home'):
    expect(page).to_have_url(re.compile(re.escape(route) + '$'))
    expect(page.locator('.stops .stop:focus')).to_have_count(0)
    assert page.evaluate('scrollY') <= 2


def main():
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-list-return-'))
    output.mkdir(parents=True, exist_ok=True)
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        context, page = open_mobile(browser, url)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            for mode in ('PC', 'SOLO'):
                for finder in (False, True):
                    route = prepare(page, mode, finder)
                    before_state = page.evaluate("import('./core.js').then(C=>JSON.stringify([C.st.requests,C.st.daylist,C.st.events]))")
                    before = enter(page)
                    page.get_by_role('button', name='← 목록', exact=True).click()
                    restored(page, route, before)
                    before = enter(page)
                    page.locator('.dock-back').click()
                    restored(page, route, before)
                    before = enter(page)
                    page.go_back()
                    restored(page, route, before)
                    assert page.evaluate("import('./core.js').then(C=>JSON.stringify([C.st.requests,C.st.daylist,C.st.events]))") == before_state
                    page.screenshot(path=str(output / f'list-return-{mode.lower()}-{"find" if finder else "home"}.png'))

                route = prepare(page, mode, True)
                before = enter(page)
                page.get_by_role('button', name='+ 현장 품목 추가', exact=True).click()
                page.get_by_role('button', name='← 취소', exact=True).click()
                page.get_by_role('button', name='PC 입력 도움', exact=True).click()
                help_back = page.get_by_role('button', name='접수 상세로', exact=True)
                help_back.click()
                expect(page.get_by_role('button', name='PC 입력 도움', exact=True)).to_be_focused()
                page.locator('.dock-next').click()
                expect(page).to_have_url(url + '#/stop/workflow-16')
                page.get_by_role('button', name='← 목록', exact=True).click()
                restored(page, route, before)
                expect(page.get_by_label('찾을 접수', exact=True)).to_have_value('가상')

                prepare(page, mode)
                page.evaluate("""async mode=>{
                    const C=await import('./core.js');
                    if(mode==='PC')C.ui.filter='REMAIN';else C.ui.soloView='OPEN';
                    await C.render();
                }""", mode)
                before = enter(page, number=14)
                page.evaluate("""async()=>{
                    const C=await import('./core.js'), s=C.findStop('workflow-14');
                    await C.addEvent(s,'ITEM_RESULT',{line_id:'line-14',qty_confirmed:1,qty_collected:1});
                    await C.addEvent(s,'ATTEMPT',{outcome:'DONE'});
                }""")
                page.get_by_role('button', name='← 목록', exact=True).click()
                restored(page, '#/home', before, number=16)
                expect(page.locator('.stops .stop[href="#/stop/workflow-14"]')).to_have_count(0)

            # 조건이 바뀌거나 자료 출처가 바뀌면 예전 위치를 다시 쓰지 않는다.
            for change in ('query', 'state', 'daylist', 'date', 'mode', 'lock', 'unrelated'):
                prepare(page, 'PC')
                enter(page)
                if change == 'lock':
                    page.evaluate("import('./core.js').then(C=>C.lockNow())")
                    page.get_by_placeholder('PIN', exact=True).fill(PIN)
                    page.get_by_role('button', name='잠금 해제', exact=True).click()
                    expect(page.get_by_role('button', name='← 목록', exact=True)).to_be_visible()
                elif change == 'unrelated':
                    page.evaluate("async()=>{location.hash='#/settings';await(await import('./core.js')).render()}")
                    page.evaluate("async()=>{location.hash='#/home';await(await import('./core.js')).render()}")
                else:
                    page.evaluate("""async change=>{
                        const C=await import('./core.js');
                        if(change==='query')C.ui.stopQ='없는 접수';
                        if(change==='state')C.setSt({...C.st});
                        if(change==='daylist')C.st.daylist={...C.st.daylist};
                        if(change==='date')C.st.daylist.work_date=C.todayKST(1);
                        if(change==='mode'){C.st.requests=C.st.daylist.stops;C.st.mode='SOLO';}
                    }""", change)
                if change != 'unrelated':
                    page.get_by_role('button', name='← 목록', exact=True).click()
                no_restore(page)

            # 같은 조건에서 해당 행이 사라진 경우 현재 목록의 이웃으로 안전하게 복귀한다.
            prepare(page, 'SOLO')
            before = enter(page)
            page.evaluate("import('./core.js').then(C=>{C.st.requests=C.st.requests.filter(s=>s.request_id!=='workflow-15')})")
            page.get_by_role('button', name='← 목록', exact=True).click()
            restored(page, '#/home', before, number=16)

            # 검색 조건 초기화·새 조회에서 이전 상세의 카드 초점을 되살리지 않는다.
            prepare(page, 'SOLO', True)
            enter(page)
            page.get_by_role('button', name='← 목록', exact=True).click()
            page.get_by_role('button', name='조회조건 초기화', exact=True).click()
            expect(page.locator('.stops .stop:focus')).to_have_count(0)
            prepare(page, 'SOLO', True)
            enter(page)
            page.evaluate("import('./screens/find.js').then(F=>F.openFinder())")
            no_restore(page, '#/find')
            expect(page.get_by_label('찾을 접수', exact=True)).to_have_value('')
            assert not errors, errors
            print('PASS: PC/SOLO 홈·찾기 목록 위치/초점·버튼/브라우저 뒤로·품목/입력도움 왕복·다음접수 연쇄·조건/잠금/자료교체 경계·삭제행 대체·업무자료 불변')
        except BaseException:
            page.screenshot(path=str(output / 'list-return-failure.png'), full_page=True)
            raise
        finally:
            context.close()
            browser.close()


if __name__ == '__main__':
    main()
