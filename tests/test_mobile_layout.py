"""모바일 버튼: 실제 PIN/저장소·작은 화면·큰 글자·오류 안내·품목 선택 회귀."""
import argparse
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_review_ui import mobile_server
from test_workflow_mobile import open_mobile, seed, stop


def open_case(browser, url, solo=False):
    context, page = open_mobile(browser, url)
    page.emulate_media(reduced_motion='reduce')
    today = page.evaluate("import('./core.js').then(C=>C.todayKST())")
    receipt = stop(1, today)
    receipt['items'][0].update(qty=9999, prior_collected=1234, amount=9999000)
    state = {'mode': 'SOLO', 'requests': [receipt, stop(2, today)]} if solo else {
        'daylist': {'work_date': today, 'crew': '가상 수거팀', 'assign_ver': 1,
                    'stops': [receipt, stop(2, today)]}}
    seed(page, state, registered=False)
    page.goto(url + '#/stop/' + receipt['request_id'])
    expect(page.locator('.who .name')).to_have_text(receipt['name'])
    return context, page


def appearance(page, width, text, theme, hc=False):
    page.set_viewport_size({'width': width, 'height': 844})
    page.evaluate("""async ({text, theme, hc}) => {
        const C = await import('./core.js');
        C.st.textSize = text;
        C.applyTextSize();
        await C.setPrefs({theme, hc});
        await C.render();
    }""", {'text': text, 'theme': theme, 'hc': hc})


def metrics(page):
    return page.evaluate("""() => {
        const box = e => { const r=e.getBoundingClientRect(); return {left:r.left, right:r.right, top:r.top,
            bottom:r.bottom, width:r.width, height:r.height, text:e.textContent.trim(),
            clipped:e.scrollWidth>e.clientWidth+1 || e.scrollHeight>e.clientHeight+1}; };
        return {viewport:innerWidth, page:document.documentElement.scrollWidth,
            font:getComputedStyle(document.documentElement).fontSize,
            dock:[...document.querySelectorAll('.dock button')].map(box),
            quantities:[...document.querySelectorAll('.line .stepper')].map(e=>({
                ...box(e), row:box(e.parentElement), controls:[...e.querySelectorAll('button')].map(box),
                value:box(e.querySelector('.val'))}))};
    }""")


def assert_layout(data):
    assert data['page'] <= data['viewport'] + 1, f'가로 화면 넘침: {data}'
    for button in data['dock']:
        assert button['width'] >= 48 and button['height'] >= 48, f'작은 하단 터치 영역: {button}'
        assert button['left'] >= -1 and button['right'] <= data['viewport'] + 1, f'하단 버튼 화면 밖: {button}'
        assert not button['clipped'], f'하단 버튼 글자 잘림: {button}'
    for quantity in data['quantities']:
        assert quantity['left'] >= quantity['row']['left'] - 1, f'수량 행 왼쪽 넘침: {quantity}'
        assert quantity['right'] <= quantity['row']['right'] + 1, f'수량 행 오른쪽 넘침: {quantity}'
        assert not quantity['value']['clipped'], f'수량 숫자 잘림: {quantity}'
        for button in quantity['controls']:
            assert button['width'] >= 48 and button['height'] >= 48, f'작은 수량 터치 영역: {button}'


def contrast(page, selector, focus=False):
    return page.locator(selector).evaluate("""(e, focus) => {
        const s=getComputedStyle(e);
        const lum=v=>{const c=v.match(/[\\d.]+/g).slice(0,3).map(Number).map(x=>{
            x/=255;return x<=.04045?x/12.92:((x+.055)/1.055)**2.4;});
            return .2126*c[0]+.7152*c[1]+.0722*c[2];};
        const foreground=focus?s.outlineColor:s.color;
        const background=focus?getComputedStyle(document.body).backgroundColor:s.backgroundColor;
        const f=lum(foreground), b=lum(background);
        return {text:e.textContent.trim(),foreground,background,
            ratio:(Math.max(f,b)+.05)/(Math.min(f,b)+.05)};
    }""", focus)


