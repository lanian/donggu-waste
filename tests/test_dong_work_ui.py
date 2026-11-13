"""동별 통합관리 UI 회귀: 원본 ES 모듈 + Chromium, 가상 API (운영 DB 미사용)."""
from contextlib import contextmanager
from functools import partial
import http.server
import os
import tempfile
from pathlib import Path
import threading

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
passed = 0


def ok(value, label):
    global passed
    assert value, label
    passed += 1
    print("  OK", label, flush=True)


@contextmanager
def fixture_server():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript"}

        def log_message(self, *args):
            pass

        def translate_path(self, path):
            if path.startswith('/fonts/'):
                return str(ROOT / 'pc/ui/fonts' / Path(path).name)
            return super().translate_path(path)

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), partial(Quiet, directory=str(ROOT)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_address[1]}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


FIXTURE = r"""async ({role}) => {
 const C=await import('/pc/ui/core.js');
 window.__C=C; window.__calls=[];window.__gates=[];window.__hold=null;window.__fail=null;window.__requestItems=null;window.__duplicateDongs=[];
 window.__dongs=[{id:'d1',name:'가상1동',active:true},{id:'d2',name:'가상2동',active:true}];
 window.__crews=[{id:'c1',name:'가상 수거팀',active:true,capacity_m3:8,shift_start:'09:00',shift_end:'18:00'}];
 window.__boot=(username='fixture',role0=role)=>C.setBoot({today:'2026-11-08',me:{id:username,username,role:role0,display_name:'가상 담당자',prefs:{}},settings:{},channels:['전화'],dongs:window.__dongs,crews:window.__crews});
 window.__boot();
 const realFetch=window.fetch;
 window.fetch=async(path,options={})=>{
  if(!String(path).startsWith('/api/'))return realFetch(path,options);
  const u=new URL(path,location.origin),endpoint=u.pathname.slice(5),body=options.body?JSON.parse(options.body):null;
  window.__calls.push({endpoint,search:u.search,body,method:options.method||'GET'});
  const hold=window.__hold===endpoint;if(hold)window.__hold=null;
  if(window.__fail===endpoint){window.__fail=null;return new Response(JSON.stringify({error:'가상 권한 또는 버전 충돌'}),{status:409});}
  const dong=u.searchParams.get('dong')||'all',date=u.searchParams.get('date')||'2026-11-08';
  const metrics={scheduled:2,done:1,unassigned:1,revisit:0,overdue:3,no_date:1};
  const row={id:'r1',receipt_no:'DG-20261108-0001',req_ver:2,name:'가상 주민',phone:'01012345678',address:'가상로 1',dong:'가상1동',dong_id:'d1',planned_date:date,status:'RECEIVED',channel:'전화',items:window.__requestItems||[{line_id:'l1',name:'가상 의자',spec:'일반',unit:'EA',unit_label:'개',qty:1}],locked_lines:[],versions:[]};
  let out={};
  if(endpoint==='dong-work')out={date,selected_dong:dong,scope:{mode:role==='DONG'?'assigned':'all',dong_ids:['d1','d2']},dongs:window.__dongs,crews:window.__crews,summary:metrics,rows:window.__dongs.map(d=>({...d,...metrics}))};
  else if(endpoint==='dong-work/requests'&&(options.method||'GET')==='GET')out={rows:[{...row,name:'가상 주민 '+dong}],total:1,page:1,pages:1};
  else if(endpoint==='dong-work/requests/r1'&&(options.method||'GET')==='GET')out=row;
  else if(endpoint==='dong-work/requests')out={id:'r1',receipt_no:row.receipt_no,warnings:[]};
  else if(endpoint==='dong-work/duplicates')out=window.__duplicateDongs.includes(dong)?[{id:'duplicate-fixture',receipt_no:'DG-20261108-0002'}]:[];
  else if(endpoint==='dong-work/geocode')out={lat:35.145,lon:126.923,label:'가상 위치',exact:true};
  else if(endpoint==='dong-work/assignments')out=[];
  else if(endpoint==='dong-work/route')out={routes:[{crew_id:'c1',crew_name:'가상 수거팀',stops:[{request_id:'r1',eta:'09:00'}]}],requests:{r1:row},unassigned:[],basis:{date:body.date,dong:body.dong,crew_versions:{c1:0}}};
  else if(endpoint==='dong-work/assign')out=[{count:1,assign_ver:1}];
  else if(endpoint==='fee-usage')out=[];
  else if(endpoint==='users')out=[{id:'u1',username:'dong-user',display_name:'동 담당',role:'DONG',dong_ids:['d1'],active:true}];
  else if(endpoint.startsWith('audit?')||endpoint==='audit')out={rows:[],total:0,page:1,pages:1};
  else if(endpoint==='privacy')out={retention_years:5,preview:{count:0},log:[]};
  else if(endpoint==='diag')out={db_bytes:0,wal_bytes:0,free_bytes:0,photos:{count:0,bytes:0},sessions:1,jobs_queued:0,tables:{}};
  else if(endpoint==='ops-status')out={items:[]};
  else if(!endpoint.startsWith('dong-work/')&&!endpoint.startsWith('users/')&&!['prefs'].includes(endpoint))throw Error('전역 API 호출 금지: '+endpoint);
  const json=JSON.stringify(out);if(hold)await new Promise(release=>window.__gates.push(release));
  return new Response(json,{status:200,headers:{'Content-Type':'application/json'}});
 };
 try{window.__S=await import('/pc/ui/screens/dongs.js');window.__mount=async()=>document.getElementById('app').replaceChildren(await window.__S.screenDongs(location.hash.split('/')[2]||null));addEventListener('hashchange',window.__mount);await window.__mount();return true;}
 catch(error){window.__loadError=error.message;return false;}
}"""


