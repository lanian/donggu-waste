"""현장 수량 직접 입력과 짧은 화면: 실제 PIN/IndexedDB·가상 업무 자료."""
import argparse
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_review_ui import PIN, mobile_server
from test_mobile_collection_save import open_case
from test_mobile_intake_save import FAIL_WRITE, form
from test_workflow_mobile import open_mobile, seed, stop


def display(page, height=844):
    page.set_viewport_size({'width': 360, 'height': height})
    page.emulate_media(reduced_motion='reduce')
    page.evaluate("""async () => {
        const C=await import('./core.js');C.st.textSize='xl';C.applyTextSize();
        await C.setPrefs({theme:'dark',hc:false});await C.render();
    }""")


def events(page):
    return page.evaluate("import('./core.js').then(C=>JSON.stringify(C.st.events))")


def apply_quantity(page, value):
    dialog = page.get_by_role('dialog', name='수량 직접 입력', exact=True)
    expect(dialog).to_be_visible()
    dialog.locator('input[type=number]').fill(str(value))
    dialog.get_by_role('button', name='적용', exact=True).click()
    return dialog


def numeric_case(browser, url, shots):
    context, page, receipt = open_case(browser, url, qty=150, previous=2)
    try:
        display(page)
        assert page.evaluate("import('./core.js').then(C=>typeof C.quantityInput==='function')"), '공유 수량 직접 입력 기능이 없다'
        before_zero = events(page)
        page.evaluate("""async()=>{
            const C=await import('./core.js');window.__zeroQuantity='pending';
            C.quantityInput(0,0,9999,'가상 수량').then(value=>{window.__zeroQuantity=value;});
        }""")
        expect(page.get_by_role('dialog').get_by_role('spinbutton')).to_have_value('0')
        apply_quantity(page,0)
        assert page.evaluate('window.__zeroQuantity') == 0, '최솟값 0을 빈 입력으로 처리함'
        assert events(page) == before_zero, '공유 수량 입력만으로 업무 기록 생성'
        button = page.get_by_role('button', name='가상 의자 현장 수량 직접 입력', exact=True)
        expect(button).to_be_visible()
        before = events(page)
        button.click()
        dialog = page.get_by_role('dialog', name='수량 직접 입력', exact=True)
        number = dialog.get_by_role('spinbutton')
        expect(number).to_have_value('150')
        for invalid in ('', '1.5', '10000', '1'):
            number.fill(invalid)
            dialog.get_by_role('button', name='적용', exact=True).click()
            expect(dialog).to_be_visible()
            expect(dialog.get_by_role('alert')).not_to_be_empty()
            assert events(page) == before, f'잘못된 수량 {invalid!r}이 기록을 변경함'
        apply_quantity(page, 149)
        expect(dialog).to_have_count(0)
        page.wait_for_function('()=>window.__collectionFees.deriveStop(window.__collectionCore.st.daylist.stops[0],window.__collectionCore.st.events).lines[0].qty_confirmed===149')
        page.evaluate('async()=>{await window.__collectionCore.saving;}')
        expect(page.get_by_role('button', name='가상 의자 현장 수량 직접 입력', exact=True)).to_be_focused()
        before_cancel = events(page)
        page.get_by_role('button', name='가상 의자 현장 수량 직접 입력', exact=True).click()
        number = page.get_by_role('dialog', name='수량 직접 입력', exact=True).get_by_role('spinbutton')
        number.fill('140')
        page.get_by_role('dialog').get_by_role('button', name='취소', exact=True).click()
        assert events(page) == before_cancel, '수량 입력 취소가 업무 기록을 바꿈'
        expect(page.get_by_role('button', name='가상 의자 현장 수량 직접 입력', exact=True)).to_have_text('149')
        collected = page.get_by_role('button', name='가상 의자 실은 수량 직접 입력', exact=True)
        collected.click()
        for invalid in ('1', '150'):
            apply_quantity(page, invalid)
            expect(page.get_by_role('dialog')).to_be_visible()
        apply_quantity(page, 100)
        page.wait_for_function('()=>window.__collectionFees.deriveStop(window.__collectionCore.st.daylist.stops[0],window.__collectionCore.st.events).lines[0].qty_collected===100')
        page.evaluate('async()=>{await window.__collectionCore.saving;}')
        before_leave = events(page)
        page.get_by_role('button', name='가상 의자 실은 수량 직접 입력', exact=True).click()
        page.get_by_role('dialog').get_by_role('spinbutton').fill('110')
        page.evaluate("async()=>{location.hash='#/home';await window.__collectionCore.render();}")
        if page.get_by_role('dialog').count():
            page.get_by_role('dialog').get_by_role('button', name='적용', exact=True).click()
        assert events(page) == before_leave, '이탈한 수량 입력창이 이전 접수를 수정함'
        page.goto(url+'#/stop/'+receipt['request_id'])
        page.get_by_role('button', name='가상 의자 실은 수량 직접 입력', exact=True).click()
        page.get_by_role('dialog').get_by_role('spinbutton').fill('110')
        page.evaluate('()=>window.__collectionCore.lockNow()')
        expect(page.get_by_placeholder('PIN', exact=True)).to_be_visible()
        expect(page.get_by_role('dialog')).to_have_count(0)
        page.get_by_placeholder('PIN', exact=True).fill(PIN)
        page.get_by_role('button', name='잠금 해제', exact=True).click()
        expect(page.get_by_placeholder('PIN', exact=True)).to_have_count(0)
        assert events(page) == before_leave, '잠금 중 닫힌 수량창이 기록을 수정함'
        page.screenshot(path=str(shots/'numeric-large-dark.png'))
        print('현장 수량: 150→149·정수/범위/이전분 검증·취소·초점복귀·화면이탈/잠금 보호 통과', flush=True)
    except Exception:
        page.screenshot(path=str(shots/'numeric-failure.png'),full_page=True)
        raise
    finally:
        context.close()


