"""현장 메모: 실제 PIN/암호화 IndexedDB 실패·재시도·지연 응답 회귀."""
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_review_ui import PIN, mobile_server
from test_mobile_collection_save import disk_state, open_case


def state(page):
    return page.evaluate("()=>({events:window.__collectionCore.st.events,seq:window.__collectionCore.st.seq})")


def failure_case(browser, url, shots):
    context, page, _ = open_case(browser, url)
    try:
        page.evaluate("""()=>{window.__memoPut=IDBObjectStore.prototype.put;
          IDBObjectStore.prototype.put=function(value,key){if(key==='s:state')throw new DOMException('synthetic memo quota','QuotaExceededError');return window.__memoPut.apply(this,arguments);};}""")
        draft = page.get_by_role('textbox', name='현장 메모', exact=True)
        draft.fill('가상 현장 메모 원본')
        page.get_by_role('button', name='메모 남기기', exact=True).click()
        page.wait_for_function('()=>window.__collectionCore.saveFailed===true')
        expect(draft).to_have_value('가상 현장 메모 원본')
        expect(page.get_by_text('메모를 남겼습니다', exact=True)).to_have_count(0)
        expect(page.get_by_text('메모 저장 대기', exact=True)).to_be_visible()
        first = state(page)
        assert len(first['events']) == 1 and first['events'][0]['kind'] == 'FIELD_MEMO'
        assert disk_state(page)['events'] == []
        retry = page.get_by_role('button', name='메모 저장 다시 시도', exact=True)
        retry.click()
        expect(page.get_by_text('메모 저장 대기', exact=True)).to_be_visible()
        assert state(page) == first
        page.screenshot(path=str(shots / 'memo-save-failure.png'), full_page=True)
        draft.fill('실패 뒤 새로 적는 가상 메모')
        page.evaluate('()=>{IDBObjectStore.prototype.put=window.__memoPut;}')
        retry.click()
        expect(page.get_by_text('메모 저장 대기', exact=True)).to_have_count(0)
        expect(draft).to_have_value('실패 뒤 새로 적는 가상 메모')
        expect(draft).to_be_focused()
        assert state(page) == first
        assert disk_state(page)['events'] == first['events']
        page.get_by_role('button', name='메모 남기기', exact=True).click()
        expect(draft).to_have_value('')
        assert len(disk_state(page)['events']) == 2
        assert disk_state(page)['events'][0] == first['events'][0]
        print('PASS: 메모 저장 실패는 입력을 보존하고 동일 이벤트/순번으로 재시도함', flush=True)
    finally:
        context.close()


def hold_write(page):
    page.evaluate("""()=>{
      window.__memoHold=true;
      const descriptor=Object.getOwnPropertyDescriptor(IDBTransaction.prototype,'oncomplete');
      Object.defineProperty(IDBTransaction.prototype,'oncomplete',{...descriptor,set(fn){
        descriptor.set.call(this,typeof fn!=='function'?fn:function(event){
          if(this.__heldMemo)window.__releaseMemo=()=>fn.call(this,event);else fn.call(this,event);
        });
      }});
      const put=IDBObjectStore.prototype.put;
      IDBObjectStore.prototype.put=function(value,key){
        if(key==='s:state'&&window.__memoHold)this.transaction.__heldMemo=true;
        return put.apply(this,arguments);
      };
    }""")


def late_case(browser, url, shots, reenter):
    context, page, first = open_case(browser, url)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        hold_write(page)
        draft = page.get_by_role('textbox', name='현장 메모', exact=True)
        draft.fill('가상 보류 중 원본 메모')
        page.get_by_role('button', name='메모 남기기', exact=True).click()
        page.wait_for_function('()=>typeof window.__releaseMemo==="function"')
        page.locator('.memo-box button').first.dispatch_event('click')
        assert len(state(page)['events']) == 1
        if reenter:
            page.goto(url + '#/home')
            page.goto(url + '#/stop/' + first['request_id'])
            expect(page.get_by_role('button', name='메모 저장 중…', exact=True)).to_be_disabled()
            page.get_by_role('button', name='메모 저장 중…', exact=True).dispatch_event('click')
            expect(draft).to_have_value('가상 보류 중 원본 메모')
        else:
            page.goto(url + '#/stop/workflow-2')
        draft.fill('늦은 응답에도 보존하는 가상 새 초안')
        if reenter:
            draft.evaluate('(input)=>input.setSelectionRange(2,5)')
        page.evaluate('()=>{window.__memoHold=false;window.__releaseMemo();}')
        page.wait_for_timeout(200)
        expect(draft).to_have_value('늦은 응답에도 보존하는 가상 새 초안')
        if not reenter:
            expect(page).to_have_url(url + '#/stop/workflow-2')
            expect(page.get_by_text('메모를 남겼습니다', exact=True)).to_have_count(0)
        else:
            expect(page.get_by_role('button', name='메모 남기기', exact=True)).to_be_enabled()
            expect(draft).to_be_focused()
            assert draft.evaluate('(input)=>[input.selectionStart,input.selectionEnd]') == [2, 5]
        assert len(disk_state(page)['events']) == 1
        assert disk_state(page)['events'][0]['body']['text'] == '가상 보류 중 원본 메모'
        assert not errors, errors
        print('PASS: 메모 중복 클릭/지연 응답은 ' + ('재진입한 같은 접수' if reenter else '다른 접수') + '의 새 초안을 덮지 않음', flush=True)
    finally:
        context.close()


