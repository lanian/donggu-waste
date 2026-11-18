"""모바일 현장접수 품목 선택: 실제 PIN/IndexedDB·가상 접수·원본 ES 모듈."""
import argparse
import os
from pathlib import Path
import re

from playwright.sync_api import expect, sync_playwright
from test_review_ui import PIN, mobile_server
from test_mobile_intake_save import form


def open_picker(browser, url, mode='PC'):
    context, page, _ = form(browser, url, mode=mode)
    page.evaluate("""async()=>{
        const C=await import('./core.js');C.ui.form.items=[];C.st.feeUsage={};C.st.feeFavs=[];
        await C.render();
    }""")
    page.get_by_role('button', name='+ 품목 담기', exact=True).click()
    expect(page.get_by_role('heading', name='품목 담기', exact=True)).to_be_visible()
    return context, page


def lines(page):
    return page.evaluate("import('./core.js').then(C=>C.ui.form.items)")


def basket(page):
    expect(page.get_by_role('heading', name='품목 담기', exact=True)).to_be_visible()
    if not page.get_by_role('button', name='품목 검색으로 돌아가기', exact=True).count():
        page.get_by_role('button', name=re.compile(r'^담은 품목 .*종 확인$')).click(timeout=2000)


def quick_case(browser, url, shots):
    context, page = open_picker(browser, url)
    try:
        quick = page.get_by_role('group', name='빠른 품목 선택', exact=True)
        expect(quick).to_be_visible(timeout=2000)
        header = page.locator('.pick-head').bounding_box()
        status = page.locator('.pick-results-status').bounding_box()
        assert status['y'] >= header['y'] + header['height'] - 1, f'첫 안내가 고정 검색창 뒤에 가려짐: {header}, {status}'
        sofa = quick.get_by_role('button', name=re.compile(r'^소파'))
        expect(sofa).to_contain_text('응접세트')
        expect(quick.get_by_role('button', name=re.compile(r'^매트리스'))).to_contain_text('침대')
        page.screenshot(path=str(shots / 'after-start.png'))
        sofa.click()
        dialog = page.get_by_role('dialog', name='응접세트', exact=True)
        expect(dialog).to_be_visible()
        dialog.locator('.pick-add').first.click()
        dialog.get_by_role('button', name='완료', exact=True).click()
        assert len(lines(page)) == 1 and lines(page)[0]['fee_rate_id'], lines(page)
        page.get_by_role('button', name='가구', exact=True).click()
        expect(page.get_by_role('button', name=re.compile(r'^응접세트.*1개 담김'))).to_be_visible()
        print('PASS: 첫 사용 빠른 선택·친숙한 이름과 공식 품목명·규격 선택', flush=True)
    finally:
        context.close()


def search_case(browser, url, shots):
    context, page = open_picker(browser, url)
    try:
        query = page.get_by_role('searchbox', name='품목 찾기', exact=True)
        query.fill('침대')
        expected = page.evaluate("""async()=>{
            const {BUILTIN_FEES:T}=await import('./fees-data.js'),{searchFees}=await import('./fees.js');
            return searchFees(T,'침대').map(r=>({name:r.name,spec:r.spec}));
        }""")
        rows = page.locator('.pick-list > .pick-row')
        expect(rows).to_have_count(len(expected))
        for index, rate in enumerate(expected):
            assert rate['name'] in rows.nth(index).locator('.pick-info').inner_text(), f'{index}번 품목명 누락: {rate}'
            assert rate['spec'] in rows.nth(index).locator('.pick-info').inner_text(), rate
        expect(page.locator('.pick-results-status')).to_contain_text(str(len(expected)))
        header = page.locator('.pick-head').bounding_box()
        first = rows.first.bounding_box()
        assert first['y'] >= header['y'] + header['height'] - 1, f'첫 검색 결과가 고정 검색창 뒤에 가려짐: {header}, {first}'
        expect(page.locator('.pick-list .pick-star')).to_have_count(len(expected))
        page.screenshot(path=str(shots / 'after-search.png'))
        query.fill('가구')
        count = page.evaluate("Promise.all([import('./fees-data.js'),import('./fees.js')]).then(([{BUILTIN_FEES:T},{searchFees}])=>searchFees(T,'가구').length)")
        assert count > 40, count
        expect(rows).to_have_count(40)
        page.get_by_role('button', name=re.compile(r'^검색 결과 더 보기')).click()
        expect(rows).to_have_count(min(count, 80))
        query.fill('ㅈㄹ')
        expect(rows.first.locator('.pick-info')).to_contain_text('장롱')
        print('PASS: 모든 검색 결과 품목명·규격·즐겨찾기·검색 건수·더 보기·초성 검색', flush=True)
    finally:
        context.close()


