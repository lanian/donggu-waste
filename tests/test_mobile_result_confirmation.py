"""실제 PIN/IndexedDB: 수량 수정 뒤 결과 재확정과 부분 수량 보호."""
import argparse
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_review_ui import PIN, mobile_server
from test_mobile_collection_save import open_case


def state(page):
    return page.evaluate("""() => {
        const C=window.__collectionCore, stop=C.st.daylist.stops[0];
        return {derived:window.__collectionFees.deriveStop(stop,C.st.events),
            ids:C.st.events.map(e=>e.event_id), attempts:C.st.events.filter(e=>e.kind==='ATTEMPT').map(e=>e.body.outcome)};
    }""")


def settle(page):
    page.evaluate('async()=>{await window.__collectionCore.saving;}')


def dirty_case(browser, url, shots):
    context, page, receipt = open_case(browser, url, qty=3)
    try:
        page.get_by_role('button', name='전부 실음 · 수거 완료', exact=True).click()
        expect(page.locator('.dock-next')).to_have_text('다음 2번 ▶')
        page.get_by_role('button', name='가상 의자 실은 수량 빼기', exact=True).click()
        page.wait_for_function('()=>window.__collectionFees.deriveStop(window.__collectionCore.st.daylist.stops[0],window.__collectionCore.st.events).lines[0].qty_collected===2')
        settle(page)
        expect(page.locator('.dock-next')).to_have_text('수거 결과 재확인 ↓')
        expect(page.locator('main > .bar .state')).to_have_text('결과 재확인 필요')
        expect(page.get_by_role('button', name='PC 입력 도움', exact=True)).to_be_disabled()
        before = state(page)
        assert before['derived']['outcome'] == 'DONE' and before['derived']['resultNeedsConfirmation'] is True
        assert not page.locator('.steps a').last.evaluate("e=>e.classList.contains('done')")
        page.locator('.dock-next').click()
        assert state(page)['ids'] == before['ids'], '재확인 영역 이동은 기록을 추가하지 않는다'
        page.screenshot(path=str(shots / 'result-reconfirmation-needed.png'))
        page.reload()
        page.get_by_placeholder('PIN', exact=True).fill(PIN)
        page.get_by_role('button', name='잠금 해제', exact=True).click()
        page.evaluate("async()=>{window.__collectionCore=await import('./core.js');window.__collectionFees=await import('./fees.js');}")
        expect(page.locator('.dock-next')).to_have_text('수거 결과 재확인 ↓')
        page.goto(url + '#/pc-input/' + receipt['request_id'])
        expect(page.locator('.pc-input-help')).to_contain_text('결과 재확인 필요')
        expect(page.get_by_role('button', name='전달 내용 복사', exact=True)).to_be_disabled()
        page.get_by_role('button', name='접수 상세로', exact=True).click()
        page.get_by_label('가상 의자 못 실은 사유', exact=True).select_option('CANNOT_LOAD')
        settle(page)
        page.get_by_role('button', name='일부 수거', exact=True).click()
        expect(page.locator('.dock-next')).to_have_text('다음 2번 ▶')
        final = state(page)
        assert final['derived']['outcome'] == 'PARTIAL' and final['derived']['resultNeedsConfirmation'] is False
        assert final['attempts'] == ['DONE', 'PARTIAL'], final
        expect(page.get_by_role('button', name='PC 입력 도움', exact=True)).to_be_enabled()
        page.screenshot(path=str(shots / 'result-reconfirmed.png'))
        print('수거 결과 재확인: 완료후수량수정·다음/복사보호·재실행보존·새결과저장해제 통과', flush=True)
    finally:
        context.close()


def overwrite_case(browser, url, shots):
    context, page, _ = open_case(browser, url, qty=3)
    try:
        page.get_by_role('button', name='가상 의자 실은 수량 더하기', exact=True).click()
        page.wait_for_function('()=>window.__collectionFees.deriveStop(window.__collectionCore.st.daylist.stops[0],window.__collectionCore.st.events).lines[0].qty_collected===1')
        settle(page)
        before = state(page)
        page.get_by_role('button', name='전부 실음 · 수거 완료', exact=True).click()
        dialog = page.get_by_role('dialog', name='수량을 채우고 완료할까요?', exact=True)
        expect(dialog).to_be_visible()
        expect(dialog).to_contain_text('1개 → 3개')
        dialog.screenshot(path=str(shots / 'partial-overwrite-summary.png'))
        dialog.get_by_role('button', name='취소', exact=True).click()
        expect(dialog).to_have_count(0)
        assert state(page)['ids'] == before['ids'] and state(page)['derived']['lines'][0]['qty_collected'] == 1
        page.get_by_role('button', name='전부 실음 · 수거 완료', exact=True).click()
        page.get_by_role('dialog', name='수량을 채우고 완료할까요?', exact=True).get_by_role('button', name='수량 채우고 완료', exact=True).click()
        expect(page.locator('.dock-next')).to_have_text('다음 2번 ▶')
        final = state(page)
        assert final['derived']['lines'][0]['qty_collected'] == 3 and final['attempts'] == ['DONE']
        page.screenshot(path=str(shots / 'partial-overwrite-confirmed.png'))
        print('부분 수량 보호: 변경요약·취소무변경·명시확인후전체완료 통과', flush=True)
    finally:
        context.close()


