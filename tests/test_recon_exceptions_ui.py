"""가상 CSV로 세입·대사의 반입 및 다시 열기 흐름을 확인한다."""
import os

from playwright.sync_api import expect, sync_playwright

from test_recon_exceptions import ReconExceptionTest
from dgpc import service as S


def main():
    fixture = ReconExceptionTest()
    fixture.setUp()
    try:
        raw, _, different, cancelled, amount, day = fixture.mixed_file()
        fixture.con.execute("""INSERT INTO recon_import (id, sha256, at, actor, rows, matched, summary_json)
                               VALUES ('old', 'legacy-summary', ?, 'legacy', 3, 1, '{"MATCH":1,"AMOUNT_DIFF":2}')""", (S.now(),))
        root = f'http://127.0.0.1:{fixture.port}/'
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
            ctx = browser.new_context(service_workers='block', viewport={'width': 1440, 'height': 900})
            ctx.route('**/*', lambda route: route.continue_() if route.request.url.startswith(root) else route.abort())
            name, value = fixture.worker_cookie.split('=', 1)
            ctx.add_cookies([{'name': name, 'value': value, 'url': root}])
            page = ctx.new_page()
            page.goto(root + '#/revenue')
            queue = page.get_by_role('region', name='대사 불일치 검토')
            file = page.get_by_label('대사 CSV 파일')
            review = page.get_by_role('dialog', name='대사 불일치 처리 이력')
            expect(queue.get_by_text('조회 0건')).to_be_visible()
            page.get_by_text('반영 기록 1건').click()
            expect(page.get_by_text('집계만 보관된 반입')).to_be_visible()
            expect(page.get_by_text('일치 1건 · 금액 다름 2건')).to_be_visible()

            def select_file(filename, contents):
                file.set_input_files({'name': filename, 'mimeType': 'text/csv', 'buffer': contents})

            only = f'부과번호,금액,납부일\n\n2099-ZZ-00002,100,{day}\n'.encode()
            select_file('mixed.csv', raw)
            page.get_by_role('button', name='미리보기', exact=True).click()
            commit = page.get_by_role('button', name='일치 1건 납부로 기록')
            expect(commit).to_be_enabled()
            expect(queue.get_by_text('조회 0건')).to_be_visible()
            select_file('only.csv', only)
            expect(commit).to_have_count(0)
            page.get_by_role('button', name='미리보기', exact=True).click()
            page.get_by_role('button', name='불일치 1건 검토 목록에 등록').click()
            page.get_by_role('dialog', name='대사 반영').get_by_role('button', name='기록', exact=True).click()
            expect(queue.get_by_role('button', name='3행 처리 이력 열기')).to_be_visible()
            assert not S.rows(fixture.con, 'SELECT * FROM payment')

            select_file('mixed.csv', raw)
            page.get_by_role('button', name='미리보기', exact=True).click()
            commit.click()
            page.get_by_role('dialog', name='대사 반영').get_by_role('button', name='기록', exact=True).click()
            expect(queue.get_by_text('조회 8건')).to_be_visible()
            page.reload()
            expect(queue.get_by_text('조회 8건')).to_be_visible()
            different_row = queue.get_by_role('row').filter(has_text=different['receipt_no']).filter(has_text='금액 다름')
            different_row.get_by_role('button', name='3행 처리 이력 열기').click()
            expect(review.get_by_role('link', name=different['receipt_no'])).to_be_visible()
            review.get_by_label('처리 상태').select_option('RESOLVED')
            review.get_by_label('처리 근거').select_option('LEDGER_CHECKED')
            review.get_by_role('button', name='저장', exact=True).click()
            expect(queue.get_by_text('조회 7건')).to_be_visible()

            select_file('mixed.csv', raw)
            page.get_by_role('button', name='미리보기', exact=True).click()
            expect(page.get_by_text('이미 반영한 파일입니다', exact=False)).to_be_visible()
            expect(page.get_by_role('button', name='불일치 7건 검토 목록에 등록')).to_be_disabled()
            page.get_by_role('button', name='이 반입의 불일치 보기').click()
            expect(queue.get_by_text('조회 7건')).to_be_visible()
            queue.get_by_label('불일치 상태').select_option('RESOLVED')
            expect(queue.get_by_text('조회 1건')).to_be_visible()
            different_row.get_by_role('button', name='3행 처리 이력 열기').click()
            expect(review.get_by_text('미해결 → 해결 완료', exact=True)).to_be_visible()
            expect(review.get_by_role('cell', name='수납 원장 대조', exact=True)).to_be_visible()
            expect(review.get_by_role('cell', name='session_worker', exact=True)).to_be_visible()
            assert len(S.rows(fixture.con, 'SELECT * FROM payment')) == 1

            review.get_by_role('button', name='닫기', exact=True).click()
            receipt_csv = ('PC 접수번호,부과번호,금액,납부일\n'
                           f'{different["receipt_no"]},,{amount},{day}\n'
                           f'{different["receipt_no"]},{cancelled["levy_no"]},{amount},{day}\n').encode()
            select_file('receipt.csv', receipt_csv)
            page.get_by_role('button', name='미리보기', exact=True).click()
            expect(page.get_by_role('columnheader', name='PC 접수번호')).to_be_visible()
            expect(page.get_by_text('부과번호와 PC 접수번호가 같은 접수에 연결되지 않음', exact=True)).to_be_visible()
            expect(page.get_by_text('접수 연결 불가 1', exact=True)).to_be_visible()
            commit.click()
            page.get_by_role('dialog', name='대사 반영').get_by_role('button', name='기록', exact=True).click()
            expect(queue.get_by_role('row').filter(has_text='접수 연결 불가').get_by_role('cell', name=cancelled['levy_no'], exact=True)).to_be_visible()
            assert S.payment_status(fixture.con, different['id'])['paid'] == amount
            assert S.payment_status(fixture.con, cancelled['id'])['paid'] == 0
            ctx.close()
            browser.close()
            print('실HTTP UI 통과: 예외만 반입 · 혼합 반입 · 재조회 · 완료 · 재반입 차단 · 이력 · 이전 집계 · PC 접수번호 검증')
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    main()
