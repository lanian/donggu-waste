"""실제 PIN/암호화 저장소: PC 입력 대조·복사 소유권·여러 기록일 안내."""
import datetime as dt
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_review_ui import mobile_server
from test_workflow_mobile import daylist, open_mobile, seed, stop


def fixtures(today):
    request = stop(1, today, receipt_no='PC-HELP-2026-1234')
    request['items'][0].update(name='가상 장롱', spec='180cm 이상', qty=5, prior_collected=1, unit='SIDE', unit_label='쪽')
    request['items'].append({'line_id': 'line-2', 'name': '가상 의자', 'spec': '회의용', 'qty': 4,
                             'prior_collected': 1, 'unit': 'EA', 'unit_label': '개', 'price_status': 'CONFIRMED', 'unit_price': 1000, 'amount': 4000})
    def event(kind, body, **extra):
        return {'request_id': request['request_id'], 'kind': kind, 'ts': today + 'T03:00:00Z', 'body': body, **extra}
    events = [event('ITEM_RESULT', {'line_id': 'line-1', 'qty_confirmed': 5, 'qty_collected': 3, 'reason': 'CANNOT_LOAD'}),
              event('ITEM_RESULT', {'line_id': 'line-2', 'qty_confirmed': 4, 'qty_collected': 2, 'reason': 'NOT_PUT_OUT'}),
              event('FIELD_ITEM_ADD', {'line_id': 'field-extra', 'name': '가상 추가 책상', 'spec': '일반형', 'qty': 3,
                                       'unit': 'EA', 'unit_label': '개', 'price_status': 'CONFIRMED', 'unit_price': 1000}),
              event('ITEM_RESULT', {'line_id': 'field-extra', 'qty_confirmed': 3, 'qty_collected': 1, 'reason': 'NO_STICKER'}),
              event('FIELD_MEMO', {'text': 'FIELD_MEMO_PRIVATE_SENTINEL · 가상 비공개 수거 메모'}),
              event('ATTEMPT', {'outcome': 'PARTIAL'})]
    return request, events


def show(page, today, request, events, **patch):
    seed(page, {'daylist': daylist(today, [request]), 'events': events, **patch})
    page.evaluate("async()=>{const C=await import('./core.js');C.applyTextSize();location.hash='#/pc-input/workflow-1';await C.render()}")
    expect(page.get_by_role('heading', name='PC 입력 도움', exact=True)).to_be_visible()
    return page.locator('.pc-input-help')