def stale_prompt_case(browser, url, shots):
    context, page, _ = open_case(browser, url, qty=3)
    try:
        page.get_by_role('button', name='가상 의자 실은 수량 더하기', exact=True).click()
        page.wait_for_function('()=>window.__collectionCore.st.events.length===1')
        settle(page)
        before = state(page)
        page.get_by_role('button', name='전부 실음 · 수거 완료', exact=True).click()
        dialog = page.get_by_role('dialog', name='수량을 채우고 완료할까요?', exact=True)
        expect(dialog).to_be_visible()
        page.evaluate("location.hash='#/stop/workflow-2'")
        expect(page.locator('.who .name')).to_have_text('가상 주민 2')
        dialog.get_by_role('button', name='수량 채우고 완료', exact=True).click()
        assert state(page)['ids'] == before['ids'], '이동한 뒤 옛 확인창은 수거 기록을 만들면 안 된다'
        assert state(page)['derived']['lines'][0]['qty_collected'] == 1
        print('부분 수량 확인: 다른 접수 이동뒤 늦은확인도 기존수량/기록 보존 통과', flush=True)
    finally:
        context.close()


def derivation_case(browser, url, shots):
    context, page, receipt = open_case(browser, url)
    try:
        result = page.evaluate("""receipt=>{
            const F=window.__collectionFees,id=receipt.request_id,line=receipt.items[0].line_id;
            const ev=(kind,body={},extra={})=>({request_id:id,kind,body,...extra});
            const attempt=ev('ATTEMPT',{outcome:'DONE'}),quantity=ev('ITEM_RESULT',{line_id:line,qty_confirmed:1,qty_collected:0});
            const base=[attempt,ev('CONTACT',{result:'REACHED'}),ev('PHOTO_META',{id:'fake'})];
            const derive=(stop,events)=>F.deriveStop(stop,events);
            return {unchanged:derive(receipt,base), dirty:derive(receipt,[...base,quantity]),
                confirmed:derive(receipt,[...base,quantity,attempt]),
                old:derive({...receipt,since_seq:10},[{...attempt,seq:9},{...quantity,seq:10}]),
                version:derive({...receipt,pc_collection_ver:2},[
                    ev('ATTEMPT',{outcome:'DONE',pc_collection_ver:2},{seq:11,base_req_ver:2}),
                    ev('ITEM_RESULT',{...quantity.body,pc_collection_ver:1},{seq:12,base_req_ver:1})]),
                added:derive(receipt,[attempt,ev('FIELD_ITEM_ADD',{line_id:'added',name:'가상 추가',unit:'EA',unit_label:'개',qty:1})])};
        }""", receipt)
        assert result['unchanged']['resultNeedsConfirmation'] is False
        assert result['dirty']['resultNeedsConfirmation'] is True and result['dirty']['outcome'] == 'DONE'
        assert result['confirmed']['resultNeedsConfirmation'] is False
        assert result['old']['resultNeedsConfirmation'] is False and result['version']['resultNeedsConfirmation'] is False
        assert result['added']['resultNeedsConfirmation'] is True
        print('수거 결과 파생: 순서·기존결과보존·무관기록·이전목록/PC버전제외·현장품목추가 통과', flush=True)
    finally:
        context.close()


def consumer_case(browser, url, shots):
    context, page, _ = open_case(browser, url, qty=3)
    try:
        page.evaluate("""async()=>{
            const S=await import('./store.js');
            await S.putRaw('device',window.__workflowDevice);await S.putRaw('pc',window.__workflowPc);
        }""")
        page.get_by_role('button', name='전부 실음 · 수거 완료', exact=True).click()
        expect(page.locator('.dock-next')).to_have_text('다음 2번 ▶')
        page.get_by_role('button', name='가상 의자 실은 수량 빼기', exact=True).click()
        page.wait_for_function('()=>window.__collectionFees.deriveStop(window.__collectionCore.st.daylist.stops[0],window.__collectionCore.st.events).lines[0].qty_collected===2')
        settle(page)
        before = state(page)['ids']
        page.goto(url + '#/home')
        expect(page.locator('.next-action').get_by_role('button', name='1번 결과 재확인', exact=True)).to_be_visible()
        page.goto(url + '#/export')
        expect(page.get_by_role('button', name='PC 화면 카메라로 보내기 (QR)', exact=True)).to_be_disabled()
        expect(page.get_by_role('button', name='결과 파일 만들어 보내기', exact=False)).to_be_disabled()
        expect(page.get_by_role('button', name='오늘 자료 정리', exact=True)).to_be_disabled()
        page.get_by_role('button', name='기록 보호용 백업', exact=True).click()
        expect(page).to_have_url(url + '#/backup')
        page.evaluate("""async()=>{
            const C=window.__collectionCore;C.st.mode='SOLO';C.st.requests=C.st.daylist.stops;
            location.hash='#/home';await C.render();
        }""")
        expect(page.locator('.next-action').get_by_role('button', name='1번 결과 재확인', exact=True)).to_be_visible()
        assert state(page)['ids'] == before, '재확인 안내/보호용 백업 진입은 수거 기록을 변경하지 않는다'
        page.screenshot(path=str(shots / 'home-reconfirmation.png'))
        print('수거 결과 재확인: PC/단독홈우선안내·일반전달/정리보호·보호용백업진입 통과', flush=True)
    finally:
        context.close()