def collection_case(browser, url, shots):
    context, page = open_case(browser, url)
    try:
        jump = page.locator('.dock-next')
        expect(jump).to_have_text('수거 결과 선택 ↓')
        before = page.evaluate("import('./core.js').then(C=>JSON.stringify(C.st.events))")
        variants = [(width, text, theme, hc) for width in (360, 390, 430)
                    for text in ('', 'xl') for theme, hc in (('light', False), ('dark', False), ('light', True))]
        for width, text, theme, hc in variants:
            appearance(page, width, text, theme, hc)
            data = metrics(page)
            assert data['font'] == ('19px' if text else '15px'), data
            assert_layout(data)
            page.locator('.dock-next').click()
            clearance = page.evaluate("""() => ({
                result:document.querySelector('#st-result h2').getBoundingClientRect().top,
                steps:document.querySelector('.steps').getBoundingClientRect().bottom})""")
            assert clearance['result'] >= clearance['steps'] - 1, f'결과 제목이 단계 막대에 가려짐: {clearance}'
            if text == 'xl' and theme == 'dark':
                page.screenshot(path=str(shots / f'collection-{width}-large-dark.png'))
        appearance(page, 360, 'xl', 'light')
        page.evaluate("""() => {
            document.documentElement.style.setProperty('--safe-top', '24px');
            document.documentElement.style.setProperty('--safe-bottom', '24px');
        }""")
        page.locator('.dock-next').click()
        inset = page.evaluate("""() => ({
            back:document.querySelector('main > .bar button').getBoundingClientRect().top,
            primary:document.querySelector('.dock-next').getBoundingClientRect().bottom,
            result:document.querySelector('#st-result h2').getBoundingClientRect().top,
            steps:document.querySelector('.steps').getBoundingClientRect().bottom})""")
        assert inset['back'] >= 23 and inset['primary'] <= 821, f'안전 영역 안쪽 버튼 배치 부족: {inset}'
        assert inset['result'] >= inset['steps'] - 1, f'안전 영역에서 결과 제목 가려짐: {inset}'
        page.screenshot(path=str(shots / 'collection-safe-area.png'))
        assert page.evaluate("import('./core.js').then(C=>JSON.stringify(C.st.events))") == before, '결과 영역 이동만으로 업무 기록 생성'
        print('모바일 배치: 360/390/430·보통/아주 크게·밝게/어둡게/고대비18종·수량/하단 버튼·기록 없는 결과 선택 이동 통과', flush=True)
    except Exception:
        page.screenshot(path=str(shots / 'collection-failure.png'), full_page=True)
        raise
    finally:
        context.close()


def error_case(browser, url, shots):
    context, page = open_case(browser, url)
    try:
        appearance(page, 360, 'xl', 'dark')
        result = page.evaluate("""async () => {
            window.__layoutOriginalPut = IDBObjectStore.prototype.put;
            IDBObjectStore.prototype.put = function(value, key) {
                if (key === 's:state') throw new DOMException('test quota', 'QuotaExceededError');
                return window.__layoutOriginalPut.apply(this, arguments);
            };
            return (await import('./core.js')).commit();
        }""")
        assert result is False, '실제 저장 실패가 재현되어야 함'
        banner = page.get_by_role('alert').filter(has_text='앱을 닫지 말고').last
        expect(banner).to_be_visible()
        data = page.evaluate("""() => {
            const e=document.querySelector('#save-fail'), s=getComputedStyle(e), b=e.getBoundingClientRect();
            const rgb = v => v.match(/[\\d.]+/g).slice(0,3).map(Number).map(x=>{
                x/=255;return x<=.04045?x/12.92:((x+.055)/1.055)**2.4;});
            const lum=v=>{const c=rgb(v);return .2126*c[0]+.7152*c[1]+.0722*c[2];};
            const f=lum(s.color), bg=lum(s.backgroundColor);
            const overlap = x => {const r=x.getBoundingClientRect();return Math.max(0,Math.min(r.right,b.right)-Math.max(r.left,b.left))*Math.max(0,Math.min(r.bottom,b.bottom)-Math.max(r.top,b.top));};
            return {foreground:s.color,background:s.backgroundColor,contrast:(Math.max(f,bg)+.05)/(Math.min(f,bg)+.05),
                covered:[...document.querySelectorAll('main > .bar button,.steps a,.dock button')].filter(x=>overlap(x)>1).map(x=>x.textContent),
                page:document.documentElement.scrollWidth,viewport:innerWidth};
        }""")
        assert data['contrast'] >= 4.5, f'어두운 테마 저장 오류 글자 대비 부족: {data}'
        assert not data['covered'], f'저장 오류가 필수 이동 버튼을 가림: {data}'
        assert data['page'] <= data['viewport'] + 1, data
        page.screenshot(path=str(shots / 'error-large-dark.png'))
        page.locator('main > .bar').get_by_role('button', name='← 목록', exact=True).click()
        expect(page).to_have_url(url + '#/home')
        expect(banner).to_be_visible()
        print('모바일 오류: 실제 저장 실패·어두운 테마 대비·상단 복귀/단계/하단 버튼 가림 없음·경고 유지 통과', flush=True)
    except Exception:
        page.screenshot(path=str(shots / 'error-failure.png'), full_page=True)
        raise
    finally:
        context.close()