def main():
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-mobile-pc-help-')) / 'shots'
    output.mkdir(parents=True, exist_ok=True)
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        context, page = open_mobile(browser, url)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            today = page.evaluate("import('./core.js').then(C=>C.todayKST())")
            old = (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat()
            request, events = fixtures(today)
            body = show(page, today, request, events, textSize='xl')
            page.set_viewport_size({'width': 360, 'height': 844})
            full = page.get_by_role('button', name='전달 내용 복사', exact=True)
            receipt = page.get_by_role('button', name='접수번호 복사', exact=True)
            expect(full).to_be_enabled()
            expect(body.locator('.pc-help-item').first).to_contain_text('현장 확인 5쪽')
            expect(body).to_contain_text('목표 누적 3쪽')
            expect(body).to_contain_text('이전 수거 1쪽')
            expect(body).to_contain_text('이번 수거 2쪽')
            expect(body).to_contain_text('현장 추가')
            expect(body).to_contain_text('스티커·필증 없음·불일치')
            expect(body).to_contain_text('품목명·규격·단위')
            expect(body).to_contain_text('다시 입력하지')
            assert full.bounding_box()['y'] + full.bounding_box()['height'] <= 844, '주 복사는 긴 품목 목록을 내리기 전에 접근할 수 있다'
            before = page.evaluate("import('./core.js').then(C=>JSON.stringify(C.st.events))")
            page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{(window.__copies||=[]).push(text)}}})")
            receipt.click()
            page.wait_for_function('window.__copies?.length===1')
            assert page.evaluate('window.__copies[0]') == request['receipt_no']
            full.click()
            page.wait_for_function('window.__copies?.length===2')
            note = page.evaluate('window.__copies[1]')
            for required in ('현장 확인 5쪽', '목표 누적 3쪽', '이번 수거 2쪽', '현장 추가', '스티커·필증 없음·불일치', '다시 입력하지'):
                assert required in note, note
            for private in ('가상 주민', '01000000000', '가상시 가상동', 'FIELD_MEMO_PRIVATE_SENTINEL'):
                assert private not in note, note
            assert page.evaluate("import('./core.js').then(C=>JSON.stringify(C.st.events))") == before

            page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw Error('fixture denial')}}})")
            full.click()
            fallback = page.get_by_label('PC 전달 내용', exact=True)
            expect(fallback).to_be_visible()
            assert page.evaluate('document.activeElement.selectionEnd-document.activeElement.selectionStart') == len(fallback.input_value())
            receipt.click()
            expect(fallback).to_have_value(request['receipt_no'])

            # One unresolved native clipboard request owns both controls, including reentry.
            page.evaluate("window.__copyCalls=0;Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:()=>{window.__copyCalls++;return new Promise((resolve,reject)=>{window.__copyReject=reject})}}})")
            receipt.click()
            expect(full).to_be_disabled()
            expect(receipt).to_be_disabled()
            page.evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent==='전달 내용 복사').click()")
            assert page.evaluate('window.__copyCalls') == 1
            page.get_by_role('button', name='접수 상세로', exact=True).click()
            page.get_by_role('button', name='PC 입력 도움', exact=True).click()
            expect(page.get_by_role('button', name='전달 내용 복사', exact=True)).to_be_disabled()
            page.evaluate("window.__copyReject(Error('late old denial'))")
            expect(page.get_by_role('button', name='전달 내용 복사', exact=True)).to_be_enabled()
            expect(page.get_by_label('PC 전달 내용', exact=True)).to_be_hidden()

            # In-place source edits do not reuse a stale receipt/reference note.
            page.evaluate("window.__copies=[];Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>window.__copies.push(text)}})")
            page.evaluate("""async()=>{const C=await import('./core.js');C.st.daylist.stops[0].receipt_no='PC-HELP-CHANGED';
                [...document.querySelectorAll('button')].find(b=>b.textContent==='전달 내용 복사').click();}""")
            expect(page.get_by_label('PC에서 찾을 접수번호', exact=True)).to_have_text('PC-HELP-CHANGED')
            assert page.evaluate('window.__copies.length') == 0

            # Mixed recording dates remain totals for reference, never a fabricated per-day allocation.
            request, events = fixtures(today)
            events[0]['ts'] = old + 'T03:00:00Z'
            body = show(page, today, request, events)
            expect(full).to_be_enabled()
            expect(body).to_contain_text('여러 날짜')
            expect(body).to_contain_text(old)
            expect(body).to_contain_text(today)
            expect(body).to_contain_text('합계')
            full.click()
            page.wait_for_function('window.__copies.length===1')
            mixed = page.evaluate('window.__copies[0]')
            assert '여러 날짜' in mixed and '일자별' in mixed and old in mixed and today in mixed
            assert f'수거일 {today}\n' not in mixed, mixed

            # Long histories must not turn the sticky context into a layer covering the copy controls.
            request, events = fixtures(today)
            dates = [(dt.date.fromisoformat(today) - dt.timedelta(days=59 - i)).isoformat() for i in range(60)]
            many = events[:-1] + [dict(events[-1], ts=date + 'T03:00:00Z') for date in dates]
            body = show(page, today, request, many, textSize='xl')
            expect(full).to_be_enabled()
            full.scroll_into_view_if_needed()
            identity = page.locator('.pc-help-identity').bounding_box()
            assert identity['height'] <= page.viewport_size['height'] * .35, '기록일 요약이 입력 대조 화면을 가리지 않는다'
            assert full.evaluate("""button=>{const r=button.getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
                return hit===button||button.contains(hit)}"""), '상단 복사는 고정 접수 요약에 가리지 않고 실제로 누를 수 있다'
            count = page.evaluate('window.__copies.length')
            full.click()
            page.wait_for_function('count=>window.__copies.length===count+1', arg=count)
            all_dates = page.evaluate('window.__copies.at(-1)')
            assert all(date in all_dates for date in dates), all_dates
            assert '60일' in body.inner_text() and '여러 날짜' in all_dates

            # Dates from before since_seq or an older PC collection version do not mark current totals as mixed.
            request, events = fixtures(today)
            request.update(since_seq=10, pc_collection_ver=4, req_ver=4)
            filtered = [dict(events[0], seq=9, ts=old + 'T03:00:00Z'),
                        dict(events[0], seq=11, base_req_ver=3, ts=old + 'T03:00:00Z', body={**events[0]['body'], 'pc_collection_ver': 3}),
                        dict(events[0], seq=12, base_req_ver=4, body={**events[0]['body'], 'pc_collection_ver': 4}),
                        dict(events[-1], seq=13, base_req_ver=4, body={'outcome': 'PARTIAL', 'pc_collection_ver': 4})]
            body = show(page, today, request, filtered)
            expect(full).to_be_enabled()
            assert '여러 날짜' not in body.inner_text()
            assert old not in body.inner_text()

            body = show(page, today, request, [])
            expect(full).to_be_enabled()  # existing unconfirmed reference-copy policy
            expect(body).to_contain_text('결과 미확정')
            request, events = fixtures(today)
            body = show(page, today, request, events + [dict(events[0], body={**events[0]['body'], 'qty_collected': 4})])
            expect(full).to_be_disabled()
            expect(receipt).to_be_disabled()
            expect(body).to_contain_text('결과 재확인 필요')
            assert not errors, errors
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / 'pc-input-help-reconfirm.png'), full_page=True)
            print('PASS: PC입력 대조/혼합단위/FIELD/사유/복사상단·개인정보/순차복사·재진입·변경소유권/다중일자·버전필터/기존미확정·재확인정책', flush=True)
        except BaseException:
            page.screenshot(path=str(output / 'pc-input-help-failure.png'), full_page=True)
            raise
        finally:
            context.close()
            browser.close()


if __name__ == '__main__':
    main()
