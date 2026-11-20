"""실제 임시 PC 서버/선택적 EXE: 추천안 조정·재계산·확정 흐름."""
import argparse
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import expect, sync_playwright
from test_review_ui import pc_server


def check_map(page, card, first, other, team, *, total, team_total, teams, excluded=None):
    picker = card.get_by_label('지도 수거팀', exact=True)
    details = card.get_by_role('region', name='선택한 배차 지점', exact=True)
    pins = card.locator('.pin[aria-pressed]')

    def expect_lines(count):
        page.wait_for_function("""async (count) => {
            const { liveMaps } = await import('/core.js');
            const features = liveMaps.at(-1)?.getSource('routes')?.serialize().data?.features;
            return features?.length === count;
        }""", arg=count)

    expect(picker).to_be_visible()
    expect(pins).to_have_count(total)
    expect_lines(teams)
    pin = pins.and_(card.get_by_role('button', name=first['name'], exact=False))
    pin.press('Enter')
    expect(pin).to_have_attribute('aria-pressed', 'true')
    expect(details).to_contain_text(first['address'])
    expect(details).to_contain_text(first['receipt_no'])
    expect(details).to_contain_text('1번 방문')
    expect(details).to_contain_text(team)
    picker.select_option(label=team)
    expect(pin).to_have_attribute('aria-pressed', 'true')
    expect(pins).to_have_count(team_total)
    expect_lines(1)
    if excluded:
        expect(pins.and_(card.locator('.pin.un'))).to_have_count(0)
        picker.select_option(label='미배정')
        expect(pins).to_have_count(1)
        expect_lines(0)
        pins.first.click()
        expect(details).to_contain_text(excluded['address'])
        expect(details).to_contain_text('미배정')
        picker.select_option(label=team)
        expect(details).not_to_contain_text(excluded['address'])
    page.get_by_role('button', name=f"{other['name']} 지도에서 보기", exact=True).click()
    expect(details).to_contain_text(other['address'])
    other_pin = pins.and_(card.get_by_role('button', name=other['name'], exact=False))
    expect(other_pin).to_be_focused()
    expect(other_pin).to_have_attribute('aria-pressed', 'true')
    picker.select_option(label=team)
    expect(details).not_to_contain_text(other['address'])
    picker.select_option(label='전체 팀')
    expect(pins).to_have_count(total)
    expect_lines(teams)


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
            crews = {name: api('crews', {'name': name, 'capacity_m3': 8, 'shift_start': '09:00', 'shift_end': '17:00'})['id']
                     for name in ('가상 A팀', '가상 B팀')}
            fee = next(row['id'] for row in json.loads((Path(__file__).resolve().parent.parent / 'data/fee_table.json').read_text(encoding='utf-8'))['rows'] if row['status'] == 'CONFIRMED' and row.get('price', 0) > 0)
            requests = [api('requests', {'name': '가상 배정 주민 ' + str(i), 'phone': '01000000000', 'address': '가상구 시험길 ' + str(i),
                'planned_date': day, 'lat': 35.15 + i * .002, 'lon': 126.92, 'items': [{'fee_rate_id': fee, 'qty': 1}]}) for i in range(3)]
            postponed = api('requests', {'name': '가상 미배정 주민', 'phone': '01000000000', 'address': '가상 위치 확인 대기',
                'planned_date': day, 'items': [{'fee_rate_id': fee, 'qty': 1}]})
            page.goto(url + '#/dispatch')
            expect(page.get_by_role('navigation', name='배정 진행 단계', exact=True)).to_be_visible()
            page.get_by_role('button', name='선택 해제', exact=True).click()
            for name in crews:
                page.locator('.crewpick label').filter(has_text=name).get_by_role('checkbox').check()
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
            cards = review.locator('.route-card')
            # 각 팀에 좌표가 있는 한 곳을 남기고 세 번째 지점은 미배정으로 확인한다.
            third = cards.filter(has=page.get_by_role('button', name='2번 위로', exact=True)).locator('.stopline[data-request-id]').nth(1)
            excluded = api('requests/' + third.get_attribute('data-request-id'))
            third.get_by_role('combobox', name='다른 팀으로 이동', exact=True).select_option('OUT')
            ids = cards.locator('.stopline[data-request-id]').evaluate_all('(rows) => rows.map(row => row.dataset.requestId)')
            shown = [api('requests/' + rid) for rid in ids]
            team = next(name for name, cid in crews.items() if cid == cards.filter(has=page.locator(f'[data-request-id="{ids[0]}"]')).get_attribute('data-route-crew'))
            batches_before = api('batches?date=' + day)
            check_map(page, review.locator('.mapcard'), *shown, team, total=3, team_total=1, teams=2, excluded=excluded)
            assert cards.locator('.stopline[data-request-id]').evaluate_all('(rows) => rows.map(row => row.dataset.requestId)') == ids
            expect(page.locator('.crewpick input:checked')).to_have_count(2)
            assert api('batches?date=' + day) == batches_before
            page.get_by_role('button', name=f"{shown[0]['name']} 지도에서 보기", exact=True).click()
            review.locator('.mapcard').screenshot(path=str(output / 'shots' / 'dispatch-route-map.png'))
            page.set_viewport_size({'width': 980, 'height': 820})
            assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'), '980px에서 팀 필터와 선택 지점 표시'
            page.set_viewport_size({'width': 1440, 'height': 1000})
            page.get_by_role('button', name='조정 되돌리기', exact=True).click()
            expect(page.get_by_role('button', name='배정 확정', exact=True)).to_be_enabled()
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
            order_before_graph_change = review.locator('.stopline[data-request-id]').evaluate_all('(rows)=>rows.map(r=>r.dataset.requestId)')
            page.route('**/api/assign', lambda route: route.fulfill(status=409, json={
                'code': 'ROAD_RULES_CHANGED', 'error': '도로 통행 정보가 바뀌었습니다.',
            }), times=1)
            with page.expect_response(lambda response: response.url.endswith('/api/assign') and response.status == 409):
                page.get_by_role('button', name='배정 확정', exact=True).click()
            expect(review.get_by_role('status').filter(has_text='도로 통행 정보가 바뀌었습니다')).to_be_visible(timeout=4000)
            expect(page.get_by_role('button', name='배정 확정', exact=True)).to_be_disabled()
            expect(reason).to_have_value('가상 골목 진입 순서 조정')
            assert review.locator('.stopline[data-request-id]').evaluate_all('(rows)=>rows.map(r=>r.dataset.requestId)') == order_before_graph_change
            page.get_by_role('button', name='순서 유지 재계산', exact=True).click()
            expect(page.get_by_role('button', name='배정 확정', exact=True)).to_be_enabled(timeout=30000)
            assert review.locator('.stopline[data-request-id]').evaluate_all('(rows)=>rows.map(r=>r.dataset.requestId)') == order_before_graph_change
            page.get_by_role('button', name='배정 확정', exact=True).click()
            expect(page).to_have_url(url + '#/send')
            batches = [b for b in api('batches?date=' + day) if b['status'] == 'ACTIVE' and b['stops']]
            assert len(batches) >= 2 and sum(len(batch['stops']) for batch in batches) == 3
            page.goto(url + '#/home')
            home = page.locator('.dash-map-card')
            first, other = batches[0]['stops'][0], batches[1]['stops'][0]
            check_map(page, home, first, other, batches[0]['crew_name'], total=3, team_total=len(batches[0]['stops']), teams=len(batches))
            page.get_by_role('button', name=f"{first['name']} 지도에서 보기", exact=True).click()
            expect(home.get_by_role('region', name='선택한 배차 지점', exact=True)).to_contain_text('도착 ' + first['eta'])
            expect(home.get_by_role('region', name='선택한 배차 지점', exact=True)).to_contain_text('배정')
            home.screenshot(path=str(output / 'shots' / 'home-route-map.png'))
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'), '390px에서 선택한 배차 지점 표시'
            page.set_viewport_size({'width': 1440, 'height': 1000})
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
            changing = next(b for b in batches if len(b['stops']) > 1)
            changed_crew = changing['crew_name']
            page.get_by_role('button', name='확정 배차 지도 보기').click()
            confirmed = page.get_by_role('region', name='확정 배차 지도', exact=True)
            confirmed.get_by_label('지도 수거팀').select_option(changing['crew_id'])
            pins = confirmed.locator('.pin[aria-pressed]')
            expect(pins.first).to_have_attribute('aria-label', f"{changed_crew} · 1번 방문 · {changing['stops'][0]['name']}")
            page.get_by_label('재계산 시작 시각 (선택)').fill('09:00')
            page.get_by_role('button', name=f'{changed_crew} 1번 아래로', exact=True).click()
            edit_dialog = page.get_by_role('dialog', name='배정 변경 검토', exact=True)
            edit_dialog.get_by_role('button', name='변경 배정 확정', exact=True).click()
            expect(edit_dialog).to_have_count(0)
            expect(confirmed.get_by_label('지도 수거팀')).to_have_value(changing['crew_id'])
            expect(pins.first).to_have_attribute('aria-label', f"{changed_crew} · 1번 방문 · {changing['stops'][1]['name']}")
            assert not errors, errors
            print('배정 UI: 두 지도 팀 필터·핀/목록 선택·표시 노선·미배정, 순서 변경·되돌리기·실HTTP 배정·후보 전 페이지 통과')
        finally:
            context.close(); browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe', type=Path)
    main(parser.parse_args().exe)
