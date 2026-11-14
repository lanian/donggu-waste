"""모바일 목록의 실제 수량·완료 상태·검색 소유권: 가상 암호화 상태만 사용."""
import datetime as dt
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright

from test_review_ui import mobile_server
from test_workflow_mobile import daylist, open_mobile, seed, stop


def fixtures(today):
    records = [stop(i, today, receipt_no=f'LIST-20261114-{i:04}') for i in range(1, 10)]
    records[0]['address'] = '가상시 가상구 시험행복로 101 가상아파트 101동 1001호 (공동현관 안쪽)'
    records[0]['items'][0].update(qty=3, amount=3000)
    records[4]['status'] = 'CANCELLED'
    records[5]['removed'] = True
    records[6].update(pii_purged=True, name='정리된 가상 이름', address='정리된 가상 주소',
                      phone='01098765432', dong='정리된 가상동', spot_note='정리된 가상 메모')
    records[7]['status'] = 'DONE'
    events = [
        {'request_id': 'workflow-1', 'kind': 'FIELD_ITEM_ADD', 'body': {
            'line_id': 'field-extra', 'name': '가상 추가 책상', 'spec': '시험 규격',
            'unit': 'SET', 'unit_label': '세트', 'qty': 2, 'price_status': 'PENDING'}},
        {'request_id': 'workflow-1', 'kind': 'ITEM_RESULT', 'body': {
            'line_id': 'line-1', 'qty_confirmed': 4, 'qty_collected': 1}},
        {'request_id': 'workflow-1', 'kind': 'ATTEMPT', 'body': {'outcome': 'PARTIAL'}},
    ]
    for number in (2, 3, 4):
        events.append({'request_id': f'workflow-{number}', 'kind': 'ITEM_RESULT', 'body': {
            'line_id': f'line-{number}', 'qty_confirmed': 1, 'qty_collected': 1}})
        if number != 2:
            events.append({'request_id': f'workflow-{number}', 'kind': 'ATTEMPT', 'body': {'outcome': 'DONE'}})
    events.append({'request_id': 'workflow-4', 'kind': 'ITEM_RESULT', 'body': {
        'line_id': 'line-4', 'qty_confirmed': 2, 'qty_collected': 1}})
    events.extend([
        {'request_id': 'workflow-9', 'kind': 'ITEM_RESULT', 'body': {
            'line_id': 'line-9', 'qty_confirmed': 1, 'qty_collected': 1}},
        {'request_id': 'workflow-9', 'kind': 'ATTEMPT', 'body': {'outcome': 'ON_HOLD'}},
    ])
    return records, events


def card(page, number):
    return page.locator(f'.stop[href="#/stop/workflow-{number}"]')


def card_contract(page):
    first = card(page, 1)
    expect(first).to_contain_text('LIST-20261114-0001')
    expect(first.locator('strong')).to_have_text('가상 주민 1')
    expect(first.locator('.items')).to_contain_text('가상 의자 시험용 4개')
    expect(first.locator('.items')).to_contain_text('가상 추가 책상 시험 규격 2세트')
    expect(first.locator('.stop-quantity')).to_have_text('남음 3개 · 2세트')
    expect(first.locator('.stop-state')).to_have_text('일부 수거')
    expect(card(page, 2).locator('.stop-state')).to_have_text('결과 미저장')
    assert 'done' not in card(page, 2).get_attribute('class').split()
    expect(card(page, 3).locator('.stop-state')).to_have_text('수거 완료')
    assert 'done' in card(page, 3).get_attribute('class').split()
    expect(card(page, 4).locator('.stop-state')).to_have_text('결과 재확인')
    assert 'done' not in card(page, 4).get_attribute('class').split()
    expect(card(page, 5).locator('.stop-state')).to_have_text('취소')
    expect(card(page, 6).locator('.stop-state')).to_have_text('목록에서 제외됨')
    expect(card(page, 5).locator('.stop-quantity')).to_have_count(0)
    expect(card(page, 6).locator('.stop-quantity')).to_have_count(0)
    expect(card(page, 7).locator('strong')).to_have_text('개인정보 정리됨')
    for private in ('정리된 가상 이름', '정리된 가상 주소', '01098765432', '정리된 가상동', '정리된 가상 메모'):
        assert private not in card(page, 7).inner_text()
    expect(card(page, 7)).to_contain_text('LIST-20261114-0007')
    expect(card(page, 8).locator('.stop-state')).to_have_text('수거 완료')
    expect(card(page, 8).locator('.stop-quantity')).to_have_count(0)
    expect(card(page, 9).locator('.stop-state')).to_have_text('보류')
    assert 'done' not in card(page, 9).get_attribute('class').split()
    expect(page.locator('.summary .sum-main')).to_have_text('2 / 7곳 완료')
    expect(page.locator('.summary .sum-sub')).to_contain_text('재방문·보류 3')


