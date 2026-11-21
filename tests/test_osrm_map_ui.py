"""오늘 현황·배정 지도의 도로 방향, 카메라, 응답 역전, 수기 순서 회귀. 가상 자료만 사용한다."""
import argparse
from datetime import date, timedelta
from io import BytesIO
import os
from statistics import mean, median
from urllib.parse import urlsplit

from PIL import Image
from playwright.sync_api import expect, sync_playwright

from test_osrm_map_api import DEPOT, END, UNLOAD, RouteMapHTTPTest, point
from test_osrm_runtime import loopback
from dgpc import area, service as S


HOLD_PATHS = """() => {
  const send = window.fetch.bind(window);
  window.pathCalls = [];
  window.fetch = (target, options) => {
    if (target !== '/api/route-path') return send(target, options);
    return new Promise((resolve) => {
      const body = JSON.parse(options.body);
      // 취소 신호가 와도 응답하는 전송을 재현한다.
      window.pathCalls.push({ body, signal: options.signal, finish: (km, code = 200, line = null, reason = null, tollExcluded = false) => {
        const [first, ...rest] = body.points;
        const json = reason ? { source: 'ESTIMATE', reason } : code === 200 ? { source: 'OSRM', distance_km: km,
          toll_excluded: tollExcluded,
          geometry: { type: 'LineString', coordinates: line || [first, [first[0] + 0.0002, first[1] + 0.0001], ...rest] } }
          : { error: '가상 도로 조회 실패' };
        resolve(new Response(JSON.stringify(json), { status: code }));
      } });
    });
  };
}"""


def with_map(code):
    return '(value) => { const map = window.testMap(); ' + code + ' }'


def arrow_direction(canvas, row):
    """수평 합성 도로의 렌더링 픽셀에서 화살촉이 가리키는 쪽을 읽는다."""
    raster = Image.open(BytesIO(canvas.screenshot(scale='css'))).convert('RGB')
    columns = {}
    for y in range(max(0, row - 12), min(raster.height, row + 12)):
        for x in range(24, raster.width - 24):
            r, g, b = raster.getpixel((x, y))
            if abs(r - 16) <= 5 and abs(g - 38) <= 5 and abs(b - 61) <= 5:  # 화살표 #10263d
                columns.setdefault(x, []).append(y)
    groups = []
    for x in sorted(columns):
        if not groups or x - groups[-1][-1] > 1:
            groups.append([])
        groups[-1].append(x)
    directions = []
    for group in groups:
        ink = [(x, y) for x in group for y in columns[x]]
        low, high = min(y for _, y in ink), max(y for _, y in ink)
        if not (5 <= len(group) <= 20 and 5 <= high - low <= 20):
            continue
        middle = (low + high) / 2
        tips = [x for x, y in ink if abs(y - middle) <= 1.5]
        tails = [x for x, y in ink if abs(y - middle) >= (high - low) / 3]
        if tips and tails:
            directions.append(mean(tips) - mean(tails))
    assert directions, '화면의 도로선에서 방향 화살표 픽셀을 찾지 못했습니다.'
    return median(directions)


