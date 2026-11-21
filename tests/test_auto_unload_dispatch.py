"""실제 본청·동별 화면에서 자동 하차 배차를 확정하고 지도와 암호화된 휴대폰 목록을 확인한다."""
import argparse
import json
import os
from pathlib import Path
import time

from playwright.sync_api import expect, sync_playwright

from test_dong_work_e2e import api
from test_reports_labels_bundle import frozen_server
from test_review_ui import ROOT, mobile_server, pc_server
from test_workflow_mobile import open_mobile


CREW = '가상 반입팀'
DONG = '가상 하차동'
GARAGE = {'lat': 35.1462, 'lon': 126.9233, 'label': '가상 차고지'}
UNLOAD = {'lat': 35.1467, 'lon': 126.9237, 'label': '가상 반입지'}
PASSWORD = 'unload-test-only-2026'


def pair_phone(admin, url, phone):
    image = api(admin, url, 'registration-qr')['image']
    code = phone.evaluate("""async image => {
      const P = await import('./proto.js');
      window.readFixtureQr = async src => {
        const img = new Image(); img.src = src; await img.decode();
        const canvas = document.createElement('canvas'); canvas.width = img.width; canvas.height = img.height;
        const c = canvas.getContext('2d'); c.drawImage(img, 0, 0);
        const pixels = c.getImageData(0, 0, img.width, img.height);
        return jsQR(pixels.data, pixels.width, pixels.height).data;
      };
      window.__workflowPc = P.parseRegistration(await readFixtureQr(image));
      const d = window.__workflowDevice;
      return P.deviceCode(d.id, await P.exportRaw(d.kex.publicKey), await P.exportRaw(d.sign.publicKey));
    }""", image)
    device = api(admin, url, 'devices/register', {'code': code, 'label': '가상 반입 휴대폰'})['device_id']
    api(admin, url, f'devices/{device}/confirm', {})
    return device


def read_phone_list(admin, url, batch_id, phone):
    bundle = api(admin, url, 'bundles', {'batch_id': batch_id})
    sheet = admin.new_page()
    sheet.goto(url + 'bundle/' + bundle['bundle_id'][:8])
    images = sheet.evaluate('imgs')
    sheet.close()
    assert len(images) == bundle['frames']
    payload, receipt = phone.evaluate("""async images => {
      const P = await import('./proto.js'), c = new P.Collector(), device = window.__workflowDevice;
      let key;
      for (const src of images) key = c.add(await readFixtureQr(src)).key;
      const signature = await P.importEcdsaPub(window.__workflowPc.pcSignPub);
      const {payload, receipt} = await P.openEnvelope(c.assemble(key), signature, device.kex.privateKey, device.id);
      return [P.expandDaylist(payload), receipt];
    }""", images)
    api(admin, url, f"bundles/{bundle['bundle_id']}/receipt", {'code': receipt})
    return payload


def dispatch(context, url, day, dong_id, scoped):
    page = context.new_page()
    page.goto(url + ('#/dongs' if scoped else '#/dispatch'))
    if scoped:
        page.get_by_label('행정동 범위', exact=True).select_option(dong_id)
        date = page.get_by_label('업무 기준일', exact=True)
        crews = page.locator('.dw-teams label')
    else:
        date = page.locator('#dp-date')
        crews = page.locator('.crewpick label')
    date.fill(day)
    date.press('Tab')
    expect(crews.filter(has_text=CREW).get_by_role('checkbox')).to_be_checked()
    for other in crews.filter(has_not_text=CREW).get_by_role('checkbox').all():
        other.uncheck()
    prefix = url + 'api/' + ('dong-work/' if scoped else '')
    with page.expect_response(lambda r: r.url == prefix + 'route', timeout=60000) as response:
        page.get_by_role('button', name='노선 계산', exact=True).click()
    assert response.value.ok, response.value.text()
    result = response.value.json()
    assert not result['unassigned'], result['unassigned']
    route, = result['routes']
    assert route['unloads'] == len(route['stops']) == 7, route
    unload_rows = page.locator('.dw-route li' if scoped else '.route-card .stopline.unload')
    expect(unload_rows.filter(has_text='중간 하차')).to_have_count(6)
    expect(unload_rows.filter(has_text='최종 하차')).to_have_count(1)
    for i, stop in enumerate(route['stops']):
        for value in (stop['unload_after']['eta'], UNLOAD['label'], '5분'):
            expect(unload_rows.nth(i)).to_contain_text(value)
    expect(page.get_by_role('button', name='배정 확정', exact=True)).to_be_enabled()
    with page.expect_response(lambda r: r.url == prefix + 'assign', timeout=30000) as saved:
        page.get_by_role('button', name='배정 확정', exact=True).click()
        if scoped:
            page.get_by_role('dialog').get_by_role('button', name='이 범위로 배정 확정').click()
    assert saved.value.ok, saved.value.text()
    batch, = saved.value.json()
    page.close()
    return result, batch['batch_id']


