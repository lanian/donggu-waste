"""실제 PIN/IndexedDB: 하차 숫자·초안·실패/지연 저장 회귀."""
import argparse
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_mobile_add_save import disk_state, release, writes
from test_review_ui import mobile_server, unlock
from test_workflow_mobile import daylist, open_mobile, seed, stop


def case(browser, url, mode='PC'):
    context, page = open_mobile(browser, url)
    dates = page.evaluate("import('./core.js').then(C=>({today:C.todayKST(),listDay:C.todayKST(-1)}))")
    record = stop(1, dates['listDay'])
    seed(page, {'mode': mode, 'daylist': daylist(dates['listDay'], [record]) if mode == 'PC' else None,
                'requests': [record] if mode == 'SOLO' else []}, registered=mode == 'PC')
    page.evaluate("async()=>{window.__C=await import('./core.js');window.__workDate=window.__C.st.daylist?.work_date||window.__C.todayKST();}")
    page.goto(url + '#/unload')
    expect(page.get_by_role('heading', name='하차 기록', exact=True)).to_be_visible()
    return context, page


def fill(page, volume='2.5', weight=''):
    page.get_by_label('반입지', exact=True).fill('가상 반입지')
    page.get_by_label('시각', exact=True).fill('13:45')
    page.get_by_label('부피 (m³, 추정)', exact=True).fill(volume)
    page.get_by_label('중량 (kg)', exact=True).fill(weight)
    page.get_by_label('메모', exact=True).fill('가상 계근 확인')


def invalid_case(browser, url, shots):
    context, page = case(browser, url)
    try:
        for volume, weight in [('-1', ''), ('', '-1'), ('0', '0'), ('0', ''), ('', '0'), ('', '')]:
            fill(page, volume, weight)
            page.get_by_role('button', name='하차 기록', exact=True).click()
            try:
                expect(page).to_have_url(url + '#/unload', timeout=2500)
                expect(page.get_by_text('부피·중량은 0 이상이고 하나는 0보다 커야 합니다.', exact=True)).to_be_visible()
            except BaseException:
                page.screenshot(path=str(shots / 'red-unload-numeric.png'), full_page=True)
                raise
            assert not disk_state(page)['events']
            expect(page.get_by_label('부피 (m³, 추정)', exact=True)).to_have_value(volume)
            expect(page.get_by_label('중량 (kg)', exact=True)).to_have_value(weight)
        # number 입력은 NaN/Infinity 문자열을 정리하므로, 비유한 DOM 값은 합성 getter로 검증한다.
        for field_name in ('부피 (m³, 추정)', '중량 (kg)'):
            for nonfinite in ('NaN', 'Infinity', '-Infinity', '1e309'):
                fill(page, '1', '1')
                numeric = page.get_by_label(field_name, exact=True)
                numeric.evaluate("(el,value)=>Object.defineProperty(el,'value',{configurable:true,get:()=>value})", nonfinite)
                page.locator('.dock-next').click()
                expect(page.get_by_text('부피·중량은 0 이상이고 하나는 0보다 커야 합니다.', exact=True)).to_be_visible()
                assert not disk_state(page)['events']
                numeric.evaluate("el=>{delete el.value;}")
    finally:
        context.close()
    print('PASS: 음수·양쪽0/빈값·합성 비유한 하차 입력 거부·초안 유지·이벤트 없음', flush=True)


