"""모바일 수거 수량·실제 IndexedDB 저장 실패·동일 기록 재저장·늦은 응답 회귀."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_review_ui import PIN, ROOT, mobile_server
from test_workflow_mobile import open_mobile, seed, stop


def open_case(browser, url, qty=1, previous=0, two_lines=False):
    context, page = open_mobile(browser, url)
    today = page.evaluate("import('./core.js').then(C=>C.todayKST())")
    first = stop(1, today)
    first['items'][0].update(qty=qty, prior_collected=previous, amount=1000 * qty)
    if two_lines:
        first['items'].append({**first['items'][0], 'line_id': 'line-extra', 'name': '가상 책상'})
    seed(page, {'daylist': {'work_date': today, 'crew': '가상 수거팀', 'assign_ver': 1,
                           'stops': [first, stop(2, today)]}}, registered=False)
    page.goto(url + '#/stop/' + first['request_id'])
    page.evaluate("async()=>{window.__collectionCore=await import('./core.js');window.__collectionFees=await import('./fees.js');}")
    expect(page.locator('.who .name')).to_have_text(first['name'])
    return context, page, first


def quantity_case(browser, url, output):
    context, page, _ = open_case(browser, url, qty=150, previous=1)
    try:
        page.get_by_role('button', name='가상 의자 현장 수량 빼기', exact=True).click()
        page.wait_for_function('()=>window.__collectionCore.st.events.some(e=>e.kind==="ITEM_RESULT")')
        quantity = page.evaluate('()=>window.__collectionFees.deriveStop(window.__collectionCore.st.daylist.stops[0],window.__collectionCore.st.events).lines[0].qty_confirmed')
        assert quantity == 149, f'150개에서 −1: 기대149, 실제{quantity}'
        expect(page.get_by_text('이전 수거 1개 · 이번 추가 0개 · 누적 1개', exact=True)).to_be_visible()
        page.screenshot(path=str(output / 'mobile-large-quantity.png'), full_page=True)
        print('모바일 수거: 수량150에서−1은149·이전/이번/누적 표시 통과', flush=True)
    finally:
        context.close()


def disk_state(page):
    return page.evaluate("""async pin=>{
        const S=await import('./store.js'),L=await S.getRaw('lock'),raw=await S.getRaw('s:state'),enc=new TextEncoder();
        const base=await crypto.subtle.importKey('raw',enc.encode(pin),'PBKDF2',false,['deriveKey']);
        const wrapping=await crypto.subtle.deriveKey({name:'PBKDF2',hash:'SHA-256',salt:L.salt,iterations:L.iter},base,{name:'AES-GCM',length:256},false,['unwrapKey']);
        const data=await crypto.subtle.unwrapKey('raw',L.wrapped,wrapping,{name:'AES-GCM',iv:L.iv},{name:'AES-GCM',length:256},false,['decrypt']);
        return JSON.parse(new TextDecoder().decode(await crypto.subtle.decrypt({name:'AES-GCM',iv:raw.iv,additionalData:enc.encode('state')},data,raw.ct)));
    }""", PIN)


def prior_case(browser, url, output):
    context, page, _ = open_case(browser, url, qty=3, previous=2)
    try:
        minus = page.get_by_role('button', name='가상 의자 실은 수량 빼기', exact=True)
        expect(minus).to_be_disabled()
        page.get_by_role('button', name='가상 의자 실은 수량 더하기', exact=True).click()
        page.wait_for_function('()=>window.__collectionFees.deriveStop(window.__collectionCore.st.daylist.stops[0],window.__collectionCore.st.events).lines[0].qty_collected===3')
        expect(page.get_by_text('이전 수거 2개 · 이번 추가 1개 · 누적 3개', exact=True)).to_be_visible()
        minus.click()
        page.wait_for_function('()=>window.__collectionFees.deriveStop(window.__collectionCore.st.daylist.stops[0],window.__collectionCore.st.events).lines[0].qty_collected===2')
        expect(minus).to_be_disabled()
        page.get_by_role('button', name='가상 의자 현장 수량 빼기', exact=True).click()
        page.wait_for_function('()=>window.__collectionFees.deriveStop(window.__collectionCore.st.daylist.stops[0],window.__collectionCore.st.events).lines[0].qty_confirmed===2')
        expect(page.get_by_role('button', name='가상 의자 현장 수량 빼기', exact=True)).to_be_disabled()
        labels = json.loads((ROOT / 'data/labels.json').read_text(encoding='utf-8'))
        page.get_by_role('group', name='가상 의자 스티커·신고필증', exact=True).get_by_role('button', name=labels['sticker']['MISSING'], exact=True).click()
        page.get_by_role('button', name='미수거 (사유 기록)', exact=True).click()
        page.wait_for_function('()=>window.__collectionCore.st.events.some(e=>e.kind==="STICKER_CHECK")')
        page.evaluate('async()=>{await window.__collectionCore.saving;}')
        totals = page.evaluate('()=>window.__collectionFees.deriveStop(window.__collectionCore.st.daylist.stops[0],window.__collectionCore.st.events).lines[0]')
        assert totals['qty_collected'] == 2 and totals['qty_confirmed'] == 2, totals
        assert all(event['body']['qty_collected'] >= 2 and event['body']['qty_confirmed'] >= 2
                   for event in disk_state(page)['events'] if event['kind'] == 'ITEM_RESULT')
        page.screenshot(path=str(output / 'mobile-collection-prior.png'), full_page=True)
        print('모바일 수거: 앞선2개 보존·오늘1개만 되돌리기·현장 수량/스티커 결정 하한 통과', flush=True)
    finally:
        context.close()


def save_case(browser, url, output):
    context, page, _ = open_case(browser, url)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.evaluate("""()=>{window.__realCollectionPut=IDBObjectStore.prototype.put;window.__collectionWrites=0;
            IDBObjectStore.prototype.put=function(value,key){
                if(key==='s:state'&&++window.__collectionWrites>=2)throw new DOMException('synthetic collection quota','QuotaExceededError');
                return window.__realCollectionPut.apply(this,arguments);
            };}""")
        page.get_by_role('button', name='전부 실음 · 수거 완료', exact=True).click()
        page.wait_for_function('()=>window.__collectionCore.saveFailed===true')
        expect(page.get_by_text('수거 기록 저장 대기', exact=True)).to_be_visible()
        expect(page.get_by_text('수거 완료로 저장했습니다', exact=True)).to_have_count(0)
        expect(page.locator('.dock-next')).to_have_text('저장 다시 시도')
        expect(page.get_by_text('저장된 결과: 수거 완료', exact=True)).to_have_count(0)
        before = page.evaluate('()=>window.__collectionCore.st.events.map(e=>e.event_id)')
        assert len(disk_state(page)['events']) == 1
        assert page.evaluate('()=>window.__collectionCore.st.events.filter(e=>e.kind==="ATTEMPT").length') == 1
        retry = page.locator('#st-result').get_by_role('button', name='저장 다시 시도', exact=True)
        retry.click()
        expect(page.get_by_text('수거 기록 저장 대기', exact=True)).to_be_visible()
        assert page.evaluate('()=>window.__collectionCore.st.events.map(e=>e.event_id)') == before
        page.screenshot(path=str(output / 'mobile-collection-pending.png'), full_page=True)
        page.evaluate('()=>{IDBObjectStore.prototype.put=window.__realCollectionPut;}')
        retry.click()
        expect(page.get_by_text('저장된 결과: 수거 완료', exact=True)).to_be_visible()
        expect(page.locator('.dock-next')).to_have_text('다음 2번 ▶')
        assert page.evaluate('()=>window.__collectionCore.st.events.map(e=>e.event_id)') == before
        assert [event['event_id'] for event in disk_state(page)['events']] == before
        assert not errors, errors
        print('모바일 수거: 저장 실패시 성공/다음 이동 금지·보류 안내·동일 이벤트 재저장 통과', flush=True)
    finally:
        context.close()


def late_case(browser, url, output):
    context, page, first = open_case(browser, url)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.evaluate("""()=>{
            const descriptor=Object.getOwnPropertyDescriptor(IDBTransaction.prototype,'oncomplete');
            Object.defineProperty(IDBTransaction.prototype,'oncomplete',{...descriptor,set(handler){
                descriptor.set.call(this,typeof handler!=='function'?handler:function(event){
                    if(this.__holdCollection)window.__releaseCollection=()=>handler.call(this,event);
                    else handler.call(this,event);
                });
            }});
            const put=IDBObjectStore.prototype.put;let writes=0;
            IDBObjectStore.prototype.put=function(value,key){
                if(key==='s:state'&&++writes===2)this.transaction.__holdCollection=true;
                return put.apply(this,arguments);
            };
        }""")
        page.get_by_role('button', name='전부 실음 · 수거 완료', exact=True).click()
        page.wait_for_function('()=>typeof window.__releaseCollection==="function"')
        expect(page.get_by_role('button', name='전부 실음 · 수거 완료', exact=True)).to_be_disabled()
        page.goto(url + '#/stop/workflow-2')
        expect(page.locator('.who .name')).to_have_text('가상 주민 2')
        page.goto(url + '#/stop/' + first['request_id'])
        expect(page.get_by_role('button', name='전부 실음 · 수거 완료', exact=True)).to_be_disabled()
        expect(page.locator('.dock-next')).not_to_contain_text('다음 2번')
        page.goto(url + '#/stop/workflow-2')
        page.evaluate('()=>window.__releaseCollection()')
        page.wait_for_function('()=>window.__collectionCore.st.events.some(e=>e.kind==="ATTEMPT")')
        expect(page).to_have_url(url + '#/stop/workflow-2')
        expect(page.locator('.who .name')).to_have_text('가상 주민 2')
        expect(page.get_by_text('수거 완료로 저장했습니다', exact=True)).to_have_count(0)
        page.goto(url + '#/stop/' + first['request_id'])
        expect(page.locator('.dock-next')).to_have_text('다음 2번 ▶')
        assert page.evaluate('()=>window.__collectionCore.st.events.filter(e=>e.kind==="ATTEMPT").length') == 1
        assert not errors, errors
        print('모바일 수거: 저장 중 재진입/중복방지·다른 접수 이동후 늦은 응답 격리 통과', flush=True)
    finally:
        context.close()


def flush_case(browser, url, output):
    context, page, _ = open_case(browser, url, qty=3)
    try:
        page.evaluate("""async()=>{const C=window.__collectionCore;
            await C.addEvent(C.st.daylist.stops[0],'ITEM_RESULT',{line_id:'line-1',qty_confirmed:3,qty_collected:1,reason:'OTHER'});
            await C.render();}""")
        page.get_by_role('button', name='가상 의자 실은 수량 더하기', exact=True).click()
        page.get_by_role('button', name='일부 수거', exact=True).click()
        expect(page.get_by_text('저장된 결과: 일부 수거', exact=True)).to_be_visible()
        result = page.evaluate('()=>window.__collectionFees.deriveStop(window.__collectionCore.st.daylist.stops[0],window.__collectionCore.st.events)')
        assert result['lines'][0]['qty_collected'] == 2, result['lines'][0]
        assert len([event for event in disk_state(page)['events'] if event['kind'] == 'ATTEMPT']) == 1
        print('모바일 수거: 수량을 누른 직후 결과 저장도 최신값으로 한 번 확정 통과', flush=True)
    finally:
        context.close()


def hold_failed_write(page, at):
    page.evaluate("""at=>{
        const descriptor=Object.getOwnPropertyDescriptor(IDBTransaction.prototype,'onabort');
        Object.defineProperty(IDBTransaction.prototype,'onabort',{...descriptor,set(handler){
            descriptor.set.call(this,typeof handler!=='function'?handler:function(event){
                if(this.__holdCollectionFailure)window.__releaseCollectionFailure=()=>handler.call(this,event);
                else handler.call(this,event);
            });
        }});
        const put=IDBObjectStore.prototype.put;let writes=0;
        IDBObjectStore.prototype.put=function(value,key){
            if(key==='s:state'&&++writes===at){this.transaction.__holdCollectionFailure=true;throw new DOMException('synthetic deferred collection failure','QuotaExceededError');}
            return put.apply(this,arguments);
        };
    }""", at)


def help_case(browser, url, output):
    context, page, first = open_case(browser, url)
    try:
        hold_failed_write(page, 2)
        page.get_by_role('button', name='전부 실음 · 수거 완료', exact=True).click()
        page.wait_for_function('()=>typeof window.__releaseCollectionFailure==="function"')
        page.goto(url + '#/pc-input/' + first['request_id'])
        expect(page.get_by_role('heading', name='PC 입력 도움', exact=True)).to_be_visible()
        expect(page.locator('.pc-input-help > p').filter(has_text='저장 중 · 저장 결과를 확인한 뒤 PC에 전달하세요')).to_be_visible()
        expect(page.get_by_role('button', name='전달 내용 복사', exact=True)).to_be_disabled()
        page.evaluate('()=>window.__releaseCollectionFailure()')
        page.wait_for_function('()=>window.__collectionCore.saveFailed===true')
        expect(page.locator('.pc-input-help > p').filter(has_text='저장 대기 · 수거 기록을 다시 저장한 뒤 PC에 전달하세요')).to_be_visible()
        expect(page.get_by_role('button', name='전달 내용 복사', exact=True)).to_be_disabled()
        page.get_by_role('button', name='접수 상세로', exact=True).click()
        expect(page.get_by_role('button', name='PC 입력 도움', exact=True)).to_be_disabled()
        page.screenshot(path=str(output / 'mobile-collection-help-pending.png'), full_page=True)
        print('모바일 수거: 저장중 도움 직접진입·늦은 실패 반영·보류 기록 복사/진입 금지 통과', flush=True)
    finally:
        context.close()


def help_quantity_case(browser, url, output):
    context, page, first = open_case(browser, url, qty=2)
    try:
        page.get_by_role('button', name='전부 실음 · 수거 완료', exact=True).click()
        expect(page.get_by_text('저장된 결과: 수거 완료', exact=True)).to_be_visible()
        hold_failed_write(page, 1)
        page.get_by_role('button', name='가상 의자 실은 수량 빼기', exact=True).click()
        page.wait_for_function('()=>typeof window.__releaseCollectionFailure==="function"')
        page.goto(url + '#/pc-input/' + first['request_id'])
        expect(page.locator('.pc-input-help > p').filter(has_text='저장 중 · 저장 결과를 확인한 뒤 PC에 전달하세요')).to_be_visible()
        expect(page.get_by_role('button', name='전달 내용 복사', exact=True)).to_be_disabled()
        page.evaluate('()=>window.__releaseCollectionFailure()')
        expect(page.locator('.pc-input-help > p').filter(has_text='저장 대기 · 수거 기록을 다시 저장한 뒤 PC에 전달하세요')).to_be_visible()
        expect(page.get_by_role('button', name='전달 내용 복사', exact=True)).to_be_disabled()
        print('모바일 수거: 수량 저장중/늦은 실패도 도움 완료/복사로 넘기지 않음 통과', flush=True)
    finally:
        context.close()


def help_copy_case(browser, url, output):
    context, page, first = open_case(browser, url)
    try:
        page.get_by_role('button', name='전부 실음 · 수거 완료', exact=True).click()
        expect(page.get_by_text('저장된 결과: 수거 완료', exact=True)).to_be_visible()
        page.get_by_role('button', name='PC 입력 도움', exact=True).click()
        expect(page.get_by_role('heading', name='PC 입력 도움', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='전달 내용 복사', exact=True)).to_be_enabled()
        page.evaluate("""async()=>{
            window.__collectionCopies=[];
            Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>window.__collectionCopies.push(text)}});
            const put=IDBObjectStore.prototype.put;
            IDBObjectStore.prototype.put=function(value,key){if(key==='s:state')throw new DOMException('synthetic late failure','QuotaExceededError');return put.apply(this,arguments);};
            await window.__collectionCore.addEvent(window.__collectionCore.st.daylist.stops[0],'FIELD_MEMO',{text:'가상 지연 기록'});
        }""")
        page.get_by_role('button', name='전달 내용 복사', exact=True).click()
        expect(page.locator('.pc-input-help > p').filter(has_text='저장 대기 · 수거 기록을 다시 저장한 뒤 PC에 전달하세요')).to_be_visible()
        assert page.evaluate('()=>window.__collectionCopies.length') == 0
        expect(page.get_by_role('button', name='전달 내용 복사', exact=True)).to_be_disabled()
        print('모바일 수거: 이미 열린 도움도 복사 직전 최신 저장상태를 확인 통과', flush=True)
    finally:
        context.close()


def queue_quantity_timers(page):
    """사용자가 수락한 두 입력의 debounce 순서를 고정한다. 실제 저장은 그대로 수행한다."""
    page.evaluate("""()=>{
        const later=window.setTimeout;window.__collectionTimers=[];
        window.setTimeout=function(fn,delay,...args){
            if(delay===350){window.__collectionTimers.push(()=>fn(...args));return 100000+window.__collectionTimers.length;}
            return later(fn,delay,...args);
        };
    }""")
    page.get_by_role('button', name='가상 의자 실은 수량 빼기', exact=True).click()
    page.get_by_role('button', name='가상 책상 실은 수량 빼기', exact=True).click()
    assert page.evaluate('()=>window.__collectionTimers.length') == 2


def multi_write_case(browser, url, output):
    context, page, first = open_case(browser, url, qty=3, two_lines=True)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.get_by_role('button', name='전부 실음 · 수거 완료', exact=True).click()
        expect(page.get_by_text('저장된 결과: 수거 완료', exact=True)).to_be_visible()
        queue_quantity_timers(page)
        hold_failed_write(page, 2)
        page.evaluate("""()=>{
            const descriptor=Object.getOwnPropertyDescriptor(IDBTransaction.prototype,'oncomplete');
            Object.defineProperty(IDBTransaction.prototype,'oncomplete',{...descriptor,set(handler){
                descriptor.set.call(this,typeof handler!=='function'?handler:function(event){
                    if(this.__holdFirstQuantity)window.__releaseFirstQuantity=()=>handler.call(this,event);
                    else handler.call(this,event);
                });
            }});
            const put=IDBObjectStore.prototype.put;let writes=0;
            IDBObjectStore.prototype.put=function(value,key){
                if(key==='s:state'&&++writes===1)this.transaction.__holdFirstQuantity=true;
                return put.apply(this,arguments);
            };
            window.__collectionTimers.forEach(fn=>fn());
        }""")
        page.wait_for_function('()=>typeof window.__releaseFirstQuantity==="function"')
        assert page.evaluate('()=>window.__collectionCore.st.events.filter(e=>e.kind==="ITEM_RESULT"&&e.body.qty_collected===2).length') == 2
        page.evaluate('()=>window.__releaseFirstQuantity()')
        page.wait_for_function('()=>typeof window.__releaseCollectionFailure==="function"')
        page.goto(url + '#/pc-input/' + first['request_id'])
        expect(page.locator('.pc-input-help > p').filter(has_text='저장 중 · 저장 결과를 확인한 뒤 PC에 전달하세요')).to_be_visible()
        expect(page.get_by_role('button', name='전달 내용 복사', exact=True)).to_be_disabled()
        page.evaluate('()=>window.__releaseCollectionFailure()')
        expect(page.locator('.pc-input-help > p').filter(has_text='저장 대기 · 수거 기록을 다시 저장한 뒤 PC에 전달하세요')).to_be_visible()
        expect(page.get_by_role('button', name='전달 내용 복사', exact=True)).to_be_disabled()
        assert not errors, errors
        print('모바일 수거: 두 품목 동시 저장은 마지막 쓰기/실패까지 도움 복사 보호 통과', flush=True)
    finally:
        context.close()


def queued_failure_case(browser, url, output):
    context, page, _ = open_case(browser, url, qty=3, two_lines=True)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.get_by_role('button', name='전부 실음 · 수거 완료', exact=True).click()
        expect(page.get_by_text('저장된 결과: 수거 완료', exact=True)).to_be_visible()
        queue_quantity_timers(page)
        page.evaluate("""()=>{
            window.__realQueuedPut=IDBObjectStore.prototype.put;
            IDBObjectStore.prototype.put=function(value,key){
                if(key==='s:state')throw new DOMException('synthetic queued collection failure','QuotaExceededError');
                return window.__realQueuedPut.apply(this,arguments);
            };
            window.__collectionTimers[0]();
        }""")
        page.wait_for_function('()=>window.__collectionCore.saveFailed===true')
        page.evaluate('async()=>{await window.__collectionCore.flushSteps();await window.__collectionCore.saving;}')
        expect(page.get_by_text('수거 기록 저장 대기', exact=True)).to_be_visible()
        quantities = page.evaluate('()=>window.__collectionFees.deriveStop(window.__collectionCore.st.daylist.stops[0],window.__collectionCore.st.events).lines.map(l=>l.qty_collected)')
        assert quantities == [2, 2], f'첫 저장 실패 뒤 수락된 두 수량 보존: {quantities}'
        before = page.evaluate('()=>window.__collectionCore.st.events.map(e=>e.event_id)')
        assert page.evaluate('()=>window.__collectionCore.st.events.filter(e=>e.kind==="ATTEMPT").length') == 1
        page.evaluate('()=>{IDBObjectStore.prototype.put=window.__realQueuedPut;}')
        page.locator('#st-result').get_by_role('button', name='저장 다시 시도', exact=True).click()
        expect(page.get_by_text('저장된 결과: 수거 완료', exact=True)).to_be_visible()
        assert page.evaluate('()=>window.__collectionCore.st.events.map(e=>e.event_id)') == before
        saved = disk_state(page)
        assert [event['event_id'] for event in saved['events']] == before
        last_quantities = {event['body']['line_id']: event['body']['qty_collected'] for event in saved['events'] if event['kind'] == 'ITEM_RESULT'}
        assert last_quantities == {'line-1': 2, 'line-extra': 2}, last_quantities
        assert not errors, errors
        print('모바일 수거: 첫 저장 실패 뒤 예약된 다른 품목 수량도 RAM/동일 이벤트 재저장 보존 통과', flush=True)
    finally:
        context.close()


def direct_write_help_case(browser, url, output):
    context, page, first = open_case(browser, url, qty=2)
    try:
        page.get_by_role('button', name='전부 실음 · 수거 완료', exact=True).click()
        expect(page.get_by_text('저장된 결과: 수거 완료', exact=True)).to_be_visible()
        hold_failed_write(page, 2)
        labels = json.loads((ROOT / 'data/labels.json').read_text(encoding='utf-8'))
        page.get_by_role('group', name='가상 의자 스티커·신고필증', exact=True).get_by_role('button', name=labels['sticker']['MISSING'], exact=True).click()
        page.get_by_role('button', name='미수거 (사유 기록)', exact=True).click()
        page.wait_for_function('()=>typeof window.__releaseCollectionFailure==="function"')
        page.goto(url + '#/pc-input/' + first['request_id'])
        expect(page.locator('.pc-input-help > p').filter(has_text='저장 중 · 저장 결과를 확인한 뒤 PC에 전달하세요')).to_be_visible()
        expect(page.get_by_role('button', name='전달 내용 복사', exact=True)).to_be_disabled()
        page.evaluate('()=>window.__releaseCollectionFailure()')
        expect(page.locator('.pc-input-help > p').filter(has_text='저장 대기 · 수거 기록을 다시 저장한 뒤 PC에 전달하세요')).to_be_visible()
        expect(page.get_by_role('button', name='전달 내용 복사', exact=True)).to_be_disabled()
        print('모바일 수거: 스티커 미수거의 직접 수량 쓰기도 암호화 저장 정착 전 도움 복사 금지 통과', flush=True)
    finally:
        context.close()


def main(case):
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-mobile-collection-save-')) / 'shots'
    output.mkdir(parents=True, exist_ok=True)
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        try:
            for name, fn in [('quantity', quantity_case), ('prior', prior_case), ('save', save_case), ('late', late_case), ('flush', flush_case), ('help', help_case), ('copy', help_copy_case), ('help-quantity', help_quantity_case), ('multi-write', multi_write_case), ('queued-failure', queued_failure_case), ('direct-help', direct_write_help_case)]:
                if case in ('all', name):
                    fn(browser, url, output)
        finally:
            browser.close()
    print('적용 소스 SHA256:', json.dumps({name: hashlib.sha256((ROOT / 'web' / name).read_bytes()).hexdigest() for name in ('core.js','store.js','screens/stop.js')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=('all', 'quantity', 'prior', 'save', 'late', 'flush', 'help', 'copy', 'help-quantity', 'multi-write', 'queued-failure', 'direct-help'), default='all')
    main(parser.parse_args().case)