def queued_help_case(browser, url, shots):
    context, page, receipt = open_case(browser, url, qty=3)
    try:
        page.get_by_role('button', name='전부 실음 · 수거 완료', exact=True).click()
        expect(page.locator('.dock-next')).to_have_text('다음 2번 ▶')
        page.evaluate("""()=>{
            const later=window.setTimeout;window.__heldQuantity=[];
            window.setTimeout=(fn,delay,...args)=>delay===350
                ? (window.__heldQuantity.push(()=>fn(...args)),100000+window.__heldQuantity.length)
                : later(fn,delay,...args);
        }""")
        page.get_by_role('button', name='가상 의자 실은 수량 빼기', exact=True).click()
        assert page.evaluate('window.__heldQuantity.length') == 1
        page.get_by_role('button', name='PC 입력 도움', exact=True).click()
        page.wait_for_function('()=>window.__collectionFees.deriveStop(window.__collectionCore.st.daylist.stops[0],window.__collectionCore.st.events).lines[0].qty_collected===2')
        expect(page).to_have_url(url + '#/stop/' + receipt['request_id'])
        expect(page.get_by_role('button', name='PC 입력 도움', exact=True)).to_be_disabled()
        expect(page.locator('.dock-next')).to_have_text('수거 결과 재확인 ↓')
        assert state(page)['attempts'] == ['DONE']
        # 직접 도움 주소로 이동해도 이전 결과를 복사할 수 없다.
        page.goto(url + '#/pc-input/' + receipt['request_id'])
        expect(page.get_by_role('button', name='전달 내용 복사', exact=True)).to_be_disabled()
        expect(page.locator('.pc-input-help')).to_contain_text('이번 수거 2개')
        print('수거 입력 도움: 예약된수량먼저저장·재확인전진입/복사보호 통과', flush=True)
    finally:
        context.close()
    context, page, receipt = open_case(browser, url, qty=3)
    try:
        page.get_by_role('button', name='전부 실음 · 수거 완료', exact=True).click()
        expect(page.locator('.dock-next')).to_have_text('다음 2번 ▶')
        page.evaluate("""()=>{
            const later=window.setTimeout;window.__heldQuantity=[];
            window.setTimeout=(fn,delay,...args)=>delay===350
                ? (window.__heldQuantity.push(()=>fn(...args)),100000+window.__heldQuantity.length)
                : later(fn,delay,...args);
        }""")
        page.get_by_role('button', name='가상 의자 실은 수량 빼기', exact=True).click()
        page.goto(url + '#/pc-input/' + receipt['request_id'])
        expect(page.locator('.pc-input-help')).to_contain_text('이번 수거 2개')
        expect(page.locator('.pc-input-help')).to_contain_text('결과 재확인 필요')
        expect(page.get_by_role('button', name='전달 내용 복사', exact=True)).to_be_disabled()
        assert state(page)['derived']['resultNeedsConfirmation'] is True
        print('수거 입력 도움: 직접주소 진입도 예약수량 flush후 현재값/재확인 보호 통과', flush=True)
    finally:
        context.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=('all', 'dirty', 'overwrite', 'stale', 'derive', 'consumer', 'queued-help'), default='all')
    args = parser.parse_args()
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-result-confirm-'))
    shots = output / 'shots'
    shots.mkdir(parents=True, exist_ok=True)
    failures = []
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        try:
            for name, case in [('dirty', dirty_case), ('overwrite', overwrite_case), ('stale', stale_prompt_case), ('derive', derivation_case), ('consumer', consumer_case), ('queued-help', queued_help_case)]:
                if args.case not in ('all', name):
                    continue
                try:
                    case(browser, url, shots)
                except Exception as error:
                    failures.append({'case': name, 'error': str(error)})
                    print(name + ' FAIL: ' + str(error), flush=True)
        finally:
            browser.close()
    if failures:
        (output / 'failures.json').write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding='utf-8')
        raise AssertionError(f'{len(failures)} collection result cases failed')


if __name__ == '__main__':
    main()
