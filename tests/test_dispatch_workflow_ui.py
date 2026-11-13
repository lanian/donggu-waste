"""실제 임시 PC 서버/선택적 EXE: 추천안 조정·재계산·확정 흐름."""
import argparse
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import expect, sync_playwright
from test_review_ui import pc_server


def main(exe=None):
    if exe:
        from test_reports_labels_bundle import frozen_server
        server = frozen_server(exe.resolve())
    else:
        server = pc_server()
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-dispatch-workflow-'))
    (output / 'shots').mkdir(parents=True, exist_ok=True)
    with server as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000}, service_workers='block')
        context.route('https://tiles.openfreemap.org/**', lambda route: route.fulfill(json={'version': 8, 'sources': {}, 'layers': [{'id': 'bg', 'type': 'background', 'paint': {'background-color': '#f4f6fa'}}]}))
        page = context.new_page()
        page.set_default_timeout(15000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        def api(path, body=None):
            options = {'headers': {'X-DG-CSRF': '1'}}
            response = context.request.get(url + 'api/' + path, **options) if body is None else context.request.post(url + 'api/' + path, data=body, **options)
            assert response.ok, (path, response.status, response.text())
            return response.json()
        try:
            api('setup', {'username': 'dispatch_admin', 'display_name': '가상 배정 담당', 'password': 'dispatch-test-only-123'})
            day = api('bootstrap')['today']
            for name in ('가상 A팀', '가상 B팀'):
                api('crews', {'name': name, 'capacity_m3': 8, 'shift_start': '09:00', 'shift_end': '17:00'})
            fee = next(row['id'] for row in json.loads((Path(__file__).resolve().parent.parent / 'data/fee_table.json').read_text(encoding='utf-8'))['rows'] if row['status'] == 'CONFIRMED' and row.get('price', 0) > 0)
            requests = [api('requests', {'name': '가상 배정 주민 ' + str(i), 'phone': '01000000000', 'address': '가상구 시험길 ' + str(i),
                'planned_date': day, 'lat': 35.15 + i * .002, 'lon': 126.92, 'items': [{'fee_rate_id': fee, 'qty': 1}]}) for i in range(3)]
            postponed = api('requests', {'name': '가상 미배정 주민', 'phone': '01000000000', 'address': '가상 위치 확인 대기',
                'planned_date': day, 'items': [{'fee_rate_id': fee, 'qty': 1}]})
            page.goto(url + '#/dispatch')
            expect(page.get_by_role('navigation', name='배정 진행 단계', exact=True)).to_be_visible()
            page.get_by_role('button', name='노선 계산', exact=True).click()
            review = page.get_by_role('region', name='노선 조정안', exact=True)
            expect(review).to_be_visible(timeout=30000)
            expect(review.get_by_text('직선 합계', exact=False).first).to_be_visible(timeout=30000)
            assert 'null' not in review.inner_text(), '조건부 안내가 null 문자열로 표시되지 않는다'
            find = page.get_by_label('노선 접수 찾기', exact=True)
            find.fill(requests[0]['receipt_no'])
            expect(review.locator('.stopline[data-request-id]:visible')).to_have_count(1)
            find.fill('')
            card = review.locator('.route-card').filter(has=page.get_by_role('button', name='2번 위로', exact=True)).first
            first = card.locator('.stopline[data-request-id]').first
            original_id = first.get_attribute('data-request-id')
            reason = page.get_by_label('노선 조정 사유', exact=True)
            first.get_by_role('button', name='1번 아래로', exact=True).click()
            expect(page.get_by_role('button', name='배정 확정', exact=True)).to_be_disabled()
            expect(page.get_by_text('순서·팀이 바뀌어 시간 재계산이 필요합니다.', exact=False)).to_be_visible()
            reason.fill('가상 골목 진입 순서 조정')
            page.get_by_role('button', name='조정 되돌리기', exact=True).click()
            expect(card.locator('.stopline[data-request-id]').first).to_have_attribute('data-request-id', original_id)
            first.get_by_role('button', name='1번 아래로', exact=True).click()
            reason.fill('가상 골목 진입 순서 조정')
            move_select = review.locator('.stopline[data-request-id]').first.get_by_role('combobox', name='다른 팀으로 이동', exact=True)
            target = move_select.locator('option:not([disabled])').evaluate_all("(rows)=>rows.find(r=>r.value!=='' && r.value!=='OUT').value")
            move_select.select_option(target)
            order = review.locator('.stopline[data-request-id]').evaluate_all('(rows)=>rows.map(r=>r.dataset.requestId)')
            page.route('**/api/assign/current?**', lambda route: route.fulfill(status=503, json={'error': '가상 배정 조회 실패'}), times=1)
            page.get_by_role('button', name='현재 배정 불러오기', exact=True).click()
            page.get_by_role('dialog', name='확정 배정 불러오기', exact=True).get_by_role('button', name='확정 배정 불러오기', exact=True).click()
            expect(page.get_by_text('가상 배정 조회 실패', exact=True)).to_be_visible()
            assert review.locator('.stopline[data-request-id]').evaluate_all('(rows)=>rows.map(r=>r.dataset.requestId)') == order
            expect(reason).to_have_value('가상 골목 진입 순서 조정')
            page.route('**/api/route', lambda route: route.fulfill(status=503, json={'error': '가상 재계산 실패'}), times=1)
            page.get_by_role('button', name='순서 유지 재계산', exact=True).click()
            expect(page.get_by_text('가상 재계산 실패', exact=True)).to_be_visible()
            assert review.locator('.stopline[data-request-id]').evaluate_all('(rows)=>rows.map(r=>r.dataset.requestId)') == order
            expect(reason).to_have_value('가상 골목 진입 순서 조정')
            page.get_by_role('button', name='순서 유지 재계산', exact=True).click()
            expect(page.get_by_role('button', name='배정 확정', exact=True)).to_be_enabled(timeout=30000)
            assert review.locator('.stopline[data-request-id]').evaluate_all('(rows)=>rows.map(r=>r.dataset.requestId)') == order
            expect(reason).to_have_value('가상 골목 진입 순서 조정')
            page.screenshot(path=str(output / 'shots' / 'dispatch-reviewed.png'), full_page=True)
            page.set_viewport_size({'width': 980, 'height': 820})
            assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'), '980px에서 가로 넘침 없이 조정안 표시'
            page.screenshot(path=str(output / 'shots' / 'dispatch-reviewed-980.png'), full_page=True)
            page.set_viewport_size({'width': 1440, 'height': 1000})
            page.get_by_role('button', name='다른 날로 미루기', exact=True).click()
            postpone_dialog = page.get_by_role('dialog', name='다른 날로 미루기', exact=True)
            expect(postpone_dialog.get_by_label('새 수거 예정일', exact=True)).not_to_have_value('')
            selected_date = postpone_dialog.get_by_label('새 수거 예정일', exact=True).input_value()
            assert selected_date != day, (selected_date, day)
            with page.expect_response(lambda response: '/api/requests/bulk' in response.url) as moved:
                postpone_dialog.get_by_role('button', name='미루기', exact=True).click()
            assert moved.value.ok, moved.value.text()
            assert moved.value.request.post_data_json['ids'] == [postponed['id']], moved.value.request.post_data_json
            expect(postpone_dialog).to_have_count(0)
            expect(page.get_by_role('button', name='배정 확정', exact=True)).to_be_disabled()
            page.get_by_role('button', name='순서 유지 재계산', exact=True).click()
            expect(page.get_by_role('button', name='배정 확정', exact=True)).to_be_enabled(timeout=30000)
            assert api('requests/' + postponed['id'])['planned_date'] != day
            held = []
            def hold_batches(route):
                held.append((route, route.fetch()))
                page.evaluate('window.__heldBatches=true')
            page.route('**/api/batches?**', hold_batches, times=1)
            confirming = page.get_by_role('button', name='배정 확정', exact=True).element_handle()
            page.get_by_role('button', name='배정 확정', exact=True).click()
            page.wait_for_function('() => window.__heldBatches===true')
            assert confirming.get_attribute('data-busy') == '1'
            review.locator('button[aria-label="1번 아래로"]:not(:disabled)').first.click()
            route, response = held[0]
            route.fulfill(response=response)
            page.wait_for_function('(button) => !button.dataset.busy', arg=confirming)
            assert not [batch for batch in api('batches?date=' + day) if batch['status'] == 'ACTIVE'], '확정 대기 중 바뀐 순서는 검토 없이 저장하지 않는다'
            page.get_by_role('button', name='순서 유지 재계산', exact=True).click()
            expect(page.get_by_role('button', name='배정 확정', exact=True)).to_be_enabled(timeout=30000)
            page.get_by_role('button', name='배정 확정', exact=True).click()
            expect(page).to_have_url(url + '#/send')
            assert sum(len(batch['stops']) for batch in api('batches?date=' + day) if batch['status'] == 'ACTIVE') == 3
            # Only this paginated candidate response is synthetic; route/review/assignment above use the actual server.
            seen_pages = []
            def candidates(route):
                query = parse_qs(urlparse(route.request.url).query)
                if query.get('quick') != ['UNASSIGNED']:
                    route.continue_(); return
                number = int(query.get('page', ['1'])[0]); seen_pages.append(number)
                route.fulfill(json={'page': number, 'pages': 3, 'total': 3, 'rows': [{'id': 'candidate-' + str(number), 'receipt_no': 'FAKE-' + str(number), 'name': '가상 후보 ' + str(number), 'address': '가상 주소', 'lat': 35.15}]})
            page.route('**/api/requests?**', candidates)
            page.goto(url + '#/dispatch')
            page.get_by_role('button', name='현재 배정 불러오기', exact=True).click()
            expect(page.get_by_role('combobox', name='가상 A팀에 넣을 접수', exact=True).locator('option')).to_have_count(4)
            expect(page.get_by_role('navigation', name='배정 진행 단계', exact=True).locator('[aria-current=step]')).to_contain_text('노선 조정')
            assert seen_pages == [1, 2, 3], seen_pages
            page.get_by_label('가상 A팀 미배정 접수 찾기', exact=True).fill('FAKE-3')
            expect(page.get_by_role('combobox', name='가상 A팀에 넣을 접수', exact=True).locator('option')).to_have_count(2)
            page.screenshot(path=str(output / 'shots' / 'dispatch-current.png'), full_page=True)
            assert not errors, errors
            print('배정 UI: 진행 단계·순서/되돌리기·시간 무효화·조정 사유·재계산 실패 보존/재시도·순서 유지·실HTTP 배정·후보 전 페이지 통과')
        finally:
            context.close(); browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe', type=Path)
    main(parser.parse_args().exe)
