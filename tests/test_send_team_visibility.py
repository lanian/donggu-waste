"""목록 전송의 기본 표시·빈 갱신 수신 확인·날짜 및 변경팀 바로가기. 실제 PC 라우터에 합성 전송 자료를 제공한다."""
import os
import re
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

from test_review_ui import pc_server


DAY, NEXT = '2030-05-10', '2030-05-11'


def batch(key, name, *, stops=(), bundles=(), status='ACTIVE', **extra):
    return {'id': key, 'crew_id': 'crew-' + key, 'crew_name': name, 'device_id': 'phone-' + key,
            'assign_ver': 1, 'status': status, 'stops': list(stops), 'bundles': list(bundles), **extra}


def main():
    visit = {'seq': 1, 'name': '가상 방문', 'request_id': 'visit-1'}
    received = '2030-05-10T08:00:00Z'
    previous_qr = {'id': 'old-qr', 'sent_stops': True, 'receipt_confirmed_at': received}
    empty_qr = {'id': 'new-qr', 'sent_stops': False, 'receipt_confirmed_at': None}
    cancelled = batch('changed', '가상 취소팀', bundles=[previous_qr])
    rows = [batch('visit', '가상 방문팀', stops=[visit]),
            batch('unload', '가상 하차팀', final_unload={'eta': '13:20', 'label': '가상 반입지'}),
            cancelled, batch('idle', '가상 대기팀'),
            batch('pending', '가상 미확인팀', bundles=[{**empty_qr, 'id': 'pending-qr'}]),
            batch('legacy', '가상 구버전팀', bundles=[{**previous_qr, 'id': 'legacy-qr', 'sent_stops': None}]),
            batch('revision', '가상 갱신팀', assign_ver=2),
            batch('revision-old', '가상 이전갱신팀', status='SUPERSEDED', crew_id='crew-revision',
                  bundles=[previous_qr], stops=[visit]),
            batch('old', '가상 이력팀', stops=[visit], status='SUPERSEDED')]
    with pc_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.getenv('CHROME') or pw.chromium.executable_path)
        try:
            ctx = browser.new_context(service_workers='block')
            setup = ctx.request.post(url + 'api/setup', headers={'X-DG-CSRF': '1'},
                                     data={'username': 'dispatch_admin', 'display_name': '가상 관리자', 'password': 'send-teams-qa-1234'})
            assert setup.ok, setup.status
            page = ctx.new_page()
            calls = []

            def batches(route):
                params = parse_qs(urlsplit(route.request.url).query)
                calls.append(params)
                route.fulfill(json=rows if params.get('date') == [DAY] else [batch('next', '가상 다음날팀', stops=[visit])])

            def display_qr(route):
                assert route.request.post_data_json['batch_id'] == cancelled['id']
                cancelled['bundles'].insert(0, empty_qr)
                route.fulfill(json={'bundle_id': empty_qr['id'], 'frames': 1, 'count': 0})

            def confirm(route):
                assert route.request.post_data_json['code'] == 'R-ABCD EFGH'
                empty_qr['receipt_confirmed_at'] = received
                route.fulfill(json={'ok': True})

            page.route('**/api/batches?**', batches)
            page.route('**/api/bundles', display_qr)
            page.route('**/api/bundles/new-qr/receipt', confirm)
            page.add_init_script('window.open=()=>null')
            page.goto(url + '#/send?date=' + DAY)

            def heading(name):
                return page.get_by_role('heading', name=re.compile('^' + name + ' ·'))

            toggle = page.get_by_role('checkbox', name='배차 없는 팀·이전 목록 보기')
            for name in ('가상 방문팀', '가상 하차팀', '가상 취소팀', '가상 미확인팀', '가상 구버전팀', '가상 갱신팀'):
                expect(heading(name)).to_be_visible()
            expect(heading('가상 대기팀')).to_be_hidden()
            expect(heading('가상 이력팀')).to_be_hidden()
            expect(heading('가상 이전갱신팀')).to_be_hidden()
            expect(toggle).not_to_be_checked()
            toggle.check()
            expect(heading('가상 대기팀')).to_be_visible()
            expect(heading('가상 이력팀')).to_be_visible()
            toggle.uncheck()
            changed = page.locator('.card').filter(has=heading('가상 취소팀'))
            changed.get_by_role('button', name='QR 표시').click()
            expect(changed.get_by_label('수신 코드', exact=True)).to_be_visible()
            changed.get_by_label('수신 코드', exact=True).fill('R-ABCD EFGH')
            changed.get_by_role('button', name='수신 확인', exact=True).click()
            expect(heading('가상 취소팀')).to_be_hidden()
            expect(heading('가상 미확인팀')).to_be_visible()

            page.goto(url + '#/send/changed?date=' + DAY)
            expect(heading('가상 취소팀')).to_be_visible()
            expect(heading('가상 방문팀')).to_be_hidden()
            expect(changed.get_by_text('휴대폰 수신 확인됨')).to_be_visible()
            page.get_by_role('button', name='전체 팀 보기').click()
            expect(heading('가상 방문팀')).to_be_visible()
            expect(heading('가상 취소팀')).to_be_hidden()
            page.goto(url + '#/send/changed?date=' + DAY)
            expect(heading('가상 취소팀')).to_be_visible()
            page.get_by_label('수거일', exact=True).fill(NEXT)
            expect(heading('가상 다음날팀')).to_be_visible()
            expect(heading('가상 방문팀')).to_be_hidden()
            assert {p['date'][0] for p in calls} == {DAY, NEXT}
            assert all(p.get('for_send') == ['1'] for p in calls)
            print('목록 전송 기본 표시, 하차 전용·취소 QR·빈 갱신 수신 확인, 이전 목록·직접 링크·날짜 변경 통과')
        finally:
            browser.close()


if __name__ == '__main__':
    main()