def mount(browser, url, role='DONG'):
    context = browser.new_context(viewport={'width': 1440, 'height': 900}, service_workers='block')
    page = context.new_page()
    page.set_default_timeout(7000)
    page.route('**/ui-fixture.html', lambda r: r.fulfill(content_type='text/html', body="<html lang='ko'><link rel='stylesheet' href='/pc/ui/pc.css'><main id='app'></main></html>"))
    page.goto(url + '/ui-fixture.html#/dongs')
    loaded = page.evaluate(FIXTURE, {'role': role})
    ok(loaded, '원본 동별관리 모듈 mount: ' + (page.evaluate('window.__loadError') or role))
    return context, page


def location_is_new(page):
    return page.url.endswith('/new')


def regressions(browser, url):
    context, page = mount(browser, url)
    try:
        expect(page.get_by_role('heading', name='동별 관리', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='접수 등록', exact=True)).to_be_visible()
        ok('null' not in page.locator('#app').inner_text(), '화면의 빈 조건 요소를 null 문자열로 출력하지 않는다')
        ok(page.get_by_label('행정동 범위', exact=True).locator('option[value="-"]').count() == 0, '지정동 계정은 미지정동 선택을 제공하지 않는다')
        page.locator('.dw-metric').nth(1).click()
        expect(page.get_by_role('heading', name='선택일 완료', exact=True)).to_be_visible()
        ok(page.evaluate("window.__calls.some(x=>x.endpoint==='dong-work/requests'&&x.search.includes('bucket=done'))") and page.locator('.dw-metric[aria-pressed="true"]').count() == 1, '완료 카드는 완료 전용 목록을 요청하고 해당 카드만 선택 표시한다')
        page.evaluate("window.__hold='dong-work/requests'")
        page.get_by_label('행정동 범위', exact=True).select_option('d1')
        page.wait_for_function('window.__gates.length===1')
        page.get_by_label('행정동 범위', exact=True).select_option('d2')
        expect(page.get_by_text('가상 주민 d2', exact=True)).to_be_visible()
        page.evaluate('window.__gates.shift()()')
        expect(page.get_by_text('가상 주민 d2', exact=True)).to_be_visible()
        ok(page.get_by_text('가상 주민 d1', exact=True).count() == 0, '이전동 늦은 목록이 새 범위를 덮지 않는다')
        page.get_by_role('button', name='접수 등록', exact=True).click()
        dlg = page.get_by_role('region', name='동별 접수 등록' if location_is_new(page) else '동별 접수 상세', exact=True)
        dlg.get_by_label('이름', exact=True).fill('초안 주민')
        page.get_by_role('button', name='목록으로 · 초안 보관', exact=True).click()
        page.get_by_role('button', name='접수 등록', exact=True).click()
        expect(page.locator('.dw-form').get_by_label('이름', exact=True)).to_have_value('초안 주민')
        page.get_by_role('button', name='목록으로 · 초안 보관', exact=True).click()
        page.get_by_label('행정동 범위', exact=True).select_option('d1')
        page.get_by_role('button', name='접수 등록', exact=True).click()
        expect(page.locator('.dw-form').get_by_label('이름', exact=True)).to_have_value('')
        page.get_by_role('button', name='목록으로 · 초안 보관', exact=True).click()
        ok(True, '등록 초안은 원래 동에 묶여 복구하고 다른 동에 복사하지 않는다')
        page.get_by_role('button', name='노선 계산', exact=True).click()
        expect(page.get_by_role('button', name='배정 확정', exact=True)).to_be_enabled()
        page.get_by_label('업무 기준일', exact=True).fill('2026-11-09')
        expect(page.get_by_role('button', name='배정 확정', exact=True)).to_be_disabled()
        ok(True, '기준일 변경 즉시 과거 노선 확정을 차단한다')
        page.get_by_role('button', name='노선 계산', exact=True).click()
        expect(page.get_by_role('button', name='배정 확정', exact=True)).to_be_enabled()
        page.evaluate("window.__fail='dong-work/assign'")
        page.get_by_role('button', name='배정 확정', exact=True).click()
        page.get_by_role('dialog').get_by_role('button', name='이 범위로 배정 확정', exact=True).click()
        expect(page.get_by_role('dialog').get_by_role('alert')).to_contain_text('가상 권한 또는 버전 충돌')
        page.keyboard.press('Escape')
        expect(page.get_by_role('button', name='배정 확정', exact=True)).to_be_disabled()
        ok(True, '배정 충돌시 계산 결과를 폐기하고 명시 재계산을 요구한다')
        page.evaluate("async()=>{window.__boot('other');await window.__mount();}")
        page.get_by_label('행정동 범위', exact=True).select_option('d2')
        page.get_by_role('button', name='접수 등록', exact=True).click()
        expect(page.locator('.dw-form').get_by_label('이름', exact=True)).to_have_value('')
        page.get_by_role('button', name='목록으로 · 초안 보관', exact=True).click()
        ok(True, '계정 교체시 이전 담당자 초안을 비운다')
        ok(all(x['endpoint'].startswith('dong-work') or x['endpoint'] == 'fee-usage' for x in page.evaluate('window.__calls')), '동 계정은 기존 전역 주민 API를 호출하지 않는다')
        for width, height in [(1440, 900), (980, 680), (390, 844)]:
            page.set_viewport_size({'width': width, 'height': height})
            expect(page.get_by_role('button', name='접수 등록', exact=True)).to_be_visible()
            ok(page.evaluate('document.documentElement.scrollWidth<=innerWidth'), f'{width}px에서 페이지 가로 넘침 없이 주동작 제공')
    finally:
        context.close()
    for role in ['LEAD', 'FIELD']:
        context, page = mount(browser, url, role)
        try:
            ok(page.get_by_role('button', name='접수 등록', exact=True).count() == 0, role + ' 계정의 변경 동작을 제공하지 않는다')
            if role == 'FIELD':
                ok(not page.evaluate('window.__calls'), '수거팀은 동별 업무 API를 읽지 않는다')
        finally:
            context.close()


