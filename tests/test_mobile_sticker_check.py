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

ROOT = Path(__file__).resolve().parents[1]


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


def main(case):
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-mobile-sticker-')) / 'shots'
    output.mkdir(parents=True, exist_ok=True)
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        try:
            for name, fn in [('semantics', semantics_case), ('number', number_case), ('ownership', ownership_case), ('pending', pending_case), ('accepted', accepted_case), ('queued', queued_case), ('layout', layout_case)]:
                if case in ('all', name):
                    fn(browser, url, output)
        finally:
            browser.close()
    print('소스 SHA256:', json.dumps({name: hashlib.sha256((ROOT / 'web' / name).read_bytes()).hexdigest() for name in ('screens/stop.js', 'style.css')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=('all', 'semantics', 'number', 'ownership', 'pending', 'accepted', 'queued', 'layout'), default='all')
    main(parser.parse_args().case)
