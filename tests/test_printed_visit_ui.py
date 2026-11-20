"""임시 PC와 휴대폰 브라우저로 보호 인쇄 QR을 읽고 한 건씩 가져온다."""
import argparse
import hashlib
import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from test_dong_work_e2e import api
from test_osrm_runtime import loopback
from test_review_ui import ROOT, mobile_server, pc_server
from test_workflow_mobile import open_mobile, seed


def main(exe=None):
    if exe:
        from test_reports_labels_bundle import frozen_server
        server = frozen_server(exe.resolve())
    else:
        server = pc_server()
    with server as pc_url, mobile_server() as mobile_url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        pc = browser.new_context(service_workers='block')
        phone, app = open_mobile(browser, mobile_url)
        try:
            api(pc, pc_url, 'setup', {'username': 'printed_visit', 'display_name': '가상 배차 담당자', 'password': 'printed-visit-fixture-123'})
            day = api(pc, pc_url, 'bootstrap')['today']
            crew = api(pc, pc_url, 'crews', {'name': '가상 인쇄 수거팀', 'capacity_m3': 10})['id']
            fee = next(row['id'] for row in json.loads((ROOT / 'data/fee_table.json').read_text(encoding='utf-8'))['rows']
                       if row['status'] == 'CONFIRMED' and row['price'] > 0)
            long_memo = ''.join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(20))
            requests = [api(pc, pc_url, 'requests', {'name': f'인쇄검증 주민 {i}', 'address': f'가상구 시험용 상세주소 {i}',
                        'phone': '01000006261', 'memo': f'가상 비공개 메모 {i}' + (long_memo if i else ''), 'spot_note': f'가상 현장 위치 메모 {i}', 'planned_date': day,
                        'lat': 35.146 + i * 0.002, 'lon': 126.922 + i * 0.001,
                        'items': [{'fee_rate_id': fee, 'qty': 1}]}) for i in range(2)]
            requests = [api(pc, pc_url, 'requests/' + r['id']) for r in requests]
            garage = {'lat': 35.147, 'lon': 126.920, 'label': '가상 차고지'}
            unload = {'lat': 35.152, 'lon': 126.926, 'label': '가상 반입지'}
            api(pc, pc_url, 'settings', {'depot': garage, 'unload_site': unload})
            stops = [{'request_id': r['id']} for r in requests]
            stops[-1]['unload_after'] = {'label': unload['label'], 'eta': '10:00', 'minutes': 20, 'volume_m3': 1.25, 'final': True}
            batch = api(pc, pc_url, 'assign', {'date': day, 'plan': {crew: stops}})[0]['batch_id']
            address = pc_url + 'print/batch/' + batch[:8]
            sheet = pc.new_page()
            sheet.goto(address)
            expect(sheet.get_by_text('개인정보 보호 인쇄', exact=True)).to_be_visible()
            expect(sheet.locator('#print')).to_have_count(0)
            assert requests[0]['name'] not in sheet.content()
            expect(sheet.get_by_role('link', name='일반 인쇄', exact=True)).to_be_visible()

            registration = api(pc, pc_url, 'registration-qr')['image']
            code = app.evaluate("""async (src) => {
                const P = await import('./proto.js');
                const image = new Image(); image.src = src; await image.decode();
                const canvas = document.createElement('canvas'); canvas.width = image.width; canvas.height = image.height;
                const ctx = canvas.getContext('2d'); ctx.drawImage(image, 0, 0);
                window.__workflowPc = P.parseRegistration(jsQR(ctx.getImageData(0, 0, image.width, image.height).data, image.width, image.height).data);
                const d = window.__workflowDevice;
                return P.deviceCode(d.id, await P.exportRaw(d.kex.publicKey), await P.exportRaw(d.sign.publicKey));
            }""", registration)
            device = api(pc, pc_url, 'devices/register', {'label': '가상 스캔 휴대폰', 'code': code})['device_id']
            api(pc, pc_url, 'devices/' + device + '/confirm', {})
            api(pc, pc_url, 'crews', {'id': crew, 'name': '가상 인쇄 수거팀', 'device_id': device, 'capacity_m3': 10})
            seed(app)
            sheet.reload()
            expect(sheet.locator('.visit-qr-cell')).to_have_count(2)
            protected_html = sheet.content()
            for request in requests:
                assert request['name'] not in protected_html and request['address'] not in protected_html
            assert '010-0000-6261' not in protected_html and '01000006261' not in protected_html
            assert '가상 비공개 메모' not in protected_html and '가상 현장 위치 메모' not in protected_html
            route = sheet.locator('.print-route')
            expect(route.get_by_role('heading', name='방문 순서도')).to_be_visible()
            assert not any(raw in route.inner_html() for raw in ('126.922', '126.923', '35.146'))
            images = [[img.get_attribute('src') for img in cell.locator('img.visit-qr').all()]
                      for cell in sheet.locator('.visit-qr-cell').all()]
            assert len(images[0]) == 1 and len(images[1]) > 1, [len(parts) for parts in images]
            assert all(src.startswith('data:image/png;base64,') for row in images for src in row)
            sheet.emulate_media(media='print')
            expect(sheet.locator('.print-options')).to_be_hidden()
            expect(sheet.locator('img.visit-qr').first).to_be_visible()
            expect(route.locator('.print-route-order')).to_be_visible()
            sheet.emulate_media(media='screen')
            geometry = {'type': 'LineString', 'coordinates': [[126.920, 35.147], [126.922, 35.146], [126.923, 35.148],
                                                               [126.926, 35.152], [126.920, 35.147]]}
            road_reply = {'code': 'Ok', 'routes': [{'distance': 2750, 'geometry': geometry}]}
            with loopback(lambda _: (200, {}, road_reply)) as (url, calls):
                api(pc, pc_url, 'settings', {'osrm_url': url, 'external': {'travel_method': 'OSRM'}})
                sheet.reload()
                assert not calls, '위치를 제외한 보호 인쇄는 좌표를 조회하지 않는다'
                sheet.get_by_role('link', name='노선도에 방문 위치 표시', exact=True).click()
                expect(route.get_by_role('heading', name='배차 노선도')).to_be_visible()
                expect(route).to_contain_text('2.75')
                expect(sheet.locator('.visit-qr-cell')).to_have_count(2)
                assert calls and all(r['address'] not in sheet.content() for r in requests)
                assert not any(raw in route.inner_html() for raw in ('126.922', '126.923', '35.146'))
                sheet.get_by_role('link', name='방문 위치 숨기기', exact=True).click()
                expect(route.get_by_role('heading', name='방문 순서도')).to_be_visible()
                sheet.get_by_role('link', name='일반 인쇄', exact=True).click()
                expect(route).to_contain_text('2.75')
            api(pc, pc_url, 'settings', {'external': {'travel_method': 'ESTIMATE'}})
            sheet.reload()
            expect(route).to_contain_text('직선')
            expect(sheet.locator('tbody')).to_contain_text(requests[0]['name'])
            expect(sheet.locator('tbody')).to_contain_text(requests[0]['address'])
            expect(sheet.locator('img.visit-qr')).to_have_count(0)
            assert not api(pc, pc_url, 'batches?date=' + day)[0]['bundles'], '배차표 출력은 팀 목록 전체 수신 확인을 만들지 않는다'

            app.evaluate("""() => {
                const canvas = document.createElement('canvas'); canvas.width = canvas.height = 640;
                const ctx = canvas.getContext('2d'); ctx.imageSmoothingEnabled = false;
                navigator.mediaDevices.getUserMedia = async () => canvas.captureStream(15);
                let timer;
                window.printedQr = async (frames) => {
                    const images = await Promise.all(frames.map(async src => {
                        const img = new Image(); img.src = src; await img.decode(); return img;
                    }));
                    clearInterval(timer);
                    let frame = 0;
                    const draw = () => {
                        ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 640, 640);
                        ctx.drawImage(images[Math.floor(frame++ / 8) % images.length], 20, 20, 600, 600);
                    };
                    draw(); timer = setInterval(draw, 70);
                    location.hash = '#/scan';
                };
            }""")
            phone.set_offline(True)

            app.evaluate("""async () => {
                const P = await import('./proto.js'), S = await import('./store.js');
                await S.putRaw('device', { id: P.randomBytes(16), ...await P.generateDeviceKeys() });
            }""")
            app.evaluate('(frames) => window.printedQr(frames)', images[0])
            expect(app.get_by_text('다른 휴대폰용 목록입니다.', exact=False)).to_be_visible()
            assert app.evaluate("import('./core.js').then(({ st }) => st.daylist)") is None
            app.evaluate("import('./store.js').then(S => S.putRaw('device', window.__workflowDevice))")
            app.get_by_role('button', name='QR 다시 읽기', exact=True).click()
            expect(app.locator('.who .name')).to_have_text(requests[0]['name'], timeout=20000)

            def scan(index):
                app.evaluate('(src) => window.printedQr(src)', images[index])
                expect(app.locator('.who .name')).to_have_text(requests[index]['name'], timeout=20000)

            app.evaluate("""async () => {
                const C = await import('./core.js');
                await C.addEvent(C.st.daylist.stops[0], 'ATTEMPT', { outcome: 'NOT_COLLECTED', reason: 'NOT_PUT_OUT' });
            }""")
            scan(1)
            scan(0)
            got = app.evaluate("""async () => {
                const { getSecure } = await import('./store.js');
                const st = await getSecure('state');
                return { ids: st.daylist.stops.map(s => s.request_id), events: st.events.length, receipt: st.lastReceipt,
                         unloads: st.daylist.stops.map(s => s.unload_after) };
            }""")
            assert got == {'ids': [r['id'] for r in requests], 'events': 1, 'receipt': None,
                           'unloads': [None, {**unload, 'eta': '10:00', 'minutes': 20, 'volume_m3': 1.25,
                            'final': True, 'depot': {'lat': garage['lat'], 'lon': garage['lon']}, 'stop_count': 2}]}, got

            updated = api(pc, pc_url, 'assign', {'date': day, 'plan': {crew: [{'request_id': r['id']} for r in requests]}})[0]['batch_id']
            sheet.goto(pc_url + 'print/batch/' + updated[:8])
            newer = [img.get_attribute('src') for img in sheet.locator('.visit-qr-cell').first.locator('img.visit-qr').all()]
            app.evaluate('(frames) => window.printedQr(frames)', newer)
            expect(app.get_by_text('최신 팀 목록 QR을 요청하세요.', exact=False)).to_be_visible()
            assert app.evaluate("import('./core.js').then(({ st }) => st.daylist.assign_ver)") == 1

            bundle = api(pc, pc_url, 'bundles', {'batch_id': updated})
            sheet.goto(pc_url + 'bundle/' + bundle['bundle_id'][:8])
            full_list_images = sheet.evaluate('() => imgs')
            app.evaluate('(frames) => window.printedQr(frames)', full_list_images)
            app.get_by_role('button', name='QR 다시 읽기', exact=True).click()
            expect(app.get_by_role('heading', name='목록 받기 완료')).to_be_visible(timeout=20000)
            full = app.evaluate("""async () => {
                const { getSecure } = await import('./store.js');
                const st = await getSecure('state');
                return { ver: st.daylist.assign_ver, events: st.events.length, code: st.lastReceipt.code };
            }""")
            assert full['ver'] == 2 and full['events'] == 1, full
            api(pc, pc_url, 'bundles/' + bundle['bundle_id'] + '/receipt', {'code': full['code']})
            app.evaluate('(frames) => window.printedQr(frames)', images[0])
            expect(app.get_by_text('이미 받은 것보다 오래된 목록입니다.', exact=False)).to_be_visible()
            print('보호 배차표: 오프라인 건별·다중 조각 수신, 다른 기기·낡은 갱신본 거부, 중복·수거기록 보존, 일반 인쇄·최신 팀 목록 수신 확인 통과')
        finally:
            phone.close()
            pc.close()
            browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path)
    main(parser.parse_args().exe)