def form_regressions(browser, url):
    context, page = mount(browser, url)
    try:
        page.get_by_label('행정동 범위', exact=True).select_option('d1')
        page.get_by_role('button', name='접수 등록', exact=True).click()
        dlg = page.get_by_role('region', name='동별 접수 등록' if location_is_new(page) else '동별 접수 상세', exact=True)
        dlg.get_by_label('이름', exact=True).fill('가상 새 주민')
        dlg.get_by_label('연락처', exact=True).fill('01000000001')
        dlg.get_by_label('배출 주소', exact=True).fill('가상로 10')
        page.evaluate("window.__hold='dong-work/geocode'")
        dlg.get_by_role('button', name='주소로 위치 찾기', exact=True).click()
        page.wait_for_function('window.__gates.length===1')
        dlg.get_by_label('배출 주소', exact=True).fill('가상로 20')
        page.evaluate('window.__gates.shift()()')
        expect(dlg.get_by_label('위도', exact=True)).to_have_value('')
        ok(True, '이전 주소의 늦은 위치 응답은 새 주소에 적용하지 않는다')
        dlg.get_by_role('button', name='주소로 위치 찾기', exact=True).click()
        expect(dlg.get_by_label('위도', exact=True)).to_have_value('35.145')
        dlg.get_by_label('접수 행정동', exact=True).select_option('d2')
        expect(dlg.get_by_label('위도', exact=True)).to_have_value('')
        ok(True, '주소 위치를 찾은 뒤 행정동을 바꾸면 좌표를 지운다')
        dlg.get_by_role('button', name='주소로 위치 찾기', exact=True).click()
        expect(dlg.get_by_label('위도', exact=True)).to_have_value('35.145')
        dlg.get_by_label('품목명·규격 검색', exact=True).fill('의자')
        dlg.locator('.results button').first.click()  # 여러 규격은 먼저 명시적으로 선택한다.
        dlg.get_by_label('품목명·규격 검색', exact=True).press('Enter')
        expect(dlg.locator('.dw-item-row')).to_have_count(1)
        expect(dlg.locator('.dw-detail-actions [role=alert]')).to_have_text('')
        ok(not page.evaluate("window.__calls.some(x=>x.endpoint==='dong-work/requests'&&x.method==='POST')"), '품목 검색 Enter는 품목만 추가하고 접수를 저장하지 않는다')
        for width, height in [(1440, 900), (980, 680), (390, 844)]:
            page.set_viewport_size({'width': width, 'height': height})
            bounds = dlg.get_by_role('button', name='접수 저장', exact=True).bounding_box()
            ok(bounds is not None and bounds['y'] >= 0 and bounds['y'] + bounds['height'] <= height, f'{width}px 접수 양식에서 저장 버튼을 스크롤 없이 표시한다')
        page.set_viewport_size({'width': 1440, 'height': 900})
        page.evaluate("window.__hold='dong-work/requests'")
        dlg.get_by_role('button', name='접수 저장', exact=True).dblclick()
        page.wait_for_function('window.__gates.length===1')
        posts = page.evaluate("window.__calls.filter(x=>x.endpoint==='dong-work/requests'&&x.method==='POST')")
        ok(len(posts) == 1, '등록 연속 클릭은 요청 한 번만 보낸다')
        body = posts[0]['body']
        ok(body['dong_id'] == 'd2' and body['planned_date'] == '2026-11-08' and body['lat'] == 35.145 and body['lon'] == 126.923 and body['client_req_id'], '등록은 명시 동·예정일·위치·멱등키를 전용 API에 보낸다')
        page.evaluate('window.__gates.shift()()')
        expect(page.get_by_role('dialog')).to_have_count(0)
        page.get_by_role('button', name='DG-20261108-0001', exact=True).click()
        dlg = page.get_by_role('region', name='동별 접수 등록' if location_is_new(page) else '동별 접수 상세', exact=True)
        dlg.get_by_label('이름', exact=True).fill('고친 가상 주민')
        dlg.get_by_label('수정 사유', exact=True).fill('가상 수정')
        page.evaluate("window.__fail='dong-work/requests/r1'")
        dlg.get_by_role('button', name='수정 저장', exact=True).click()
        expect(dlg.get_by_label('이름', exact=True)).to_have_value('고친 가상 주민')
        expect(dlg.get_by_text('다른 곳에서 접수가 바뀌었습니다.', exact=False)).to_be_visible()
        ok(True, '수정 충돌은 값을 지우지 않고 접수 버전을 유지한다')
        page.evaluate("window.__fail='dong-work/requests/r1'")
        dlg.get_by_role('button', name='초안 버리고 다시 열기', exact=True).click()
        page.get_by_role('button', name='다시 시도', exact=True).click()
        dlg = page.get_by_role('region', name='동별 접수 등록' if location_is_new(page) else '동별 접수 상세', exact=True)
        expect(dlg.get_by_label('이름', exact=True)).to_have_value('가상 주민')
        page.get_by_role('button', name='목록으로 · 초안 보관', exact=True).click()
        page.get_by_role('button', name='DG-20261108-0001', exact=True).click()
        expect(page.locator('.dw-form').get_by_label('이름', exact=True)).to_have_value('가상 주민')
        page.get_by_role('button', name='목록으로 · 초안 보관', exact=True).click()
        ok(True, '충돌 초안 버리기는 이전 수정값을 다시 복구하지 않는다')
    finally:
        context.close()