def inject_failure(page):
    assert page.evaluate("""async () => {
        const put=IDBObjectStore.prototype.put;
        IDBObjectStore.prototype.put=function(value,key){
            if(key==='s:state') throw new DOMException('test quota','QuotaExceededError');
            return put.apply(this,arguments);
        };
        return (await import('./core.js')).commit();
    }""") is False


def reachable(locator):
    return locator.evaluate("""e => {
        const r=e.getBoundingClientRect(), top=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
        return {top:r.top,bottom:r.bottom,height:r.height,viewport:innerHeight,
            hit:!!top&&(top===e||e.contains(top)),hitText:top?.textContent?.slice(0,50)};
    }""")


def reveal_body_target(locator):
    """고정 영역 사이로 직접 스크롤한다. 가림을 무시하는 강제 클릭은 쓰지 않는다."""
    locator.evaluate("""e => {
        let top=0,bottom=innerHeight;
        for(const bar of document.querySelectorAll('main > .bar,.steps')) {
            const style=getComputedStyle(bar),r=bar.getBoundingClientRect();
            if(['sticky','fixed'].includes(style.position)&&r.bottom>0) top=Math.max(top,r.bottom);
        }
        for(const overlay of document.querySelectorAll('.dock,#save-fail')) {
            const r=overlay.getBoundingClientRect();
            if(r.bottom>0&&r.top<innerHeight) bottom=Math.min(bottom,r.top);
        }
        const r=e.getBoundingClientRect();
        scrollBy(0,r.top-(top+(bottom-top-r.height)/2));
    }""")


