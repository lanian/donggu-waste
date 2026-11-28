"""실제 PIN/암호화 IndexedDB의 품목별 스티커·필증 확인 회귀."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright

from test_mobile_collection_save import disk_state, hold_failed_write, open_case as open_collection_case, open_sticker_options
from test_review_ui import mobile_server
from test_workflow_mobile import open_mobile, seed, stop as virtual_stop

ROOT = Path(__file__).resolve().parents[1]
NAMES = ('가상 의자', '가상 책상', '가상 서랍장', '가상 옷장', '가상 침대', '가상 소파', '가상 식탁', '가상 선반')


def open_case(browser, url, **options):
    context, page, record = open_collection_case(browser, url, **options)
    open_sticker_options(page)
    return context, page, record


def sticker(page):
    return page.get_by_role('group', name='가상 의자 스티커·신고필증', exact=True)


def record(page):
    return page.evaluate('()=>window.__collectionFees.deriveStop(window.__collectionCore.st.daylist.stops[0],window.__collectionCore.st.events)')


def settle(page):
    page.evaluate('async()=>{await window.__collectionCore.saving;}')
    expect(sticker(page).get_by_role('button', name='확인', exact=True)).to_be_enabled()


def semantics_case(browser, url, output):
    ctx, page, _ = open_case(browser, url, qty=3, previous=1)
    try:
        expect(page.locator('.sticker-summary')).to_contain_text('미확인 1')
        expect(page.locator('.chk-label')).to_contain_text('미확인')
        sizes = sticker(page).locator('button').evaluate_all('els=>els.map(e=>e.getBoundingClientRect().height)')
        assert min(sizes) >= 56, sizes
        sticker(page).get_by_role('button', name='확인', exact=True).click()
        settle(page)
        expect(page.locator('.sticker-summary')).to_contain_text('확인 1')
        expect(page.locator('.chk-label')).to_contain_text('확인됨')
        assert record(page)['lines'][0]['qty_collected'] == 1
        sticker(page).get_by_role('button', name='없음', exact=True).click()
        expect(page.get_by_role('dialog')).to_contain_text('이번 추가')
        page.get_by_role('button', name='그래도 수거', exact=True).click()
        settle(page)
        assert record(page)['lines'][0]['qty_collected'] == 1
        assert record(page)['outcome'] is None
        expect(page.locator('.sk-note')).to_contain_text('없음 → 수거 결정')
        expect(page.locator('.sk-note')).to_contain_text('이번 실은 0개')
        page.get_by_role('button', name='가상 의자 실은 수량 더하기', exact=True).click()
        page.wait_for_function('()=>window.__collectionFees.deriveStop(window.__collectionCore.st.daylist.stops[0],window.__collectionCore.st.events).lines[0].qty_collected===2')
        settle(page)
        sticker(page).get_by_role('button', name='없음', exact=True).click()
        expect(page.get_by_role('dialog')).to_contain_text('이전 수거 1개')
        page.get_by_role('button', name='미수거 (사유 기록)', exact=True).click()
        settle(page)
        line = record(page)['lines'][0]
        assert line['qty_collected'] == 1 and line['reason'] == 'NO_STICKER', line
        sticker(page).get_by_role('button', name='확인', exact=True).click()
        settle(page)
        line = record(page)['lines'][0]
        assert line['qty_collected'] == 1 and line['reason'] == 'NO_STICKER', line
        expect(page.locator('.sk-review')).to_contain_text('미수거 사유')
        page.get_by_role('button', name='품목 수량을 모두 실음으로 채우기', exact=True).click()
        settle(page)
        assert record(page)['lines'][0]['qty_collected'] == 1
        print('필증 상태/수거 결정/실은 수량 분리·미확인·56px·이전 수량/NO_STICKER 보존 통과', flush=True)
    finally:
        ctx.close()


def number_case(browser, url, output):
    ctx, page, _ = open_case(browser, url)
    try:
        group = sticker(page)
        group.get_by_role('button', name='번호 불일치', exact=True).click()
        dlg = page.get_by_role('dialog')
        inp = dlg.get_by_role('textbox', name='붙어 있는 번호 (선택)', exact=True)
        inp.fill('0' * 31)
        dlg.get_by_role('button', name='그래도 수거', exact=True).click()
        expect(dlg).to_be_visible()
        expect(inp).to_have_attribute('aria-invalid', 'true')
        expect(dlg.get_by_role('alert')).to_contain_text('30자')
        assert page.evaluate('()=>window.__collectionCore.st.events.length') == 0
        inp.fill('0011-0042')
        dlg.get_by_role('button', name='그래도 수거', exact=True).click()
        settle(page)
        assert record(page)['stickers']['line-1']['number'] == '0011-0042'
        group.get_by_role('button', name='번호 불일치', exact=True).click()
        expect(inp).to_have_value('0011-0042')
        inp.fill('0002-0010')
        dlg.get_by_role('button', name='취소', exact=True).click()
        expect(group.get_by_role('button', name='번호 불일치', exact=True)).to_be_focused()
        assert record(page)['stickers']['line-1']['number'] == '0011-0042'
        assert len([e for e in disk_state(page)['events'] if e['kind'] == 'STICKER_CHECK']) == 1
        print('필증 번호 30자 검증·0/하이픈 보존·이전 번호 수정·취소/초점 통과', flush=True)
    finally:
        ctx.close()


def ownership_case(browser, url, output):
    ctx, page, _ = open_case(browser, url)
    try:
        page.evaluate('()=>{document.querySelector(".chk-opts button").click();location.hash="#/home";}')
        page.wait_for_timeout(80)
        assert page.evaluate('()=>window.__collectionCore.st.events.length') == 0
        expect(page.get_by_role('dialog')).to_have_count(0)
    finally:
        ctx.close()
    for change in ('route', 'route-return', 'stop', 'state', 'lock'):
        ctx, page, _ = open_case(browser, url)
        try:
            sticker(page).get_by_role('button', name='없음', exact=True).click()
            page.evaluate('()=>{window.__stickerOwner=window.__collectionCore.st;window.__staleStickerChoice=document.querySelector("dialog .sheet-actions button");}')
            if change == 'route':
                page.evaluate('()=>{location.hash="#/home";}')
                expect(page.get_by_role('dialog')).to_have_count(0)
            elif change == 'route-return':
                page.evaluate('()=>{location.hash="#/home";location.hash="#/stop/workflow-1";}')
                expect(page.get_by_role('dialog')).to_have_count(0)
            elif change == 'stop':
                page.evaluate('()=>{const s=window.__collectionCore.st;s.daylist.stops[0]={...s.daylist.stops[0]};}')
            elif change == 'state':
                page.evaluate('()=>{const C=window.__collectionCore;C.setSt({...C.st,events:[]});}')
            else:
                page.evaluate('async()=>{const S=await import("./store.js");S.lock();}')
            page.evaluate('()=>window.__staleStickerChoice.click()')
            page.wait_for_timeout(70)
            assert page.evaluate('()=>window.__stickerOwner.events.length') == 0, change
            assert not page.evaluate('()=>window.__collectionCore.st?.events.length'), change
        finally:
            ctx.close()
    print('필증 선택창 이후 이동/원본·접수교체/잠금의 늦은 결정 차단 통과', flush=True)


def pending_case(browser, url, output):
    ctx, page, _ = open_case(browser, url)
    try:
        hold_failed_write(page, 1)
        page.evaluate('()=>{window.__stickerDouble=document.querySelector(".chk-opts button");window.__stickerDouble.click();window.__stickerDouble.click();}')
        page.wait_for_function('()=>typeof window.__releaseCollectionFailure==="function"')
        expect(sticker(page).get_by_role('button', name='없음', exact=True)).to_be_disabled()
        page.evaluate('()=>window.__stickerDouble.click()')
        assert page.evaluate('()=>window.__collectionCore.st.events.filter(e=>e.kind==="STICKER_CHECK").length') == 1
        page.evaluate('()=>window.__releaseCollectionFailure()')
        page.wait_for_function('()=>window.__collectionCore.saveFailed')
        expect(page.get_by_text('수거 기록 저장 대기', exact=True)).to_be_visible()
        before = page.evaluate('()=>window.__collectionCore.st.events.map(e=>e.event_id)')
        expect(sticker(page).get_by_role('button', name='확인', exact=True)).to_be_disabled()
        page.locator('#st-result').get_by_role('button', name='저장 다시 시도', exact=True).click()
        settle(page)
        assert [e['event_id'] for e in disk_state(page)['events']] == before
        sticker(page).get_by_role('button', name='확인', exact=True).click()
        settle(page)
        assert len([e for e in disk_state(page)['events'] if e['kind'] == 'STICKER_CHECK']) == 2
        print('필증 pending 이중누름 차단·실패 재저장 동일ID·완료후 순차새기록 통과', flush=True)
    finally:
        ctx.close()


def accepted_case(browser, url, output):
    ctx, page, _ = open_case(browser, url, qty=3, previous=1)
    try:
        page.get_by_role('button', name='가상 의자 실은 수량 더하기', exact=True).click()
        page.wait_for_function('()=>window.__collectionFees.deriveStop(window.__collectionCore.st.daylist.stops[0],window.__collectionCore.st.events).lines[0].qty_collected===2')
        settle(page)
        hold_failed_write(page, 1)
        sticker(page).get_by_role('button', name='없음', exact=True).click()
        page.get_by_role('button', name='미수거 (사유 기록)', exact=True).click()
        page.wait_for_function('()=>typeof window.__releaseCollectionFailure==="function"')
        kinds = page.evaluate('()=>window.__collectionCore.st.events.slice(-2).map(e=>e.kind)')
        assert kinds == ['STICKER_CHECK', 'ITEM_RESULT'], kinds
        page.evaluate('()=>{location.hash="#/home";window.__releaseCollectionFailure();}')
        page.evaluate('async()=>{await window.__collectionCore.saving;}')
        saved = disk_state(page)
        assert [e['kind'] for e in saved['events'][-2:]] == ['STICKER_CHECK', 'ITEM_RESULT']
        line = record(page)['lines'][0]
        assert line['qty_collected'] == 1 and line['reason'] == 'NO_STICKER', line
        print('확정한 미수거는 저장중 목록이동에도 두 기록/이전수량을 원본에 완결 통과', flush=True)
    finally:
        ctx.close()


def queued_case(browser, url, output):
    ctx, page, _ = open_case(browser, url, qty=3, previous=1)
    try:
        page.get_by_role('button', name='가상 의자 실은 수량 더하기', exact=True).click()
        sticker(page).get_by_role('button', name='없음', exact=True).click()
        expect(page.get_by_role('dialog')).to_contain_text('이번 추가 1개')
        page.get_by_role('dialog').get_by_role('button', name='취소', exact=True).click()
        settle(page)
        assert record(page)['lines'][0]['qty_collected'] == 2
        assert not record(page)['stickers']
        expect(sticker(page).get_by_role('button', name='없음', exact=True)).to_be_focused()
    finally:
        ctx.close()
    ctx, page, _ = open_case(browser, url, qty=3, previous=1)
    try:
        page.get_by_role('button', name='가상 의자 실은 수량 더하기', exact=True).click()
        sticker(page).get_by_role('button', name='없음', exact=True).click()
        page.wait_for_function('()=>window.__collectionFees.deriveStop(window.__collectionCore.st.daylist.stops[0],window.__collectionCore.st.events).lines[0].qty_collected===2')
        page.get_by_role('dialog').get_by_role('button', name='미수거 (사유 기록)', exact=True).click()
        page.wait_for_function('()=>window.__collectionCore.st.events.some(e=>e.kind==="STICKER_CHECK")')
        settle(page)
        assert record(page)['lines'][0]['qty_collected'] == 1
        assert record(page)['lines'][0]['reason'] == 'NO_STICKER'
        events = disk_state(page)['events']
        assert len([e for e in events if e['kind'] == 'STICKER_CHECK']) == 1
        assert len([e for e in events if e['kind'] == 'ITEM_RESULT']) == 2
        expect(sticker(page).get_by_role('button', name='없음', exact=True)).to_be_focused()
        print('수량 예약 직후 필증 선택·timer 정착 후 결정/초점 유실 방지 통과', flush=True)
    finally:
        ctx.close()


def layout_case(browser, url, output):
    rows = []
    ctx, page, _ = open_case(browser, url)
    try:
        for width, height, size, theme, hc in ((390, 844, 'm', 'light', False), (360, 844, 'xl', 'dark', False), (390, 480, 'xl', 'light', False), (360, 400, 'xl', 'dark', True), (360, 280, 'xl', 'light', False)):
            page.set_viewport_size({'width': width, 'height': max(400, height)})
            page.evaluate('async p=>{const C=window.__collectionCore;C.st.textSize=p.size;C.applyTextSize();await C.setPrefs({theme:p.theme,hc:p.hc});await C.render();}', {'size': size, 'theme': theme, 'hc': hc})
            group = sticker(page)
            group.get_by_role('button', name='번호 불일치', exact=True).click()
            dlg = page.get_by_role('dialog')
            inp = dlg.get_by_role('textbox', name='붙어 있는 번호 (선택)', exact=True)
            expect(inp).to_be_focused()
            if height < 400:
                page.set_viewport_size({'width': width, 'height': height})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            shot = f'{width}-{height}-{size}-{theme}{"-hc" if hc else ""}'
            page.screenshot(path=str(output / f'{shot}-top.png'))
            actions = dlg.locator('.sheet-actions button')
            for button in actions.all():
                button.scroll_into_view_if_needed()
                assert button.evaluate('b=>{const r=b.getBoundingClientRect(),e=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return r.height>=56&&(e===b||b.contains(e));}'), button.inner_text()
            actions.filter(has_text='취소').click()
            if height < 400:
                page.set_viewport_size({'width': width, 'height': 400})
            expect(group.get_by_role('button', name='번호 불일치', exact=True)).to_be_focused()
            group.get_by_role('button', name='없음', exact=True).click()
            collect = page.get_by_role('dialog').get_by_role('button', name='그래도 수거', exact=True)
            collect.scroll_into_view_if_needed()
            collect.click()
            settle(page)
            rows.append({'width': width, 'height': height, 'size': size, 'theme': theme, 'hc': hc, 'scrollHitAndClick': True})
        (output / 'layout.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
        print('필증 360/390 일반·XL/짧은창/다크HC·입력초점·스크롤 후 모든 결정 hit/click 통과 (280px은 키보드 크기 가상조건)', flush=True)
    finally:
        ctx.close()


def open_many(browser, url, checks=(), reasons=(), loaded=(), quantities=None):
    """품목이 8개인 가상 접수를 연다. checks는 (품목 번호, 결과, 결정) 스티커 기록,
    reasons는 싣지 않은 채 미수거 사유(NO_STICKER)가 남은 품목 번호, loaded는 다 실었는데 그 사유가 남은 품목 번호,
    quantities는 {품목 번호: (접수 수량, 이전 수거량)}(없으면 1개·이전 수거 없음)."""
    quantities = quantities or {}
    context, page = open_mobile(browser, url)
    today = page.evaluate("import('./core.js').then(C=>C.todayKST())")
    first = virtual_stop(1, today)
    first['items'] = [{**first['items'][0], 'line_id': f'line-{n}', 'name': name, 'qty': quantities.get(n, (1, 0))[0],
                       'prior_collected': quantities.get(n, (1, 0))[1], 'amount': 1000 * quantities.get(n, (1, 0))[0]} for n, name in enumerate(NAMES, 1)]
    rid = first['request_id']
    events = [{'request_id': rid, 'kind': 'STICKER_CHECK', 'body': {'line_id': f'line-{n}', 'result': result, **({'decision': decision} if decision else {})}}
              for n, result, decision in checks]
    events += [{'request_id': rid, 'kind': 'ITEM_RESULT', 'body': {'line_id': f'line-{n}', 'line_kind': 'RECEIVED', 'qty_confirmed': quantities.get(n, (1, 0))[0],
                                                               'qty_collected': quantities.get(n, (1, 0))[0 if full else 1], 'reason': 'NO_STICKER'}}
               for numbers, full in ((reasons, False), (loaded, True)) for n in numbers]
    seed(page, {'daylist': {'work_date': today, 'crew': '가상 수거팀', 'assign_ver': 1, 'stops': [first, virtual_stop(2, today)]}, 'events': events}, registered=False)
    page.goto(url + '#/stop/' + rid)
    page.evaluate("async()=>{window.__collectionCore=await import('./core.js');window.__collectionFees=await import('./fees.js');}")
    expect(page.locator('.who .name')).to_have_text(first['name'])
    open_sticker_options(page)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    return context, page, errors


def jump(page):
    return page.locator('#st-result .sk-jump')


def choice(page, name, label):
    return page.get_by_role('group', name=f'{name} 스티커·신고필증', exact=True).get_by_role('button', name=label, exact=True)


def reached(page, button):
    """초점이 그 단추에 갔고, 스크롤이 끝난 뒤 실제로 누를 수 있는 자리(고정 막대에 가리지 않음)인지 확인한다."""
    expect(button).to_be_focused()
    button.evaluate("""async b=>{for(let i=0;i<60;i++){const r=b.getBoundingClientRect(),e=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
        if(e===b||b.contains(e))return;await new Promise(done=>setTimeout(done,50));}throw new Error('이동한 단추가 가려져 있음');}""")
    assert page.locator('.sk-jumped').count() == 1
    assert button.evaluate('b=>b.closest(".chk,.line").classList.contains("sk-jumped")')


def clear_button(page):
    return page.locator('#st-items').get_by_role('button', name='이전 미수거 사유 해제', exact=True)


# 가상 서랍장: 미수거 결정(문제, 못 실음) · 가상 소파: 확인했는데 못 실은 채 사유 남음 · 가상 식탁: 3개 중 이전 수거 1개, 다 실었는데 사유 남음
# · 가상 선반: 다 실었지만 필증은 미수거 결정(문제) → 해제 대상 아님
STALE_REASON = {'reasons': (3, 6), 'loaded': (7, 8), 'quantities': {7: (3, 1)},
                'checks': tuple({3: (3, 'MISMATCH', 'LEAVE'), 8: (8, 'MISSING', 'LEAVE')}.get(n, (n, 'OK', None)) for n in range(1, 9))}


def event_ids(page):
    return page.evaluate('()=>window.__collectionCore.st.events.map(e=>e.event_id)')


def jump_case(browser, url, output):
    # 1) 품목이 많은 접수: 확인 2 · 문제 2(이미 확인·결정 끝) · 미확인 4 → 첫 미확인 품목의 [확인] 단추로
    ctx, page, errors = open_many(browser, url, checks=((1, 'OK', None), (2, 'OK', None), (3, 'MISSING', 'COLLECT'), (5, 'MISMATCH', 'LEAVE')), reasons=(5,))
    try:
        expect(page.locator('.sticker-summary')).to_have_text('스티커·필증: 확인 2 · 문제 2 · 미확인 4')
        states = page.locator('.chk').evaluate_all('rows=>rows.map(row=>row.dataset.sticker)')
        assert states == ['ok', 'ok', 'problem', 'unchecked', 'problem', 'unchecked', 'unchecked', 'unchecked'], states
        expect(page.locator('.chk[data-sticker="problem"] .chk-label').first).to_have_text('가상 서랍장 시험용 · 문제 확인')
        expect(jump(page)).to_have_text('미확인 4 · 첫 품목으로 ↓')
        assert jump(page).evaluate('b=>b.getBoundingClientRect().height') >= 56
        before, saved = event_ids(page), [e['event_id'] for e in disk_state(page)['events']]
        jump(page).click()
        target = choice(page, '가상 옷장', '확인')
        reached(page, target)
        expect(target).to_have_attribute('aria-pressed', 'false')
        # 이동만 한다: 확인·수량·결정 기록은 그대로
        assert event_ids(page) == before and [e['event_id'] for e in disk_state(page)['events']] == saved
        assert sorted(record(page)['stickers']) == ['line-1', 'line-2', 'line-3', 'line-5']
        assert all(line['qty_collected'] == 0 for line in record(page)['lines']) and record(page)['outcome'] is None
        page.screenshot(path=str(output / 'jump-first-unchecked.png'))
        # 담당자가 그 품목의 단추를 직접 눌러야 기록된다. 다음 이동은 그다음 미확인 품목으로(문제 품목은 건너뜀)
        target.click()
        settle(page)
        added = [e for e in disk_state(page)['events'] if e['event_id'] not in saved]
        assert [(e['kind'], e['body']['line_id'], e['body']['result']) for e in added] == [('STICKER_CHECK', 'line-4', 'OK')], added
        expect(page.locator('.sticker-summary')).to_have_text('스티커·필증: 확인 3 · 문제 2 · 미확인 3')
        expect(jump(page)).to_have_text('미확인 3 · 첫 품목으로 ↓')
        jump(page).click()
        reached(page, choice(page, '가상 소파', '확인'))
        assert len(disk_state(page)['events']) == len(saved) + 1
        assert not errors, errors
        print('품목 8개: 미확인 4 → 첫 미확인 [확인] 단추로 이동·초점, 기록 없음 · 직접 눌러야 기록 · 문제 품목은 건너뜀 통과', flush=True)
    finally:
        ctx.close()

    # 2) 미확인이 없으면 이동 단추가 없다(문제 품목은 이미 확인한 것). 품목이 하나뿐이면 바로 아래라 두지 않는다
    ctx, page, errors = open_many(browser, url, checks=tuple((n, 'MISSING', 'COLLECT') if n == 3 else (n, 'OK', None) for n in range(1, 9)))
    try:
        expect(page.locator('.sticker-summary')).to_have_text('스티커·필증: 확인 7 · 문제 1 · 미확인 0')
        expect(jump(page)).to_have_count(0)
        assert not errors, errors
    finally:
        ctx.close()
    ctx, page, _ = open_case(browser, url)
    try:
        expect(page.locator('.sticker-summary')).to_contain_text('미확인 1')
        expect(jump(page)).to_have_count(0)
        print('미확인 없음·품목 하나: 이동 단추 없음 통과', flush=True)
    finally:
        ctx.close()

    # 3) 미확인은 없지만 처리할 품목이 남은 경우: 결정 전(가상 책상)·수거하기로 했는데 미수거 사유가 남음(가상 소파).
    #    결정까지 끝낸 문제 품목(가상 서랍장)은 대상이 아니다
    ctx, page, errors = open_many(browser, url, reasons=(3, 6),
                                  checks=tuple({2: (2, 'MISSING', None), 3: (3, 'MISMATCH', 'LEAVE')}.get(n, (n, 'OK', None)) for n in range(1, 9)))
    try:
        expect(page.locator('.sticker-summary')).to_have_text('스티커·필증: 확인 6 · 문제 2 · 미확인 0 · 확인한 품목 중 처리 필요 2')
        states = page.locator('.chk').evaluate_all('rows=>rows.map(row=>row.dataset.sticker)')
        assert states == ['ok', 'todo', 'problem', 'ok', 'ok', 'todo', 'ok', 'ok'], states
        labels = page.locator('.chk-label').all_inner_texts()
        assert labels[1] == '가상 책상 시험용 · 문제 확인 · 처리 필요' and labels[2] == '가상 서랍장 시험용 · 문제 확인' and labels[5] == '가상 소파 시험용 · 확인됨 · 처리 필요', labels
        expect(jump(page)).to_have_text('처리 필요 2 · 첫 품목의 필증 결정으로 ↓')
        before = event_ids(page)
        jump(page).click()
        target = choice(page, '가상 책상', '없음')
        reached(page, target)
        expect(target).to_have_attribute('aria-pressed', 'true')
        assert event_ids(page) == before
        # 결정도 담당자가 고를 때만: 선택창을 닫으면 기록이 없다
        target.click()
        expect(page.get_by_role('dialog')).to_contain_text('스티커·필증 없음')
        page.get_by_role('dialog').get_by_role('button', name='취소', exact=True).click()
        settle(page)
        assert event_ids(page) == before and record(page)['stickers']['line-2']['decision'] is None
        assert not errors, errors
        print('처리 필요(결정 전·사유 남음)와 문제(이미 확인·결정 끝) 구분, 결정 전 품목은 필증 결정 단추로 이동·결정은 직접 고를 때만 통과', flush=True)
    finally:
        ctx.close()

    # 3-1) 확인·수거 결정 뒤 미수거 사유만 남은 품목: 필증 단추를 다시 눌러도 사유는 그대로이므로 실제로 고칠 수 있는 품목 칸으로 보낸다.
    #      싣지 않은 품목(가상 소파)은 못 실은 사유 입력으로, 다 실어 사유 칸이 없는 품목(가상 식탁: 3개 중 이전 수거 1개)은 [이전 미수거 사유 해제]로.
    #      이동만으로는 기록이 없고, 해제는 수량을 그대로 둔 채 사유만 비운 정정 한 건이다
    ctx, page, errors = open_many(browser, url, **STALE_REASON)
    try:
        states = page.locator('.chk').evaluate_all('rows=>rows.map(row=>row.dataset.sticker)')
        assert states == ['ok', 'ok', 'problem', 'ok', 'ok', 'todo', 'todo', 'problem'], states
        # 해제 단추는 다 실음 + 확인/수거 결정 + 남은 NO_STICKER인 품목에만: 못 실은 품목(가상 소파)·미수거 결정 품목(가상 서랍장·가상 선반)에는 없다
        assert page.locator('.line:has(.reason-clear)').evaluate_all('rows=>rows.map(row=>row.dataset.itemLine)') == ['line-7']
        expect(jump(page)).to_have_text('처리 필요 2 · 첫 품목의 수량·사유로 ↑')
        before, saved, lines = event_ids(page), [e['event_id'] for e in disk_state(page)['events']], record(page)['lines']
        # 같은 필증 단추를 다시 눌러도 사유가 남는다는 근거(그래서 이 분기는 필증 단추로 보내지 않는다)
        choice(page, '가상 소파', '확인').click()
        settle(page)
        assert record(page)['lines'][5]['reason'] == 'NO_STICKER'
        expect(jump(page)).to_have_text('처리 필요 2 · 첫 품목의 수량·사유로 ↑')
        before, saved, lines = event_ids(page), [e['event_id'] for e in disk_state(page)['events']], record(page)['lines']
        jump(page).click()
        reason = page.get_by_role('combobox', name='가상 소파 못 실은 사유', exact=True)
        reached(page, reason)
        expect(reason).to_have_value('NO_STICKER')
        assert not page.evaluate('()=>!!document.activeElement.closest(".chk")')
        assert event_ids(page) == before and [e['event_id'] for e in disk_state(page)['events']] == saved and record(page)['lines'] == lines
        page.screenshot(path=str(output / 'jump-reason-left.png'))
        # 담당자가 사유를 직접 바꿔야 기록되고, 그 품목은 처리 필요에서 빠진다
        reason.select_option('CANNOT_LOAD')
        page.wait_for_function('()=>window.__collectionCore.st.events.at(-1).body.reason==="CANNOT_LOAD"')
        settle(page)
        added = [e for e in disk_state(page)['events'] if e['event_id'] not in saved]
        assert [(e['kind'], e['body']['line_id'], e['body']['qty_collected'], e['body']['reason']) for e in added] == [('ITEM_RESULT', 'line-6', 0, 'CANNOT_LOAD')], added
        assert page.locator('.chk').nth(5).get_attribute('data-sticker') == 'ok'
        # 다 실어서 사유 칸이 보이지 않는 품목: 이동은 [이전 미수거 사유 해제]에 초점만 준다
        expect(jump(page)).to_have_text('처리 필요 1 · 첫 품목의 수량·사유로 ↑')
        expect(page.get_by_role('combobox', name='가상 식탁 못 실은 사유', exact=True)).to_have_count(0)
        before, kept, lines = event_ids(page), [e['event_id'] for e in disk_state(page)['events']], record(page)['lines']
        assert (lines[6]['qty_confirmed'], lines[6]['qty_collected'], lines[6]['reason']) == (3, 3, 'NO_STICKER'), lines[6]
        jump(page).click()
        release = clear_button(page)
        reached(page, release)
        assert release.evaluate('b=>b.closest(".line").dataset.itemLine') == 'line-7'
        assert event_ids(page) == before and [e['event_id'] for e in disk_state(page)['events']] == kept and record(page)['lines'] == lines
        page.screenshot(path=str(output / 'jump-reason-clear.png'))
        # 담당자가 직접 눌러야(연달아 두 번 눌러도) 사유만 비운 정정 한 건: 현장 3개·실은 3개(이전 수거 1개 포함)는 그대로
        page.evaluate('()=>{const b=document.querySelector(".reason-clear");b.click();b.click();}')
        page.wait_for_function('()=>window.__collectionCore.st.events.at(-1).body.line_id==="line-7"')
        settle(page)
        added = [e for e in disk_state(page)['events'] if e['event_id'] not in kept]
        assert [(e['kind'], {k: v for k, v in e['body'].items() if k != 'worker'}) for e in added] == [
            ('ITEM_RESULT', {'line_id': 'line-7', 'line_kind': 'RECEIVED', 'qty_confirmed': 3, 'qty_collected': 3, 'reason': ''})], added
        assert event_ids(page) == before + [added[0]['event_id']]
        after = record(page)['lines']
        assert after[6] == {**lines[6], 'reason': ''} and after[:6] == lines[:6] and after[7] == lines[7], after
        expect(page.get_by_text('이전 수거 1개 · 이번 추가 2개 · 누적 3개', exact=True)).to_be_visible()
        expect(clear_button(page)).to_have_count(0)
        expect(jump(page)).to_have_count(0)
        states = page.locator('.chk').evaluate_all('rows=>rows.map(row=>row.dataset.sticker)')
        assert states == ['ok', 'ok', 'problem', 'ok', 'ok', 'ok', 'ok', 'problem'], states
        expect(page.locator('.sticker-summary')).to_have_text('스티커·필증: 확인 6 · 문제 2 · 미확인 0')
        expect(page.get_by_role('group', name='가상 식탁 실은 수량', exact=True).locator('.qty-value')).to_be_focused()
        assert record(page)['outcome'] is None
        assert not errors, errors
        print('사유만 남은 처리 필요: 필증 재선택으로는 사유 유지 · 못 실은 사유 입력/[이전 미수거 사유 해제]로 이동만(무기록) · 해제는 두 번 눌러도 수량 그대로 사유만 비운 정정 한 건 통과', flush=True)
    finally:
        ctx.close()

    # 3-2) [이전 미수거 사유 해제]의 저장 가드: 저장 대기, 예전 화면에 남은 단추, 잠금에서는 기록하지 않는다
    ctx, page, errors = open_many(browser, url, **STALE_REASON)
    try:
        expect(clear_button(page)).to_be_enabled()
        hold_failed_write(page, 1)
        choice(page, '가상 책상', '확인').click()
        page.wait_for_function('()=>typeof window.__releaseCollectionFailure==="function"')
        expect(clear_button(page)).to_be_disabled()
        page.evaluate('()=>window.__releaseCollectionFailure()')
        page.wait_for_function('()=>window.__collectionCore.saveFailed')
        expect(page.get_by_text('수거 기록 저장 대기', exact=True)).to_be_visible()
        expect(clear_button(page)).to_be_disabled()
        before = event_ids(page)
        page.evaluate('()=>document.querySelector(".reason-clear").click()')
        page.wait_for_timeout(100)
        assert event_ids(page) == before
        page.locator('#st-result').get_by_role('button', name='저장 다시 시도', exact=True).click()
        settle(page)
        assert [e['event_id'] for e in disk_state(page)['events']] == before
        page.evaluate('()=>{window.__staleClear=document.querySelector(".reason-clear");location.hash="#/home";}')
        expect(clear_button(page)).to_have_count(0)
        page.evaluate('()=>{location.hash="#/stop/workflow-1";}')
        expect(clear_button(page)).to_be_enabled()
        page.evaluate('()=>window.__staleClear.click()')
        page.wait_for_timeout(150)
        assert event_ids(page) == before
        page.evaluate("""async()=>{window.__clearOwner=window.__collectionCore.st;window.__staleClear=document.querySelector('.reason-clear');
            const S=await import('./store.js');S.lock();window.__staleClear.click();}""")
        page.wait_for_timeout(150)
        assert page.evaluate('()=>window.__clearOwner.events.map(e=>e.event_id)') == before
        page.evaluate('()=>window.__collectionCore.lockNow()')
        expect(clear_button(page)).to_have_count(0)
        page.evaluate('()=>window.__staleClear.click()')
        page.wait_for_timeout(150)
        assert page.evaluate('()=>window.__clearOwner.events.map(e=>e.event_id)') == before
        line = page.evaluate('()=>window.__collectionFees.deriveStop(window.__clearOwner.daylist.stops[0],window.__clearOwner.events).lines[6]')
        assert (line['qty_confirmed'], line['qty_collected'], line['reason']) == (3, 3, 'NO_STICKER'), line
        assert not errors, errors
        print('사유 해제 가드: 저장 중·저장 대기·예전 화면 단추·잠금에서는 기록 없음 통과', flush=True)
    finally:
        ctx.close()

    # 4) 저장 중·저장 대기로 묶인 화면에서는 이동 단추도 꺼지고, 다시 저장한 뒤에 쓸 수 있다
    ctx, page, errors = open_many(browser, url, checks=((1, 'OK', None),))
    try:
        hold_failed_write(page, 1)
        choice(page, '가상 책상', '확인').click()
        page.wait_for_function('()=>typeof window.__releaseCollectionFailure==="function"')
        expect(jump(page)).to_be_disabled()
        page.evaluate('()=>document.querySelector(".sk-jump").click()')
        assert page.locator('.chk.sk-jumped').count() == 0
        page.evaluate('()=>window.__releaseCollectionFailure()')
        page.wait_for_function('()=>window.__collectionCore.saveFailed')
        expect(page.get_by_text('수거 기록 저장 대기', exact=True)).to_be_visible()
        expect(jump(page)).to_be_disabled()
        before = event_ids(page)
        page.locator('#st-result').get_by_role('button', name='저장 다시 시도', exact=True).click()
        settle(page)
        expect(jump(page)).to_have_text('미확인 6 · 첫 품목으로 ↓')
        jump(page).click()
        reached(page, choice(page, '가상 서랍장', '확인'))
        assert event_ids(page) == before and [e['event_id'] for e in disk_state(page)['events']] == before
        assert len([e for e in disk_state(page)['events'] if e['kind'] == 'STICKER_CHECK']) == 2
        assert not errors, errors
        print('저장 중·저장 대기: 이동 단추 꺼짐 · 다시 저장 뒤 이동, 새 기록 없음 통과', flush=True)
    finally:
        ctx.close()

    # 5) 누르는 중 화면 이동·예전 화면의 단추·잠금: 새 화면을 움직이지 않고 기록도 없다
    ctx, page, errors = open_many(browser, url)
    try:
        page.evaluate('()=>{window.__staleJump=document.querySelector(".sk-jump");window.__staleJump.click();location.hash="#/home";}')
        expect(jump(page)).to_have_count(0)
        page.wait_for_function('()=>location.hash==="#/home"&&scrollY===0')
        assert event_ids(page) == []
        page.evaluate('()=>{location.hash="#/stop/workflow-1";}')
        expect(jump(page)).to_have_text('미확인 8 · 첫 품목으로 ↓')
        top = page.evaluate('()=>scrollY')
        page.evaluate('()=>window.__staleJump.click()')
        page.wait_for_timeout(400)
        assert page.evaluate('()=>scrollY') == top
        assert page.locator('.chk.sk-jumped').count() == 0
        assert not page.evaluate('()=>!!document.activeElement?.closest(".chk-opts")')
        # 저장소만 먼저 잠긴 순간(화면은 아직 그대로)에도 움직이지 않고, 잠금 화면으로 바뀐 뒤 예전 단추도 마찬가지
        page.evaluate('async()=>{window.__staleJump=document.querySelector(".sk-jump");const S=await import("./store.js");S.lock();window.__staleJump.click();}')
        page.wait_for_timeout(100)
        assert page.locator('.sk-jumped').count() == 0 and page.evaluate('()=>scrollY') == top
        assert not page.evaluate('()=>!!document.activeElement?.closest(".chk-opts")')
        page.evaluate('()=>window.__collectionCore.lockNow()')
        expect(jump(page)).to_have_count(0)
        page.evaluate('()=>window.__staleJump.click()')
        page.wait_for_timeout(100)
        assert not page.evaluate('()=>window.__collectionCore.st?.events.length')
        assert not errors, errors
        print('누르는 중 목록 이동·예전 화면 단추·잠금 뒤 누름: 화면·초점·기록 변화 없음 통과', flush=True)
    finally:
        ctx.close()

    # 6) 360px·가장 큰 글자에서도 이동 단추와 도착한 단추를 누를 수 있다
    ctx, page, errors = open_many(browser, url, checks=((1, 'OK', None), (2, 'MISSING', 'COLLECT')))
    try:
        page.set_viewport_size({'width': 360, 'height': 640})
        page.evaluate('async()=>{const C=window.__collectionCore;C.st.textSize="xl";C.applyTextSize();await C.render();}')
        expect(jump(page)).to_have_text('미확인 6 · 첫 품목으로 ↓')
        jump(page).scroll_into_view_if_needed()
        assert jump(page).evaluate('b=>{const r=b.getBoundingClientRect(),e=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return r.height>=56&&(e===b||b.contains(e));}')
        jump(page).click()
        reached(page, choice(page, '가상 서랍장', '확인'))
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(output / 'jump-360-xl.png'))
        assert event_ids(page) != [] and len(event_ids(page)) == 2
        assert not errors, errors
        print('360px·XL 글자: 이동 단추 56px 이상·가로 넘침 없음·도착 단추 누를 수 있음 통과', flush=True)
    finally:
        ctx.close()


def main(case):
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-mobile-sticker-')) / 'shots'
    output.mkdir(parents=True, exist_ok=True)
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        try:
            for name, fn in [('semantics', semantics_case), ('number', number_case), ('ownership', ownership_case), ('pending', pending_case), ('accepted', accepted_case), ('queued', queued_case), ('layout', layout_case), ('jump', jump_case)]:
                if case in ('all', name):
                    fn(browser, url, output)
        finally:
            browser.close()
    print('소스 SHA256:', json.dumps({name: hashlib.sha256((ROOT / 'web' / name).read_bytes()).hexdigest() for name in ('screens/stop.js', 'style.css')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=('all', 'semantics', 'number', 'ownership', 'pending', 'accepted', 'queued', 'layout', 'jump'), default='all')
    main(parser.parse_args().case)