def pending_save_draft_regression(browser, url):
    context, page = mount(browser, url)
    try:
        page.get_by_role('button', name='DG-20261108-0001', exact=True).click()
        page.get_by_label('이름', exact=True).fill('먼저 저장한 가상 초안')
        page.get_by_label('수정 사유', exact=True).fill('가상 응답 지연')
        page.evaluate("window.__hold='dong-work/requests/r1'")
        page.get_by_role('button', name='수정 저장', exact=True).click()
        page.wait_for_function('window.__gates.length===1')
        page.get_by_role('button', name='목록으로 · 초안 보관', exact=True).click()
        page.get_by_role('button', name='DG-20261108-0001', exact=True).click()
        page.get_by_label('이름', exact=True).fill('응답을 기다리는 동안 새로 쓴 가상 초안')
        page.get_by_label('이름', exact=True).press('Tab')
        page.evaluate('window.__gates.shift()()')
        page.wait_for_function('window.__C.pendingActions.size===0')
        page.evaluate("location.hash='#/dongs'")
        page.get_by_role('button', name='DG-20261108-0001', exact=True).click()
        expect(page.get_by_label('이름', exact=True)).to_have_value('응답을 기다리는 동안 새로 쓴 가상 초안')
        ok(True, '이전 저장 응답은 다시 연 화면에서 새로 작성한 초안을 지우지 않는다')
    finally:
        context.close()


