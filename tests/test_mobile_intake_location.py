"""가상 휴대폰 접수에서 이전 주소 좌표와 자동 행정동의 저장·복원을 확인한다."""
import os
from urllib.parse import parse_qs, urlsplit
from playwright.sync_api import expect, sync_playwright
from test_review_ui import mobile_server
from test_workflow_mobile import daylist, open_mobile, seed, stop


def check_search_and_draft(browser, url):
    ctx, page = open_mobile(browser, url)
    try:
        day = page.evaluate("import('./core.js').then(C=>C.todayKST())")
        history = stop(3, day, address='광주 동구 가상역사길 1', dong='동명동')
        seed(page, {'mode': 'SOLO', 'jusoKey': 'synthetic-search', 'jusoCoordKey': 'synthetic-coord',
                    'requests': [history], 'intakeDraft': {'name': '가상 신규님', 'phone': '01000007868',
                    'planned_date': day, 'items': history['items']}}, registered=False)
        held = []
        row = {'roadAddr': '광주 동구 가상신규길 11 (계림동)', 'hemdNm': '광주광역시 동구 계림1동 행정복지센터 (전화 062-000-0000)', 'emdNm': '계림동',
               'zipNo': '61111', 'admCd': '111', 'rnMgtSn': '222', 'buldMnnm': '11'}
        def reply(route, rows):
            route.fulfill(json={'results': {'common': {'errorCode': '0', 'totalCount': '1'}, 'juso': rows}},
                          headers={'Access-Control-Allow-Origin': '*'})
        def search(route):
            params = parse_qs(urlsplit(route.request.url).query)
            reply(route, [row if params.get('addInfoYn') == ['Y'] else {**row, 'hemdNm': ''}])
        ctx.route('**/addrlink/addrLinkApi.do?*', search)
        ctx.route('**/addrlink/addrCoordApi.do?*', lambda route: held.append(route))
        page.goto(url + '#/intake')
        address = page.get_by_label('배출 주소')
        dong = page.locator('#intake-dong')
        query = page.locator('.addr-search input')
        def pick():
            query.fill('가상신규길 11')
            with page.expect_request(lambda req: 'addrCoordApi' in req.url):
                page.locator('.addr-item').click()
            expect(address).to_have_value(row['roadAddr'])
        def finish_coord():
            with page.expect_response(lambda res: 'addrCoordApi' in res.url):
                reply(held.pop(), [{'entX': '950000', 'entY': '1700000'}])
        def reopen():
            page.get_by_role('button', name='← 취소').click()
            expect(page).to_have_url(url + '#/home')
            page.goto(url + '#/intake')

        address.fill('광주 동구 가상역사길 12')
        expect(dong).to_have_value('')
        expect(page.get_by_role('button', name='동명동', exact=True)).to_be_visible()
        pick()
        expect(dong).to_have_value('계림1동')
        address.fill('광주 동구 가상다른길 9')
        finish_coord()
        expect(dong).to_have_value('')
        expect(page.locator('.addr-geo')).to_be_hidden()
        assert page.evaluate("import('./core.js').then(C=>[C.ui.form.lat,C.ui.form.lon,C.ui.form.zip])") == [None, None, '']
        pick()
        page.get_by_role('button', name='+ 품목 더 담기', exact=True).click()
        expect(query).to_have_count(0)
        finish_coord()
        page.get_by_role('button', name='완료', exact=True).click()
        expect(page.locator('.addr-geo')).to_contain_text('61111')
        reopen()
        expect(dong).to_have_value('계림1동')
        expect(page.locator('.addr-geo')).to_contain_text('61111')
        address.fill('광주 동구 가상새길 53')
        expect(dong).to_have_value('')
        expect(page.locator('.addr-geo')).to_be_hidden()
        row['hemdNm'] = '계림1동 / 계림2동 행정복지센터'
        pick()
        finish_coord()
        expect(dong).to_have_value('')
        for candidate in ('계림1동', '계림2동'):
            expect(page.get_by_role('button', name=candidate, exact=True)).to_be_visible()
        row['hemdNm'] = '광주광역시 동구 계림1동'
        dong.fill('지원2동')
        pick()
        finish_coord()
        expect(dong).to_have_value('지원2동')
        reopen()
        address.fill('광주 동구 계림1동 가상길 22')
        expect(dong).to_have_value('지원2동')
        dong.fill('')
        reopen()
        expect(dong).to_have_value('')
        address.fill('광주 동구 산수1동 가상길 23')
        expect(dong).to_have_value('')
        row.update(hemdNm='', emdNm='서남동', roadAddr='광주 동구 가상단독길 5 (서남동)')
        pick()
        finish_coord()
        expect(dong).to_have_value('')
        expect(page.get_by_role('button', name='서남동', exact=True)).to_be_visible()
        pick()
        expect(page.locator('.addr-geo')).to_contain_text('좌표 없이 저장')
        page.get_by_role('button', name='접수 등록', exact=True).click()
        expect(page).to_have_url(url + '#/home')
        finish_coord()
        saved = page.evaluate("import('./store.js').then(async S=>(await S.getSecure('state')).requests.at(-1))")
        assert saved['address'] == row['roadAddr'] and not saved.get('dong')
        assert all(key not in saved for key in ('lat', 'lon', 'zip')), saved
        print('행안부 모의 검색·좌표, 품목 왕복, 자동·수동·빈 동 초안, 후보 구분, 좌표 확인 전 저장')
    finally:
        ctx.close()


