"""실제 서버/선택한 EXE에서 동별 재고 자동 배정과 원본 서식 다운로드."""
import argparse
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from zipfile import ZipFile

from playwright.sync_api import expect, sync_playwright
from test_reports_labels_e2e import ROOT, usage_backup
from test_review_ui import pc_server


def main(exe=None):
    if exe:
        from test_reports_labels_bundle import frozen_server
        server = frozen_server(exe)
    else:
        server = pc_server()
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-official-forms-e2e-'))
    output.mkdir(parents=True, exist_ok=True)
    shots = output / 'shots'
    shots.mkdir(exist_ok=True)
    with server as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        context = browser.new_context(service_workers='block', viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            def post(path, body):
                response = context.request.post(url + 'api/' + path, headers={'X-DG-CSRF': '1'}, data=body)
                assert response.ok, response.text()
                return response.json()
            def get(path):
                response = context.request.get(url + 'api/' + path, headers={'X-DG-CSRF': '1'})
                assert response.ok, response.text()
                return response.json()
            post('setup', {'username': 'forms_admin', 'display_name': '가상 서식담당', 'password': 'forms-e2e-fixture-123'})
            dongs = get('dongs')
            dong = next(d for d in dongs if d['name'] == '충장동')
            other = next(d for d in dongs if d['name'] == '동명동')
            fee = next(row['id'] for row in json.loads((ROOT / 'data/fee_table.json').read_text(encoding='utf-8'))['rows'] if row['status'] == 'CONFIRMED' and row.get('price', 0) > 0)
            backup, password = usage_backup(fee)
            imported = context.request.post(url + 'api/import/solo', headers={'X-DG-CSRF': '1', 'X-DG-Pass': password, 'Content-Type': 'application/octet-stream'}, data=backup)
            assert imported.ok, imported.text()
            assert imported.json()['labels_pending'] == 4
            post('numbered-labels/receive', {'start': '000201', 'end': '000210', 'dong_id': other['id'], 'received_on': '2026-11-01'})
            page.goto(url + '#/labels', wait_until='domcontentloaded')
            stock = page.get_by_role('region', name='동별 번호 자동 배정', exact=True)
            expect(stock.get_by_label('미배정 장수', exact=True)).to_have_text('4장', timeout=8000)
            page.get_by_role('button', name='번호 구간 입고', exact=True).click()
            dialog = page.get_by_role('dialog', name='번호 구간 입고', exact=True)
            dialog.get_by_label('시작 번호', exact=True).fill('000101')
            dialog.get_by_label('끝 번호', exact=True).fill('000110')
            dialog.get_by_label('재고 행정동', exact=True).select_option(dong['id'])
            dialog.get_by_label('수령일', exact=True).fill('2026-11-01')
            dialog.get_by_role('button', name='입고 기록', exact=True).click()
            expect(dialog).to_have_count(0)
            expect(stock.get_by_label('미배정 장수', exact=True)).to_have_text('0장')
            assert get('numbered-labels?destination=' + other['name'])['counts']['USED'] == 4  # counts are whole-ledger totals
            assert all(row['status'] == 'DISTRIBUTED' for row in get('numbered-labels?destination=' + other['name'])['rows'])
            before = get('numbered-labels')['counts']
            page.goto(url + '#/reports', wait_until='domcontentloaded')
            forms = page.get_by_role('region', name='원본 서식 자동 작성', exact=True)
            forms.get_by_label('서식 행정동', exact=True).select_option(dong['id'])
            forms.get_by_label('서식 처리일', exact=True).fill('2026-11-12')
            forms.get_by_role('button', name='서식 조회', exact=True).click()
            expect(forms.get_by_label('월보 수거 수량', exact=True)).to_have_text('4개')
            expect(forms.get_by_label('일일 대장 수거 수량', exact=True)).to_have_text('4개')
            assert '가상 장수 주민' not in forms.inner_text()
            for label, suffix in (('월보 XLSX', '.xlsx'), ('일일 대장 ODT', '.odt'), ('두 서식 ZIP', '.zip')):
                with page.expect_download() as saving:
                    forms.get_by_role('button', name=label, exact=True).click()
                download = saving.value
                assert dong['name'] in download.suggested_filename and download.suggested_filename.endswith(suffix)
                file = output / download.suggested_filename
                download.save_as(file)
                with ZipFile(file) as package:
                    if suffix == '.zip':
                        assert len(package.namelist()) == 2
                    else:
                        entry = 'xl/worksheets/sheet4.xml' if suffix == '.xlsx' else 'content.xml'
                        assert '000101-000103' in package.read(entry).decode('utf-8')
            assert get('numbered-labels')['counts'] == before, '다운로드가 재고를 또 차감함'
            forms.get_by_label('서식 처리일', exact=True).fill('2026-11-13')
            expect(forms.get_by_role('button', name='월보 XLSX', exact=True)).to_be_disabled()
            forms.get_by_role('button', name='서식 조회', exact=True).click()
            expect(forms.get_by_label('일일 대장 수거 수량', exact=True)).to_have_text('0개')
            expect(forms.get_by_label('월보 수거 수량', exact=True)).to_have_text('4개')
            page.screenshot(path=str(shots / 'official-forms.png'), full_page=True)
            assert not errors, errors
            print('실제 서버: 암호화 백업 4개 수거 → 다른 동 재고 제외 → 해당 동 입고 즉시 4장 배정 → XLSX/ODT/ZIP → 재다운로드 재고 불변 → 실제 일/월 구분 통과')
            print('검증 파일: ' + str(output))
        except Exception:
            page.screenshot(path=str(shots / 'failure.png'), full_page=True)
            raise
        finally:
            context.close()
            browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path)
    main(parser.parse_args().exe)