def users_and_router_regressions(browser, url):
    context, page = mount(browser, url, 'ADMIN')
    try:
        page.evaluate("async()=>{const U=await import('/pc/ui/screens/users.js');window.__C.setRender(()=>{});document.getElementById('app').replaceChildren(await U.screenUsers());}")
        page.get_by_label('새 계정 역할', exact=True).select_option('DONG')
        expect(page.get_by_label('새 계정 담당 동 가상1동', exact=True)).to_be_visible()
        page.get_by_placeholder('아이디 (영문)', exact=True).fill('new-dong')
        page.get_by_placeholder('이름', exact=True).fill('가상 동 담당')
        page.get_by_placeholder('임시 비밀번호 (10자 이상)', exact=True).fill('Fixture-Secret-2026')
        page.get_by_role('button', name='사용자 추가', exact=True).click()
        expect(page.get_by_text('동 담당자 계정은 담당 동을 하나 이상 선택하세요.', exact=True)).to_be_visible()
        page.get_by_label('새 계정 담당 동 가상1동', exact=True).check()
        page.get_by_label('새 계정 담당 동 가상2동', exact=True).check()
        page.get_by_role('button', name='사용자 추가', exact=True).click()
        page.wait_for_function("window.__calls.some(x=>x.endpoint==='users'&&x.method==='POST')")
        payload = page.evaluate("window.__calls.find(x=>x.endpoint==='users'&&x.method==='POST').body")
        ok(payload['role'] == 'DONG' and payload['dong_ids'] == ['d1', 'd2'], '사용자 생성은 담당 동 필수·복수 지정을 지원한다')
        page.get_by_text('가상1동 · 지정 범위 수정', exact=True).click()
        page.get_by_label('dong-user 담당 동 가상2동', exact=True).check()
        page.get_by_role('button', name='담당 동 저장', exact=True).click()
        expect(page.get_by_text('가상1동, 가상2동 · 지정 범위 수정', exact=True)).to_be_visible()
        ok(page.evaluate("window.__calls.some(x=>x.endpoint==='users/u1/dongs'&&x.body.dong_ids.length===2)"), '기존 동 계정의 담당 범위를 서버에 저장하고 표에도 갱신한다')
    finally:
        context.close()