def check_large_map(page, panel, selected_team):
    expand = panel.get_by_role('button', name='지도 크게 보기', exact=True)
    expect(expand).to_be_enabled(timeout=3000)
    canvas = panel.locator('canvas.maplibregl-canvas')
    expect(canvas).to_be_visible()
    label = panel.locator('.pin[aria-pressed=false]').first.get_attribute('aria-label')
    pin = panel.get_by_role('button', name=label, exact=True)
    pin.press('Enter')
    expect(pin).to_have_attribute('aria-pressed', 'true')
    inline_size = canvas.bounding_box()
    map_read_count = page.evaluate('''async () => {
        const { liveMaps } = await import('/core.js');
        window.expandedTestMap = liveMaps.at(-1);
        return pathCalls.length;
    }''')
    dialog = page.get_by_role('dialog', name='확대 노선 지도', exact=True)
    restore = dialog.get_by_role('button', name='원래 크기로 보기', exact=True)
    expand.click()
    expect(dialog).to_be_visible()
    expect(restore).to_be_focused()
    expect(dialog.get_by_label('지도 수거팀')).to_have_value(selected_team)
    expect(dialog.get_by_role('button', name=label, exact=True)).to_have_attribute('aria-pressed', 'true')
    expect(dialog.get_by_label('지도 경로·거리')).to_contain_text('도로 2.75km')
    page.wait_for_function('''height => {
        const canvas = document.querySelector('dialog[open] canvas.maplibregl-canvas');
        return canvas === window.expandedTestMap.getCanvas() && canvas.clientHeight > height &&
            window.expandedTestMap.getLayer('routes-direction') &&
            window.expandedTestMap.getSource('routes').serialize().data.features.some(feature => feature.properties.road);
    }''', arg=inline_size['height'])
    large_size = dialog.locator('canvas.maplibregl-canvas').bounding_box()
    assert large_size['width'] >= page.viewport_size['width'] * 0.8, large_size
    assert large_size['height'] >= page.viewport_size['height'] * 0.6, large_size
    map_url = page.url
    restore.press('n')
    assert page.url == map_url
    expect(dialog).to_be_visible()
    page.keyboard.press('Escape')
    expect(dialog).to_be_hidden()
    expect(expand).to_be_focused()
    expect(panel.get_by_role('button', name=label, exact=True)).to_have_attribute('aria-pressed', 'true')
    page.wait_for_function('''height => {
        const canvas = document.querySelector('canvas.maplibregl-canvas');
        return canvas === window.expandedTestMap.getCanvas() && Math.abs(canvas.clientHeight - height) <= 1;
    }''', arg=inline_size['height'])
    expand.click()
    restore.click()
    expect(dialog).to_be_hidden()
    expect(expand).to_be_focused()
    assert page.evaluate('() => pathCalls.length') == map_read_count


def check_confirmed_map(page, origin, day, teams, confirmed):
    requests = []
    page.on('request', lambda request: requests.append((request.method, urlsplit(request.url))))
    page.goto(origin + '#/dispatch')
    choose_date = page.get_by_label('수거일', exact=True)
    expect(choose_date).to_have_value(day)

    open_map = page.get_by_role('button', name='확정 배차 지도 보기', exact=True)
    expect(open_map).to_be_visible(timeout=3000)
    open_map.click()
    panel = page.get_by_role('region', name='확정 배차 지도', exact=True)
    expect(panel).to_be_visible()
    picker = panel.get_by_label('지도 수거팀')
    for name in ('가상 지도 A팀', '가상 지도 B팀'):
        expect(picker.get_by_role('option', name=name, exact=True)).to_have_count(1)
    expect(picker.get_by_role('option')).to_have_count(3)
    page.wait_for_function('() => pathCalls.length >= 2')
    initial = page.evaluate('() => pathCalls.map(({body}) => body)')
    assert {route['batch_id'] for route in initial} == {batch['batch_id'] for batch in confirmed}
    picker.select_option(teams[1])
    expect(picker).to_have_value(teams[1])
    page.wait_for_function('() => pathCalls.length >= 3')
    page.evaluate('() => pathCalls.at(-1).finish(2.75)')
    expect(panel.get_by_label('지도 경로·거리')).to_contain_text('도로 2.75km')
    expect(panel.get_by_label('지도 경로·거리')).not_to_contain_text('가상 지도 A팀')
    check_large_map(page, panel, teams[1])
    assert any(method == 'GET' and url.path == '/api/batches' and f'date={day}' in url.query and 'route_map=1' in url.query
               for method, url in requests), requests
    assert not any(method == 'POST' and url.path in ('/api/route', '/api/assign') for method, url in requests), requests
    expect(page.get_by_role('button', name='배정 확정', exact=True)).to_have_count(0)

    close_map = page.get_by_role('button', name='확정 배차 지도 닫기', exact=True)
    close_map.click()
    expect(panel).to_be_hidden()
    open_map.click()
    expect(panel.get_by_label('지도 수거팀')).to_be_visible()
    choose_date.fill((date.fromisoformat(day) + timedelta(days=1)).isoformat())
    expect(panel).to_contain_text('선택한 날짜에 확정된 배정이 없습니다.')
    expect(panel.get_by_role('option', name='가상 지도 B팀', exact=True)).to_have_count(0)


