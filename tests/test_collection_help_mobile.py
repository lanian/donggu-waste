"""실제 암호화 모바일 저장소에서 PC 입력 도움의 보기/복사를 확인한다."""
import os
from pathlib import Path
import tempfile
from playwright.sync_api import expect, sync_playwright
from test_review_ui import mobile_server
from test_workflow_mobile import open_mobile, seed, stop


def main():
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-collection-help-')) / 'shots'
    output.mkdir(parents=True, exist_ok=True)
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        context, page = open_mobile(browser, url)
        try:
            today = page.evaluate("import('./core.js').then(C=>C.todayKST())")
            request = stop(1, today, receipt_no='DG-2026-001234')
            request['items'][0].update(qty=3, prior_collected=1)
            seed(page, {'daylist': {'work_date': today, 'crew': '가상 전담팀', 'stops': [request]}, 'events': [
                {'request_id': request['request_id'], 'kind': 'ITEM_RESULT', 'body': {'line_id': 'line-1', 'qty_confirmed': 3, 'qty_collected': 2}},
                {'request_id': request['request_id'], 'kind': 'ATTEMPT', 'body': {'outcome': 'PARTIAL'}}]})
            page.goto(url + '#/find')
            page.get_by_label('찾을 접수', exact=True).fill(request['receipt_no'])
            page.locator('.stops .stop').click()
            opening = page.get_by_role('button', name='PC 입력 도움', exact=True)
            expect(opening).to_be_visible(timeout=10000)
            before = page.evaluate("import('./core.js').then(C=>JSON.stringify(C.st.events))")
            opening.click()
            dialog = page.get_by_role('main').filter(has=page.get_by_role('heading', name='PC 입력 도움', exact=True))
            expect(page).to_have_url(url + '#/pc-input/' + request['request_id'])
            expect(page.get_by_role('dialog')).to_have_count(0)
            assert page.evaluate("import('./core.js').then(C=>C.wakeRoute())"), 'PC 입력 도움을 펼친 동안 화면 켜짐을 유지한다'
            expect(dialog.get_by_label('PC에서 찾을 접수번호', exact=True)).to_have_text(request['receipt_no'])
            expect(dialog).to_contain_text('이번 수거 1개')
            expect(dialog).to_contain_text('이전 수거 1개')
            expect(dialog).to_contain_text('남음 1개')
            assert '가상 주민' not in dialog.inner_text() and '01000000000' not in dialog.inner_text()
            page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw new Error('fixture');}}})")
            dialog.get_by_role('button', name='전달 내용 복사', exact=True).click()
            text = dialog.get_by_label('PC 전달 내용', exact=True)
            expect(text).to_be_visible()
            assert 'DG-2026-001234' in text.input_value() and '이번 수거 1개' in text.input_value()
            assert page.evaluate("document.activeElement.selectionEnd-document.activeElement.selectionStart") == len(text.input_value())
            assert page.evaluate("() => document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(output / 'mobile-collection-help.png'), full_page=True)
            dialog.get_by_role('button', name='접수 상세로', exact=True).click()
            expect(opening).to_be_focused()
            page.get_by_role('button', name='← 목록', exact=True).click()
            expect(page.get_by_label('찾을 접수', exact=True)).to_have_value(request['receipt_no'])
            page.locator('.stops .stop').click()
            assert page.evaluate("import('./core.js').then(C=>JSON.stringify(C.st.events))") == before
            page.evaluate("async()=>{const C=await import('./core.js');C.st.events=[];await C.commit();await C.render();}")
            page.get_by_role('button', name='PC 입력 도움', exact=True).click()
            expect(dialog).to_contain_text('결과 미확정')
            dialog.get_by_role('button', name='접수 상세로', exact=True).click()
            marker = page.evaluate("""async()=>{const C=await import('./core.js');const stop=C.st.daylist.stops[0];
                stop.pc_collection_ver=2;stop.req_ver=2;await C.addEvent(stop,'ITEM_RESULT',{line_id:'line-1',qty_confirmed:3,qty_collected:2});
                return C.st.events.at(-1).body.pc_collection_ver;}""")
            assert marker == 2, '새 휴대폰 기록은 PC 수거 기준을 확인한 버전을 함께 보냄'
            print('모바일 PC 입력 도움: 390px·접수번호·이전/이번/남음 수량·미확정 구분·개인정보 없는 전달 내용·복사 대체 선택·이벤트 불변 통과')
        finally:
            context.close()
            browser.close()


if __name__ == '__main__':
    main()