def positive_case(browser, url):
    for mode, volume, weight in [('PC', '0', '12.5'), ('SOLO', '', '12'), ('PC', '2.5', '0'), ('SOLO', '2.5', '')]:
        context, page = case(browser, url, mode)
        try:
            fill(page, volume, weight)
            page.locator('.dock-next').click()
            expect(page).to_have_url(url + '#/home')
            saved = disk_state(page)['events']
            assert len(saved) == 1
            event = saved[0]
            assert event['kind'] == 'UNLOAD' and event['request_id'] == '' and event['base_req_ver'] is None
            assert event['assign_ver'] == (1 if mode == 'PC' else 0) and event['seq'] == 1 and event['gen']
            assert event['body'] == {'place': '가상 반입지', 'at_time': '13:45',
                'volume_m3': float(volume) if volume != '' else None, 'weight_kg': float(weight) if weight != '' else None,
                'note': '가상 계근 확인', 'work_date': page.evaluate('window.__workDate'), 'worker': '가상 작업자'}
            assert page.evaluate('ts=>Number.isFinite(Date.parse(ts))&&Math.abs(Date.now()-Date.parse(ts))<10000', event['ts'])
        finally:
            context.close()
    print('PASS: 양수 중량/부피와 0·빈값 조합 허용·PC 목록일/단독 오늘·기존 이벤트 단위/스키마 유지', flush=True)


def malformed_case(browser, url, shots):
    for field_name, typed in [('부피 (m³, 추정)', '1e'), ('중량 (kg)', '-')]:
        context, page = case(browser, url)
        try:
            fill(page, '1', '1')
            numeric = page.get_by_label(field_name, exact=True)
            numeric.fill('')
            numeric.press_sequentially(typed)
            assert numeric.evaluate("el=>el.value===''&&el.validity.badInput"), '실제 불완전 숫자 입력이어야 한다'
            page.locator('.dock-next').click()
            try:
                expect(page).to_have_url(url + '#/unload', timeout=2500)
                expect(page.get_by_text('부피·중량은 0 이상이고 하나는 0보다 커야 합니다.', exact=True)).to_be_visible()
                assert not disk_state(page)['events']
            except BaseException:
                page.screenshot(path=str(shots / 'red-unload-malformed-number.png'), full_page=True)
                raise
        finally:
            context.close()
    print('PASS: 실제 1e/마이너스 단독 키입력은 반대쪽 양수가 있어도 빈값으로 저장하지 않음', flush=True)


def failed_case(browser, url, shots):
    for mode in ('PC', 'SOLO'):
        context, page = case(browser, url, mode)
        try:
            fill(page)
            writes(page, fail_at=1)
            page.get_by_role('button', name='하차 기록', exact=True).click()
            retry = page.get_by_role('button', name='저장 다시 시도', exact=True)
            expect(retry).to_be_enabled()
            expect(page).to_have_url(url + '#/unload')
            expect(page.get_by_label('반입지', exact=True)).to_have_value('가상 반입지')
            expect(page.get_by_label('메모', exact=True)).to_have_value('가상 계근 확인')
            prepared = page.evaluate('()=>structuredClone(window.__C.st.events[0])')
            before = [prepared['event_id']]
            assert len(page.evaluate('()=>window.__C.st.events')) == 1 and not disk_state(page)['events']
            retry.click()
            expect(retry).to_be_enabled()
            assert page.evaluate('()=>window.__C.st.events.map(e=>e.event_id)') == before
            page.screenshot(path=str(shots / f'unload-retry-{mode.lower()}.png'), full_page=True)
            page.evaluate('window.__failAt=0')
            retry.click()
            expect(page).to_have_url(url + '#/home')
            saved = disk_state(page)['events']
            assert [e['event_id'] for e in saved] == before
            assert saved[0] == prepared, '재시도는 event_id·seq·gen·시각·작업일·작업자를 다시 만들지 않는다'
            assert saved[0]['body']['volume_m3'] == 2.5 and saved[0]['body']['at_time'] == '13:45'
            assert saved[0]['body']['work_date'] == page.evaluate('window.__workDate')
        finally:
            context.close()
    print('PASS: PC/단독 하차 실패·동일 이벤트 재시도·날짜/시각/수량 보존', flush=True)