def cancel_reason_regressions(browser, url):
    for code in ['OTHER', 'RESIDENT']:
        context, page = mount(browser, url)
        try:
            page.get_by_role('button', name='DG-20261108-0001', exact=True).click()
            page.locator('.dw-form').get_by_role('button', name='접수 취소', exact=True).click()
            dlg = page.get_by_role('dialog', name='접수 취소', exact=True)
            dlg.get_by_role('combobox').select_option(code)
            reason = f'{code} 가상 취소 사유'
            dlg.get_by_placeholder('직접 입력 (기타는 필수, 그 밖에는 덧붙일 말)', exact=True).fill(reason)
            dlg.get_by_role('button', name='취소 확정', exact=True).click()
            page.wait_for_function("window.__calls.some(x=>x.endpoint==='dong-work/requests/r1/cancel')")
            payload = page.evaluate("window.__calls.find(x=>x.endpoint==='dong-work/requests/r1/cancel').body")
            ok(payload == {'code': code, 'reason': reason, 'req_ver': 2}, f'{code} 취소는 서버 계약에 맞는 사유와 접수 버전을 보낸다')
        finally:
            context.close()


def stored_price_regressions(browser, url):
    for status in ['CONFIRMED', 'PENDING']:
        context, page = mount(browser, url)
        try:
            page.evaluate("""(status)=>{const r=window.__C.FEES().rows.find(x=>x.price===9000&&x.status==='CONFIRMED');window.__requestItems=[{line_id:'saved-price-line',fee_rate_id:r.id,name:r.name,spec:r.spec,unit:r.unit,unit_label:r.unit_label,qty:2,unit_price:status==='CONFIRMED'?1000:null,price_status:status}];}""", status)
            page.get_by_role('button', name='DG-20261108-0001', exact=True).click()
            dlg = page.get_by_role('region', name='동별 접수 등록' if location_is_new(page) else '동별 접수 상세', exact=True)
            expected = '2,000원' if status == 'CONFIRMED' else '0원 + 금액 확인 대기'
            expect(dlg.locator('.dw-total')).to_have_text(expected)
            expect(dlg.locator('.dw-item-row small')).to_have_text('1,000원' if status == 'CONFIRMED' else '금액 확인 대기')
            ok(True, f'기존 {status} 품목은 현재 9,000원 표와 무관하게 저장 단가·확인 대기를 표시한다')
            if status == 'CONFIRMED':
                dlg.get_by_label('품목명·규격 검색', exact=True).fill('의자')
                amount_label = dlg.locator('.results button').first.locator('.p').inner_text().split(' / ')[0]
                dlg.locator('.results button').first.click()
                dlg.get_by_role('button', name='품목 추가', exact=True).click()
                expect(dlg.locator('.dw-item-row').last.locator('small')).to_have_text(amount_label)
                ok(True, '새로 추가한 품목은 현재 수수료표 단가를 표시한다')
        finally:
            context.close()


