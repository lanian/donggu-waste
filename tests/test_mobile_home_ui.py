"""모바일 홈 도구·설정 우선순위: 실제 PIN/IndexedDB, 가상 접수만 사용."""
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright

from test_review_ui import mobile_server
from test_workflow_mobile import daylist, open_mobile, seed, stop


def assert_tools(page, names):
    tools = page.get_by_role('region', name='자주 쓰는 기능', exact=True)
    expect(tools).to_be_visible()
    expect(tools.get_by_role('button')).to_have_count(len(names))
    expect(page.locator('.next-action')).to_have_count(1)
    expect(page.locator('.next-action').get_by_role('button')).to_have_count(1)
    buttons = [tools.get_by_role('button', name=name, exact=True) for name in names]
    for button in buttons:
        expect(button).to_be_visible()
    rects = [button.bounding_box() for button in buttons]
    assert abs(rects[0]['y'] - rects[1]['y']) <= 1, '처음 두 도구가 같은 행에 나란히 표시된다'
    assert rects[1]['x'] >= rects[0]['x'] + rects[0]['width'], '두 도구의 터치 영역이 겹치지 않는다'
    assert abs(rects[0]['width'] - rects[1]['width']) <= 1, '도구의 너비가 동일하다'
    assert rects[2]['y'] >= rects[0]['y'] + rects[0]['height'], '다음 도구는 다음 행에 표시된다'
    assert all(rect['height'] >= 56 for rect in rects), '모든 도구가 현장용 터치 높이를 유지한다'
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), '홈에 가로 넘침이 없다'
    return tools


def main():
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-mobile-home-ui-'))
    shots = output / 'shots'
    shots.mkdir(parents=True, exist_ok=True)
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        context, page = open_mobile(browser, url)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            dates = seed(page)
            today = dates['today']
            record = stop(1, today)
            pc_names = ['오늘 목록 받기', '결과 보내기', '+ 접수 등록 (현장·전화)', '날짜·접수 찾기', '빠른 수수료 계산']
            solo_names = ['+ 전화 접수 등록', '날짜·접수 찾기', '빠른 수수료 계산', '백업·복원', '하차 기록']
            assert_tools(page, pc_names)
            for width in (360, 390, 430):
                page.set_viewport_size({'width': width, 'height': 844})
                for text_size in ('', 'xl'):
                    for mode in ('PC', 'SOLO'):
                        patch = {'mode': mode, 'textSize': text_size}
                        patch.update({'daylist': daylist(today, [record])} if mode == 'PC' else {'requests': [record]})
                        seed(page, patch, registered=mode == 'PC')
                        page.evaluate("async()=>{const C=await import('./core.js');C.applyTextSize();await C.render();}")
                        assert_tools(page, pc_names if mode == 'PC' else solo_names)
                        if width in (360, 430) and text_size == 'xl':
                            page.screenshot(path=str(shots / f'home-{mode.lower()}-{width}-large.png'), full_page=True)
            seed(page, {'daylist': daylist(today, [record])})
            tools = assert_tools(page, pc_names)
            tools.get_by_role('button', name='빠른 수수료 계산', exact=True).click()
            expect(page).to_have_url(url + '#/calculator')
            page.goto(url + '#/home')
            page.get_by_role('region', name='자주 쓰는 기능', exact=True).get_by_role('button', name='날짜·접수 찾기', exact=True).click()
            expect(page.get_by_role('heading', name='날짜·접수 찾기', exact=True)).to_be_visible()
            page.goto(url + '#/home')
            page.get_by_role('region', name='자주 쓰는 기능', exact=True).get_by_role('button', name='+ 접수 등록 (현장·전화)', exact=True).click()
            expect(page).to_have_url(url + '#/intake')
            page.goto(url + '#/settings')
            toggle = page.get_by_role('switch', name='자동 잠금', exact=True)
            expect(toggle).to_be_visible()
            headings = page.locator('main > section.card > h2').all_text_contents()
            assert headings[:3] == ['화면', '잠금', '작업자'], headings
            assert headings.index('작업자') < headings.index('도움말') < headings.index('현장 기록'), headings
            page.get_by_label('작업자 1 이름', exact=True).fill('가상 임시수정')
            toggle.click()
            expect(toggle).not_to_be_checked()
            expect(toggle).to_be_enabled()
            expect(page.get_by_label('작업자 1 이름', exact=True)).to_have_value('가상 임시수정')
            page.screenshot(path=str(shots / 'settings-common-first.png'), full_page=True)
            assert not errors, errors
            print('모바일 홈/UI: PC·단독 360/390/430px 보통·아주큰글씨 도구2열/동작/유일한주행동·설정화면/잠금/작업자우선·설정초안보존 통과')
        finally:
            context.close()
            browser.close()


if __name__ == '__main__':
    main()
