"""가상 전화 접수에서 본청·동별 목록, 상세, 수거·수납 저장 후 진행 표시를 확인한다."""
import argparse
from datetime import date, timedelta
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from test_dong_work_e2e import ROOT, api
from test_review_ui import pc_server


def progress_cell(page, name):
    return page.get_by_role('row').filter(has=page.get_by_text(name, exact=True)).locator('.request-progress-cell')


def check_list(page, address, name, action):
    reads = []

    def track(request):
        reads.append((request.method, urlsplit(request.url).path))

    page.on('request', track)
    try:
        page.goto(address)
        expect(page.get_by_role('row').filter(has=page.get_by_text(name, exact=True))).to_be_visible(timeout=12000)
        expect(progress_cell(page, name)).to_be_visible(timeout=8000)
        expect(progress_cell(page, name)).to_contain_text(action)
        assert not any(re.match(r'/api/(?:dong-work/)?(?:requests|followups)/[^/]+', path) for _, path in reads), reads
        if address.endswith('#/dongs'):
            assert not any(path.startswith('/api/requests') for _, path in reads), reads
        assert not any(method == 'POST' and '/requests' in path for method, path in reads), reads
    finally:
        page.remove_listener('request', track)


def save_collection_and_retry_summary(page, editor, detail_url, old_action):
    # 성공한 저장과 실패한 이후 조회를 분리해, 위 요약과 아래 재조회 안내가 일치하는지 확인한다.
    summary_slot = page.get_by_label('접수 진행 상황', exact=True).locator('..').element_handle()
    submissions = []

    def track(request):
        if request.method == 'POST' and request.url == detail_url + '/collection':
            submissions.append(request)

    page.on('request', track)
    page.route(detail_url, lambda route: route.fulfill(
        status=503, content_type='application/json', body='{"error":"가상 진행 조회 실패"}'), times=1)
    try:
        editor.get_by_role('button', name='수거 결과 저장', exact=True).click()
        expect(editor).to_have_count(0)
        retry = page.get_by_role('button', name='최신 내용 다시 불러오기', exact=True)
        expect(retry).to_be_visible()
        message = summary_slot.inner_text()
        assert '수거 결과는 저장' in message and '다시' in message and old_action not in message, message
        retry.click()
        expect(retry).to_have_count(0)
        assert len(submissions) == 1, '요약 재조회가 수거 결과를 다시 저장했습니다.'
    finally:
        page.remove_listener('request', track)
        summary_slot.dispose()


