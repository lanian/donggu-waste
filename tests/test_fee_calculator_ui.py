"""빠른 수수료 계산: 실제 PC HTTP·모바일 PIN/요금표·오프라인 PWA."""
import argparse
import json
import os
from pathlib import Path
import tempfile
from playwright.sync_api import expect, sync_playwright
from test_review_ui import PIN, mobile_server, pc_server
from test_workflow_mobile import open_mobile, seed

ROOT = Path(__file__).resolve().parent.parent
TABLE = json.loads((ROOT / 'data/fee_table.json').read_text(encoding='utf-8'))
CHAIR = next(r for r in TABLE['rows'] if r['name'] == '의자' and r['spec'] == '고정식 의자')
PENDING = next(r for r in TABLE['rows'] if r['status'] == 'REVIEW')


def add(page, row):
    page.get_by_label('수수료 계산 검색', exact=True).fill(row['name'])
    page.get_by_role('button', name=f'계산에 추가: {row["name"]} {row["spec"]}', exact=True).click()


def line(page, row):
    return page.locator(f'[data-fee-id="{row["id"]}"]')


def calculate(page):
    expect(page.get_by_role('heading', name='빠른 수수료 계산', exact=True)).to_be_visible()
    add(page, CHAIR)
    expect(page.get_by_label('계산 합계', exact=True)).to_have_text('2,000원')
    line(page, CHAIR).get_by_label('수량', exact=True).fill('3')
    expect(page.get_by_label('계산 합계', exact=True)).to_have_text('6,000원')
    line(page, CHAIR).get_by_label('수량', exact=True).fill('')
    expect(page.get_by_label('계산 합계', exact=True)).to_have_text('—')
    expect(page.get_by_role('button', name='계산 내용 복사', exact=True)).to_be_disabled()
    line(page, CHAIR).get_by_label('수량', exact=True).fill('3')
    add(page, PENDING)
    expect(page.get_by_label('계산 합계', exact=True)).to_have_text('금액 확인 대기')
    expect(page.get_by_text('확정 소계 6,000원', exact=True)).to_be_visible()
    page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw new Error('test clipboard unavailable');}}})")
    page.get_by_role('button', name='계산 내용 복사', exact=True).click()
    copy = page.get_by_label('계산 내용', exact=True)
    expect(copy).to_be_visible()
    assert '확정 소계 6,000원' in copy.input_value() and '금액 확인 대기' in copy.input_value()
    line(page, PENDING).get_by_role('button', name='품목 삭제', exact=True).click()
    expect(page.get_by_label('계산 합계', exact=True)).to_have_text('6,000원')
    # 복사 응답을 기다리는 동안 수량을 바꾸면 옛 금액의 대체 입력란이 열리지 않아야 한다.
    page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:()=>new Promise((resolve,reject)=>{window.__rejectQuoteCopy=reject;})}})")
    page.get_by_role('button', name='계산 내용 복사', exact=True).click()
    page.wait_for_function('typeof window.__rejectQuoteCopy === "function"')
    line(page, CHAIR).get_by_label('수량', exact=True).fill('4')
    expect(page.get_by_label('계산 합계', exact=True)).to_have_text('8,000원')
    page.evaluate('window.__rejectQuoteCopy(new Error("delayed clipboard failure"))')
    expect(copy).to_be_hidden()
    line(page, CHAIR).get_by_label('수량', exact=True).fill('3')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def pc_test(browser, output, exe=None):
    if exe:
        from test_reports_labels_bundle import frozen_server
        server = frozen_server(exe.resolve())
    else:
        server = pc_server()
    with server as url:
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        def api(path, body=None):
            options = {'headers': {'X-DG-CSRF': '1'}}
            r = context.request.get(url + 'api/' + path, **options) if body is None else context.request.post(url + 'api/' + path, data=body, **options)
            assert r.ok, (path, r.status, r.text())
            return r.json()
        try:
            api('setup', {'username': 'quote_admin', 'display_name': '가상 계산 담당', 'password': 'quote-test-only-123'})
            dong_id = api('bootstrap')['dongs'][0]['id']
            api('users', {'username': 'quote_dong', 'display_name': '가상 동 담당', 'role': 'DONG', 'dong_ids': [dong_id], 'password': 'quote-dong-only-123'})
            page.goto(url)
            page.get_by_role('button', name='빠른 수수료 계산', exact=True).click(timeout=15000)
            calculate(page)
            page.evaluate('scrollTo(0, 0)')
            page.screenshot(path=str(output / 'pc-quote.png'), full_page=True)
            page.goto(url + '#/home')
            page.get_by_role('button', name='빠른 수수료 계산', exact=True).click()
            expect(page.get_by_label('계산 합계', exact=True)).to_have_text('6,000원')
            assert api('requests?size=1')['total'] == 0
            page.get_by_role('link', name='로그아웃', exact=True).click()
            page.get_by_label('아이디', exact=True).fill('quote_dong')
            page.get_by_label('비밀번호', exact=True).fill('quote-dong-only-123')
            page.get_by_role('button', name='로그인', exact=True).click()
            expect(page.get_by_role('heading', name='내 계정', exact=True)).to_be_visible()
            api('password', {'old': 'quote-dong-only-123', 'password': 'quote-dong-new-123'})
            page.evaluate("async()=>{const C=await import('/core.js');C.setBoot(null);await C.render(true);}")
            calls = []
            page.on('request', lambda request: calls.append(request.url))
            page.goto(url + '#/dongs')
            page.get_by_role('button', name='빠른 수수료 계산', exact=True).click()
            expect(page.get_by_label('계산 합계', exact=True)).to_have_text('0원')
            add(page, CHAIR)
            expect(page.get_by_label('계산 합계', exact=True)).to_have_text('2,000원')
            assert not any('/api/fee-usage' in u or '/api/fee-table' in u for u in calls)
            assert not errors, errors
            print('PC 수수료 계산: 첫 화면·검색/수량/대기/복사·접수 미생성·왕복 보존·동 담당 접근·계정 분리 통과')
        finally:
            context.close()


