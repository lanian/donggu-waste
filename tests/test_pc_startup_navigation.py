"""초기 화면 로딩 중 빠른 이동: 오래된 성공·실패와 현재 시작 안내를 분리한다.

실행: python tests/test_pc_startup_navigation.py
사용자 서버가 아닌 임시 SQLCipher 서버와 가상 계정만 사용한다.
"""
from __future__ import annotations

import os

from playwright.sync_api import expect, sync_playwright

from test_review_ui import pc_server

passed = 0


def ok(condition, message):
    global passed
    if not condition:
        raise AssertionError('실패: ' + message)
    passed += 1
    print('  ✓', message, flush=True)


def flush_browser(page):
    page.evaluate('() => new Promise(resolve => setTimeout(resolve, 0))')


def run_suite(browser):
    with pc_server() as url:
        for scenario in ('obsolete_success', 'obsolete_error'):
            context = browser.new_context(service_workers='block')
            context.route('https://**/*', lambda route: route.abort())
            home_routes, stats_routes, errors = [], [], []
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            context.route('**/screens/home.js', lambda route: home_routes.append(route))
            context.route('**/screens/stats.js', lambda route: stats_routes.append(route))
            try:
                response = context.request.post(url + ('api/setup' if scenario == 'obsolete_success' else 'api/login'),
                    headers={'X-DG-CSRF': '1'}, data={'username': 'startup_nav',
                    'display_name': '가상 시작이동', 'password': 'startup-nav-pass-123'})
                assert response.ok, response.text()
                page.clock.install()
                with page.expect_request('**/screens/home.js'):
                    page.goto(url, wait_until='domcontentloaded')
                # state/bootstrap의 실제 수신 뒤 home 모듈만 멈춘 상태에서 다른 화면으로 이동한다.
                if scenario == 'obsolete_error':
                    page.clock.fast_forward(5000)
                with page.expect_request('**/screens/stats.js'):
                    page.evaluate("location.hash = '#/stats'")
                context.unroute('**/screens/home.js')
                if scenario == 'obsolete_success':
                    for route in home_routes:
                        route.continue_()
                    home_routes.clear()
                    page.evaluate("async () => { await import('/screens/home.js'); }")
                    flush_browser(page)
                    page.clock.fast_forward(16000)
                    expect(page.locator('#startup-title')).to_have_text('PC 업무 화면을 불러오지 못했습니다.')
                    expect(page.get_by_role('button', name='화면 다시 불러오기', exact=True)).to_be_visible()
                    ok(not errors, '이전 initial render가 끝나도 현재 모듈 timeout은 시작 오류·복구 버튼으로 표시')
                    context.unroute('**/screens/stats.js')
                    for route in stats_routes:
                        route.continue_()
                    stats_routes.clear()
                    page.evaluate("async () => { await import('/screens/stats.js'); }")
                    flush_browser(page)
                    expect(page.locator('#startup-title')).to_have_text('PC 업무 화면을 불러오지 못했습니다.')
                    ok(not errors, '시간이 지난 현재 모듈의 늦은 완료는 오류 안내를 자동으로 덮지 않음')
                    with page.expect_navigation(wait_until='domcontentloaded'):
                        page.get_by_role('button', name='화면 다시 불러오기', exact=True).click()
                    expect(page.get_by_role('heading', name='통계·보고서', exact=True)).to_be_visible()
                    ok(not errors and page.locator('#startup-card').count() == 0,
                       '사용자가 다시 불러오면 원래 선택한 통계 화면으로 정상 복구')
                else:
                    with page.expect_event('requestfailed', predicate=lambda request: request.url.endswith('/screens/home.js')):
                        for route in home_routes:
                            route.abort('failed')
                    home_routes.clear()
                    flush_browser(page)
                    expect(page.locator('#startup-title')).to_have_text('업무 화면을 준비하고 있습니다')
                    expect(page.locator('#startup-retry')).to_be_hidden()
                    ok(not errors, '오래된 모듈 오류는 현재 시작 안내를 실패로 바꾸지 않음')
                    before = page.locator('#startup-elapsed').inner_text()
                    page.clock.fast_forward(1000)
                    ok(page.locator('#startup-elapsed').inner_text() != before,
                       '오래된 render 반환 후에도 현재 시작 화면의 경과·watchdog 유지')
                    context.unroute('**/screens/stats.js')
                    for route in stats_routes:
                        route.continue_()
                    stats_routes.clear()
                    expect(page.get_by_role('heading', name='통계·보고서', exact=True)).to_be_visible()
                    page.evaluate("location.hash = '#/requests?quick=NODATE'")
                    expect(page.locator('.qchip.on')).to_contain_text('예정일 없음')
                    ok(not errors and page.locator('#startup-card').count() == 0,
                       '현재 화면 완료 뒤 빠른 접수 이동도 정상이고 오래된 오류가 뒤늦게 표시되지 않음')
            finally:
                for route in home_routes + stats_routes:
                    try:
                        route.abort()
                    except Exception:
                        pass
                context.close()


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        try:
            run_suite(browser)
        finally:
            browser.close()
    print(f'\n통과 {passed}개')


if __name__ == '__main__':
    main()