def check_final_unload_map(page, origin, crew, confirmed):
    def preview(route):
        response = route.fetch()
        result = response.json()
        result.update(final_unload_return=True, end_site=None, unload_site=UNLOAD)
        team = next(rt for rt in result['routes'] if rt['crew_id'] == crew)
        assert len(team['stops']) == 2, team
        for i, stop in enumerate(team['stops']):
            stop['unload_after'] = {'label': UNLOAD['label'], 'eta': f'10:{i}0',
                                    'volume_m3': 1, 'minutes': 20, **({'final': True} if i else {})}
        team['unloads'] = 2
        result['route_blockers'] = [{'crew_id': crew, 'crew_name': '가상 지도 A팀',
                                     'message': '방문·경유·복귀 순서에 도로로 연결되지 않는 구간이 있습니다.'}]
        route.fulfill(response=response, json=result)

    page.route('**/api/route', preview)
    team = next(row for row in confirmed if row['crew_id'] == crew and row['status'] == 'ACTIVE')
    team['map_route']['end'] = team['map_route']['depot']
    team['stops'][-1].update(via=UNLOAD, final_unload=True)
    page.route('**/api/batches?*route_map=1*', lambda route: route.fulfill(json=confirmed))
    page.goto(origin + '#/dispatch')
    page.get_by_role('button', name='선택 해제', exact=True).click()
    page.locator('.crewpick label').filter(has_text='가상 지도 A팀').get_by_role('checkbox').check()
    page.get_by_role('button', name='노선 계산', exact=True).click()
    review = page.get_by_role('region', name='노선 조정안')
    drawer = review.locator(f'.route-card[data-route-crew="{crew}"]')
    expect(review.locator('.matrix-note')).to_contain_text('최종 하차 후 차고지 복귀', timeout=30000)
    expect(review.get_by_role('alert').filter(has_text='배정 확정이 보류되었습니다')).to_be_visible()
    expect(page.get_by_role('button', name='배정 확정', exact=True)).to_be_disabled()
    expect(drawer.locator('.stopline.unload')).to_have_count(2)
    expect(drawer.locator('.stopline.unload').first).to_contain_text('중간 하차')
    expect(drawer.locator('.stopline.unload').last).to_contain_text('최종 하차')
    expect(drawer.get_by_text('반입지 경유 1회', exact=True)).to_be_visible()
    page.wait_for_function('() => pathCalls.length >= 1')
    coords = page.evaluate('() => pathCalls.at(-1).body.points')
    assert coords[0] == coords[-1] == point(DEPOT)
    assert coords[2] == coords[-2] == point(UNLOAD)
    assert len(coords) == 6
    page.evaluate('() => pathCalls.at(-1).finish(2.75, 200, null, null, true)')
    distance = review.get_by_label('지도 경로·거리')
    expect(distance).to_contain_text('도로 2.75km')
    expect(distance).to_contain_text('유료도로 제외')
    expect(distance).to_contain_text('최종 하차 후 차고지 복귀')
    expect(review.get_by_role('button', name='최종 하차지', exact=False)).to_be_visible()

    old_calls = page.evaluate('() => pathCalls.length')
    drawer.get_by_role('button', name='1번 아래로', exact=True).click()
    page.wait_for_function('n => pathCalls.length > n', arg=old_calls)
    adjusted = page.evaluate('() => pathCalls.at(-1).body.points')
    assert adjusted == [coords[0], coords[3], coords[1], coords[-2], coords[-1]], adjusted
    expect(drawer.get_by_text('반입지 도착·차고지 복귀 시각을 확인하세요.', exact=False)).to_be_visible()

    old_calls = page.evaluate('() => pathCalls.length')
    page.get_by_role('button', name='확정 배차 지도 보기', exact=True).click()
    panel = page.get_by_role('region', name='확정 배차 지도', exact=True)
    panel.get_by_label('지도 수거팀').select_option(crew)
    page.wait_for_function('n => pathCalls.length > n && pathCalls.at(-1).body.points.length === 6', arg=old_calls)
    request = page.evaluate('() => pathCalls.at(-1).body')
    assert request['batch_id'] and request['points'][0] == request['points'][-1] == point(DEPOT)
    assert request['points'][-2] == point(UNLOAD)
    page.evaluate("() => pathCalls.at(-1).finish(0, 200, null, 'TOLL_EXCLUSION_UNAVAILABLE')")
    confirmed_distance = panel.get_by_label('지도 경로·거리')
    expect(confirmed_distance).to_contain_text('유료도로 제외를 적용할 수 없음')
    expect(confirmed_distance).to_contain_text('직선(점선) · 방향 없음 · 운행 참고 불가')
    expect(confirmed_distance).to_contain_text('최종 하차 후 차고지 복귀')
    team['map_route']['history'] = {'state': 'ROAD_CHANGED', 'road_revision': 'trial-original',
        'path': {'source': 'OSRM', 'distance_km': 4.23, 'toll_excluded': True,
                 'geometry': {'type': 'LineString', 'coordinates': request['points']}},
        'estimated_legs': ['출발 → 1번']}
    page.get_by_role('button', name='확정 배차 지도 닫기', exact=True).click()
    page.get_by_role('button', name='확정 배차 지도 보기', exact=True).click()
    panel.get_by_label('지도 수거팀').select_option(crew)
    expect(confirmed_distance).to_contain_text('확정 당시 노선')
    expect(confirmed_distance).to_contain_text('도로 4.23km')
    expect(confirmed_distance).to_contain_text('이동시간을 직선으로 추정한 구간 출발 → 1번')
    expect(confirmed_distance).not_to_contain_text('화살표는 도로 경로의 진행 방향')
    print('최종 하차 지도 통과: 중간·최종 하차 구분 · 차고지 복귀 · 유료도로 제외와 직선 대체 표시')