def mobile_test(browser, output):
    with mobile_server() as url:
        context, page = open_mobile(browser, url)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            seed(page, registered=False)
            before = page.evaluate("import('./core.js').then(C=>JSON.stringify(C.st))")
            page.get_by_role('button', name='빠른 수수료 계산', exact=True).click()
            calculate(page)
            expect(page.get_by_text('내장 요금표 기준 · 받은 PC 요금표 없음', exact=True)).to_be_visible()
            page.evaluate('scrollTo(0, 0)')
            page.screenshot(path=str(output / 'mobile-quote.png'), full_page=True)
            page.get_by_role('button', name='← 목록', exact=True).click()
            page.get_by_role('button', name='빠른 수수료 계산', exact=True).click()
            expect(page.get_by_label('계산 합계', exact=True)).to_have_text('6,000원')
            assert page.evaluate("import('./core.js').then(C=>JSON.stringify(C.st))") == before
            seed(page, {'mode': 'SOLO', 'feeOverrides': {PENDING['id']: {'price': 7777, 'reason': '가상 확정'}}, 'feeRev': 1}, registered=False)
            page.get_by_role('button', name='빠른 수수료 계산', exact=True).click()
            page.get_by_role('button', name='계산 비우기', exact=True).click()
            add(page, PENDING)
            expect(page.get_by_label('계산 합계', exact=True)).to_have_text('7,777원')
            today = page.evaluate("import('./core.js').then(C=>C.todayKST())")
            seed(page, {'daylist': {'work_date': today, 'crew': '가상 팀', 'stops': [], 'fee_table': {'version_id': 'quote-test-revision', 'base_version': TABLE['version_id'], 'overrides': {PENDING['id']: 8888}}}})
            page.get_by_role('button', name='빠른 수수료 계산', exact=True).click()
            expect(page.get_by_label('계산 합계', exact=True)).to_have_text('8,888원')
            expect(page.get_by_text('요금표 quote-test-revision', exact=True)).to_be_visible()
            page.evaluate("document.documentElement.dataset.theme='dark'")
            page.evaluate('scrollTo(0, 0)')
            page.screenshot(path=str(output / 'mobile-quote-dark.png'), full_page=True)
            seed(page, {'daylist': {'work_date': today, 'crew': '가상 팀', 'stops': [], 'fee_table': {'version_id': 'unreadable-revision', 'base_version': 'different-base', 'overrides': {PENDING['id']: 9999}}}})
            page.get_by_role('button', name='빠른 수수료 계산', exact=True).click()
            expect(page.get_by_role('heading', name='요금표 업데이트 필요', exact=True)).to_be_visible()
            expect(page.get_by_label('수수료 계산 검색', exact=True)).to_have_count(0)
            expect(page.get_by_label('계산 합계', exact=True)).to_have_count(0)
            seed(page, {'daylist': {'work_date': today, 'crew': '가상 팀', 'stops': [], 'fee_table': {'version_id': 'complete-new-table', 'base_version': 'different-base', 'rows': [{**CHAIR, 'price': 4321}]}}})
            page.get_by_role('button', name='빠른 수수료 계산', exact=True).click()
            page.get_by_role('button', name='계산 비우기', exact=True).click()
            add(page, CHAIR)
            expect(page.get_by_label('계산 합계', exact=True)).to_have_text('4,321원')
            assert not errors, errors
            print('모바일 수수료 계산: 390px·목록 왕복·업무 상태 불변·내장/단독 확정/수신 개정·호환 불가 차단/완전한 새 요금표 통과')
        finally:
            context.close()


def offline_test(browser, output):
    with mobile_server() as url:
        context = browser.new_context(service_workers='allow', viewport={'width': 390, 'height': 844})
        page = context.new_page()
        try:
            page.goto(url)
            page.get_by_placeholder('숫자 6자리 이상').fill(PIN)
            page.get_by_placeholder('한 번 더', exact=True).fill(PIN)
            page.get_by_role('button', name='PIN 설정', exact=True).click()
            expect(page.get_by_role('button', name='이 휴대폰만으로 사용')).to_be_visible()
            seed(page, {'mode': 'SOLO'}, registered=False)
            page.wait_for_function('navigator.serviceWorker.controller !== null', timeout=30000)
            page.get_by_role('button', name='빠른 수수료 계산', exact=True).click()
            context.set_offline(True)
            page.reload()
            page.get_by_placeholder('PIN').fill(PIN)
            page.get_by_role('button', name='잠금 해제', exact=True).click()
            add(page, CHAIR)
            expect(page.get_by_label('계산 합계', exact=True)).to_have_text('2,000원')
            page.screenshot(path=str(output / 'mobile-quote-offline.png'), full_page=True)
            print('모바일 수수료 계산: 실제 서비스워커 캐시·네트워크 차단 후 재시작/잠금해제/계산 통과')
        finally:
            context.close()


def main(args):
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-fee-calculator-')) / 'shots'
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        try:
            if args.only in ('all', 'pc'):
                pc_test(browser, output, args.exe)
            if args.only in ('all', 'mobile'):
                mobile_test(browser, output)
            if args.only in ('all', 'offline'):
                offline_test(browser, output)
        finally:
            browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe', type=Path)
    parser.add_argument('--only', choices=('all', 'pc', 'mobile', 'offline'), default='all')
    main(parser.parse_args())
