"""임시 PC에서 전화 접수의 행정동 자동 선택·수동 보정·저장·추가 현장시간을 검증한다."""
import argparse
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright
from test_dong_work_e2e import api
from test_review_ui import pc_server


def check_official_address(page, url, address, dong):
    search = page.get_by_role('combobox', name='주소 찾기')
    held = []
    row = {'kind': '행안부', 'label': '가상 행안부 주소', 'address': '광주광역시 동구 가상도로 11',
           'dong': '산수2동', 'ref': {'admCd': '123'}}
    page.route('**/api/juso-search?*', lambda route: route.fulfill(json={'items': [row]}))
    page.route('**/api/juso-coord?*', lambda route: held.append(route))
    search.fill('가상도로 11')
    with page.expect_request(lambda r: '/api/juso-coord?' in r.url):
        page.get_by_role('option', name='가상 행안부 주소').click()
    expect(dong).to_have_value('산수2동')
    address.fill('광주광역시 동구 계림2동 가상')
    expect(dong).to_have_value('계림2동')
    with page.expect_response(lambda r: '/api/juso-coord?' in r.url):
        held.pop().fulfill(json={'lat': 35.1, 'lon': 126.9, 'source': 'JUSO'})
    expect(address).to_have_value('광주광역시 동구 계림2동 가상')
    expect(page.locator('.reqform input[readonly]').first).to_have_value('')

    search.fill('가상도로 11')
    with page.expect_request(lambda r: '/api/juso-coord?' in r.url):
        page.get_by_role('option', name='가상 행안부 주소').click()
    held.pop().fulfill(json={'none': True})
    expect(dong).to_have_value('산수2동')
    page.goto(url + '#/requests')
    expect(address).to_have_count(0)
    page.goto(url + '#/request/new')
    expect(dong).to_have_value('산수2동')
    row.update(dong=None, emdNm='산수2동')
    search.fill('가상도로 11')
    with page.expect_request(lambda r: '/api/juso-coord?' in r.url):
        page.get_by_role('option', name='가상 행안부 주소').click()
    held.pop().fulfill(json={'none': True})
    expect(dong).to_have_value('')
    expect(page.locator('.dongmsg')).to_contain_text('직접 선택')
    print('행안부 행정동 초안 유지·법정동 단독 결과·늦은 좌표 응답', flush=True)


def check_scoped_address(browser, admin, url):
    dongs = {d['name']: d['id'] for d in api(admin, url, 'bootstrap')['dongs']}
    password = 'synthetic-dong-123'
    api(admin, url, 'users', {'username': 'scoped_address', 'password': password, 'display_name': '가상 동 담당',
                              'role': 'DONG', 'dong_ids': [dongs['계림1동'], dongs['계림2동']]})
    scoped = browser.new_context(service_workers='block')
    scoped.route('**/*', lambda route: route.continue_() if route.request.url.startswith(url) else route.abort())
    try:
        api(scoped, url, 'login', {'username': 'scoped_address', 'password': password})
        api(scoped, url, 'password', {'old': password, 'password': 'synthetic-dong-changed-123'})
        page = scoped.new_page()
        page.goto(url + '#/dongs')
        page.get_by_role('button', name='접수 등록', exact=True).click()
        editor = page.get_by_role('region', name='동별 접수 등록', exact=True)
        address = editor.get_by_label('배출 주소', exact=True)
        dong = editor.get_by_label('접수 행정동', exact=True)
        address.fill('광주광역시 동구 계림1동 가상')
        expect(dong).to_have_value(dongs['계림1동'])
        dong.select_option(dongs['계림2동'])
        endpoint = url + 'api/dong-work/dong-detect?'
        with page.expect_response(lambda res: res.url.startswith(endpoint)) as detected:
            address.fill('광주광역시 동구 계림1동 가상 다른 주소')
        assert detected.value.json()['dong_id'] == dongs['계림1동']
        expect(dong).to_have_value(dongs['계림2동'])
        editor.get_by_role('button', name='주소로 동 다시 선택').click()
        expect(dong).to_have_value(dongs['계림1동'])
        with page.expect_response(lambda res: res.url.startswith(endpoint)) as outside:
            address.fill('광주광역시 동구 산수1동 가상')
        result = outside.value.json()
        assert result['outside_scope'] and not result['dong_id'] and not result['candidates']
        expect(dong).to_have_value('')
        expect(editor.get_by_text('담당 범위 밖의 주소입니다.', exact=False)).to_be_visible()
        assert dongs['산수1동'] not in dong.locator('option').evaluate_all('opts => opts.map(o => o.value)')
        print('실제 동 담당 계정·관할 주소 자동 판정·수동 동 유지·범위 밖 주소 배제', flush=True)
    finally:
        scoped.close()


