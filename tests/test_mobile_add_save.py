"""실제 PIN/IndexedDB: 현장 품목 추가 저장 실패·재시도·잠금 경계."""
import argparse
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_mobile_collection_save import disk_state
from test_review_ui import mobile_server, unlock
from test_workflow_mobile import daylist, open_mobile, seed, stop


def writes(page, *, fail_at=0, hold_at=0):
    page.evaluate("""opts=>{
        window.__writes=0;window.__failAt=opts.fail_at;window.__holdAt=opts.hold_at;window.__releases=[];
        const put=IDBObjectStore.prototype.put,complete=Object.getOwnPropertyDescriptor(IDBTransaction.prototype,'oncomplete');
        IDBObjectStore.prototype.put=function(value,key){
          if(key==='s:state'){
            const n=++window.__writes;
            if(window.__failAt&&n>=window.__failAt)throw new DOMException('fixture quota','QuotaExceededError');
            if(window.__holdAt&&n===window.__holdAt)this.transaction.__heldFieldWrite=true;
          }
          return put.apply(this,arguments);
        };
        Object.defineProperty(IDBTransaction.prototype,'oncomplete',{...complete,set(fn){
          complete.set.call(this,typeof fn!=='function'?fn:function(event){
            if(this.__heldFieldWrite&&window.__holdAt)window.__releases.push(()=>fn.call(this,event));
            else fn.call(this,event);
          });
        }});
    }""", {'fail_at': fail_at, 'hold_at': hold_at})


def release(page):
    page.evaluate('()=>{window.__holdAt=0;window.__releases.splice(0).forEach(fn=>fn());}')


def case(browser, url, mode='PC', *, merge=False):
    context, page = open_mobile(browser, url)
    today = page.evaluate("import('./core.js').then(C=>C.todayKST())")
    record = stop(1, today)
    seed(page, {'mode': mode, 'daylist': daylist(today, [record]), 'requests': [record] if mode == 'SOLO' else []}, registered=mode == 'PC')
    page.evaluate("""async args=>{
      const C=await import('./core.js'),{BUILTIN_FEES:T}=await import('./fees-data.js');window.__C=C;
      C.st.daylist.fee_table={version_id:T.version_id};
      const rates=T.rows.filter(r=>r.status==='CONFIRMED'&&r.price>0).slice(0,2);
      const items=rates.map((r,i)=>({line_id:'added-'+i,fee_rate_id:r.id,name:r.name,spec:r.spec,unit:r.unit,unit_label:r.unit_label,
        qty:i+2,unit_price:r.price,price_status:r.status,amount:r.price*(i+2)}));
      if(args.merge){const s=C.findStop('workflow-1');await C.addEvent(s,'FIELD_ITEM_ADD',{...items[0],line_id:'existing-field',qty:3,fee_version_id:T.version_id});}
      C.ui.add={stop:'workflow-1',q:'',cat:'',items:args.merge?[items[0]]:items};
      await C.commit();location.hash='#/add/workflow-1';await C.render();
    }""", {'merge': merge})
    expect(page.get_by_role('heading', name='현장 품목 추가', exact=True)).to_be_visible()
    return context, page


def ids(page):
    return page.evaluate('()=>window.__C.st.events.map(e=>e.event_id)')


def failure_case(browser, url, shots):
    for mode in ('PC', 'SOLO'):
        context, page = case(browser, url, mode)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            writes(page, fail_at=2)
            page.get_by_role('button', name='추가 저장 (5)', exact=True).click()
            retry = page.locator('.pick-foot').get_by_role('button', name='저장 다시 시도', exact=True)
            try:
                expect(page).to_have_url(url + '#/add/workflow-1', timeout=3500)
                expect(retry).to_be_visible(timeout=3500)
            except BaseException:
                page.screenshot(path=str(shots / f'red-add-{mode.lower()}.png'), full_page=True)
                raise
            expect(page).to_have_url(url + '#/add/workflow-1')
            before = ids(page)
            assert len(before) == 2 and len(set(before)) == 2
            assert len(disk_state(page)['events']) == 1, '중간 저장까지만 디스크에 반영된 가상 실패'
            assert page.evaluate('()=>window.__C.ui.add.items.reduce((n,i)=>n+i.qty,0)') == 5
            retry.click()
            expect(retry).to_be_enabled()
            assert ids(page) == before
            page.screenshot(path=str(shots / f'add-retry-{mode.lower()}.png'), full_page=True)
            page.evaluate('window.__failAt=0')
            retry.click()
            expect(page).to_have_url(url + '#/stop/workflow-1')
            saved = disk_state(page)
            assert [e['event_id'] for e in saved['events']] == before
            assert [e['body']['qty'] for e in saved['events']] == [2, 3]
            assert all(e['gen'] for e in saved['events'])
            assert not errors, errors
        finally:
            context.close()
    print('PASS: PC/단독 다중 품목 중간 실패·입력 유지·동일 이벤트 재시도', flush=True)


