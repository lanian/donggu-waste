"""가상 PC 설정 화면에서 자동 중간 하차와 기존 차량당 횟수 제한의 저장·복원을 확인한다."""
import argparse
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from test_dong_work_e2e import api
from test_review_ui import pc_server


def main(exe=None):
    if exe:
        from test_reports_labels_bundle import frozen_server
        server = frozen_server(exe.resolve())
    else:
        server = pc_server()
    with server as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path)
        ctx = browser.new_context(service_workers='block')
        ctx.route('**/*', lambda route: route.continue_() if route.request.url.startswith(url) else route.abort())
        try:
            api(ctx, url, 'setup', {'username': 'unload_admin', 'password': 'unload-settings-test-123', 'display_name': '가상 배차 담당'})
            page = ctx.new_page()
            page.goto(url + '#/settings/basic')
            card = page.locator('.card').filter(has=page.get_by_role('heading', name='노선 계산', exact=True))
            mode = card.get_by_label('수거 중 하차 방식', exact=True)
            limit = card.get_by_label('차량당 수거 중 최대 하차 횟수', exact=True)
            button = card.get_by_role('button', name='저장', exact=True)
            expect(mode).to_have_value('auto')
            expect(limit).to_be_hidden()
            expect(card.get_by_text('최종 하차는 중간 하차 한도와 별도로 계산합니다.', exact=False)).to_be_visible()
            posts = []
            page.on('request', lambda req: posts.append(req.post_data_json['max_unloads'])
                    if req.method == 'POST' and req.url == url + 'api/settings' else None)

            def save(expected):
                with page.expect_response(lambda res: res.request.method == 'POST' and res.url == url + 'api/settings') as response:
                    button.click()
                assert response.value.ok
                assert api(ctx, url, 'bootstrap')['settings']['max_unloads'] == expected
                page.reload()
                expect(mode).to_have_value('auto' if expected == 'auto' else 'manual')
                if expected != 'auto':
                    expect(limit).to_have_value(str(expected))
                assert posts[-1] == expected

            mode.select_option('manual')
            for invalid in ('', '-1', '6', '1.5'):
                limit.fill(invalid)
                button.click()
                expect(card.get_by_text('중간 하차 한도는 0~5 사이의 정수로 입력하세요.', exact=True)).to_be_visible()
                assert not posts
            limit.fill('0')
            save(0)
            limit.fill('5')
            save(5)
            mode.select_option('auto')
            save('auto')
            expect(limit).to_be_hidden()
            api(ctx, url, 'settings', {'max_unloads': 2})
            page.reload()
            expect(mode).to_have_value('manual')
            expect(limit).to_have_value('2')
            save(2)
            print('수거 중 자동 하차 기본값, 차량당 직접 한도 0·5회, 허용 범위, 자동 전환, 기존 2회 유지')
        finally:
            ctx.close()
            browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path)
    main(parser.parse_args().exe)
