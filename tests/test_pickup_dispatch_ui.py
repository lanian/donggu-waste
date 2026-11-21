"""가상 아파트 이사 접수 3건으로 본청·동 담당의 실제 배차 계산과 시간표를 확인한다."""
import argparse
import os
from pathlib import Path
import time

from playwright.sync_api import expect, sync_playwright

from test_dong_work_e2e import api
from test_reports_labels_bundle import frozen_server
from test_review_ui import pc_server


DAY = '2099-01-05'
CREW = '가상 단지 수거팀'
DONG = '가상 공동주택동'
PASSWORD = 'apartment-test-only-2026'


def seed(admin, scoped, url, bundled_roads):
    api(admin, url, 'setup', {'username': 'pickup_admin', 'display_name': '가상 본청', 'password': PASSWORD})
    method = 'OSRM' if bundled_roads else 'ESTIMATE'
    api(admin, url, 'settings', {'osrm_url': '', 'external': {'travel_method': method}})
    settings = api(admin, url, 'bootstrap')['settings']
    assert settings['external']['travel_preference'] == settings['external']['travel_method'] == method
    assert not settings['osrm_url']
    if bundled_roads:
        depot = settings['depot']
        points = [[depot['lon'], depot['lat']], [126.924, 35.147]]
        deadline = time.monotonic() + 30
        while True:
            path = api(admin, url, 'route-path', {'points': points})
            if path.get('reason') != 'STARTING' or time.monotonic() >= deadline:
                break
            time.sleep(.25)
        # 빈 osrm_url에서 동봉 엔진은 READY 상태가 되어야 서버에 endpoint를 제공한다.
        assert path['source'] == 'OSRM' and path['toll_excluded'], path
        print('동봉 OSRM 준비 확인 · 광주 도로 형상 및 유료도로 제외 응답', flush=True)
    crew_id = api(admin, url, 'crews', {'name': CREW, 'capacity_m3': 20})['id']
    dong_id = api(admin, url, 'dongs', {'name': DONG, 'crew_id': crew_id})['id']
    api(admin, url, 'users', {'username': 'pickup_dong', 'display_name': '가상 동 담당', 'role': 'DONG',
                              'dong_ids': [dong_id], 'password': PASSWORD})
    api(scoped, url, 'login', {'username': 'pickup_dong', 'password': PASSWORD})
    api(scoped, url, 'password', {'old': PASSWORD, 'password': PASSWORD + '-changed'})

    visits = []
    for index, (building, room, extra) in enumerate([(101, 301, 37), (101, 702, 0), (102, 101, 0)], start=1):
        spot = f'{building}동 분리수거장'
        row = api(admin, url, 'requests', {'name': f'가상 신청자 {index}', 'phone': f'0100000000{index}',
                  'dong': DONG, 'address': f'가상로 10 가상아파트 {building}동 {room}호',
                  'spot_note': spot, 'lat': 35.147, 'lon': 126.924, 'geo_source': 'ADDR',
                  'planned_date': DAY, 'extra_work_min': extra,
                  'items': [{'name': '가상 의자', 'unit': 'EA', 'unit_label': '개', 'qty': 1}]})
        detail = api(scoped, url, 'dong-work/requests/' + row['id'])
        assert (detail['extra_work_min'], detail['spot_note']) == (extra, spot)
        visits.append((row['id'], row['receipt_no'], spot))
    return crew_id, dong_id, visits