def main(exe=None):
    if exe:
        from test_reports_labels_bundle import frozen_server
        server = frozen_server(exe.resolve())
    else:
        server = pc_server()
    with server as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path)
        ctx = browser.new_context(service_workers='block')
        ctx.route('**/*', lambda route: route.continue_() if route.request.url.startswith(url) else route.abort())
        page = ctx.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            api(ctx, url, 'setup', {'username': 'address_admin', 'password': 'synthetic-only-123', 'display_name': '가상 접수 담당'})
            api(ctx, url, 'settings', {'external': {'keys': {'juso_search_key': 'SYNTHETIC-ONLY-123456'}}})
            today = api(ctx, url, 'bootstrap')['today']
            page.goto(url + '#/request/new')
            address = page.get_by_label('배출 주소', exact=True)
            dong = page.get_by_label('행정동')
            extra = page.get_by_label('추가 현장시간(분)', exact=True)
            reset = page.get_by_role('button', name='주소로 동 다시 선택')
            address.fill('광주광역시 동구 계림1동 가상 주소')
            expect(dong).to_have_value('계림1동')
            expect(address).to_be_focused()
            address.fill('광주광역시 동구 계림1동 계림2동 가상')
            expect(dong).to_have_value('')
            expect(page.locator('.dongmsg')).to_contain_text('계림2동')
            print('주소 직접 입력과 분동 후보 표시', flush=True)
            check_official_address(page, url, address, dong)

            def search():
                page.get_by_role('combobox', name='주소 찾기').fill('가상 자료')
                page.get_by_role('option', name='가상 자료 주소 일치').click()

            row = {'kind': '도로명', 'label': '가상 자료 주소 일치', 'address': '광주 동구 가상자료길 42',
                   'dong': '산수1동', 'lat': 35.15, 'lon': 126.95}
            page.route('**/api/addr-search?*', lambda route: route.fulfill(json=[row]))
            search()
            expect(dong).to_have_value('산수1동')
            address.fill('광주광역시 동구 지산2동 가상')
            expect(dong).to_have_value('지산2동')
            dong.select_option('계림2동')
            search()
            expect(dong).to_have_value('계림2동')
            expect(extra).to_have_value('0')
            extra.fill('45')
            page.goto(url + '#/requests')
            expect(address).to_have_count(0)
            page.goto(url + '#/request/new')
            expect(extra).to_have_value('45')
            expect(dong).to_have_value('계림2동')
            with page.expect_response(lambda r: '/api/dong-detect?' in r.url):
                address.fill('광주광역시 동구 계림1동 가상')
            expect(dong).to_have_value('계림2동')
            reset.click()
            expect(dong).to_have_value('계림1동')
            address.fill('')
            expect(dong).to_have_value('')
            print('주소 찾기·수동 보정·초안 복원·자동 재개', flush=True)

            held = []
            def hold_first(route):
                if '지연' in parse_qs(urlsplit(route.request.url).query)['address'][0]:
                    held.append(route)
                else:
                    route.continue_()

            page.route('**/api/dong-detect?*', hold_first)
            with page.expect_request(lambda req: '/api/dong-detect?' in req.url):
                address.fill('광주광역시 동구 계림1동 가상 지연')
            address.fill('광주광역시 동구 산수2동 가상')
            expect(dong).to_have_value('산수2동')
            assert len(held) == 1
            with page.expect_response(lambda r: '/api/dong-detect?' in r.url):
                held.pop().fulfill(json={'dong': '계림1동', 'warnings': ['이전 주소 경고'], 'source': 'KEYWORD'})
            expect(dong).to_have_value('산수2동')
            expect(page.get_by_text('이전 주소 경고')).to_have_count(0)
            print('뒤늦은 이전 주소 판정 무시', flush=True)

            page.get_by_label('이름', exact=True).fill('가상 접수인')
            page.get_by_label('연락처', exact=True).fill('01000001675')
            page.get_by_label('수거 예정일', exact=True).fill(today)
            page.get_by_label('품목명·규격 검색', exact=True).fill('3인용 소파')
            page.locator('.wp-specs button').first.click()
            page.get_by_role('button', name='품목 추가', exact=True).click()
            with page.expect_request(lambda req: '/api/dong-detect?' in req.url):
                address.fill('광주광역시 동구 지산2동 가상 지연')
                page.get_by_role('button', name='접수 등록', exact=True).click()
            posts = []
            page.on('request', lambda req: posts.append(req.post_data_json) if req.method == 'POST' and req.url == url + 'api/requests' else None)
            assert len(held) == 1
            with page.expect_response(lambda r: r.request.method == 'POST' and r.url == url + 'api/requests') as saved:
                held.pop().fulfill(json={'dong': '지산2동', 'source': 'KEYWORD'})
            rid = saved.value.json()['id']
            expect(page).to_have_url(url + '#/request/' + rid)
            stored = api(ctx, url, 'requests/' + rid)
            assert stored['dong'] == '지산2동' and stored['extra_work_min'] == 45
            assert len(posts) == 1 and posts[0]['dong'] == '지산2동' and posts[0]['extra_work_min'] == 45
            assert 'dongManual' not in posts[0] and 'dongResult' not in posts[0]
            expect(extra).to_have_value('45')
            with page.expect_response(lambda r: '/api/dong-detect?' in r.url):
                address.fill('광주광역시 동구 계림1동 가상 수정')
            expect(dong).to_have_value('지산2동')
            reason = page.get_by_placeholder('수정 사유 (필수)')
            reason.fill('가상 현장시간 수정')
            extra.fill('1441')
            page.get_by_role('button', name='수정 저장').click()
            expect(page.get_by_text('추가 현장시간은 0~1440분 사이의 정수로 입력하세요.')).to_be_visible()
            assert api(ctx, url, 'requests/' + rid)['extra_work_min'] == 45
            extra.fill('0')
            with page.expect_response(lambda r: r.request.method == 'POST' and r.url == url + 'api/requests/' + rid):
                page.get_by_role('button', name='수정 저장').click()
            assert api(ctx, url, 'requests/' + rid)['extra_work_min'] == 0
            assert not errors, errors
            print('빠른 등록의 최신 판정·추가 현장시간 저장·범위·0분 수정·기존 동 유지', flush=True)
            check_scoped_address(browser, ctx, url)
        finally:
            ctx.close()
            browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path)
    main(parser.parse_args().exe)