def quantity_case(browser, url, shots):
    for mode in ('PC', 'SOLO'):
        context, page = open_picker(browser, url, mode)
        try:
            query = page.get_by_role('searchbox', name='품목 찾기', exact=True)
            query.fill('사무용의자')
            page.locator('.pick-add').first.click()
            original = lines(page)[0]
            basket(page)
            expect(query).to_have_value('')
            expect(page.locator('.pick-list .pick-row')).to_have_count(1)
            page.locator('.pick-list .qty-value').click()
            dialog = page.get_by_role('dialog', name='수량 직접 입력', exact=True)
            dialog.get_by_role('spinbutton').fill('150')
            dialog.get_by_role('button', name='적용', exact=True).click()
            assert lines(page)[0]['qty'] == 150 and lines(page)[0]['line_id'] == original['line_id'], lines(page)
            expect(page.locator('.pick-line-amount')).to_contain_text(f"{original['unit_price'] * 150:,}원")
            expect(page.locator('.pick-list .qty-value')).to_be_focused()
            page.get_by_role('button', name='품목 검색으로 돌아가기', exact=True).click()
            expect(query).to_have_value('사무용의자')
            expect(page.locator('.pick-list .qty-value').first).to_have_text('150')
            basket(page)
            page.screenshot(path=str(shots / f'after-picked-{mode.lower()}.png'))
            page.locator('.pick-foot > button').click()
            expect(page.get_by_label('이름', exact=True)).to_have_value('가상 접수 검토')
            assert lines(page)[0]['qty'] == 150 and lines(page)[0]['line_id'] == original['line_id'], lines(page)
            page.get_by_role('button', name='+ 품목 더 담기', exact=True).click()
            basket(page)
            page.get_by_role('button', name=re.compile(r'품목 삭제$')).click()
            assert lines(page) == []
            expect(page.locator('.pick-sum')).to_have_text('담은 품목 없음')
            page.get_by_role('button', name='품목 검색으로 돌아가기', exact=True).click()
            page.get_by_role('searchbox', name='품목 찾기', exact=True).fill('사무용의자')
            page.locator('.pick-add').first.click()
            selected = lines(page)[0]
            page.locator('.pick-foot > button').click()
            page.get_by_role('button', name='접수 저장' if mode == 'PC' else '접수 등록', exact=True).click()
            expect(page).to_have_url(url + '#/home')
            page.reload()
            page.get_by_placeholder('PIN', exact=True).fill(PIN)
            page.get_by_role('button', name='잠금 해제', exact=True).click()
            expect(page.get_by_placeholder('PIN', exact=True)).to_have_count(0)
            stored = page.evaluate("import('./core.js').then(C=>(C.st.mode==='PC'?C.st.fieldStops:C.st.requests)[0])")
            saved = stored['items'][0]
            for key in ('line_id', 'fee_rate_id', 'qty', 'unit', 'unit_label', 'unit_price', 'amount', 'price_status'):
                assert saved[key] == selected[key], (key, selected, saved)
            print(f'PASS: {mode} 담은 품목 바로 확인·수량150·행 ID/금액/초점·검색 복귀·접수 내용 보존·삭제', flush=True)
            print(f'PASS: {mode} 선택한 실제 수수료표 품목의 접수 저장·PIN 재진입·암호화 저장 내용 일치', flush=True)
        finally:
            context.close()


def pending_case(browser, url, shots):
    context, page = open_picker(browser, url)
    try:
        query = page.get_by_role('searchbox', name='품목 찾기', exact=True)
        query.fill('가상미등록물건')
        page.get_by_role('button', name='목록에 없는 품목 직접 입력', exact=True).click(timeout=2000)
        expect(page.get_by_label('목록에 없는 품목 이름', exact=True)).to_have_value('가상미등록물건')
        page.get_by_role('button', name='확인 대기로 담기', exact=True).click()
        pending = lines(page)[0]
        assert pending['price_status'] == 'PENDING' and pending['unit_price'] is None and pending['amount'] is None, pending
        expect(page.locator('.pick-sum')).to_contain_text('금액 확인 대기')
        assert '0원' not in page.locator('.pick-sum').inner_text()
        query.fill('장롱')
        page.locator('.pick-add').first.click()
        expect(page.locator('.pick-sum')).to_contain_text('1쪽')
        expect(page.locator('.pick-sum')).to_contain_text('1개')
        expect(page.locator('.pick-sum')).to_contain_text('확정 소계')
        rate = page.evaluate("import('./fees-data.js').then(({BUILTIN_FEES:T})=>T.rows.find(r=>r.unit_label==='20ℓ당'))")
        query.fill(rate['name'])
        volume = page.locator(f'.pick-list > .pick-row[data-fee-id="{rate["id"]}"]')
        volume.locator('.pick-add').click()
        expect(volume.locator('.pick-line-amount')).to_contain_text('20ℓ당 1')
        expect(page.locator('.pick-sum')).to_contain_text('20ℓ당 1')
        basket(page)
        expect(page.locator('.pick-line-amount').filter(has_text='금액 확인 대기')).to_have_count(1)
        page.screenshot(path=str(shots / 'after-pending.png'))
        print('PASS: 검색 없는 품목 직접 입력·확인 대기와 확정 소계 구분·개/쪽/20ℓ당 단위 보존', flush=True)
    finally:
        context.close()