def lock_case(browser, url, shots):
    context, page, _ = open_case(browser, url)
    try:
        page.evaluate("""()=>{window.__memoPut=IDBObjectStore.prototype.put;
          IDBObjectStore.prototype.put=function(value,key){if(key==='s:state')throw new DOMException('synthetic memo quota','QuotaExceededError');return window.__memoPut.apply(this,arguments);};}""")
        draft = page.get_by_role('textbox', name='현장 메모', exact=True)
        draft.fill('가상 잠금 복구 메모')
        page.get_by_role('button', name='메모 남기기', exact=True).click()
        expect(page.get_by_text('메모 저장 대기', exact=True)).to_be_visible()
        before = state(page)
        page.evaluate('()=>{IDBObjectStore.prototype.put=window.__memoPut;}')
        page.evaluate('async()=>{await window.__collectionCore.lockNow();}')
        page.get_by_placeholder('PIN', exact=True).fill(PIN)
        page.get_by_role('button', name='잠금 해제', exact=True).click()
        expect(page.get_by_role('textbox', name='현장 메모', exact=True)).to_have_value('')
        expect(page.get_by_role('button', name='메모 저장 다시 시도', exact=True)).to_be_hidden()
        assert state(page) == before
        assert disk_state(page)['events'] == before['events']
        print('PASS: 잠금/PIN 재실행의 대기 암호문 복구도 같은 메모 ID를 보존하고 재입력을 요구하지 않음', flush=True)
    finally:
        context.close()


def owner_case(browser, url, shots):
    context, page, _ = open_case(browser, url)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        hold_write(page)
        draft = page.get_by_role('textbox', name='현장 메모', exact=True)
        draft.fill('가상 이전 상태 메모')
        page.get_by_role('button', name='메모 남기기', exact=True).click()
        page.wait_for_function('()=>typeof window.__releaseMemo==="function"')
        page.evaluate("""async()=>{const C=window.__collectionCore;
          C.setSt({...C.st,events:[],daylist:{...C.st.daylist,stops:C.st.daylist.stops.map(s=>({...s}))}});
          window.__newerMemoSave=C.commit();await C.render();
        }""")
        draft.fill('가상 새 상태에서 적는 초안')
        page.evaluate('()=>{window.__memoHold=false;window.__releaseMemo();}')
        page.evaluate('async()=>{await window.__newerMemoSave;}')
        expect(draft).to_have_value('가상 새 상태에서 적는 초안')
        page.evaluate('async()=>{await window.__collectionCore.render();}')
        expect(draft).to_have_value('가상 새 상태에서 적는 초안')
        expect(page.get_by_text('메모를 남겼습니다', exact=True)).to_have_count(0)
        assert state(page)['events'] == []
        assert disk_state(page)['events'] == []
        assert not errors, errors
        print('PASS: 상태 교체 뒤 이전 저장 응답은 새 상태의 초안/암호화 기록을 덮지 않음', flush=True)
    finally:
        context.close()


def main():
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-memo-save-'))
    shots = output / 'shots'
    shots.mkdir(parents=True, exist_ok=True)
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        try:
            failure_case(browser, url, shots)
            late_case(browser, url, shots, reenter=False)
            late_case(browser, url, shots, reenter=True)
            lock_case(browser, url, shots)
            owner_case(browser, url, shots)
        finally:
            browser.close()


if __name__ == '__main__':
    main()