def check_day(admin, url, phone, day, stops, batch_id, ids, roads):
    order = [s['request_id'] for s in stops]
    assert len(set(order)) == len(ids) == 7 and set(order) == ids
    batch, = api(admin, url, f'batches?date={day}&route_map=1')
    assert batch['id'] == batch_id and batch['map_route']['complete']
    history = batch['map_route']['history']
    assert history['state'] == 'CURRENT' and history['matrix_source'] == ('OSRM' if roads else 'ESTIMATE'), history
    if roads:
        assert history['path']['source'] == 'OSRM' and len(history['path']['geometry']['coordinates']) >= 2
    for key in ('depot', 'end'):
        assert all(batch['map_route'][key][k] == GARAGE[k] for k in ('lat', 'lon'))
    payload = read_phone_list(admin, url, batch_id, phone)
    assert payload['work_date'] == day and payload['crew'] == CREW
    assert [s['request_id'] for s in batch['stops']] == [s['request_id'] for s in payload['stops']] == order
    for i, (plan, mapped, delivered) in enumerate(zip(stops, batch['stops'], payload['stops'], strict=True)):
        unload, received = plan['unload_after'], delivered['unload_after']
        assert mapped['eta'] == delivered['eta'] == plan['eta']
        assert bool(unload.get('final')) == bool(mapped.get('final_unload')) == bool(received.get('final')) == (i == 6)
        assert received['volume_m3'] == unload['volume_m3'] == plan['load_m3'] == 6
        assert received['minutes'] == unload['minutes'] == 5 and received['eta'] == unload['eta']
        assert plan['eta'] <= unload['eta'] <= '17:00'
        for key in ('lat', 'lon', 'label'):
            assert mapped['via'][key] == received[key] == UNLOAD[key]
    assert received['stop_count'] == 7
    assert all(received['depot'][k] == GARAGE[k] for k in ('lat', 'lon'))


def main(exe=None, roads=False):
    with (frozen_server(exe.resolve()) if exe else pc_server()) as url, mobile_server() as mobile_url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path)
        try:
            admin, scoped = (browser.new_context(service_workers='block') for _ in range(2))
            admin.route('https://tiles.openfreemap.org/**', lambda r: r.fulfill(json={'version': 8, 'sources': {}, 'layers': []}))
            _, phone = open_mobile(browser, mobile_url)
            api(admin, url, 'setup', {'username': 'unload_admin', 'display_name': '가상 본청', 'password': PASSWORD})
            method = 'OSRM' if roads else 'ESTIMATE'
            api(admin, url, 'settings', {'external': {'travel_method': method}, 'osrm_url': '', 'max_unloads': 'auto',
                'depot': GARAGE, 'unload_site': UNLOAD, 'unload_min': 5, 'service_min_base': 1, 'service_min_per_item': 0,
                'unload_hours': {'start': '08:00', 'end': '17:00'}})
            settings = api(admin, url, 'bootstrap')['settings']
            assert settings['max_unloads'] == 'auto' and settings['external']['travel_preference'] == method and not settings['osrm_url']
            if roads:
                points = [[s['lon'], s['lat']] for s in (GARAGE, UNLOAD)]
                deadline = time.monotonic() + 30
                while True:
                    path = api(admin, url, 'route-path', {'points': points})
                    if path.get('reason') != 'STARTING' or time.monotonic() >= deadline:
                        break
                    time.sleep(.25)
                assert path['source'] == 'OSRM' and path['toll_excluded'], path
            device = pair_phone(admin, url, phone)
            crew = api(admin, url, 'crews', {'name': CREW, 'device_id': device, 'capacity_m3': 10, 'shift_start': '08:00', 'shift_end': '17:00'})['id']
            dong = api(admin, url, 'dongs', {'name': DONG, 'crew_id': crew})['id']
            api(admin, url, 'users', {'username': 'unload_dong', 'display_name': '가상 동 담당', 'role': 'DONG', 'dong_ids': [dong], 'password': PASSWORD})
            api(scoped, url, 'login', {'username': 'unload_dong', 'password': PASSWORD})
            api(scoped, url, 'password', {'old': PASSWORD, 'password': PASSWORD + '-changed'})
            fees = json.loads((ROOT / 'data/fee_table.json').read_text(encoding='utf-8'))['rows']
            fee = next(row['id'] for row in fees if row['status'] == 'CONFIRMED' and row['price'] > 0)
            api(admin, url, 'volumes', {'fee_rate_id': fee, 'm3': 6})
            for day, restricted in [('2099-01-05', False), ('2099-01-06', True)]:
                ids = set()
                for i in range(7):
                    req = api(admin, url, 'requests', {'name': f'가상 신청자 {i}', 'phone': f'0100000000{i}',
                        'address': f'가상로 {i + 1}', 'dong': DONG, 'planned_date': day, 'lat': 35.147, 'lon': 126.924,
                        'items': [{'fee_rate_id': fee, 'qty': 1}]})
                    ids.add(req['id'])
                result, batch = dispatch(scoped if restricted else admin, url, day, dong, restricted)
                assert result['matrix_source'] == method and result['summary']['auto_unloads'], result['matrix_info']
                if roads:
                    assert result['matrix_info']['road_pairs'] > 0 and result['matrix_info']['fallback_pairs'] == 0
                check_day(admin, url, phone, day, result['routes'][0]['stops'], batch, ids, roads)
                print(f"{'동별' if restricted else '본청'} {method} (도로 {result['matrix_info']['road_pairs']}쌍, 추정 {result['matrix_info']['fallback_pairs']}쌍)"
                      ' 실제 계산·확정·지도·휴대폰 QR 수신확인 통과 · 중간 하차 6회 및 최종 하차 1회', flush=True)
        finally:
            browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path)
    parser.add_argument('--bundled-roads', action='store_true')
    opts = parser.parse_args()
    if opts.bundled_roads and not opts.exe:
        parser.error('--bundled-roads에는 --exe가 필요합니다.')
    main(opts.exe, opts.bundled_roads)
