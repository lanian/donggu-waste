"""실제 서버/배포 EXE에서 PC 수거 입력·부모 초안·번호·서식을 확인한다."""
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
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-pc-collection-ui-'))
    (output / 'shots').mkdir(parents=True, exist_ok=True)
    with server as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        context = browser.new_context(service_workers='block', viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
        page = context.new_page()
        page.set_default_timeout(15000)
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        def api(path, body=None):
            opts = {'headers': {'X-DG-CSRF': '1'}}
            result = context.request.get(url + 'api/' + path, **opts) if body is None else context.request.post(url + 'api/' + path, data=body, **opts)
            assert result.ok, (path, result.status, result.text())
            return result.json()
        try:
            api('setup', {'username': 'collection_admin', 'display_name': '가상 수거 담당', 'password': 'collection-ui-test-123'})
            boot = api('bootstrap')
            dong = next(d for d in boot['dongs'] if d['name'] == '충장동')
            fee = next(r['id'] for r in json.loads((Path(__file__).resolve().parent.parent / 'data/fee_table.json').read_text(encoding='utf-8'))['rows'] if r['status'] == 'CONFIRMED' and r.get('price', 0) > 0)
            r = api('requests', {'name': '가상 수거 주민', 'phone': '01000000000', 'address': '가상구 예시로 1', 'dong': dong['name'], 'planned_date': boot['today'], 'items': [{'fee_rate_id': fee, 'qty': 3}]})
            api('numbered-labels/receive', {'start': '000101', 'end': '000110', 'dong_id': dong['id'], 'received_on': boot['today']})
            page.goto(url + '#/request/' + r['id'])
            page.get_by_label('요청 메모', exact=True).fill('수거 중에도 보존할 가상 수정 초안')
            opening = page.get_by_role('button', name='수거 처리', exact=True)
            expect(opening).to_be_visible(timeout=10000)
            page.evaluate("""() => { const native=window.fetch;let hold=true;window.fetch=(path,options)=>{
                if(hold && String(path).includes('/api/requests/') && (!options?.method || options.method==='GET')){
                  hold=false;return new Promise((resolve,reject)=>{window.__releaseCollectionDetail=()=>native(path,options).then(resolve,reject);});}
                return native(path,options);};}""")
            opening.click()
            page.wait_for_function("typeof window.__releaseCollectionDetail==='function'")
            quantity = page.locator('.it input[type=number]').first
            quantity.fill('4')
            quantity.press('Tab')
            with page.expect_response(lambda reply: '/api/requests/' + r['id'] in reply.url and reply.request.method == 'GET') as details:
                page.evaluate('window.__releaseCollectionDetail()')
            assert details.value.ok, details.value.text()
            expect(page.locator('.toast.bad')).to_contain_text('품목 수정 내용을 먼저 저장한 뒤 수거 처리하세요.')
            expect(page.get_by_role('region', name='PC 수거 처리', exact=True)).to_have_count(0)
            quantity.fill('3')
            quantity.press('Tab')
            opening.click()
            dialog = page.get_by_role('region', name='PC 수거 처리', exact=True)
            expect(dialog.get_by_label('실제 수거일', exact=True)).to_have_value(boot['today'])
            dialog.get_by_role('button', name='입력 접기', exact=True).click()
            expect(opening).to_be_focused()
            assert not api('requests/' + r['id'])['state']['outcome']
            opening.click()
            dialog.get_by_label('1번 이번 수거 개수', exact=True).fill('4')
            dialog.get_by_label('1번 이번 수거 개수', exact=True).press('Control+Enter')
            expect(dialog.get_by_text('이번 수거 개수가 남은 수량을 넘습니다.', exact=True)).to_be_visible()
            dialog.get_by_label('1번 이번 수거 개수', exact=True).fill('1')
            dialog.get_by_role('button', name='입력 접기', exact=True).click()
            expect(opening).to_be_focused()
            opening.click()
            expect(dialog.get_by_label('1번 이번 수거 개수', exact=True)).to_have_value('1')
            expect(page.get_by_role('dialog')).to_have_count(0)
            dialog.get_by_role('button', name='수거 결과 저장', exact=True).click()
            expect(dialog.get_by_text('일부 수거·미수거 사유를 입력하세요.', exact=True)).to_be_visible()
            dialog.get_by_label('일부 수거·미수거 사유', exact=True).fill('가상 현장 전달: 한 개 수거')
            page.route('**/api/requests/' + r['id'], lambda route: route.fulfill(status=503, content_type='application/json', body='{"error":"가상 최신 내용 조회 실패"}'), times=1)
            page.screenshot(path=str(output / 'shots' / 'pc-collection-entry.png'), full_page=True)
            dialog.get_by_role('button', name='수거 결과 저장', exact=True).click()
            expect(dialog).to_have_count(0)
            refresh = page.get_by_role('button', name='최신 내용 다시 불러오기', exact=True)
            expect(refresh).to_be_visible()
            expect(page.get_by_role('button', name='수거 처리', exact=True)).to_be_disabled()
            refresh.click()
            expect(page.get_by_label('요청 메모', exact=True)).to_have_value('수거 중에도 보존할 가상 수정 초안')
            expect(page.get_by_text('저장된 결과: 일부 수거', exact=True)).to_be_visible()
            assert api('requests/' + r['id'])['state']['lines'][0]['qty_collected'] == 1
            assert api('label-auto?dong_id=' + dong['id'])['assigned'] == 1
            page.get_by_placeholder('수정 사유').fill('가상 메모 보존 확인')
            with page.expect_response(lambda reply: reply.url.endswith('/api/requests/' + r['id']) and reply.request.method == 'POST') as saving:
                page.get_by_role('button', name='수정 저장', exact=True).click()
            assert saving.value.ok, saving.value.text()
            expect(page.get_by_label('요청 메모', exact=True)).to_have_value('수거 중에도 보존할 가상 수정 초안')
            assert api('requests/' + r['id'])['memo'] == '수거 중에도 보존할 가상 수정 초안'
            page.goto(url + '#/dongs')
            page.get_by_label('행정동 범위', exact=True).select_option(dong['id'])
            page.get_by_label('접수 검색어', exact=True).fill(r['receipt_no'])
            page.get_by_role('button', name='조회', exact=True).click()
            page.get_by_role('button', name=r['receipt_no'], exact=True).click()
            outer = page.get_by_role('region', name='동별 접수 상세', exact=True)
            expect(page).to_have_url(url + '#/dongs/' + r['id'])
            expect(page.get_by_role('dialog')).to_have_count(0)
            outer.get_by_label('요청 메모', exact=True).fill('동별 수거 중 보관할 수정 초안')
            outer.get_by_role('button', name='수거 처리', exact=True).click()
            expect(dialog.get_by_label('수거 입력 요약', exact=True)).to_contain_text('이전 수거 1개')
            dialog.get_by_role('button', name='남은 수량 전부', exact=True).click()
            expect(dialog.get_by_label('1번 이번 수거 개수', exact=True)).to_have_value('2')
            dialog.get_by_role('button', name='수거 결과 저장', exact=True).click()
            expect(dialog).to_have_count(0)
            assert api('requests/' + r['id'])['status'] == 'DONE'
            assert api('label-auto?dong_id=' + dong['id'])['assigned'] == 3
            expect(outer).to_be_visible()
            expect(outer.get_by_label('요청 메모', exact=True)).to_have_value('동별 수거 중 보관할 수정 초안')
            page.screenshot(path=str(output / 'shots' / 'pc-collection.png'), full_page=True)
            page.get_by_role('button', name='목록으로 · 초안 보관', exact=True).click()
            expect(page.get_by_label('행정동 범위', exact=True)).to_have_value(dong['id'])
            expect(page.get_by_label('접수 검색어', exact=True)).to_have_value(r['receipt_no'])
            expect(page.get_by_role('button', name=r['receipt_no'], exact=True)).to_be_focused()
            page.get_by_role('button', name=r['receipt_no'], exact=True).click()
            expect(outer.get_by_label('요청 메모', exact=True)).to_have_value('동별 수거 중 보관할 수정 초안')
            retry = api('requests', {'name': '가상 재시도 주민', 'phone': '01000000001', 'address': '가상구 재시도로 1', 'dong': dong['name'], 'planned_date': boot['today'], 'items': [{'fee_rate_id': fee, 'qty': 2}]})
            page.goto(url + '#/request/' + retry['id'])
            page.get_by_role('button', name='수거 처리', exact=True).click()
            dialog.get_by_label('1번 이번 수거 개수', exact=True).fill('1')
            dialog.get_by_label('일부 수거·미수거 사유', exact=True).fill('가상 응답 유실 재시도')
            def lose_response(route):
                assert route.fetch().ok
                route.abort()
            page.route('**/api/requests/' + retry['id'] + '/collection', lose_response, times=1)
            dialog.get_by_role('button', name='수거 결과 저장', exact=True).click()
            expect(dialog).to_contain_text('같은 저장 버튼을 누르면 동일한 요청의 반영 여부를 확인합니다.')
            expect(dialog.get_by_label('1번 이번 수거 개수', exact=True)).to_be_disabled()
            dialog.get_by_role('button', name='입력 접기', exact=True).click()
            page.get_by_role('button', name='수거 처리', exact=True).click()
            expect(dialog.get_by_label('1번 이번 수거 개수', exact=True)).to_have_value('1')
            dialog.get_by_role('button', name='저장 여부 다시 확인', exact=True).click()
            expect(dialog).to_have_count(0)
            assert api('requests/' + retry['id'])['state']['lines'][0]['qty_collected'] == 1
            assert api('label-auto?dong_id=' + dong['id'])['assigned'] == 4
            assert not errors, errors
            print('PC 직접 수거: 상세 조회 중 품목 편집·취소/초점·수량/사유 검증·초안/수정 저장·동별 재수거·배출표 1+2장·응답 유실 재시도 중복 차감 방지 통과')
        finally:
            context.close()
            browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe', type=Path)
    main(parser.parse_args().exe)