def picker_case(browser, url, shots):
    context, page = open_case(browser, url, solo=True)
    try:
        appearance(page, 360, 'xl', 'dark')
        page.get_by_role('button', name='+ 현장 품목 추가', exact=True).click()
        expect(page.get_by_label('품목 찾기', exact=True)).to_be_visible()
        tabs = page.get_by_role('group', name='품목 분류', exact=True).get_by_role('button')
        for index in range(tabs.count()):
            box = tabs.nth(index).bounding_box()
            assert box['width'] >= 48 and box['height'] >= 48, f'품목 분류 터치 영역 부족: {box}'
        page.get_by_label('품목 찾기', exact=True).fill('사무용의자')
        star = page.locator('.pick-star').first
        expect(star).to_be_visible()
        box = star.bounding_box()
        assert box['width'] >= 48 and box['height'] >= 48, f'즐겨찾기 터치 영역 부족: {box}'
        page.screenshot(path=str(shots / 'picker-results-large-dark.png'))
        page.get_by_role('button', name='검색어 지우기', exact=True).click()
        tab = tabs.first
        tab.focus()
        page.keyboard.press('Shift+Tab')
        page.keyboard.press('Tab')
        focus = tab.evaluate("""e => {
            const s=getComputedStyle(e), r=e.getBoundingClientRect(), p=e.parentElement.getBoundingClientRect();
            return {active:document.activeElement===e, outline:parseFloat(s.outlineWidth),
                offset:parseFloat(s.outlineOffset),
                top:r.top,bottom:r.bottom,parentTop:p.top,parentBottom:p.bottom};
        }""")
        assert focus['active'] and focus['outline'] >= 2, f'분류 키보드 포커스 없음: {focus}'
        edge = focus['outline'] + focus['offset']
        assert focus['top'] - edge >= focus['parentTop'] - 1, f'분류 포커스 상단 잘림: {focus}'
        assert focus['bottom'] + edge <= focus['parentBottom'] + 1, f'분류 포커스 하단 잘림: {focus}'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), '품목 화면 가로 넘침'
        page.screenshot(path=str(shots / 'picker-large-dark.png'))
        print('모바일 품목 선택: 큰 글자·분류/즐겨찾기 터치 영역·키보드 포커스·가로 넘침 없음 통과', flush=True)
    except Exception:
        page.screenshot(path=str(shots / 'picker-failure.png'), full_page=True)
        raise
    finally:
        context.close()


def auto_hc_case(browser, url, shots):
    context, page = open_case(browser, url, solo=True)
    try:
        page.emulate_media(color_scheme='dark', reduced_motion='reduce')
        appearance(page, 360, 'xl', 'auto', hc=True)
        page.get_by_role('button', name='+ 현장 품목 추가', exact=True).click()
        expect(page.get_by_role('group', name='품목 분류', exact=True)).to_be_visible()
        surfaces = {'category': contrast(page, '.pick-tabs .on'),
                    'primary': contrast(page, '.pick-foot .primary')}
        category = page.locator('.pick-tabs .on')
        category.focus()
        page.keyboard.press('Shift+Tab')
        page.keyboard.press('Tab')
        expect(category).to_be_focused()
        surfaces['focus'] = contrast(page, '.pick-tabs .on', focus=True)
        page.screenshot(path=str(shots / 'picker-auto-dark-hc.png'))
        page.get_by_role('button', name='← 취소', exact=True).click()
        expect(page.locator('.dock-next')).to_be_visible()
        saved = page.evaluate("""async () => {
            const put = IDBObjectStore.prototype.put;
            IDBObjectStore.prototype.put = function(value, key) {
                if (key === 's:state') throw new DOMException('test quota', 'QuotaExceededError');
                return put.apply(this, arguments);
            };
            return (await import('./core.js')).commit();
        }""")
        assert saved is False, '실제 저장 실패가 재현되어야 함'
        expect(page.locator('#save-fail')).to_be_visible()
        surfaces['error'] = contrast(page, '#save-fail')
        page.screenshot(path=str(shots / 'error-auto-dark-hc.png'))
        assert all(surface['ratio'] >= 4.5 for surface in surfaces.values()), f'자동 테마·OS 어둡게·고대비 글자 대비 부족: {surfaces}'
        print('모바일 자동 테마: OS 어둡게+고대비의 선택 분류·주 버튼·실제 저장 오류 대비 통과', flush=True)
    except Exception:
        page.screenshot(path=str(shots / 'auto-hc-failure.png'), full_page=True)
        raise
    finally:
        context.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=('all', 'collection', 'error', 'picker', 'auto-hc'), default='all')
    args = parser.parse_args()
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-mobile-layout-'))
    shots = output / 'shots'
    shots.mkdir(parents=True, exist_ok=True)
    failures = []
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        try:
            for name, case in [('collection', collection_case), ('error', error_case), ('picker', picker_case), ('auto-hc', auto_hc_case)]:
                if args.case not in ('all', name):
                    continue
                try:
                    case(browser, url, shots)
                except Exception as error:
                    failures.append({'case': name, 'error': str(error)})
                    print(name + ' FAIL: ' + str(error), flush=True)
        finally:
            browser.close()
    if failures:
        (output / 'failures.json').write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding='utf-8')
        raise AssertionError(f'{len(failures)} mobile layout cases failed')


if __name__ == '__main__':
    main()
