"""실제 PIN/IndexedDB: 목록 분류·동 전환·안전한 순서 변경·목록 바로 보기."""
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_review_ui import mobile_server
from test_workflow_mobile import daylist, event, open_mobile, seed, stop


def main():
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-list-filters-')) / 'shots'
    output.mkdir(parents=True, exist_ok=True)
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        context, page = open_mobile(browser, url)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            dates = seed(page)
            today, tomorrow = dates['today'], dates['tomorrow']
            records = [stop(i, today) for i in range(1, 6)]
            records[3]['status'] = 'CANCELLED'
            events = [event(1, 'ITEM_RESULT', line_id='line-1', qty_confirmed=1, qty_collected=1),
                      event(2, 'ITEM_RESULT', line_id='line-2', qty_confirmed=1, qty_collected=1),
                      event(2, 'ATTEMPT', outcome='DONE'),
                      event(3, 'ITEM_RESULT', line_id='line-3', qty_confirmed=1, qty_collected=1),
                      event(3, 'ATTEMPT', outcome='DONE'),
                      event(3, 'ITEM_RESULT', line_id='line-3', qty_confirmed=2, qty_collected=2),
                      event(5, 'ITEM_RESULT', line_id='line-5', qty_confirmed=1, qty_collected=1),
                      event(5, 'ATTEMPT', outcome='ON_HOLD')]
            for mode in ('PC', 'SOLO'):
                patch = {'mode': mode, 'events': events}
                patch.update({'daylist': daylist(today, records)} if mode == 'PC' else {'requests': records})
                seed(page, patch, registered=mode == 'PC')
                before = page.evaluate("import('./core.js').then(C=>JSON.stringify(C.st))")
                expect(page.get_by_role('progressbar')).to_have_attribute('aria-valuenow', '1')
                expect(page.get_by_role('progressbar')).to_have_attribute('aria-valuemax', '4')
                label = '남음 3' if mode == 'PC' else '미처리 3'
                page.get_by_role('button', name=label, exact=True).click()
                expect(page.locator('.stops .stop')).to_have_count(3)
                expect(page.get_by_label('목록 조회 결과', exact=True)).to_contain_text('3건')
                expect(page.locator('.stops .stop').first).to_have_attribute('href', '#/stop/workflow-1')
                expect(page.locator('.stop[href="#/stop/workflow-5"] .stop-state')).to_have_text('보류')
                page.get_by_role('button', name='수거 목록 바로 보기', exact=True).click()
                assert page.locator('#collection-list').evaluate('e=>document.activeElement===e')
                title_y = page.locator('#collection-list').bounding_box()['y']
                bar = page.locator('main > .bar').bounding_box()
                assert bar['y'] + bar['height'] <= title_y < 160, '목록 제목이 고정 헤더에 가리지 않는다'
                page.get_by_role('button', name='날짜·접수 찾기', exact=True).click()
                page.get_by_label('찾기 처리 상태', exact=True).select_option('OPEN')
                expect(page.locator('.stops .stop')).to_have_count(3)
                page.get_by_label('찾기 처리 상태', exact=True).select_option('DONE')
                expect(page.locator('.stops .stop')).to_have_count(1)
                expect(page.locator('.stops .stop')).to_have_attribute('href', '#/stop/workflow-2')
                assert page.evaluate("import('./core.js').then(C=>JSON.stringify(C.st))") == before
                print(f'PASS: {mode} 수량만입력/결과재확인/취소의 완료·미처리 분류, 목록바로보기, 찾기 일치, 업무자료 불변', flush=True)

            inactive = [stop(6, today, removed=True), stop(7, today, status='CANCELLED'),
                        stop(8, today, status='DONE', pii_purged=True, name='', address='', phone=''),
                        stop(9, today, status='DONE')]
            seed(page, {'daylist': daylist(today, inactive), 'events': [
                event(6, 'CONTACT', result='CALLBACK'), event(7, 'CONTACT', result='CALLBACK'),
                event(9, 'CONTACT', result='CALLBACK')]})
            page.get_by_role('button', name='남음 0', exact=True).click()
            expect(page.locator('.stops .stop')).to_have_count(0)
            expect(page.get_by_role('button', name='재방문·보류 0', exact=True)).to_be_visible()
            page.get_by_role('button', name='재방문·보류 0', exact=True).click()
            expect(page.locator('.stops .stop')).to_have_count(0)
            page.get_by_role('button', name='날짜·접수 찾기', exact=True).click()
            page.get_by_label('찾기 처리 상태', exact=True).select_option('DONE')
            expect(page.locator('.stops .stop')).to_have_count(2)
            print('PASS: PC 제외/취소+재연락은 남음·보류에서 제외, 정리된 개인정보/기존 완료 상태 보존', flush=True)

            records = [stop(1, today, dong='가상A동'), stop(2, today, dong='가상B동'),
                       stop(3, today, dong='가상A동'), stop(4, tomorrow, dong='가상B동')]
            seed(page, {'mode': 'SOLO', 'requests': records}, registered=False)
            before = page.evaluate("import('./core.js').then(C=>JSON.stringify(C.st))")
            page.get_by_role('button', name='방문 순서 바꾸기', exact=True).click()
            expect(page.get_by_role('button', name='아래로', exact=True)).to_have_count(3)
            page.get_by_role('button', name='가상A동 2', exact=True).click()
            expect(page.locator('.stops .stop')).to_have_count(2)
            expect(page.get_by_role('button', name='아래로', exact=True)).to_have_count(0)
            expect(page.get_by_role('button', name='순서 정하기 끝', exact=True)).to_have_count(0)
            page.get_by_role('button', name='내일 1', exact=True).click()
            expect(page.locator('.stops .stop')).to_have_count(1)
            expect(page.locator('.stops .stop')).to_have_attribute('href', '#/stop/workflow-4')
            page.get_by_role('button', name='오늘 3', exact=True).click()
            search = page.get_by_label('목록에서 찾기', exact=True)
            search.fill('가상-1')
            expect(page.locator('.stops .stop')).to_have_count(1)
            expect(page.get_by_role('button', name='아래로', exact=True)).to_have_count(0)
            assert page.evaluate("import('./core.js').then(C=>JSON.stringify(C.st))") == before
            assert not errors, errors
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / 'solo-filtered-list.png'), full_page=True)
            print('PASS: SOLO 동조건 정규화후 탭전환, 동/검색중 순서변경 비활성, 접수순서 불변', flush=True)
        except BaseException:
            page.screenshot(path=str(output / 'filters-failure.png'), full_page=True)
            raise
        finally:
            context.close()
            browser.close()


if __name__ == '__main__':
    main()
