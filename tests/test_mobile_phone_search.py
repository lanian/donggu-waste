"""실제 PIN/암호화 IndexedDB: 연락처 검색과 기존 날짜·상태·초성 조건 보존."""
import datetime as dt
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_review_ui import mobile_server
from test_workflow_mobile import daylist, open_mobile, seed, stop


def main():
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-phone-search-')) / 'shots'
    output.mkdir(parents=True, exist_ok=True)
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        context, page = open_mobile(browser, url)
        errors, outbound = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: outbound.append(request.url))
        try:
            today = page.evaluate("import('./core.js').then(C=>C.todayKST())")
            old = (dt.date.fromisoformat(today) - dt.timedelta(days=5)).isoformat()
            records = [stop(1, today, receipt_no='PHONE-FIRST', name='가상 김수거', phone='01011112233'),
                       stop(2, today, receipt_no='PHONE-SECOND', name='가상 이접수', phone='010-4444-5566'),
                       stop(3, today, receipt_no='PHONE-PURGED', name='', address='', phone='01099998877', pii_purged=True),
                       stop(4, today, receipt_no='PHONE-LANDLINE', phone='0212345678')]
            for mode in ('PC', 'SOLO'):
                patch = {'mode': mode, 'requests': records} if mode == 'SOLO' else {'daylist': daylist(today, records)}
                seed(page, patch, registered=mode == 'PC')
                before = page.evaluate("import('./core.js').then(C=>JSON.stringify([C.st.requests,C.st.daylist,C.st.events]))")
                search = page.get_by_label('목록에서 찾기', exact=True)
                for query, number in [('01011112233', 1), ('010-1111-2233', 1), ('2233', 1), ('ㄱㅅ 2233', 1),
                                      ('01044445566', 2), ('010-4444-5566', 2), ('010 4444 5566', 2), ('02-1234-5678', 4)]:
                    search.fill(query)
                    expect(page.locator('.stops .stop')).to_have_count(1)
                    expect(page.locator('.stops .stop')).to_have_attribute('href', f'#/stop/workflow-{number}')
                search.fill('2233 없는단어')
                expect(page.locator('.stops .stop')).to_have_count(0)
                search.fill('8877')
                expect(page.locator('.stops .stop')).to_have_count(0)
                search.fill('')
                expect(page.locator('.stops .stop')).to_have_count(4)
                page.get_by_role('button', name='날짜·접수 찾기', exact=True).click()
                finder = page.get_by_label('찾을 접수', exact=True)
                for query, count in [('01011112233', 1), ('010-4444-5566', 1), ('5566 가상', 1), ('8877', 0)]:
                    finder.fill(query)
                    expect(page.locator('.stops .stop')).to_have_count(count)
                finder.fill('2233')
                expect(page.locator('.stops .stop')).to_have_count(1)
                page.locator('.stops .stop').click()
                expect(page.get_by_role('button', name='PC 입력 도움', exact=True)).to_be_visible()
                page.get_by_role('button', name='← 목록', exact=True).click()
                expect(finder).to_have_value('2233')
                expect(page.locator('.stops .stop')).to_have_count(1)
                assert page.evaluate("import('./core.js').then(C=>JSON.stringify([C.st.requests,C.st.daylist,C.st.events]))") == before
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / f'phone-search-{mode.lower()}.png'), full_page=True)

            historical = [records[0], stop(5, old, receipt_no='PHONE-OLD', phone='01011112233', status='CANCELLED')]
            seed(page, {'mode': 'SOLO', 'requests': historical}, registered=False)
            page.get_by_role('button', name='날짜·접수 찾기', exact=True).click()
            page.get_by_label('찾을 접수', exact=True).fill('2233')
            expect(page.locator('.stops .stop')).to_have_count(2)
            page.get_by_label('찾기 처리 상태', exact=True).select_option('CANCELLED')
            expect(page.locator('.stops .stop')).to_have_count(1)
            expect(page.locator('.stops .stop')).to_have_attribute('href', '#/stop/workflow-5')
            page.get_by_label('찾기 날짜 범위', exact=True).select_option('DAY')
            page.get_by_label('찾기 예정일', exact=True).fill(today)
            expect(page.locator('.stops .stop')).to_have_count(0)
            page.get_by_label('찾기 예정일', exact=True).fill(old)
            expect(page.locator('.stops .stop')).to_have_count(1)
            assert not errors, errors
            for raw in ('01011112233', '010-4444-5566', '2233', '5566', '8877'):
                assert all(raw not in request for request in outbound), outbound
            raw_entries = page.evaluate("""async()=>{
                const S=await import('./store.js');
                const keys=await S.keysRaw();
                return JSON.stringify(await Promise.all(keys.map(async key=>[key,await S.getRaw(key)])));
            }""")
            for query in ('2233 없는단어', '5566 가상'):
                assert query not in raw_entries, '검색어를 평문 저장소에 남기지 않는다'
            print('PASS: PC·단독 연락처 원문/하이픈/뒷자리·초성 AND·정리된 개인정보 제외·날짜/상태·복귀 조건·업무 자료 불변·외부 전송 없음')
        except BaseException:
            page.screenshot(path=str(output / 'phone-search-failure.png'), full_page=True)
            raise
        finally:
            context.close()
            browser.close()


if __name__ == '__main__':
    main()