def duplicate_scope_regressions(browser, url):
    context, page = mount(browser, url)
    try:
        page.get_by_label('행정동 범위', exact=True).select_option('d1')
        page.get_by_role('button', name='접수 등록', exact=True).click()
        dlg = page.get_by_role('region', name='동별 접수 등록' if location_is_new(page) else '동별 접수 상세', exact=True)
        dlg.get_by_label('이름', exact=True).fill('가상 중복 후보')
        dlg.get_by_label('연락처', exact=True).fill('01000000001')
        dlg.get_by_label('배출 주소', exact=True).fill('가상로 10')
        dlg.get_by_label('접수 행정동', exact=True).select_option('d2')
        dlg.get_by_label('품목명·규격 검색', exact=True).fill('의자')
        dlg.locator('.results button').first.click()  # 여러 규격은 먼저 명시적으로 선택한다.
        dlg.get_by_label('품목명·규격 검색', exact=True).press('Enter')
        page.evaluate("window.__duplicateDongs=['d1','d2']")
        dlg.get_by_role('button', name='접수 저장', exact=True).click()
        page.wait_for_function("window.__calls.some(x=>x.endpoint==='dong-work/duplicates')")
        calls = page.evaluate("window.__calls.filter(x=>x.endpoint==='dong-work/duplicates')")
        ok('dong=d2' in calls[-1]['search'], '중복 검색은 목록의 d1 대신 양식에서 저장할 d2를 확인한다')
        expect(dlg.get_by_text('담당 범위에 비슷한 접수 1건이 있습니다.', exact=False)).to_be_visible()
        ok(not page.evaluate("window.__calls.some(x=>x.endpoint==='dong-work/requests'&&x.method==='POST')"), '다른 동 중복 경고 확인 전에는 새 접수를 보내지 않는다')
        dlg.get_by_label('접수 행정동', exact=True).select_option('d1')
        dlg.get_by_role('button', name='접수 저장', exact=True).click()
        page.wait_for_function("window.__calls.filter(x=>x.endpoint==='dong-work/duplicates').length===2")
        ok(page.evaluate("window.__calls.filter(x=>x.endpoint==='dong-work/duplicates')[1].search.includes('dong=d1')") and not page.evaluate("window.__calls.some(x=>x.endpoint==='dong-work/requests'&&x.method==='POST')"), '동이 바뀌면 이전 동의 중복 확인을 재사용하지 않는다')
    finally:
        context.close()


def router_regressions(browser, url):
    context, page = mount(browser, url)
    try:
        page.evaluate("location.hash='#/request/r1'")
        page.evaluate("removeEventListener('hashchange',window.__mount)")
        page.add_script_tag(url=url + '/pc/ui/pc.js')
        expect(page.locator('.shell .dong-workspace h1')).to_have_text('동별 관리')
        ok(page.locator('nav.side a[href="#/requests"],nav.side a[href="#/home"],nav.side a[href="#/dispatch"]').count() == 0, '원본 PC 라우터는 DONG의 전역 상세 주소를 동별 화면으로 돌리고 전역 메뉴를 숨긴다')
        ok(page.locator('a.brand').get_attribute('href') == '#/dongs', '동 담당자 로고도 사용 가능한 기본 업무 화면으로 이동한다')
        ok(all(x['endpoint'].startswith('dong-work') for x in page.evaluate('window.__calls')), 'DONG의 실제 PC shell은 전역 home badge API를 호출하지 않는다')
    finally:
        context.close()


def reauthentication_regressions(browser, url):
    for action in ['cancel', 'continue']:
        context, page = mount(browser, url)
        try:
            page.evaluate("removeEventListener('hashchange',window.__mount)")
            page.add_script_tag(url=url + '/pc/ui/pc.js')
            expect(page.locator('.shell .dong-workspace h1')).to_have_text('동별 관리')
            page.get_by_role('button', name='접수 등록', exact=True).click()
            page.locator('.dw-form').get_by_label('이름', exact=True).fill('이전 담당자 주민 비공개초안')
            page.evaluate("""()=>{
              const original=window.fetch;window.__expireOnce=true;window.__loggedOut=true;window.__reauthDone=false;
              window.fetch=async(path,options={})=>{
                const u=new URL(path,location.origin);
                if(u.pathname==='/api/dong-work/assignments'&&window.__expireOnce){window.__expireOnce=false;return new Response(JSON.stringify({login:true,error:'로그인 필요'}),{status:401});}
                if(u.pathname==='/api/state')return new Response(JSON.stringify({setup_needed:false,logged_in:!window.__loggedOut}),{status:200});
                if(u.pathname==='/api/login'){window.__loggedOut=false;return new Response('{}',{status:200});}
                return original(path,options);
              };
              window.__C.api('dong-work/assignments?date=2026-11-08&dong=all').then(()=>{window.__reauthDone=true;},()=>{window.__reauthDone=true;});
            }""")
            relogin = page.get_by_role('dialog', name='다시 로그인', exact=True)
            expect(relogin).to_be_visible()
            if action == 'cancel':
                relogin.get_by_role('button', name='로그인 화면으로', exact=True).click()
                expect(page.locator('#app .login')).to_be_visible()
                ok(page.locator('[data-dong-owner]').count() == 0 and page.get_by_role('dialog').count() == 0 and '이전 담당자 주민 비공개초안' not in page.locator('body').inner_text(), '재인증 취소 후 로그인 화면에는 이전 주민 양식과 대화 상자가 남지 않는다')
                page.evaluate("async()=>{window.__boot();history.replaceState(null,'','#/dongs');await window.__C.render(true);}")
                page.get_by_role('button', name='접수 등록', exact=True).click()
                expect(page.locator('.dw-form').get_by_label('이름', exact=True)).to_have_value('')
                ok(True, '로그인 화면으로 돌아간 뒤에는 같은 계정도 이전 주민 초안을 복구하지 않는다')
            else:
                relogin.get_by_label('비밀번호', exact=True).fill('Fixture-Secret-2026')
                relogin.get_by_role('button', name='로그인하고 계속', exact=True).click()
                page.wait_for_function('window.__reauthDone')
                expect(page.locator('.dw-form').get_by_label('이름', exact=True)).to_have_value('이전 담당자 주민 비공개초안')
                page.get_by_role('button', name='목록으로 · 초안 보관', exact=True).click()
                page.get_by_role('button', name='접수 등록', exact=True).click()
                expect(page.locator('.dw-form').get_by_label('이름', exact=True)).to_have_value('이전 담당자 주민 비공개초안')
                ok(True, '같은 계정으로 재인증하고 계속하면 원래 입력과 보관 초안을 유지한다')
        finally:
            context.close()


