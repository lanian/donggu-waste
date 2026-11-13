"""실제 서버/EXE: 동별 차고지 편집·취소·순서 초안·배차·지도 출발점."""
import argparse
import json
import os
from pathlib import Path
import tempfile
from playwright.sync_api import expect, sync_playwright
from test_review_ui import pc_server


def main(exe=None):
    if exe:
        from test_reports_labels_bundle import frozen_server
        server = frozen_server(exe)
    else:
        server = pc_server()
    shots = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-depots-ui-')) / 'shots'
    shots.mkdir(parents=True, exist_ok=True)
    with server as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True, args=['--enable-unsafe-swiftshader'])
        context = browser.new_context(service_workers='block', viewport={'width': 1500, 'height': 1000})
        # A local empty basemap exercises actual MapLibre pins/GeoJSON without a provider call.
        context.route('https://tiles.openfreemap.org/**', lambda route: route.fulfill(json={'version': 8, 'sources': {}, 'layers': [{'id': 'bg', 'type': 'background', 'paint': {'background-color': '#f4f6fa'}}]}))
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        def api(path, body=None):
            response = (context.request.get(url + 'api/' + path, headers={'X-DG-CSRF': '1'}) if body is None else
                        context.request.post(url + 'api/' + path, data=body, headers={'X-DG-CSRF': '1'}, timeout=30000))
            assert response.ok, response.text()
            return response.json()
        try:
            api('setup', {'username': 'depot_admin', 'display_name': '가상 차고지담당', 'password': 'depot-ui-fixture-123'})
            a = api('crews', {'name': '가상 A 전담팀'})['id']
            b = api('crews', {'name': '가상 B 전담팀'})['id']
            da = api('dongs', {'name': '차고가상A동', 'crew_id': a})['id']
            db = api('dongs', {'name': '차고가상B동', 'crew_id': b})['id']
            pa = {'lat': 35.14, 'lon': 126.90, 'label': '가상 A 차고지'}
            pb = {'lat': 35.20, 'lon': 127.00, 'label': '가상 B 차고지'}
            page.goto(url + '#/settings/dongs')
            row = page.locator(f'tr[data-dong-id="{da}"]')
            open_button = row.get_by_role('button', name='차고가상A동 차고지 설정', exact=True)
            open_button.click()
            dialog = page.get_by_role('dialog', name='차고가상A동 차고지 설정', exact=True)
            dialog.get_by_label('차고지 이름', exact=True).fill(pa['label'])
            dialog.get_by_label('차고지 위도', exact=True).fill('91')
            dialog.get_by_label('차고지 경도', exact=True).fill(str(pa['lon']))
            dialog.get_by_role('button', name='행에 적용', exact=True).click()
            expect(dialog.get_by_text('차고지 좌표가 국내 범위를 벗어났습니다.', exact=True)).to_be_visible()
            dialog.get_by_label('차고지 위도', exact=True).fill(str(pa['lat']))
            dialog.get_by_role('button', name='취소', exact=True).click()
            expect(open_button).to_be_focused()
            assert next(d for d in api('dongs') if d['id'] == da)['depot'] is None
            open_button.click()
            dialog.get_by_label('차고지 이름', exact=True).fill(pa['label'])
            dialog.get_by_label('차고지 위도', exact=True).fill(str(pa['lat']))
            dialog.get_by_label('차고지 경도', exact=True).fill(str(pa['lon']))
            dialog.get_by_role('button', name='행에 적용', exact=True).click()
            expect(dialog).to_have_count(0)
            assert next(d for d in api('dongs') if d['id'] == da)['depot'] is None, '적용만으로 서버 저장됨'
            page.get_by_role('button', name='차고가상B동 위로 이동', exact=True).click()
            page.get_by_role('button', name='순서 저장', exact=True).click()
            expect(page.get_by_text('동 순서를 저장했습니다.', exact=True)).to_be_visible()
            open_button.click()
            expect(dialog.get_by_label('차고지 위도', exact=True)).to_have_value(str(pa['lat']))
            dialog.get_by_role('button', name='취소', exact=True).click()
            row_b = page.locator(f'tr[data-dong-id="{db}"]')
            row_b.get_by_role('button', name='차고가상B동 차고지 설정', exact=True).click()
            dialog_b = page.get_by_role('dialog', name='차고가상B동 차고지 설정', exact=True)
            dialog_b.get_by_label('차고지 이름', exact=True).fill(pb['label'])
            dialog_b.get_by_label('차고지 위도', exact=True).fill(str(pb['lat']))
            dialog_b.get_by_label('차고지 경도', exact=True).fill(str(pb['lon']))
            dialog_b.get_by_role('button', name='행에 적용', exact=True).click()
            with page.expect_response(lambda r: r.url.endswith('/api/dongs') and r.request.method == 'POST') as saving:
                row.get_by_role('button', name='저장', exact=True).click()
            assert saving.value.ok, saving.value.text()
            expect(row.get_by_label('차고가상A동 차고지 현황', exact=True)).to_contain_text(pa['label'])
            assert next(d for d in api('dongs') if d['id'] == da)['depot'] == pa
            expect(row_b.get_by_label('차고가상B동 차고지 현황', exact=True)).to_contain_text(pb['label'])
            expect(row_b.get_by_label('차고가상B동 차고지 현황', exact=True)).to_contain_text('저장 전')
            with page.expect_response(lambda r: r.url.endswith('/api/dongs') and r.request.method == 'POST') as saving_b:
                row_b.get_by_role('button', name='저장', exact=True).click()
            assert saving_b.value.ok
            assert next(d for d in api('dongs') if d['id'] == db)['depot'] == pb
            boot = api('bootstrap')
            root = Path(__file__).resolve().parent.parent
            fee = next(r['id'] for r in json.loads((root / 'data/fee_table.json').read_text(encoding='utf-8'))['rows'] if r['status'] == 'CONFIRMED' and r.get('price', 0) > 0)
            for name, garage in [('차고가상A동', pa), ('차고가상B동', pb)]:
                api('requests', {'name': '가상 차고지 주민', 'phone': '01000000000', 'address': '가상구 예시로 1', 'dong': name, 'planned_date': boot['today'],
                                 'lat': garage['lat'], 'lon': garage['lon'], 'items': [{'fee_rate_id': fee, 'qty': 1}]})
            page.goto(url + '#/dongs')
            expect(page.get_by_role('heading', name='동별 관리', exact=True)).to_be_visible()
            page.get_by_role('button', name='노선 계산', exact=True).click()
            expect(page.locator('.dw-routes')).to_contain_text(pa['label'], timeout=30000)
            expect(page.locator('.dw-routes')).to_contain_text(pb['label'])
            page.goto(url + '#/dispatch')
            page.get_by_role('button', name='노선 계산', exact=True).click()
            expect(page.get_by_label('가상 A 전담팀 차고지', exact=True)).to_contain_text(pa['label'], timeout=30000)
            expect(page.get_by_label('가상 B 전담팀 차고지', exact=True)).to_contain_text(pb['label'])
            page.wait_for_function("async () => {const C=await import('/core.js');return C.liveMaps.some(m=>m.getSource('routes'));}", timeout=15000)
            lines = page.evaluate("async () => {const C=await import('/core.js');return C.liveMaps.find(m=>m.getSource('routes')).getSource('routes').serialize().data.features.map(f=>f.geometry.coordinates);}")
            assert sorted(line[0] for line in lines) == sorted([[pa['lon'], pa['lat']], [pb['lon'], pb['lat']]])
            assert all(line[0] == line[-1] for line in lines), '기본 복귀선이 팀 차고지가 아님'
            page.screenshot(path=str(shots / 'dong-depots-dispatch.png'), full_page=True)
            assert not errors, errors
            print('동별 차고지: 잘못된 좌표·취소/초점·임시 적용·직제순 초안 보존·저장·동별/전체 배차·실제 MapLibre 팀별 출발/복귀 좌표 통과')
        except Exception:
            page.screenshot(path=str(shots / 'failure.png'), full_page=True)
            raise
        finally:
            context.close(); browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path)
    main(parser.parse_args().exe)
