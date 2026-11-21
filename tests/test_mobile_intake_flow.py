"""휴대폰 폭의 현장접수에서 필수 입력, 오류 초점, 품목 왕복을 실제 원본 화면으로 확인한다."""
import os

from playwright.sync_api import expect, sync_playwright

from test_review_ui import mobile_server
from test_workflow_mobile import open_mobile, seed


def check(browser, url, width, *, large=False):
    context, page = open_mobile(browser, url)
    try:
        page.set_viewport_size({'width': width, 'height': 844})
        seed(page, {'mode': 'PC'})
        if large:
            page.evaluate("""async () => {
              const C = await import('./core.js'); C.st.textSize='xl'; C.applyTextSize();
              await C.setPrefs({theme:'dark',hc:true});
            }""")
        page.goto(url + '#/intake')
        pick = page.get_by_role('button', name='+ 품목 담기', exact=True)
        save = page.get_by_role('button', name='접수 저장', exact=True)
        expect(save).to_be_visible()
        for action in (pick, save):
            box = action.bounding_box()
            assert box and 0 <= box['y'] and box['y'] + box['height'] <= 844, '첫 화면에서 품목과 저장 버튼을 사용할 수 있어야 한다'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        for label, message, value in [('이름', '이름을 적어', '가상 확인'), ('연락처', '연락처를 확인', '01000000000'),
                                      ('배출 주소', '배출 주소를 적어', '광주 동구 금남로 123')]:
            target = page.get_by_label(label, exact=True)
            save.click()
            expect(target).to_be_focused()
            expect(target).to_have_attribute('aria-invalid', 'true')
            expect(page.get_by_role('alert')).to_contain_text(message)
            target.fill(value)
            expect(target).not_to_have_attribute('aria-invalid', 'true')
        planned = page.get_by_label('수거 예정일', exact=True)
        day = planned.input_value()
        planned.fill('')
        save.click()
        expect(planned).to_be_focused()
        planned.fill(day)
        save.click()
        expect(pick).to_be_focused()
        expect(pick).to_have_attribute('aria-invalid', 'true')
        pick.click()
        page.get_by_role('searchbox', name='품목 찾기', exact=True).fill('사무용의자')
        page.locator('.pick-add').first.click()
        page.get_by_role('button', name='완료', exact=True).click()
        expect(page.get_by_role('button', name='+ 품목 더 담기', exact=True)).to_be_visible()
        expect(page.locator('#intake-items')).to_contain_text('3,000원')
        expect(page.get_by_label('이름', exact=True)).to_have_value('가상 확인')
        expect(page.get_by_label('수거 예정일', exact=True)).to_have_value(day)
        save.click()
        page.wait_for_url('**/#/stop/**')
        expect(page.locator('.who')).to_contain_text('가상 확인')
        print(f'{width}px {"큰 글씨·고대비" if large else "기본"} · 필수 입력 초점·최초 화면 품목 담기·왕복·현장접수 저장 통과', flush=True)
    finally:
        context.close()


if __name__ == '__main__':
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path)
        try:
            check(browser, url, 390)
            check(browser, url, 360, large=True)
            check(browser, url, 768)
        finally:
            browser.close()