def search_contract(page):
    result = page.evaluate("""async () => {
        const C = await import('./core.js'), F = await import('./fees.js'), L = await import('./screens/stoplist.js');
        const all = (C.st.daylist?.stops || C.st.requests).map(s => ({s, d:F.deriveStop(s,C.st.events)}));
        C.ui.stopQ='추가 책상'; const found = L.filterQ(all).map(x=>x.s.request_id); C.ui.stopQ='';
        const purged = all.find(x=>x.s.pii_purged);
        return { found, hay:L.stopHay(purged.s,purged.d),
            done:all.map(x=>L.isStopDone(x.s,x.d)) };
    }""")
    assert result['found'] == ['workflow-1'], result
    assert result['done'] == [False, False, True, False, False, False, False, True, False], result
    assert 'LIST-20261114-0007' in result['hay']
    for private in ('정리된 가상 이름', '정리된 가상 주소', '01098765432', '98765432', '정리된 가상동', '정리된 가상 메모'):
        assert private not in result['hay'], result


def timer_contract(page, records, events, today):
    for boundary in ('state', 'mode', 'route', 'query'):
        seed(page, {'daylist': daylist(today, records), 'events': events, 'releasedSeq': len(events)})
        page.wait_for_timeout(50)
        page.evaluate("""async boundary => {
            const C=await import('./core.js'); window.__listRenders=0;
            window.__listObserver=new MutationObserver(records=>{window.__listRenders+=records.length});
            window.__listObserver.observe(C.$app,{childList:true});
            const input=document.querySelector('[aria-label="목록에서 찾기"]');
            input.value='가상'; input.dispatchEvent(new Event('input',{bubbles:true}));
            if(boundary==='state') C.setSt({...C.st});
            if(boundary==='mode') C.st.mode='SOLO';
            if(boundary==='route') location.hash='#/find';
            if(boundary==='query') C.ui.stopQ='다른 검색';
        }""", boundary)
        page.wait_for_timeout(360)
        calls = page.evaluate('window.__listRenders')
        page.evaluate('window.__listObserver.disconnect()')
        assert calls == (1 if boundary == 'route' else 0), f'{boundary} 변경 뒤 이전 검색 타이머가 화면을 다시 그림: {calls}'


def main():
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-mobile-list-cards-'))
    shots = output / 'shots'
    shots.mkdir(parents=True, exist_ok=True)
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        context, page = open_mobile(browser, url)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            today = page.evaluate("import('./core.js').then(C=>C.todayKST())")
            records, events = fixtures(today)
            for mode in ('PC', 'SOLO'):
                patch = {'mode': mode, 'events': events, 'releasedSeq': len(events),
                         'lastBackupAt': dt.datetime.now(dt.timezone.utc).isoformat()}
                patch.update({'daylist': daylist(today, records)} if mode == 'PC' else {'requests': records})
                seed(page, patch, registered=mode == 'PC')
                before = page.evaluate("import('./core.js').then(C=>JSON.stringify(C.st))")
                if mode == 'PC':
                    card_contract(page)
                search_contract(page)
                for width in (360, 390, 430):
                    for text in ('', 'xl'):
                        for theme in ('light', 'dark'):
                            page.set_viewport_size({'width': width, 'height': 844})
                            page.evaluate("""async ({text,theme})=>{
                                const C=await import('./core.js');C.st.textSize=text;C.applyTextSize();
                                await C.setPrefs({theme});await C.render();
                            }""", {'text': text, 'theme': theme})
                            first = card(page, 1)
                            first.scroll_into_view_if_needed()
                            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                            assert first.bounding_box()['height'] >= 56
                            assert first.locator('.addr').evaluate('(e)=>getComputedStyle(e).overflow') != 'hidden'
                            if width in (360, 430) and text == 'xl':
                                page.screenshot(path=str(shots / f'list-{mode.lower()}-{width}-{theme}.png'))
                assert page.evaluate("import('./core.js').then(C=>{const copy={...C.st};delete copy.textSize;return JSON.stringify(copy)})") == page.evaluate("s=>{const copy=JSON.parse(s);delete copy.textSize;return JSON.stringify(copy)}", before)
            timer_contract(page, records, events, today)
            assert not errors, errors
            print('모바일 목록 카드: PC·단독, 접수번호·실제/현장추가/혼합단위 수량·완료/미저장/재확인·취소/제외·PII 검색·검색 타이머 소유권·24 화면 조합·업무 자료 불변 통과')
        finally:
            context.close()
            browser.close()


if __name__ == '__main__':
    main()