def merge_case(browser, url, shots):
    context, page = case(browser, url, merge=True)
    try:
        writes(page, fail_at=1)
        done = page.locator('.pick-foot > button')
        done.click()
        done.dispatch_event('click')
        expect(page.get_by_role('heading', name='같은 품목이 있습니다', exact=True)).to_have_count(1)
        page.get_by_role('button', name='기존 행에 더하기', exact=True).click()
        retry = page.locator('.pick-foot').get_by_role('button', name='저장 다시 시도', exact=True)
        expect(retry).to_be_enabled()
        before = ids(page)
        assert len(before) == 2
        assert page.evaluate('()=>window.__C.st.events.at(-1).body.qty_confirmed') == 5
        retry.click()
        expect(retry).to_be_enabled()
        assert ids(page) == before
        page.evaluate('window.__failAt=0')
        retry.click()
        expect(page).to_have_url(url + '#/stop/workflow-1')
        assert [e['event_id'] for e in disk_state(page)['events']] == before
    finally:
        context.close()
    print('PASS: 병합 선택 연타·실패 재시도에도 같은 품목 수량 한 번만 증가', flush=True)


def late_lock_case(browser, url, shots):
    context, page = case(browser, url)
    try:
        page.evaluate("window.__initialAddView=document.querySelector('main')")
        writes(page, hold_at=1)
        done = page.locator('.pick-foot > button')
        done.click()
        page.wait_for_function('()=>window.__releases.length===1')
        assert len(ids(page)) == 2, '모든 행은 첫 쓰기를 기다리기 전에 한 번만 준비한다'
        done.dispatch_event('click')
        assert len(ids(page)) == 2
        page.evaluate("()=>{location.hash='#/settings';}")
        try:
            expect(page.get_by_role('heading', name='설정·정보', exact=True)).to_be_visible(timeout=2500)
        except BaseException:
            page.screenshot(path=str(shots / 'red-add-navigation.png'), full_page=True)
            raise
        page.get_by_label('작업자 이름', exact=True).fill('가상 새 초안')
        page.goto(url + '#/add/workflow-1')
        expect(page.get_by_role('heading', name='현장 품목 추가', exact=True)).to_be_visible()
        assert page.evaluate("document.querySelector('main')!==window.__initialAddView")
        expect(page.locator('.pick-foot > button')).to_be_disabled()
        page.locator('.pick-foot > button').dispatch_event('click')
        assert len(ids(page)) == 2
        page.goto(url + '#/settings')
        expect(page.get_by_role('heading', name='설정·정보', exact=True)).to_be_visible()
        page.get_by_label('작업자 이름', exact=True).fill('가상 다른 초안')
        release(page)
        expect(page).to_have_url(url + '#/settings')
        expect(page.get_by_role('heading', name='설정·정보', exact=True)).to_be_visible()
        expect(page.get_by_label('작업자 이름', exact=True)).to_have_value('가상 다른 초안')
        assert len(disk_state(page)['events']) == 2
        page.goto(url + '#/add/workflow-1')
        expect(page.get_by_role('button', name='저장된 품목 보기', exact=True)).to_be_visible()
        assert len(ids(page)) == 2
    finally:
        context.close()
    context, page = case(browser, url)
    try:
        writes(page, hold_at=1)
        page.get_by_role('button', name='추가 저장 (5)', exact=True).click()
        page.wait_for_function('()=>window.__releases.length===1')
        page.evaluate('()=>{window.__locked=false;window.__C.lockNow().then(()=>window.__locked=true);}')
        assert page.evaluate("import('./store.js').then(S=>S.isUnlocked())")
        assert not page.evaluate('window.__locked')
        release(page)
        expect(page.get_by_placeholder('PIN', exact=True)).to_be_visible()
        assert len(disk_state(page)['events']) == 2
        unlock(page)
        page.goto(url + '#/add/workflow-1')
        expect(page.get_by_role('button', name='닫기', exact=True)).to_be_visible()
        assert len(ids(page)) == 2
    finally:
        context.close()
    print('PASS: 늦은 저장은 다른 화면 이동을 덮지 않으며 잠금은 전체 품목 저장을 기다림', flush=True)