def main():
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path)
        for mode in ('SOLO', 'PC'):
            ctx, page = open_mobile(browser, url)
            try:
                day = page.evaluate("import('./core.js').then(C=>C.todayKST())")
                old = stop(1, day, address='광주 동구 계림1동 가상길 11', dong='계림1동',
                           lat=35.11, lon=126.91, geo_source='ADDR', dongAuto=True, zip='61111',
                           field=True, status='ACTIVE', channel='현장')
                patch = {'mode': mode, 'daylist': daylist(day, [])}
                if mode == 'SOLO':
                    patch['requests'] = [old]
                else:
                    patch.update(fieldStops=[old], events=[{'request_id': old['request_id'], 'kind': 'FIELD_INTAKE',
                                                            'body': {'temp_no': old['receipt_no'], **old}},
                                                           {'request_id': old['request_id'], 'kind': 'LOCATION_FIX', 'event_id': 'fixture-first-fix',
                                                            'body': {'lat': 35.111, 'lon': 126.911, 'source': 'GPS', 'worker': '가상 팀'}}])
                seed(page, patch, registered=mode == 'PC')
                page.goto(url + '#/intake/workflow-1')
                address = page.get_by_label('배출 주소')
                dong = page.locator('#intake-dong')
                address.fill('광주 동구 가상새길 35')
                expect(dong).to_have_value('')
                assert page.evaluate("import('./core.js').then(C=>[C.ui.form.lat,C.ui.form.lon,C.ui.form.zip])") == [None, None, '']
                if mode == 'PC':
                    save = page.get_by_role('button', name='고친 내용 저장', exact=True)
                    page.evaluate("import('./core.js').then(C=>C.st.exports=[{from:2,to:2}])")
                    save.click()
                    expect(page.get_by_role('button', name='지점 상세로')).to_be_visible()
                    expect(address).to_be_editable()
                    assert page.evaluate("import('./core.js').then(C=>C.st.events[1].kind)") == 'LOCATION_FIX'
                    page.evaluate("import('./core.js').then(C=>C.st.exports=[])")
                if mode == 'SOLO':
                    page.get_by_label('수정 사유').fill('가상 배출 장소 이동')
                page.get_by_role('button', name='수정 저장' if mode == 'SOLO' else '고친 내용 저장', exact=True).click()
                expect(page).to_have_url(url + '#/stop/workflow-1')
                records = page.evaluate("""async () => {
                    const d = await (await import('./store.js')).getSecure('state');
                    return d.mode === 'SOLO' ? d.requests : [...d.fieldStops, d.events[0].body];
                }""")
                for record in records:
                    assert record['address'] == '광주 동구 가상새길 35' and not record.get('dong')
                    assert all(record.get(k) is None for k in ('lat', 'lon', 'geo_source')), record
                if mode == 'PC':
                    fixed = page.evaluate("import('./store.js').then(async S=>(await S.getSecure('state')).events[1])")
                    assert (fixed['seq'], fixed['event_id'], fixed['kind']) == (2, 'fixture-first-fix', 'FIELD_MEMO')
                    assert fixed['body']['worker'] == '가상 팀' and 'lat' not in fixed['body']
            finally:
                ctx.close()
        print('단독·PC 연결 수정 접수와 전송 기록에서 이전 주소의 자동 동·좌표 제거')
        check_search_and_draft(browser, url)
        browser.close()


if __name__ == '__main__':
    main()