def check_dispatch(context, url, crew_id, dong_id, visits, *, scoped, bundled_roads):
    page = context.new_page()
    page.set_default_timeout(15000)
    page.goto(url + ('#/dongs' if scoped else '#/dispatch'))
    if scoped:
        page.get_by_label('행정동 범위', exact=True).select_option(dong_id)
        date = page.get_by_label('업무 기준일', exact=True)
        date.fill(DAY)
        date.press('Tab')
        checkboxes = page.locator('.dw-teams label')
        expect(page.get_by_role('checkbox', name=CREW, exact=True)).to_be_checked()
    else:
        date = page.locator('#dp-date')
        date.fill(DAY)
        date.press('Tab')
        checkboxes = page.locator('.crewpick label')
        expect(checkboxes.filter(has_text=CREW).get_by_role('checkbox')).to_be_checked()
    for other in checkboxes.filter(has_not_text=CREW).get_by_role('checkbox').all():
        other.uncheck()

    endpoint = 'dong-work/route' if scoped else 'route'
    with page.expect_response(lambda response: response.url == url + 'api/' + endpoint, timeout=45000) as response:
        page.get_by_role('button', name='노선 계산', exact=True).click()
    assert response.value.ok, response.value.text()
    res = response.value.json()
    info = res['matrix_info']
    if bundled_roads:
        assert res['matrix_source'] == 'OSRM' and info['status'] == 'Ok', (res['matrix_source'], info)
        assert info['road_pairs'] > 0 and info['fallback_pairs'] == info['unreachable_pairs'] == 0, info
    else:
        assert res['matrix_source'] == 'ESTIMATE', res['matrix_source']
    assert not res['unassigned'], res['unassigned']
    route, = res['routes']
    assert route['crew_id'] == crew_id
    stops = {stop['request_id']: stop for stop in route['stops']}
    assert set(stops) == {rid for rid, _, _ in visits}
    moving, regular, separate = (stops[rid] for rid, _, _ in visits)
    assert moving['service_min'] == regular['service_min'] + 37
    assert moving['extra_work_min'] == 37 and regular['service_min'] == separate['service_min']
    assert route['work_min'] == sum(s['service_min'] for s in stops.values())
    assert [s.get('same_pickup', 0) for s in (moving, regular, separate)] == [2, 2, 0]
    assert abs(route['stops'].index(moving) - route['stops'].index(regular)) == 1

    for rid, receipt, spot in visits:
        stop = stops[rid]
        assert res['requests'][rid]['spot_note'] == spot
        row = page.locator('.dw-route li').filter(has_text=receipt) if scoped else page.locator(f'.stopline[data-request-id="{rid}"]')
        expect(row).to_contain_text(receipt)
        expect(row).to_contain_text(spot)
        expect(row).to_contain_text(stop['eta'])
        expect(row).to_contain_text(f"현장 {stop['service_min']}분")
        if rid == moving['request_id']:
            expect(row).to_contain_text('추가 37분 포함')
        if rid == separate['request_id']:
            expect(row).not_to_contain_text('같은 수거장')
        else:
            expect(row).to_contain_text('같은 수거장 2건')
    print(f"{'동별' if scoped else '본청'} 실HTTP·화면 통과 · 동일 수거장 2건, 별도 1건, 이사 현장 {moving['service_min']}분(추가 37분)"
          f" · {res['matrix_source']} {info.get('status', '')} 도로 {info['road_pairs']}쌍 / 추정 {info['fallback_pairs']}쌍", flush=True)
    page.close()


def main(exe=None, bundled_roads=False):
    with (frozen_server(exe.resolve()) if exe else pc_server()) as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path)
        try:
            admin = browser.new_context(service_workers='block')
            scoped = browser.new_context(service_workers='block')
            # 계산 API와 시간표는 실제 서버를 사용한다. 외부 지도 배경은 요청하지 않는다.
            admin.route('https://tiles.openfreemap.org/**', lambda r: r.fulfill(json={'version': 8, 'sources': {}, 'layers': []}))
            crew_id, dong_id, visits = seed(admin, scoped, url, bundled_roads)
            check_dispatch(admin, url, crew_id, dong_id, visits, scoped=False, bundled_roads=bundled_roads)
            check_dispatch(scoped, url, crew_id, dong_id, visits, scoped=True, bundled_roads=bundled_roads)
            assert not api(admin, url, 'batches?date=' + DAY)
        finally:
            browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path)
    parser.add_argument('--bundled-roads', action='store_true', help='EXE 옆에 동봉한 도로 팩으로 실제 배차를 검증')
    options = parser.parse_args()
    if options.bundled_roads and not options.exe:
        parser.error('--bundled-roads 검증에 사용할 --exe 경로를 지정하세요.')
    main(options.exe, options.bundled_roads)