def lifetime_case(browser, url, shots):
    context, page = case(browser, url)
    try:
        writes(page, hold_at=2)
        page.get_by_role('button', name='추가 저장 (5)', exact=True).click()
        page.wait_for_function('()=>window.__releases.length===1')
        page.evaluate("""()=>{
          window.__oldAddView=document.querySelector('main');
          location.hash='#/settings';location.hash='#/add/workflow-1';
          window.__holdAt=0;window.__releases.splice(0).forEach(fn=>fn());
        }""")
        try:
            expect(page).to_have_url(url + '#/add/workflow-1', timeout=2500)
            expect(page.get_by_role('button', name='저장된 품목 보기', exact=True)).to_be_visible(timeout=2500)
        except BaseException:
            page.screenshot(path=str(shots / 'red-add-samehash.png'), full_page=True)
            raise
        assert page.evaluate("document.querySelector('main')!==window.__oldAddView")
        assert len(ids(page)) == 2 and len(disk_state(page)['events']) == 2
    finally:
        context.close()
    print('PASS: 다른 route→같은 hash 즉시 복귀의 옛 DOM ACK도 새 화면을 강제 이동하지 않음', flush=True)


def limit_case(browser, url, shots):
    context, page = case(browser, url, merge=True)
    try:
        page.evaluate("async()=>{window.__C.ui.add.items[0].qty=9999;await window.__C.render();}")
        page.get_by_role('button', name='추가 저장 (9999)', exact=True).click()
        page.get_by_role('button', name='기존 행에 더하기', exact=True).click()
        try:
            expect(page.get_by_text('같은 행의 수량은 9999를 넘을 수 없습니다. 따로 한 줄 더 추가해 주세요.', exact=True)).to_be_visible(timeout=2500)
        except BaseException:
            page.screenshot(path=str(shots / 'red-add-merge-limit.png'), full_page=True)
            raise
        assert len(ids(page)) == 1, '상한 초과 병합은 이벤트를 만들지 않는다'
        page.get_by_role('button', name='추가 저장 (9999)', exact=True).click()
        page.get_by_role('button', name='따로 한 줄 더 추가', exact=True).click()
        expect(page).to_have_url(url + '#/stop/workflow-1')
        assert disk_state(page)['events'][-1]['body']['qty'] == 9999
    finally:
        context.close()
    print('PASS: 기존 행 병합9999 상한·별도 줄로 추가는 정상 보존', flush=True)


def modal_case(browser, url, shots):
    context, page = case(browser, url, merge=True)
    try:
        page.get_by_role('button', name='추가 저장 (2)', exact=True).click()
        page.get_by_role('button', name='돌아가기', exact=True).click()
        expect(page.get_by_role('button', name='추가 저장 (2)', exact=True)).to_be_enabled()
        assert len(ids(page)) == 1
        page.get_by_role('button', name='추가 저장 (2)', exact=True).click()
        page.evaluate("location.hash='#/settings'")
        expect(page.get_by_role('heading', name='설정·정보', exact=True)).to_be_visible()
        expect(page.get_by_role('heading', name='같은 품목이 있습니다', exact=True)).to_have_count(0)
        page.goto(url + '#/add/workflow-1')
        expect(page.get_by_role('button', name='추가 저장 (2)', exact=True)).to_be_enabled()
        assert len(ids(page)) == 1
        page.get_by_role('button', name='추가 저장 (2)', exact=True).click()
        expect(page.get_by_role('heading', name='같은 품목이 있습니다', exact=True)).to_be_visible()
        assert page.evaluate('()=>window.__C.stepFlush.size') == 0
        page.evaluate('()=>{window.__C.lockNow();}')
        expect(page.get_by_placeholder('PIN', exact=True)).to_be_visible()
        assert len(disk_state(page)['events']) == 1
        unlock(page)
        page.goto(url + '#/add/workflow-1')
        expect(page.get_by_role('button', name='닫기', exact=True)).to_be_visible()
        page.evaluate("async()=>{window.__C.st.daylist.fee_table=null;await window.__C.render();}")
        expect(page.get_by_text('PC와 앱의 수수료표 버전이 다릅니다. 앱을 업데이트하거나 담당자에게 문의하세요.', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='← 취소', exact=True)).to_be_visible()
    finally:
        context.close()
    print('PASS: 병합 취소/선택 중 잠금은 무기록 취소·요금표 없음 안내', flush=True)


def main(only='all'):
    shots = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-add-save-')) / 'shots'
    shots.mkdir(parents=True, exist_ok=True)
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        try:
            if only == 'all':
                failure_case(browser, url, shots)
                merge_case(browser, url, shots)
                late_lock_case(browser, url, shots)
                modal_case(browser, url, shots)
            if only in ('all', 'limit'):
                limit_case(browser, url, shots)
            if only == 'route':
                late_lock_case(browser, url, shots)
            if only in ('all', 'lifetime'):
                lifetime_case(browser, url, shots)
        finally:
            browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=('all', 'limit', 'route', 'lifetime'), default='all')
    main(parser.parse_args().case)