def layout_case(browser, url, shots):
    context, page = open_picker(browser, url)
    try:
        for width, height, theme, text in ((360, 844, 'light', ''), (390, 844, 'dark', ''),
                                          (430, 844, 'light', 'xl'), (360, 400, 'dark', 'xl')):
            page.set_viewport_size({'width': width, 'height': height})
            page.evaluate("""async ({theme,text})=>{
                const C=await import('./core.js');C.st.textSize=text;C.applyTextSize();
                await C.setPrefs({theme,hc:false});await C.render();
            }""", {'theme': theme, 'text': text})
            quick = page.get_by_role('group', name='빠른 품목 선택', exact=True)
            expect(quick).to_be_visible(timeout=2000)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), (width,height,theme,text)
            for button in quick.get_by_role('button').all():
                box = button.bounding_box()
                assert box['height'] >= 48 and box['width'] >= 48, box
            query = page.get_by_role('searchbox', name='품목 찾기', exact=True)
            query.fill('장롱')
            first = page.locator('.pick-list > .pick-row').first
            if first.locator('.pick-add').count():
                first.locator('.pick-add').click()
            else:
                first.get_by_role('button', name=re.compile(r'더하기$')).click()
            basket(page)
            for selector in ('.pick-list .qty-value', '.pick-list .pick-remove', '.pick-foot > button'):
                target = page.locator(selector).first
                target.scroll_into_view_if_needed()
                metric = target.evaluate("""e=>{const b=e.getBoundingClientRect();return {
                    width:b.width,height:b.height,left:b.left,right:b.right,
                    hit:e.contains(document.elementFromPoint(b.x+b.width/2,b.y+b.height/2))};}""")
                assert metric['width'] >= 48 and metric['height'] >= 48 and metric['hit'], (selector,metric)
                assert metric['left'] >= 0 and metric['right'] <= width, (selector,metric)
            page.screenshot(path=str(shots / f'after-layout-{width}-{height}-{theme}-{text or "normal"}.png'))
            page.get_by_role('button', name='품목 검색으로 돌아가기', exact=True).click()
            page.get_by_role('button', name='검색어 지우기', exact=True).click()
        page.evaluate("""async()=>{
            const C=await import('./core.js'),{BUILTIN_FEES:T}=await import('./fees-data.js');
            C.ui.form.items=['개','쪽','20ℓ당'].map(unit=>C.lineFromRow(T.rows.find(r=>r.unit_label===unit),9999,T));
            C.ui.form.items.push({line_id:'pending-volume',name:'가상 확인대기 품목',qty:9999,
                unit_label:'세트',price_status:'PENDING',unit_price:null,amount:null});
            C.ui.form.pick.cat='PICKED';await C.render();scrollTo(0,0);
        }""")
        height = page.evaluate("""()=>({footer:document.querySelector('.pick-foot').getBoundingClientRect().height,
            reserve:parseFloat(getComputedStyle(document.querySelector('.pick-screen')).paddingBottom)})""")
        assert height['reserve'] >= height['footer'], f'혼합 단위·긴 금액의 하단 요약이 마지막 품목을 가림: {height}'
        page.screenshot(path=str(shots / 'after-mixed-units-large.png'))
        print('PASS: 360/390/430px·밝게/어둡게·큰 글자·400px 높이·수량/삭제/완료 실제 터치 접근', flush=True)
    finally:
        context.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=('all', 'quick', 'search', 'quantity', 'pending', 'layout'), default='all')
    args = parser.parse_args()
    shots = Path(os.environ.get('DG_TEST_DIR') or 'shots/mobile-intake-picker')
    shots.mkdir(parents=True, exist_ok=True)
    failures = []
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        try:
            for name, case in [('quick', quick_case), ('search', search_case), ('quantity', quantity_case),
                               ('pending', pending_case), ('layout', layout_case)]:
                if args.case not in ('all', name):
                    continue
                try:
                    case(browser, url, shots)
                except Exception as error:
                    failures.append(f'{name}: {error}')
                    print(f'FAIL: {name}: {error}', flush=True)
        finally:
            browser.close()
    if failures:
        raise AssertionError('\n'.join(failures))
    print('모바일 현장접수 품목 선택 전체 통과', flush=True)


if __name__ == '__main__':
    main()
