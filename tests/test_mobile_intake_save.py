"""실제 PIN/IndexedDB: 접수 수량·저장 실패/재시도·연타·화면 소유권 회귀."""
import argparse
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_review_ui import PIN, mobile_server
from test_workflow_mobile import daylist, open_mobile, seed, stop


FAIL_WRITE = """() => {
    const put=IDBObjectStore.prototype.put;
    IDBObjectStore.prototype.put=function(value,key){
        if(key==='s:state'&&window.__failIntake) throw new DOMException('fixture quota','QuotaExceededError');
        return put.apply(this,arguments);
    };
    window.__failIntake=true;
}"""
HOLD_WRITE = """() => {
    window.__holdIntake=true;window.__releaseIntake=[];
    const put=IDBObjectStore.prototype.put;
    const done=Object.getOwnPropertyDescriptor(IDBTransaction.prototype,'oncomplete');
    IDBObjectStore.prototype.put=function(value,key){
        if(key==='s:state'&&window.__holdIntake)this.transaction.__heldIntake=true;
        return put.apply(this,arguments);
    };
    Object.defineProperty(IDBTransaction.prototype,'oncomplete',{...done,set(fn){
        done.set.call(this,typeof fn!=='function'?fn:function(event){
            if(this.__heldIntake&&window.__holdIntake)window.__releaseIntake.push(()=>fn.call(this,event));
            else fn.call(this,event);
        });
    }});
}"""


def form(browser, url, mode='PC', edit=False, qty=1):
    context, page = open_mobile(browser, url)
    today = page.evaluate("import('./core.js').then(C=>C.todayKST())")
    item = stop(1, today)['items'][0]
    item.update(qty=qty, amount=1000*qty)
    draft = {'name': '가상 접수 검토', 'phone': '01000000000', 'address': '광주 동구 금남로 123',
             'planned_date': today, 'channel': '전화', 'items': [item]}
    patch = {'mode': mode, 'daylist': daylist(today, []), 'intakeDraft': draft}
    if edit:
        record = {**stop(1, today), **draft, 'status': 'ACTIVE', 'req_ver': 1, 'versions': []}
        patch['intakeDraft'] = None
        if mode == 'PC':
            record.update(field=True, assign_ver=0)
            patch.update(fieldStops=[record], events=[{'request_id': record['request_id'], 'kind': 'FIELD_INTAKE',
                         'body': {'temp_no': record['receipt_no'], **draft}}])
        else:
            patch['requests'] = [record]
    seed(page, patch, registered=mode == 'PC')
    page.goto(url + ('#/intake/workflow-1' if edit else '#/intake'))
    label = '고친 내용 저장' if edit and mode == 'PC' else '수정 저장' if edit else '접수 저장' if mode == 'PC' else '접수 등록'
    expect(page.get_by_role('button', name=label, exact=True)).to_be_visible()
    if edit:
        page.get_by_label('이름', exact=True).fill('가상 수정 접수')
        if mode == 'SOLO':
            page.get_by_label('수정 사유 (필수)', exact=True).fill('가상 시험 수정')
    return context, page, label


def snapshot(page):
    return page.evaluate("""async()=>{
        const C=await import('./core.js'),S=await import('./store.js');
        const records=C.st.mode==='SOLO'?C.st.requests:C.st.fieldStops;
        return {records:records.map(r=>({id:r.request_id,name:r.name,version:r.req_ver,versions:(r.versions||[]).length})),
            events:C.st.events.map(e=>({id:e.event_id,kind:e.kind,request:e.request_id,body:e.body})),
            counter:{...C.st.receiptCounter},pending:S.hasPendingSecure('state'),form:!!C.ui.form};
    }""")


def failure_case(browser, url, shots):
    for mode in ('PC', 'SOLO'):
        for edit in (False, True):
            context, page, label = form(browser, url, mode, edit)
            try:
                page.evaluate("import('./store.js').then(async S=>{const r=await S.getRaw('s:state');window.__intakeDisk=JSON.stringify({iv:[...r.iv],ct:[...r.ct]});})")
                page.evaluate(FAIL_WRITE)
                page.get_by_role('button', name=label, exact=True).click()
                retry = page.get_by_role('button', name='저장 다시 시도', exact=True)
                expect(retry).to_be_visible()
                expect(retry).to_be_enabled()
                expect(page).to_have_url(url + ('#/intake/workflow-1' if edit else '#/intake'))
                expect(page.get_by_label('이름', exact=True)).to_have_value('가상 수정 접수' if edit else '가상 접수 검토')
                assert not page.locator('.toast').filter(has_text='저장했습니다').count(), '실패를 성공으로 표시하지 않는다'
                before = snapshot(page)
                assert before['pending'] and before['form'], before
                assert len(before['records']) == 1, before
                assert page.evaluate("import('./store.js').then(async S=>{const r=await S.getRaw('s:state');return window.__intakeDisk===JSON.stringify({iv:[...r.iv],ct:[...r.ct]});})"), '실패 동안 원본 디스크가 그대로다'
                page.screenshot(path=str(shots / f'failure-{mode.lower()}-{edit}.png'))
                page.evaluate('window.__failIntake=false')
                retry.click()
                expect(page).to_have_url(url + ('#/stop/workflow-1' if edit else '#/home'))
                after = snapshot(page)
                assert after['records'] == before['records'] and after['events'] == before['events'], (before, after)
                assert after['counter'] == before['counter'] and not after['pending'] and not after['form'], (before, after)
                assert after['records'][0]['versions'] == (1 if edit and mode == 'SOLO' else 0), after
            finally:
                context.close()
    print('접수 저장: PC/단독 신규·수정 실패시 폼/최초ID 보존·실제 성공 후 이동·재시도 중복/버전 증가 없음 통과', flush=True)


