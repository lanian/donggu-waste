"""PC 수거: 품목별 수량 조절·부과 단위·불명확 응답 재확인의 실제 HTTP 회귀."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
from playwright.sync_api import expect, sync_playwright
from test_review_ui import pc_server


def main(exe=None):
    if exe:
        from test_reports_labels_bundle import frozen_server
        server = frozen_server(exe.resolve())
    else:
        server = pc_server()
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-collection-quick-'))
    output.mkdir(parents=True, exist_ok=True)
    fees = json.loads((Path(__file__).resolve().parent.parent / 'data/fee_table.json').read_text(encoding='utf-8'))['rows']
    chair = next(r for r in fees if r['name'] == '의자' and r['spec'] == '고정식 의자')
    wardrobe = next(r for r in fees if r['name'] == '장롱' and r['unit_label'] == '쪽' and r['status'] == 'CONFIRMED')
    with server as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000}, service_workers='block')
        page = context.new_page()
        errors, sent = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        def api(path, body=None):
            options = {'headers': {'X-DG-CSRF': '1'}}
            r = context.request.get(url + 'api/' + path, **options) if body is None else context.request.post(url + 'api/' + path, data=body, **options)
            assert r.ok, (path, r.status, r.text())
            return r.json()
        try:
            api('setup', {'username': 'quick_collect', 'display_name': '가상 수량 담당', 'password': 'quick-collection-test-123'})
            boot = api('bootstrap')
            day = (dt.date.fromisoformat(boot['today']) - dt.timedelta(days=1)).isoformat()
            dong = next(d for d in boot['dongs'] if d['name'] == '충장동')
            received = api('requests', {'name': '가상 수량 주민', 'phone': '01000000000', 'address': '가상구 수량길 1',
                'dong': dong['name'], 'planned_date': boot['today'], 'items': [{'fee_rate_id': chair['id'], 'qty': 150}, {'fee_rate_id': wardrobe['id'], 'qty': 2}]})
            api('numbered-labels/receive', {'start': '001001', 'end': '001200', 'dong_id': dong['id'], 'received_on': day})
            page.goto(url + '#/request/' + received['id'])
            page.get_by_label('요청 메모', exact=True).fill('수량 입력 중에도 보존할 초안')
            page.get_by_role('button', name='수거 처리', exact=True).click()
            region = page.get_by_role('region', name='PC 수거 처리', exact=True)
            first = region.get_by_label('1번 이번 수거 개수', exact=True)
            second = region.get_by_label('2번 이번 수거 개수', exact=True)
            minus = region.get_by_role('button', name='1번 수거 수량 줄이기', exact=True)
            plus = region.get_by_role('button', name='1번 수거 수량 늘리기', exact=True)
            remaining = region.get_by_role('button', name='1번 남은 수량 채우기', exact=True)
            expect(minus).to_be_disabled()
            plus.click()
            expect(first).to_have_value('1')
            first.fill('')
            expect(minus).to_be_disabled()
            expect(plus).to_be_disabled()
            expect(first).to_have_attribute('aria-invalid', 'true')
            first.fill('151')
            expect(plus).to_be_disabled()
            remaining.click()
            expect(first).to_have_value('150')
            expect(plus).to_be_disabled()
            minus.click()
            expect(first).to_have_value('149')
            region.get_by_role('button', name='2번 남은 수량 채우기', exact=True).click()
            region.get_by_role('button', name='2번 수거 수량 줄이기', exact=True).click()
            expect(second).to_have_value('1')
            expect(first.locator('..')).to_contain_text('개')
            expect(second.locator('..')).to_contain_text('쪽')
            region.get_by_label('실제 수거일', exact=True).fill(day)
            region.get_by_label('일부 수거·미수거 사유', exact=True).fill('가상 현장 확인: 남은 품목은 다음 방문')
            endpoint = url + 'api/requests/' + received['id'] + '/collection'
            page.on('request', lambda request: sent.append(request.post_data_json) if request.url == endpoint and request.method == 'POST' else None)
            def lose_response(route):
                assert route.fetch().ok
                route.abort()
            page.route(endpoint, lose_response, times=1)
            region.get_by_role('button', name='수거 결과 저장', exact=True).click()
            retry = region.get_by_role('button', name='저장 여부 다시 확인', exact=True)
            expect(retry).to_be_enabled()
            for control in (first, second, minus, plus, remaining, region.get_by_role('button', name='2번 남은 수량 채우기', exact=True)):
                expect(control).to_be_disabled()
            assert api('label-auto?dong_id=' + dong['id'])['assigned'] == 150
            # DOM 값을 강제로 바꿔도 재확인은 최초 제출 본문을 그대로 사용한다.
            first.evaluate('(input)=>{input.value="0";}')
            region.get_by_role('button', name='입력 접기', exact=True).click()
            page.get_by_role('button', name='수거 처리', exact=True).click()
            expect(retry).to_be_enabled()
            retry.click()
            expect(region).to_have_count(0)
            assert len(sent) == 2 and sent[0] == sent[1], sent
            assert sent[0]['collected_on'] == day
            assert [i['qty'] for i in sent[0]['items']] == [149, 1]
            saved = api('requests/' + received['id'])
            assert [i['qty_collected'] for i in saved['state']['lines']] == [149, 1]
            assert api('label-auto?dong_id=' + dong['id'])['assigned'] == 150
            expect(page.get_by_label('요청 메모', exact=True)).to_have_value('수량 입력 중에도 보존할 초안')
            page.get_by_role('button', name='수거 처리', exact=True).click()
            expect(region.get_by_label('1번 이번 수거 개수', exact=True)).to_have_value('0')
            region.get_by_role('button', name='남은 수량 전부', exact=True).click()
            expect(region.get_by_label('1번 이번 수거 개수', exact=True)).to_have_value('1')
            expect(region.get_by_label('2번 이번 수거 개수', exact=True)).to_have_value('1')
            page.screenshot(path=str(output / 'pc-collection-quick.png'), full_page=True)
            region.screenshot(path=str(output / 'pc-collection-quick-panel.png'))
            # 확정적인 거절은 새 입력을 허용하며 증감 상한을 다시 적용한다.
            page.route(endpoint, lambda route: route.fulfill(status=409, json={'error': '가상 확정 거절'}), times=1)
            region.get_by_role('button', name='수거 결과 저장', exact=True).click()
            expect(region.get_by_text('가상 확정 거절', exact=True)).to_be_visible()
            expect(region.get_by_label('1번 이번 수거 개수', exact=True)).to_be_enabled()
            expect(region.get_by_role('button', name='1번 수거 수량 늘리기', exact=True)).to_be_disabled()
            expect(region.get_by_role('button', name='1번 수거 수량 줄이기', exact=True)).to_be_enabled()
            expect(region.get_by_role('button', name='저장 여부 다시 확인', exact=True)).to_have_count(0)
            region.get_by_role('button', name='수거 결과 저장', exact=True).click()
            expect(region).to_have_count(0)
            assert sent[-2]['client_req_id'] != sent[-1]['client_req_id']
            assert api('label-auto?dong_id=' + dong['id'])['assigned'] == 152
            assert not errors, errors
            print('PC 수거 빠른 입력: 품목별 증감/남은 수량·0~잔량/잘못된 입력·개/쪽 단위·날짜/부모 초안·응답 유실 동결 본문/요청번호 재확인·확정 거절 후 입력 복구·배출표150+2장 중복 차감 방지 통과')
        except Exception:
            page.screenshot(path=str(output / 'pc-collection-failure.png'), full_page=True)
            print('페이지 오류:', errors)
            print('현재 화면:', page.locator('body').inner_text()[:1800])
            raise
        finally:
            context.close()
            browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe', type=Path)
    main(parser.parse_args().exe)