def draft_late_case(browser, url, shots):
    context, page = case(browser, url)
    try:
        # 외부 지도 조각 대신 빈 바탕 스타일만 제공한다. 지도 조작/좌표 저장은 실제 MapLibre 경로다.
        context.route('https://tiles.openfreemap.org/**', lambda route: route.fulfill(content_type='application/json',
            body='{"version":8,"sources":{},"layers":[{"id":"background","type":"background","paint":{"background-color":"#eef1f4"}}]}'))
        page.evaluate("async()=>{window.__C.st.mapEnabled=true;await window.__C.commit();await window.__C.render();}")
        fill(page, '1.5', '12')
        page.get_by_role('button', name='반입지 위치 지정', exact=True).click()
        expect(page.get_by_role('heading', name='반입지 위치 지정', exact=True)).to_be_visible()
        page.wait_for_function('()=>window.__C.liveMap&&window.__C.liveMap.loaded()')
        page.get_by_role('button', name='← 취소', exact=True).click()
        expect(page.get_by_label('반입지', exact=True)).to_have_value('가상 반입지')
        expect(page.get_by_label('시각', exact=True)).to_have_value('13:45')
        expect(page.get_by_label('부피 (m³, 추정)', exact=True)).to_have_value('1.5')
        expect(page.get_by_label('중량 (kg)', exact=True)).to_have_value('12')
        expect(page.get_by_label('메모', exact=True)).to_have_value('가상 계근 확인')
        page.get_by_role('button', name='반입지 위치 지정', exact=True).click()
        page.wait_for_function('()=>window.__C.liveMap&&window.__C.liveMap.loaded()')
        canvas = page.locator('canvas.maplibregl-canvas')
        rectangle = canvas.bounding_box()
        assert rectangle and rectangle['width'] > 0
        before_loc = page.evaluate('()=>{const p=window.__C.liveMap.getCenter();return {lat:p.lat,lon:p.lng};}')
        x, y = rectangle['x'] + rectangle['width'] * 0.45, rectangle['y'] + rectangle['height'] * 0.5
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + 35, y + 20, steps=8)
        page.mouse.up()
        page.wait_for_function('before=>Math.abs(window.__C.liveMap.getCenter().lng-before.lon)>1e-6', arg=before_loc)
        page.wait_for_function('()=>!window.__C.liveMap.isMoving()')
        chosen = page.evaluate('()=>{const p=window.__C.liveMap.getCenter();return {lat:Math.round(p.lat*1e6)/1e6,lon:Math.round(p.lng*1e6)/1e6};}')
        page.get_by_role('button', name='이 위치로 저장', exact=True).click()
        expect(page).to_have_url(url + '#/unload')
        expect(page.get_by_label('반입지', exact=True)).to_have_value('가상 반입지')
        expect(page.get_by_label('시각', exact=True)).to_have_value('13:45')
        expect(page.get_by_label('부피 (m³, 추정)', exact=True)).to_have_value('1.5')
        expect(page.get_by_label('중량 (kg)', exact=True)).to_have_value('12')
        expect(page.get_by_label('메모', exact=True)).to_have_value('가상 계근 확인')
        located = disk_state(page)
        assert located['unloadSite'] == chosen and not located['events'], (located['unloadSite'], chosen, located['events'])
        writes(page, hold_at=1)
        save = page.locator('.dock-next')
        save.click()
        page.wait_for_function('()=>window.__releases.length===1')
        save.dispatch_event('click')
        assert len(page.evaluate('()=>window.__C.st.events')) == 1
        page.evaluate("()=>{location.hash='#/settings';}")
        expect(page.get_by_role('heading', name='설정·정보', exact=True)).to_be_visible()
        expect(page).to_have_url(url + '#/settings')
        newer_draft = page.get_by_label('작업자 이름', exact=True)
        newer_draft.fill('가상 새화면초안')
        release(page)
        page.wait_for_timeout(100)
        expect(newer_draft).to_have_value('가상 새화면초안')
        page.goto(url + '#/unload')
        expect(page.get_by_role('button', name='새 하차 기록', exact=True)).to_be_visible()
        assert len(disk_state(page)['events']) == 1
    finally:
        context.close()
    print('PASS: 실제 지도 위치 지정/취소 왕복 초안 보존·연타/늦은 응답 중복 및 새 화면 초안 침범 없음', flush=True)


