"""센터 차고지의 원본/배포 EXE 화면·초안·재시도·실제 노선 확인."""
import argparse
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
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-center-ui-'))
    (output / 'shots').mkdir(parents=True, exist_ok=True)
    with server as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000}, service_workers='block')
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        def api(path, body=None):
            options = {'headers': {'X-DG-CSRF': '1'}}
            reply = context.request.get(url + 'api/' + path, **options) if body is None else context.request.post(url + 'api/' + path, data=body, **options)
            assert reply.ok, (path, reply.status, reply.text())
            return reply.json()
        try:
            api('setup', {'username': 'center_admin', 'display_name': '가상 센터 담당', 'password': 'center-test-only-123'})
            initial = api('bootstrap')
            custom = {'label': '가상 기존 차고지', 'lat': 35.14, 'lon': 126.92}
            saved = {}
            for name in ('충장동', '동명동', '산수1동'):
                crew = api('crews', {'name': '가상 ' + name + ' 전담팀'})['id']
                dong = next(d for d in initial['dongs'] if d['name'] == name)
                api('dongs', {**dong, 'crew_id': crew, 'depot': custom if name == '동명동' else None})
                saved[name] = (dong['id'], crew)
            page.goto(url + '#/settings/dongs')
            bulk = page.get_by_role('button', name='행정복지센터 차고지 일괄 적용', exact=True)
            expect(bulk).to_be_visible(timeout=15000)
            cj = page.locator(f'tr[data-dong-id="{saved["충장동"][0]}"]')
            dm = page.locator(f'tr[data-dong-id="{saved["동명동"][0]}"]')
            cj.get_by_role('button', name='충장동 차고지 설정', exact=True).click()
            dialog = page.get_by_role('dialog', name='충장동 차고지 설정', exact=True)
            dialog.get_by_role('button', name='행정복지센터 위치 채우기', exact=True).click()
            expect(dialog.get_by_label('차고지 이름', exact=True)).to_have_value('충장동 행정복지센터')
            expect(dialog.get_by_label('차고지 위도', exact=True)).to_have_value('35.149141')
            dialog.get_by_role('button', name='취소', exact=True).click()
            assert next(d for d in api('dongs') if d['name'] == '충장동')['depot'] is None
            cj.get_by_role('button', name='충장동 차고지 설정', exact=True).click()
            dialog.get_by_role('button', name='행정복지센터 위치 채우기', exact=True).click()
            dialog.get_by_role('button', name='행에 적용', exact=True).click()
            dm.get_by_label('동명동 부과번호 코드', exact=True).fill('DM')
            bulk.click()
            expect(page.get_by_text('작성 중인 동 설정을 먼저 저장한 뒤 일괄 적용하세요.', exact=True)).to_be_visible()
            cj.get_by_role('button', name='저장', exact=True).click()
            expect(cj.get_by_label('충장동 차고지 현황', exact=True)).to_contain_text('충장동 행정복지센터')
            expect(dm.get_by_label('동명동 부과번호 코드', exact=True)).to_have_value('DM')
            dm.get_by_role('button', name='저장', exact=True).click()
            expect(page.get_by_text('작성 중인 동 설정을 먼저 저장한 뒤 일괄 적용하세요.', exact=True)).to_have_count(0)
            def lose_response(route):
                assert route.fetch().ok
                route.abort()
            page.route('**/api/dongs/centers', lose_response, times=1)
            bulk.click()
            retry = page.get_by_role('button', name='적용 결과 다시 확인', exact=True)
            expect(retry).to_be_enabled()
            expect(dm.get_by_role('button', name='저장', exact=True)).to_be_disabled()
            expect(dm.get_by_label('동명동 부과번호 코드', exact=True)).to_be_disabled()
            page.goto(url + '#/home')
            page.goto(url + '#/settings/dongs')
            expect(dm.get_by_role('button', name='저장', exact=True)).to_be_disabled()
            retry.click()
            sansu = page.locator(f'tr[data-dong-id="{saved["산수1동"][0]}"]')
            expect(sansu.get_by_label('산수1동 차고지 현황', exact=True)).to_contain_text('산수1동 행정복지센터')
            data = {d['name']: d for d in api('dongs')}
            assert data['동명동']['depot'] == custom and data['동명동']['code'] == 'DM'
            for name, (_, crew) in saved.items():
                assert data[name]['crew_id'] == crew
            # 서버 반영 후 응답을 보류한 채 화면을 다시 열어도 보호가 이어진다.
            api('dongs', {**data['산수1동'], 'depot': None})
            cj.get_by_label('충장동 부과번호 코드', exact=True).fill('H1')
            cj.get_by_role('button', name='저장', exact=True).click()
            expect(sansu.get_by_label('산수1동 차고지 현황', exact=True)).to_contain_text('공통 차고지 사용')
            held = []
            def hold_response(route):
                held.append((route, route.fetch()))
            page.route('**/api/dongs/centers', hold_response, times=1)
            bulk.click()
            expect(dm.get_by_role('button', name='저장', exact=True)).to_be_disabled()
            page.goto(url + '#/home')
            expect(page.get_by_role('heading', name='설정', exact=True)).to_have_count(0)
            page.goto(url + '#/settings/dongs/')
            expect(dm.get_by_role('button', name='저장', exact=True)).to_be_disabled()
            expect(bulk).to_be_disabled()
            assert len(held) == 1
            held[0][0].fulfill(response=held[0][1])
            expect(sansu.get_by_label('산수1동 차고지 현황', exact=True)).to_contain_text('산수1동 행정복지센터')
            expect(dm.get_by_role('button', name='저장', exact=True)).to_be_enabled()
            # POST 성공 뒤 목록 GET만 실패했다면 재확인은 GET만 수행한다.
            center_posts = []
            page.on('request', lambda request: center_posts.append(request) if request.url.endswith('/api/dongs/centers') else None)
            page.route('**/api/dongs', lambda route: route.abort(), times=1)
            bulk.click()
            expect(retry).to_be_enabled()
            expect(dm.get_by_label('동명동 부과번호 코드', exact=True)).to_be_disabled()
            assert len(center_posts) == 1
            retry.click()
            expect(dm.get_by_role('button', name='저장', exact=True)).to_be_enabled()
            assert len(center_posts) == 1
            orphan = api('dongs', {'name': '가상 삭제 동'})['id']
            cj.get_by_label('충장동 부과번호 코드', exact=True).fill('C1')
            cj.get_by_role('button', name='저장', exact=True).click()
            deleted = page.locator(f'tr[data-dong-id="{orphan}"]')
            deleted.get_by_label('가상 삭제 동 부과번호 코드', exact=True).fill('DEL')
            api('dongs/' + orphan + '/delete', {})
            cj.get_by_label('충장동 부과번호 코드', exact=True).fill('C2')
            cj.get_by_role('button', name='저장', exact=True).click()
            expect(deleted).to_have_count(0)
            with page.expect_response(lambda response: '/api/dongs/centers' in response.url):
                bulk.click()
            expect(page.get_by_text('작성 중인 동 설정을 먼저 저장한 뒤 일괄 적용하세요.', exact=True)).to_have_count(0)
            page.screenshot(path=str(output / 'shots' / 'center-depots.png'), full_page=True)
            assert not errors, errors
            print('센터 차고지 UI: 채우기/취소/저장·초안 보존·응답 유실/보류 중 화면 재진입·목록 재조회 실패 복구·삭제된 동 초안·기존 차고지/팀 보존 통과')
        finally:
            context.close(); browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe', type=Path)
    main(parser.parse_args().exe)