def visual_regressions(browser, url):
    context, page = mount(browser, url)
    shots = Path(os.environ.get('DG_UI_SHOTS', tempfile.mkdtemp(prefix='dong-ui-shots-')))
    shots.mkdir(parents=True, exist_ok=True)
    try:
        expect(page.get_by_role('button', name='DG-20261108-0001', exact=True)).to_be_visible()
        for theme, font, width, height in [('light', 100, 1440, 900), ('dark', 125, 980, 680), ('dark', 125, 390, 844)]:
            page.evaluate("({theme,font})=>{document.documentElement.dataset.theme=theme;document.documentElement.style.fontSize=font+'%';}", {'theme': theme, 'font': font})
            page.set_viewport_size({'width': width, 'height': height})
            page.screenshot(path=str(shots / f'dongs-{theme}-{font}-{width}.png'), full_page=True)
            page.get_by_role('button', name='접수 등록', exact=True).click()
            bounds = page.locator('.dw-form').get_by_role('button', name='접수 저장', exact=True).bounding_box()
            ok(bounds is not None and bounds['y'] >= 0 and bounds['y'] + bounds['height'] <= height, f'{theme} {font}% {width}px에서도 양식 저장 단추가 viewport 안에 있다')
            page.screenshot(path=str(shots / f'dong-form-{theme}-{font}-{width}.png'), full_page=True)
            page.get_by_role('button', name='목록으로 · 초안 보관', exact=True).click()
            expect(page.get_by_role('button', name='접수 등록', exact=True)).to_be_focused()
        ok(True, '양식 닫기 후 접수 등록 버튼으로 키보드 초점을 돌려준다')
        print('  SCREENSHOTS', shots, flush=True)
    finally:
        context.close()


def main():
    with fixture_server() as url, sync_playwright() as p:
        options = {'headless': True}
        if os.environ.get('CHROME'):
            options['executable_path'] = os.environ['CHROME']
        browser = p.chromium.launch(**options)
        try:
            selected = os.environ.get('DG_DONG_UI_CASE')
            cases = {'cancel': cancel_reason_regressions, 'price': stored_price_regressions, 'duplicate': duplicate_scope_regressions, 'auth': reauthentication_regressions, 'race': pending_save_draft_regression}
            if selected:
                cases[selected](browser, url)
            else:
                regressions(browser, url)
                form_regressions(browser, url)
                users_and_router_regressions(browser, url)
                router_regressions(browser, url)
                visual_regressions(browser, url)
                for case in cases.values():
                    case(browser, url)
        finally:
            browser.close()
    print(f'\n통과 {passed}개 (원본 모듈·가상 API)', flush=True)


if __name__ == '__main__':
    main()