def quantity_case(browser, url, shots):
    context, page, _ = form(browser, url, mode='SOLO', qty=150)
    try:
        page.locator('#intake-items .btn.step').first.click()
        assert page.evaluate("import('./core.js').then(C=>C.ui.form.items[0].qty)") == 149, '150에서 한 번 줄이면149다'
        expect(page.locator('#intake-items .line .amt')).to_have_text('149,000원')
        page.screenshot(path=str(shots / 'intake-149.png'))
        print('접수 수량: 150−1=149·금액149000원 통과', flush=True)
    finally:
        context.close()


def hold(page):
    page.evaluate(HOLD_WRITE)


def wait_held(page):
    for _ in range(100):
        if page.evaluate('window.__releaseIntake.length>0'):
            return
        page.wait_for_timeout(25)
    raise AssertionError('실제 저장 완료 응답이 보류되어야 한다')


def release(page):
    page.evaluate('window.__holdIntake=false;window.__releaseIntake.splice(0).forEach(f=>f())')


def repeated_case(browser, url, shots):
    context, page, label = form(browser, url)
    try:
        hold(page)
        button = page.locator('.cta > button')
        button.click()
        wait_held(page)
        button.evaluate('e=>{e.click();e.click();}')
        current = snapshot(page)
        assert len(current['records']) == 1 and len(current['events']) == 1, current
        expect(page.get_by_role('button', name='저장 중…', exact=True)).to_be_disabled()
        expect(page.get_by_label('이름', exact=True)).to_be_disabled()
        release(page)
        expect(page).to_have_url(url + '#/home')
        after = snapshot(page)
        assert after['records'] == current['records'] and after['events'] == current['events'], (current, after)
        print('접수 연타: 완료 응답 지연 중 세 번 눌러도 최초 접수/이벤트1개·입력 동결 통과', flush=True)
    finally:
        context.close()


def navigation_case(browser, url, shots):
    context, page, label = form(browser, url)
    try:
        hold(page)
        page.get_by_role('button', name=label, exact=True).click()
        wait_held(page)
        page.get_by_role('button', name='← 취소', exact=True).click()
        expect(page).to_have_url(url + '#/home')
        page.goto(url + '#/intake')
        expect(page.get_by_role('button', name='저장하던 접수로 돌아가기', exact=True)).to_be_visible()
        page.get_by_role('button', name='저장하던 접수로 돌아가기', exact=True).click()
        expect(page.get_by_label('이름', exact=True)).to_have_value('가상 접수 검토')
        expect(page.get_by_role('button', name='저장 중…', exact=True)).to_be_disabled()
        release(page)
        expect(page.get_by_role('button', name='저장된 접수 보기', exact=True)).to_be_visible()
        expect(page).to_have_url(url + '#/intake')
        page.get_by_role('button', name='저장된 접수 보기', exact=True).click()
        expect(page).to_have_url(url + '#/home')
        assert len(snapshot(page)['records']) == 1
        print('접수 화면 소유권: 다른 화면/재진입·이전 작업 복귀·늦은 완료의 자동 이동 없음 통과', flush=True)
    finally:
        context.close()


def lock_case(browser, url, shots):
    context, page, label = form(browser, url)
    try:
        hold(page)
        page.get_by_role('button', name=label, exact=True).click()
        wait_held(page)
        page.evaluate("()=>{import('./core.js').then(C=>{window.__intakeLock=C.lockNow();});}")
        release(page)
        expect(page.get_by_placeholder('PIN', exact=True)).to_be_visible()
        page.get_by_placeholder('PIN', exact=True).fill(PIN)
        page.get_by_role('button', name='잠금 해제', exact=True).click()
        expect(page.get_by_placeholder('PIN', exact=True)).to_have_count(0)
        assert len(snapshot(page)['records']) == 1
        page.goto(url + '#/intake')
        expect(page.get_by_role('button', name='접수 저장', exact=True)).to_be_visible()
        expect(page.get_by_label('이름', exact=True)).to_have_value('')
        print('접수 잠금: 지연 저장 정착 후 PIN·새 상태에 옛 작업/폼 유입 없음 통과', flush=True)
    finally:
        context.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=('all', 'failure', 'quantity', 'repeated', 'navigation', 'lock'), default='all')
    args = parser.parse_args()
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-intake-save-'))
    shots = output / 'shots'
    shots.mkdir(parents=True, exist_ok=True)
    errors = []
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        try:
            for name, fn in [('failure', failure_case), ('quantity', quantity_case), ('repeated', repeated_case), ('navigation', navigation_case), ('lock', lock_case)]:
                if args.case not in ('all', name):
                    continue
                try:
                    fn(browser, url, shots)
                except Exception as error:
                    errors.append({'case': name, 'error': str(error)})
                    print(name + ' FAIL: ' + str(error), flush=True)
        finally:
            browser.close()
    if errors:
        (output / 'failures.json').write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding='utf-8')
        raise AssertionError(f'{len(errors)} intake cases failed')


if __name__ == '__main__':
    main()