def short_case(browser, url, shots):
    measurements = []
    for height in (480, 400):
        context, page, _ = open_case(browser, url, qty=3)
        try:
            display(page, height)
            inject_failure(page)
            banner = page.locator('#save-fail')
            expect(banner).to_be_visible()
            summary = banner.locator('summary')
            expect(summary).to_be_visible()
            expect(summary).to_contain_text('앱을 닫지 마세요')
            summary.click()
            expect(banner.locator('details')).to_have_attribute('open', '')
            expect(banner).to_contain_text('백업으로 먼저 내보내 주세요')
            summary.click()
            page.locator('.dock-next').click()
            target = page.get_by_role('button', name='전부 실음 · 수거 완료', exact=True)
            target.scroll_into_view_if_needed()
            reveal_body_target(target)
            measured = reachable(target)
            measurements.append({'height':height,**measured})
            assert measured['hit'] and measured['top']>=0 and measured['bottom']<=height, f'짧은 화면에서 결과 버튼이 가려짐: {measured}'
            assert measured['height']>=56, measured
            target.click(timeout=3000)
            expect(page.locator('.dock-next')).to_have_text('저장 다시 시도')
            expect(banner).to_be_visible()
            assert reachable(page.locator('.dock-next'))['hit'], '오류 상태 재시도 버튼이 가려짐'
            page.screenshot(path=str(shots/f'error-short-{height}.png'))
        except Exception:
            page.screenshot(path=str(shots/f'error-short-{height}-failure.png'),full_page=True)
            raise
        finally:
            context.close()
    (shots.parent/'short-results.json').write_text(json.dumps(measurements,ensure_ascii=False,indent=2),encoding='utf-8')
    print('현장 짧은 화면: 480/400px 실제 저장 실패·요약/상세·결과/재시도 터치 접근 통과',flush=True)


def picker_case(browser, url, shots):
    context, page = open_mobile(browser, url)
    try:
        today = page.evaluate("import('./core.js').then(C=>C.todayKST())")
        record = stop(1,today)
        seed(page,{'mode':'SOLO','requests':[record]},registered=False)
        page.goto(url+'#/stop/'+record['request_id'])
        display(page,400)
        page.get_by_role('button',name='+ 현장 품목 추가',exact=True).click()
        page.get_by_label('품목 찾기',exact=True).fill('사무용의자')
        add = page.locator('.pick-add').first
        expect(add).to_be_visible()
        add.click(timeout=3000)
        value = page.locator('.pick-ctl .qty-value').first
        expect(value).to_have_text('1')
        value.click()
        apply_quantity(page,150)
        expect(page.locator('.pick-ctl .qty-value').first).to_have_text('150')
        expect(page.locator('.pick-ctl .qty-value').first).to_be_focused()
        page.get_by_role('button',name='가구',exact=True).click()
        page.locator('.name-chip[aria-label$="규격 고르기"]').first.click()
        specs = page.get_by_role('dialog').first
        specs.locator('.pick-add').first.click()
        nested = specs.locator('.qty-value').first
        nested.click()
        apply_quantity(page,10)
        expect(specs.locator('.qty-value').first).to_have_text('10')
        expect(specs.locator('.qty-value').first).to_be_focused()
        page.screenshot(path=str(shots/'picker-specification-numeric.png'))
        specs.get_by_role('button',name='완료',exact=True).click()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), '짧은 품목 화면 가로 넘침'
        page.screenshot(path=str(shots/'picker-short-numeric.png'))
        print('현장 품목: 400px 실제 담기·숫자150·규격 창의 숫자10·포커스복귀 통과',flush=True)
    except Exception:
        page.screenshot(path=str(shots/'picker-failure.png'),full_page=True)
        raise
    finally:
        context.close()


def intake_case(browser, url, shots):
    context,page,label = form(browser,url)
    try:
        display(page,480)
        page.evaluate(FAIL_WRITE)
        page.get_by_role('button',name=label,exact=True).click()
        retry=page.get_by_role('button',name='저장 다시 시도',exact=True)
        expect(retry).to_be_visible()
        banner=page.locator('#save-fail')
        expect(banner).to_be_visible()
        target=reachable(retry)
        overlap=retry.evaluate("""e=>{
            const a=e.getBoundingClientRect(),b=document.querySelector('#save-fail').getBoundingClientRect();
            return Math.max(0,Math.min(a.right,b.right)-Math.max(a.left,b.left))*
                Math.max(0,Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top));
        }""")
        page.screenshot(path=str(shots/'intake-error-footer.png'))
        assert target['hit'] and overlap<=1, f'접수 저장 실패 안내가 재시도 버튼을 가림: {target}, 겹침={overlap}'
        before=events(page)
        retry.click(timeout=3000)
        expect(retry).to_be_enabled()
        assert events(page)==before, '접수 저장 재시도로 새 접수 이벤트 생성'
        print('현장 접수: 실제 저장 실패 안내와 하단 재시도 분리·클릭 접근 통과',flush=True)
    except Exception:
        page.screenshot(path=str(shots/'intake-footer-failure.png'),full_page=True)
        raise
    finally:
        context.close()