def main(exe=None):
    if exe:
        from test_reports_labels_bundle import frozen_server
        server = frozen_server(exe.resolve())
    else:
        server = pc_server()
    shots = Path(os.environ['DG_TEST_DIR']) / 'shots' if os.environ.get('DG_TEST_DIR') else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    with server as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        admin = browser.new_context(service_workers='block', viewport={'width': 1440, 'height': 960})
        scoped = browser.new_context(service_workers='block')
        page = admin.new_page()
        dong_page = scoped.new_page()
        errors = []
        for tab in (page, dong_page):
            tab.on('pageerror', lambda error: errors.append(str(error)))
        for context in (admin, scoped):
            context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(url) else route.abort())
        try:
            password = 'progress-test-only-123'
            api(admin, url, 'setup', {'username': 'progress_admin', 'display_name': '가상 진행 담당', 'password': password})
            # 진행 상태를 비교하도록 가상 두 방문을 직접 배정한다. 도로 연결은 별도 노선 시험에서 확인한다.
            api(admin, url, 'settings', {'external': {'travel_method': 'ESTIMATE'}})
            day = api(admin, url, 'bootstrap')['today']
            dong = api(admin, url, 'dongs', {'name': '진행가상동'})['id']
            crew_name = '진행가상수거팀'
            crew = api(admin, url, 'crews', {'name': crew_name, 'capacity_m3': 20})['id']
            api(admin, url, 'users', {'username': 'progress_dong', 'display_name': '가상 동담당', 'role': 'DONG',
                                      'password': password, 'dong_ids': [dong]})
            api(scoped, url, 'login', {'username': 'progress_dong', 'password': password})
            api(scoped, url, 'password', {'old': password, 'password': 'progress-dong-changed-123'})
            fees = json.loads((ROOT / 'data/fee_table.json').read_text(encoding='utf-8'))['rows']
            fee = next(f for f in fees if f['name'] == '의자' and f['unit_label'] == '개' and f['status'] == 'CONFIRMED')
            name = '가상 수거 진행'
            seed = {'name': name, 'phone': '01000001351', 'address': '가상구 진행로 1', 'channel': '전화',
                    'dong': '진행가상동', 'lat': 35.146, 'lon': 126.923, 'planned_date': day,
                    'items': [{'fee_rate_id': fee['id'], 'qty': 3}]}
            created = api(admin, url, 'requests', seed)
            rid = created['id']
            no_date = '가상 일정 미정'
            second = api(admin, url, 'requests', {**seed, 'name': no_date, 'phone': '01000001352', 'planned_date': ''})

            check_list(page, url + '#/requests', name, '배정')
            expect(progress_cell(page, name).get_by_text('접수', exact=True)).to_be_visible()
            expect(progress_cell(page, no_date)).to_contain_text(re.compile('예정일|일정'))
            check_list(dong_page, url + '#/dongs', name, '배정')
            expect(progress_cell(dong_page, name).get_by_text('접수', exact=True)).to_be_visible()
            print('가상 전화 접수의 본청·동별 목록에서 접수 단계와 다음 조치 표시, 건별 상세 추가 조회 없음')

            page.locator(f'a[href="#/request/{rid}"]').click()
            panel = page.get_by_label('접수 진행 상황', exact=True)
            expect(panel).to_contain_text('다음 할 일')
            expect(panel).to_contain_text('수납 상태')
            expect(panel).to_contain_text('미납')
            memo = page.get_by_label('요청 메모', exact=True)
            memo.fill('본청 수거 중 보존할 초안')
            page.get_by_role('button', name='수거 처리', exact=True).click()
            editor = page.get_by_role('region', name='PC 수거 처리', exact=True)
            editor.get_by_label('1번 이번 수거 개수', exact=True).fill('1')
            editor.get_by_label('일부 수거·미수거 사유', exact=True).fill('재방문 예정')
            save_collection_and_retry_summary(page, editor, url + 'api/requests/' + rid, '수거팀 배정')
            expect(panel).to_contain_text('일부 수거')
            expect(panel).to_contain_text('재방문')
            expect(panel).to_contain_text(re.compile(r'2\s*개'))
            expect(panel).to_contain_text('미납')
            expect(memo).to_have_value('본청 수거 중 보존할 초안')

            # 부과액 전액을 수납해도 실제 수거 단계와 부모 양식은 유지되어야 한다.
            page.get_by_role('button', name='수납 기록', exact=True).click()
            expect(panel).to_contain_text('납부 확인')
            expect(panel).to_contain_text('일부 수거')
            expect(memo).to_have_value('본청 수거 중 보존할 초안')
            print('본청 일부 수거와 수납 직후 진행 요약에서 각각 갱신, 미저장 메모 보존')

            api(admin, url, 'requests/' + second['id'] + '/contacts', {'kind': '독촉', 'content': '가상 일정 조정 요청'})
            api(admin, url, 'assign', {'date': day, 'plan': {crew: [{'request_id': rid}, {'request_id': second['id']}]}})
            # 별도 창에서 새 목록을 열어 미저장 중인 본청 양식을 건드리지 않는다.
            list_page = admin.new_page()
            check_list(list_page, url + '#/requests', name, '일부 수거')
            expect(progress_cell(list_page, name)).to_contain_text('배정')
            expect(progress_cell(list_page, name)).to_contain_text('직전 수거 결과')
            if shots:
                list_page.get_by_role('row').filter(has=list_page.get_by_text(name, exact=True)).screenshot(path=shots / 'request-list-progress.png')
            list_page.locator(f'a[href="#/request/{rid}"]').click()
            hq_assigned = list_page.get_by_label('접수 진행 상황', exact=True)
            expect(hq_assigned).to_contain_text(crew_name)
            expect(hq_assigned).to_contain_text('직전 수거 결과')
            expect(hq_assigned).to_contain_text('일부 수거')
            expect(hq_assigned).to_contain_text('납부 확인')
            if shots:
                hq_assigned.screenshot(path=shots / 'request-detail-progress.png')
            list_page.goto(url + '#/request/' + second['id'])
            expect(list_page.get_by_label('이름', exact=True)).to_have_value(no_date)
            lower_status = list_page.locator('.field-request-status')
            expect(lower_status).to_have_text('배정')
            expect(hq_assigned).to_contain_text(crew_name)
            list_page.get_by_label('요청 메모', exact=True).fill('재방문 일정 변경 중 보존할 초안')
            list_page.get_by_role('button', name='재방문 약속', exact=True).click()
            appointment = list_page.get_by_role('dialog', name='재방문 약속')
            next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
            appointment.get_by_label('재방문 날짜').fill(next_day)
            appointment.get_by_role('button', name='약속 확정', exact=True).click()
            expect(hq_assigned).to_contain_text('재방문 약속 확정')
            expect(hq_assigned).to_contain_text(next_day)
            expect(hq_assigned).to_contain_text('재방문 수거팀 배정')
            expect(hq_assigned.get_by_text('접수', exact=True)).to_be_visible()
            expect(hq_assigned).not_to_contain_text(crew_name)
            expect(lower_status).to_have_text('접수')
            expect(list_page.get_by_label('요청 메모', exact=True)).to_have_value('재방문 일정 변경 중 보존할 초안')
            assert api(admin, url, 'requests/' + second['id'])['status'] == 'RECEIVED'
            print('다른 날짜로 재방문 약속을 저장하면 상단·현장 결과에 배정 해제와 재방문 수거팀 배정 표시')
            list_page.close()

            dong_page.reload()
            current = progress_cell(dong_page, name)
            expect(current).to_contain_text('배정')
            expect(current).to_contain_text('직전 수거 결과')
            expect(current).to_contain_text('일부 수거')
            dong_page.get_by_role('button', name=created['receipt_no'], exact=True).click()
            detail = dong_page.get_by_label('접수 진행 상황', exact=True)
            expect(detail).to_contain_text(crew_name)
            expect(detail).to_contain_text('일부 수거')
            dong_memo = dong_page.get_by_label('요청 메모', exact=True)
            dong_memo.fill('동 담당자가 수거 중 작성한 초안')
            dong_page.get_by_role('button', name='수거 처리', exact=True).click()
            dong_editor = dong_page.get_by_role('region', name='PC 수거 처리', exact=True)
            dong_editor.get_by_role('button', name='남은 수량 전부', exact=True).click()
            save_collection_and_retry_summary(dong_page, dong_editor, url + 'api/dong-work/requests/' + rid,
                                              '재방문 수거 결과 확인')
            expect(detail).to_contain_text('수거 완료')
            expect(dong_memo).to_have_value('동 담당자가 수거 중 작성한 초안')
            actual = api(admin, url, 'requests/' + rid)
            assert actual['status'] == 'DONE' and actual['payment']['status'] == 'PAID'
            assert actual['state']['lines'][0]['qty_collected'] == 3
            assert not actual.get('memo') and not errors, errors
            print('재배정 후 두 화면에서 지난 일부 수거 확인, 동별 잔량 수거로 완료·초안 보존')
        finally:
            scoped.close()
            admin.close()
            browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path)
    main(parser.parse_args().exe)