def main(confirmed_only=False, final_unload_only=False):
    fixture = RouteMapHTTPTest()
    fixture.setUp()
    try:
        day, a, b, locations, receipts, confirmed = fixture.seed()
        # 자동 추천에서도 두 팀에 나누어져 고정 순서 검토를 확인할 수 있게 한다.
        area.save_dong(fixture.con, {'name': '가상 지도동 B', 'crew_id': b, 'depot': {**DEPOT, 'label': '가상 B차고지', 'lat': 35.154}})
        S.set_requests_dong(fixture.con, [receipts[2]['id']], '가상 지도동 B')
        # 동 변경으로 배정이 풀릴 수 있어 화면용 확정 자료를 다시 만든다.
        confirmed = S.confirm_plan(fixture.con, day, {
            a: [{'request_id': receipts[0]['id'], 'unload_after': {'label': UNLOAD['label']}}, {'request_id': receipts[1]['id']}],
            b: [{'request_id': receipts[2]['id']}],
        })
        state = {'road': False, 'blocked_destination': None}

        def provider(path):
            if not state['road']:
                return 400, {}, {'code': 'TooBig'}
            points = urlsplit(path).path.rsplit('/', 1)[1].split(';')
            return 200, {}, {'code': 'Ok', 'durations': [[0 if a == b else None if b == state['blocked_destination'] else 60
                                                        for b in points] for a in points]}

        with loopback(provider) as (url, _), sync_playwright() as pw:
            S.save_settings(fixture.con, {'osrm_url': url, 'external': {'travel_method': 'OSRM'}})
            origin = f'http://127.0.0.1:{fixture.port}/'
            browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
            ctx = browser.new_context(service_workers='block', viewport={'width': 1440, 'height': 1000})
            ctx.route('**/*', lambda r: r.continue_() if r.request.url.startswith(origin) else r.abort())
            ctx.route('https://tiles.openfreemap.org/**', lambda r: r.fulfill(json={'version': 8, 'sources': {}, 'layers': []}))
            name, value = fixture.admin_cookie.split('=', 1)
            ctx.add_cookies([{'name': name, 'value': value, 'url': origin}])
            ctx.add_init_script(f'({HOLD_PATHS})()')
            page = ctx.new_page()
            page.set_default_timeout(10000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))

            if not confirmed_only:
                final_page = ctx.new_page()
                final_page.set_default_timeout(10000)
                final_page.on('pageerror', lambda error: errors.append(str(error)))
                check_final_unload_map(final_page, origin, a, fixture.batches(day, route_map=True))
                final_page.close()
                assert not errors, errors
                if final_unload_only:
                    browser.close()
                    return
            check_confirmed_map(page, origin, day, (a, b), confirmed)
            assert not errors, errors
            print('확정 배차 지도 통과: 저장된 팀별 순서 조회 · 안전한 날짜 변경 · 계산 및 저장 요청 없음')
            if confirmed_only:
                browser.close()
                return
            page.reload()

            def calls(n):
                page.wait_for_function('n => pathCalls.length >= n', arg=n)
                return page.evaluate('() => pathCalls.map(c => c.body)')

            def finish(index, km=2.75, code=200, line=None, reason=None):
                page.evaluate('([i, km, code, line, reason]) => pathCalls[i].finish(km, code, line, reason)', [index, km, code, line, reason])

            def attach_map_probe():
                page.evaluate("async () => { const core = await import('/core.js'); window.testMap = () => core.liveMaps.at(-1); }")

            def expect_arrows(count_present):
                page.wait_for_function(with_map("""
                  if (!map?.getLayer('routes-direction') || !map.isSourceLoaded('routes')) return false;
                  const arrows = map.queryRenderedFeatures({ layers: ['routes-direction'] });
                  return (arrows.length > 0) === value && arrows.every(f => f.properties.road === true);
                """), arg=count_present)

            def camera():
                return page.evaluate(with_map('return [...map.getCenter().toArray(), map.getZoom()];'))

            def fits(at):
                return page.evaluate(with_map('return map.getBounds().contains(value);'), at)

            def heading(canvas, at):
                y = page.evaluate(with_map('return map.project(value).y;'), at)
                return arrow_direction(canvas, round(y))

            page.goto(origin + '#/home')
            attach_map_probe()
            map_card = page.locator('.dash-map-card')
            picker = map_card.get_by_label('지도 수거팀')
            distance = map_card.get_by_label('지도 경로·거리')
            initial = calls(2)
            assert {'batch_id': confirmed[0]['batch_id'], 'points': list(map(point, [DEPOT, locations[0], UNLOAD, locations[1], END]))} in initial
            picker.select_option(a)
            calls(3)
            picker.select_option(b)
            newest = calls(4)
            assert newest[-1]['batch_id'] == confirmed[1]['batch_id']
            page.wait_for_function(with_map("return map?.getSource('routes') != null;"))
            eastbound = [newest[-1]['points'][0], [126.917, 35.174], [126.990, 35.174], newest[-1]['points'][-1]]
            assert not fits(eastbound[2]), '응답 전에는 멀리 우회하는 지점이 보이지 않아야 합니다.'
            finish(3, line=eastbound)
            expect(distance).to_contain_text('도로 2.75km')
            assert fits(eastbound[2]), '기본 뷰가 실제 도로의 먼 우회 구간까지 포함해야 합니다.'
            expect_arrows(True)
            assert heading(map_card.locator('canvas.maplibregl-canvas'), eastbound[2]) > 1.5
            road_camera = camera()
            finish(2, 88.88)
            finish(0, 99.99)
            finish(1, 99.99)
            expect(distance).to_contain_text('가상 지도 B팀')
            expect(distance).not_to_contain_text('88.88')
            assert page.evaluate('() => pathCalls[2].signal.aborted')
            page.wait_for_function(with_map("""
              const features = map?.getSource('routes')?.serialize().data?.features;
              return features?.length === 1 && features[0].properties.road === true &&
                JSON.stringify(features[0].geometry.coordinates) === JSON.stringify(value);
            """), arg=eastbound)
            assert camera() == road_camera, '늦게 돌아온 다른 팀 응답으로 뷰가 바뀌었습니다.'
            check_large_map(page, map_card, b)
            picker.select_option(a)
            calls(5)
            pin = map_card.locator('.pin[aria-pressed]').filter(has_text='1-1')
            pin.click()
            finish(4, code=503)
            expect(distance).to_contain_text('직선')
            expect(distance).to_contain_text('도로 조회 실패')
            expect(distance).to_contain_text('방향 없음 · 운행 참고 불가')
            expect_arrows(False)
            expect(picker).to_have_value(a)
            expect(pin).to_have_attribute('aria-pressed', 'true')

            ctx.route('https://tiles.openfreemap.org/styles/liberty', lambda r: r.abort())
            page.reload()
            attach_map_probe()
            initial = calls(2)
            expect(map_card.get_by_text('바탕 지도를 불러오지 못해', exact=False)).to_be_visible()
            pin.click()
            selected_camera = camera()
            own = next(i for i, call in enumerate(initial) if call['batch_id'] == confirmed[0]['batch_id'])
            finish(own, line=[eastbound[0], [126.889, 35.151], eastbound[1]])
            expect_arrows(True)
            expect(pin).to_have_attribute('aria-pressed', 'true')
            assert camera() == selected_camera, '지점을 선택한 뒤 도로 응답이 뷰를 이동했습니다.'
            picker.select_option(b)
            calls(3)
            old_zoom = camera()[2]
            map_card.locator('.maplibregl-ctrl-zoom-in').click()
            page.wait_for_function(with_map('return map?.getZoom() > value && !map.isMoving();'), arg=old_zoom)
            zoomed = camera()
            canvas = map_card.locator('canvas.maplibregl-canvas')
            canvas.scroll_into_view_if_needed()
            rect = canvas.bounding_box()
            x, y = rect['x'] + rect['width'] * .6, rect['y'] + rect['height'] * .75
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x - 32, y, steps=8)
            page.mouse.up()
            page.wait_for_function(with_map('return map?.getCenter().lng !== value && !map.isMoving();'), arg=zoomed[0])
            picker.focus()
            manual_camera = camera()
            manual_path = [eastbound[0], [126.990, eastbound[0][1]], eastbound[-1]]
            finish(2, 9.25, line=manual_path)
            expect(distance).to_contain_text('도로 9.25km')
            expect_arrows(True)
            assert camera() == manual_camera, '지도를 수동 확대한 뒤 도로 응답이 뷰를 이동했습니다.'

            page.goto(origin + '#/dispatch')
            attach_map_probe()
            dispatch_calls = page.evaluate('() => pathCalls.length')
            page.get_by_role('button', name='선택 해제', exact=True).click()
            for team in ('가상 지도 A팀', '가상 지도 B팀'):
                page.locator('.crewpick label').filter(has_text=team).get_by_role('checkbox').check()
            page.get_by_role('button', name='노선 계산', exact=True).click()
            expect(page.locator('.matrix-note')).to_contain_text('전체 직선거리 추정', timeout=30000)
            calls(dispatch_calls + 2)
            team = page.locator(f'.route-card[data-route-crew="{a}"]')
            original = team.locator('.stopline[data-request-id]').evaluate_all('(rows) => rows.map(r => r.dataset.requestId)')
            assert len(original) == 2
            picker = page.get_by_label('지도 수거팀')
            picker.select_option(a)
            dispatch_draft = calls(dispatch_calls + 3)
            assert 'batch_id' not in dispatch_draft[-1]
            finish(len(dispatch_draft) - 1, line=eastbound[::-1])
            expect_arrows(True)
            assert heading(page.locator('.dispatch .route-map canvas.maplibregl-canvas'), eastbound[2]) < -1.5
            check_large_map(page, page.get_by_role('region', name='노선 조정안'), a)
            shown = team.get_by_role('button', name='지도에서 보기', exact=False).first
            shown.click()
            initial = page.evaluate('() => pathCalls.length')
            team.get_by_role('button', name='1번 아래로', exact=True).click()
            draft = calls(initial + 1)[-1]
            assert 'batch_id' not in draft
            swapped = team.locator('.stopline[data-request-id]').evaluate_all('(rows) => rows.map(r => r.dataset.requestId)')
            assert swapped == original[::-1]
            by_request = {r['id']: point(p) for r, p in zip(receipts[:2], locations[:2])}
            assert draft['points'] == [point(DEPOT), *[by_request[rid] for rid in swapped], point(END)]
            expect(picker).to_have_value(a)
            expect(team.get_by_role('button', name='지도에서 보기', exact=False).last).to_have_attribute('aria-pressed', 'true')
            finish(initial, reason='NoRoute')
            expect(page.get_by_label('지도 경로·거리')).to_contain_text('연결된 도로 경로 없음')
            expect(page.get_by_label('지도 경로·거리')).to_contain_text('방향 없음 · 운행 참고 불가')
            expect_arrows(False)

            page.get_by_role('button', name='확정 배차 지도 보기', exact=True).click()
            confirmed_panel = page.get_by_role('region', name='확정 배차 지도', exact=True)
            expect(confirmed_panel.get_by_label('지도 수거팀')).to_be_visible()
            page.get_by_role('button', name='확정 배차 지도 닫기', exact=True).click()
            expect(confirmed_panel).to_be_hidden()
            assert team.locator('.stopline[data-request-id]').evaluate_all('(rows) => rows.map(r => r.dataset.requestId)') == swapped
            expect(picker).to_have_value(a)

            state['road'] = True
            lon, lat = by_request[swapped[1]]
            state['blocked_destination'] = f'{lon:.6f},{lat:.6f}'
            with page.expect_response(lambda r: urlsplit(r.url).path == '/api/route', timeout=30000) as rejected:
                page.get_by_role('button', name='순서 유지 재계산', exact=True).click()
            failed = rejected.value.json()
            assert failed['infeasible'] and failed['matrix_info']['unreachable_pairs'] > 0, failed
            expect(page.locator('.actionbar .err')).to_contain_text('도로 연결 불가', timeout=2000)
            expect(page.locator('.actionbar .err')).to_contain_text(f"{failed['matrix_info']['unreachable_pairs']}쌍")
            expect(page.get_by_role('button', name='배정 확정', exact=True)).to_be_disabled()
            assert team.locator('.stopline[data-request-id]').evaluate_all('(rows) => rows.map(r => r.dataset.requestId)') == swapped

            state['blocked_destination'] = None
            with page.expect_response(lambda r: urlsplit(r.url).path == '/api/route', timeout=30000) as response:
                page.get_by_role('button', name='도로 기준 다시 계산', exact=True).click()
            assert response.value.ok, response.value.text()
            assert response.value.request.post_data_json['retry_osrm'] is True
            assert response.value.request.post_data_json['fixed_plan'][a] == swapped
            expect(page.locator('.matrix-note')).to_contain_text('전체 실제 도로 이동시간')
            assert team.locator('.stopline[data-request-id]').evaluate_all('(rows) => rows.map(r => r.dataset.requestId)') == swapped
            assert not errors, errors
            browser.close()
    finally:
        fixture.tearDown()
    print('OSRM 지도 통과: 두 화면의 반대 진행 화살표 · 직선 대체 · NoRoute · 우회 맞춤 · 선택/수동 확대 유지 · 응답 역전 · 오프라인 · 수기 순서와 도로 연결 불가 안내')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--confirmed-only', action='store_true')
    parser.add_argument('--final-unload-only', action='store_true')
    args = parser.parse_args()
    main(args.confirmed_only, args.final_unload_only)