def aggregate_case(browser,url,shots):
    for editable in (True,False):
        context,page=open_mobile(browser,url)
        try:
            today=page.evaluate("import('./core.js').then(C=>C.todayKST())")
            rate=page.evaluate("""async()=>{
                const {BUILTIN_FEES:T}=await import('./fees-data.js');
                const r=T.rows.find(x=>x.name==='의자'&&x.spec==='회전식 의자');
                if(!r)throw new Error('의자 회전식 규격 fixture 없음');
                return {rate:r,version:T.version_id};
            }""")
            row=rate['rate']
            item={'line_id':'locked-line','fee_rate_id':row['id'],'fee_version_id':rate['version'],
                  'name':row['name'],'spec':row['spec'],'unit':row['unit'],'unit_label':row['unit_label'],
                  'qty':2,'unit_price':row['price'],'amount':row['price']*2,'price_status':'CONFIRMED'}
            items=[item]
            if editable: items.append({**item,'line_id':'editable-line','qty':1,'amount':row['price']})
            record={**stop(1,today),'status':'ACTIVE','items':items,'versions':[]}
            seed(page,{'mode':'SOLO','requests':[record],'events':[{'request_id':record['request_id'],
                'kind':'ITEM_RESULT','body':{'line_id':'locked-line','line_kind':'RECEIVED',
                    'qty_confirmed':2,'qty_collected':1,'reason':''}}]},registered=False)
            page.goto(url+'#/intake/'+record['request_id'])
            page.get_by_role('button',name='+ 품목 더 담기',exact=True).click()
            page.get_by_label('품목 찾기',exact=True).fill('사무용의자')
            value=page.locator('.pick-row .qty-value').first
            expect(value).to_have_text('3' if editable else '2')
            before=events(page)
            value.click()
            apply_quantity(page,4)
            expect(page.locator('.pick-row .qty-value').first).to_have_text('4')
            actual=page.evaluate("import('./core.js').then(C=>C.ui.form.items)")
            locked=[line for line in actual if line['line_id']=='locked-line']
            assert len(locked)==1 and locked[0]['qty']==2, f'현장 기록 품목의 수량/ID 변경: {actual}'
            assert sum(line['qty'] for line in actual)==4, f'표시 합계4가 중복 가산됨: {actual}'
            assert sum(line['qty'] for line in actual if line['line_id']!='locked-line')==2, actual
            assert events(page)==before, '규격 합계 수량 입력만으로 기존 현장 이력 변경'
            page.locator('.pick-row .qty-value').first.click()
            apply_quantity(page,1)
            expect(page.get_by_role('dialog',name='수량 직접 입력',exact=True)).to_be_visible()
            page.get_by_role('dialog').get_by_role('button',name='취소',exact=True).click()
            page.screenshot(path=str(shots/f'picker-fixed-quantity-{editable}.png'))
        except Exception:
            page.screenshot(path=str(shots/f'picker-fixed-failure-{editable}.png'),full_page=True)
            raise
        finally: context.close()
    print('현장 품목: 잠긴2+편집1 합계직접입력·잠긴품목만 있는 합계증가·ID/하한 보호 통과',flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--case',choices=('all','numeric','short','picker','intake','aggregate'),default='all')
    args=parser.parse_args()
    output=Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-field-layout-'))
    shots=output/'shots';shots.mkdir(parents=True,exist_ok=True)
    failures=[]
    with mobile_server() as url,sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path,headless=True)
        try:
            for name,case in [('numeric',numeric_case),('short',short_case),('picker',picker_case),('intake',intake_case),('aggregate',aggregate_case)]:
                if args.case not in ('all',name): continue
                try: case(browser,url,shots)
                except Exception as error:
                    failures.append({'case':name,'error':str(error)})
                    print(name+' FAIL: '+str(error),flush=True)
        finally: browser.close()
    if failures:
        (output/'failures.json').write_text(json.dumps(failures,ensure_ascii=False,indent=2),encoding='utf-8')
        raise AssertionError(f'{len(failures)} field layout cases failed')


if __name__=='__main__': main()