def reentry_lock_case(browser, url, shots):
    context, page = case(browser, url)
    try:
        fill(page, '0', '12')
        writes(page, hold_at=1)
        page.evaluate("()=>{window.__oldUnloadSave=document.querySelector('.dock-next');}")
        page.locator('.dock-next').click()
        page.wait_for_function('()=>window.__releases.length===1')
        prepared = page.evaluate('()=>structuredClone(window.__C.st.events[0])')
        page.goto(url + '#/home')
        page.wait_for_function('()=>!window.__oldUnloadSave.isConnected')
        page.goto(url + '#/unload')
        page.wait_for_function("()=>document.querySelector('.dock-next')&&document.querySelector('.dock-next')!==window.__oldUnloadSave")
        expect(page.locator('.dock-next')).to_be_disabled()
        expect(page.get_by_label('중량 (kg)', exact=True)).to_have_value('12')
        page.locator('.dock-next').dispatch_event('click')
        assert len(page.evaluate('()=>window.__C.st.events')) == 1
        release(page)
        expect(page.locator('.dock-next')).to_have_text('새 하차 기록')
        expect(page).to_have_url(url + '#/unload')
        assert disk_state(page)['events'] == [prepared]
        page.locator('.dock-next').click()
        fill(page, '3.5', '0')
        page.get_by_label('메모', exact=True).fill('가상 다음 하차 초안')
        page.evaluate("()=>window.__oldUnloadSave.dispatchEvent(new Event('click'))")
        expect(page.get_by_label('메모', exact=True)).to_have_value('가상 다음 하차 초안')
        assert disk_state(page)['events'] == [prepared]
        page.screenshot(path=str(shots / 'unload-reentry-new-draft.png'), full_page=True)
    finally:
        context.close()
    for mode in ('PC', 'SOLO'):
        context, page = case(browser, url, mode)
        try:
            fill(page, '', '4')
            writes(page, hold_at=1)
            page.locator('.dock-next').click()
            page.wait_for_function('()=>window.__releases.length===1')
            prepared = page.evaluate('()=>structuredClone(window.__C.st.events[0])')
            assert page.evaluate('()=>window.__C.stepFlush.size') == 0, '즉시 append+commit 하차는 새 화면 마운트를 막지 않는다'
            page.evaluate('()=>{window.__locked=false;window.__C.lockNow().then(()=>window.__locked=true);}')
            assert page.evaluate("import('./store.js').then(S=>S.isUnlocked())")
            assert not page.evaluate('window.__locked')
            release(page)
            expect(page.get_by_placeholder('PIN', exact=True)).to_be_visible()
            assert disk_state(page)['events'] == [prepared]
            unlock(page)
            page.goto(url + '#/unload')
            expect(page.get_by_role('button', name='하차 기록', exact=True)).to_be_visible()
            assert disk_state(page)['events'] == [prepared]
            assert len(page.evaluate('()=>window.__C.st.events')) == 1
        finally:
            context.close()
    print('PASS: 저장 중 재진입/옛 버튼은 같은 이벤트만 보존·새 초안 보호·PC/단독 잠금은 저장을 기다림', flush=True)


def main(only='all'):
    shots = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-unload-save-')) / 'shots'
    shots.mkdir(parents=True, exist_ok=True)
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        try:
            if only in ('all', 'numeric'):
                invalid_case(browser, url, shots)
                positive_case(browser, url)
            if only in ('all', 'malformed'):
                malformed_case(browser, url, shots)
            if only in ('all', 'failure'):
                failed_case(browser, url, shots)
            if only in ('all', 'map'):
                draft_late_case(browser, url, shots)
            if only in ('all', 'lock'):
                reentry_lock_case(browser, url, shots)
        finally:
            browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=('all', 'numeric', 'malformed', 'failure', 'map', 'lock'), default='all')
    main(parser.parse_args().case)
