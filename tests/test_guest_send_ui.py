"""가상 팀의 PC 목록 전송 화면에서 미등록폰 QR 표시와 발급 중 화면 전환을 확인한다."""
import os
from datetime import datetime, timedelta, timezone

from playwright.sync_api import expect, sync_playwright

from test_review_ui import pc_server
from test_send_team_visibility import DAY, NEXT, batch


def main():
    visit = {'seq': 1, 'name': '가상 수거지'}
    rows = [batch('guest', '가상 수거팀', stops=[visit], device_id=None),
            batch('registered', '가상 연결팀', stops=[visit]),
            batch('archive', '가상 보관팀', stops=[visit], crew_archived=True),
            batch('old', '가상 이전팀', stops=[visit], status='SUPERSEDED')]
    key_qr = 'DGVIEW1:' + 'a' * 32 + ':' + 'b' * 43
    frames = ['DGV1.' + '0A $:+-*9' * 118, 'DGV1.SECOND-2']
    calls = []
    with pc_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.getenv('CHROME') or pw.chromium.executable_path)
        try:
            ctx = browser.new_context(service_workers='block')
            setup = ctx.request.post(url + 'api/setup', headers={'X-DG-CSRF': '1'},
                                     data={'username': 'guest_reader', 'display_name': '가상 관리자',
                                           'password': 'guest-display-qa-1234'})
            assert setup.ok
            page = ctx.new_page()
            page.set_viewport_size({'width': 960, 'height': 540})
            page.route('**/api/batches?**', lambda r: r.fulfill(json=rows))

            def issue(route):
                calls.append(route.request.post_data_json['batch_id'])
                route.fulfill(json={'key_qr': key_qr, 'frames': frames, 'count': 1,
                                    'work_date': DAY, 'crew': '가상 수거팀', 'assign_ver': 2})

            page.route('**/api/guest-lists', issue)
            page.goto(url + '#/send?date=' + DAY)
            page.add_script_tag(url=url + 'web/vendor/jsQR.js')
            guest = page.get_by_role('button', name='미등록폰 조회용 QR', exact=True)
            expect(guest).to_have_count(2, timeout=3000)
            page.get_by_role('checkbox', name='배차 없는 팀·이전 목록 보기').check()
            expect(guest).to_have_count(2)
            card = page.locator('.card').filter(has=page.get_by_role('heading', name='가상 수거팀 ·', exact=False))
            expect(card.get_by_role('button', name='QR 표시', exact=True)).to_be_disabled()
            start = card.get_by_role('button', name='미등록폰 조회용 QR', exact=True)
            start.click()
            dlg = page.get_by_role('dialog', name='미등록폰 조회용 QR')

            def read(name):
                canvas = dlg.get_by_role('img', name=name, exact=True)
                expect(canvas).to_be_visible()
                return canvas.evaluate("""canvas => { const ctx = canvas.getContext('2d');
                    return jsQR(ctx.getImageData(0,0,canvas.width,canvas.height).data,
                                canvas.width, canvas.height)?.data; }""")

            assert read('열쇠 QR') == key_qr
            first_stage = dlg.get_by_role('heading', name='1단계 · 열쇠 QR')
            expect(first_stage).to_be_in_viewport(ratio=1)
            key_color = first_stage.evaluate('el => getComputedStyle(el).backgroundColor')
            expect(dlg.get_by_role('button', name='목록 QR 표시')).to_be_in_viewport(ratio=1)
            expect(dlg.get_by_role('img', name='목록 QR', exact=True)).to_have_count(0)
            expect(dlg.get_by_text(DAY, exact=False).first).to_be_visible()
            dlg.get_by_role('button', name='목록 QR 표시').click()
            assert read('목록 QR') == frames[0]
            second_stage = dlg.get_by_role('heading', name='2단계 · 목록 QR')
            expect(second_stage).to_be_in_viewport(ratio=1)
            assert second_stage.evaluate('el => getComputedStyle(el).backgroundColor') != key_color
            dlg.get_by_role('button', name='열쇠 QR 다시 보기').click()
            expect(first_stage).to_be_in_viewport(ratio=1)
            dlg.get_by_role('button', name='목록 QR 표시').click()
            dlg.get_by_role('button', name='다음 QR').click()
            assert read('목록 QR') == frames[1]
            dlg.get_by_role('button', name='이전 QR').click()
            assert read('목록 QR') == frames[0]
            dlg.get_by_role('button', name='자동 재생').click()
            expect(dlg.get_by_role('button', name='일시 정지')).to_be_visible()
            expect(dlg.get_by_text('2 / 2', exact=True)).to_be_visible()
            dlg.get_by_role('button', name='일시 정지').click()
            page.keyboard.press('Escape')
            expect(dlg).to_have_count(0)
            expect(start).to_be_focused(timeout=1500)
            assert calls == ['guest']
            expect(card.get_by_label('수신 코드', exact=True)).to_have_count(0)

            # 같은 날짜 조회 안에서 날짜를 바꾸면 이미 열어 둔 QR도 닫는다.
            start.click()
            expect(dlg).to_be_visible()
            page.get_by_label('수거일', exact=True).evaluate("""(input,date) => {
                input.value=date; input.dispatchEvent(new Event('change', {bubbles:true})); }""", NEXT)
            expect(dlg).to_have_count(0)
            start.click()
            expect(dlg).to_be_visible()
            page.evaluate("async () => (await import('/core.js')).render(true)")
            expect(dlg).to_have_count(0, timeout=1500)

            # 네트워크 응답을 늦춰 중복 누름 및 화면을 떠난 뒤 도착한 발급 응답을 재현한다.
            page.evaluate("""() => {
                const orig = window.fetch;
                window.fetch = async (...args) => {
                  const response = await orig(...args);
                  if (String(args[0]).includes('/api/guest-lists'))
                    await new Promise(r => {
                      window.fetch = orig;
                      window.releaseGuest = r;
                      document.body.dataset.guestWait = 'held';
                    });
                  return response;
                };
            }""")
            start.evaluate('b => { b.click(); b.click(); }')
            expect(page.locator('body')).to_have_attribute('data-guest-wait', 'held')
            expect(start).to_be_disabled()
            page.evaluate("(date) => { location.hash = '#/send/registered?date=' + date; }", NEXT)
            page.evaluate('() => window.releaseGuest()')
            expect(page.get_by_role('heading', name='목록 전송 (QR)')).to_be_visible()
            expect(dlg).to_have_count(0)
            assert calls == ['guest'] * 4

            page.goto(url + '#/send?date=' + DAY)
            page.clock.install()
            start.click()
            expect(dlg).to_be_visible()
            page.clock.fast_forward(4 * 60_000)
            dlg.get_by_role('button', name='목록 QR 표시').click()
            dlg.get_by_role('button', name='자동 재생').click()
            page.clock.fast_forward(61_000)
            expect(dlg).to_have_count(0, timeout=1500)
            expect(card.get_by_text('QR 표시 시간이 끝났습니다.', exact=False)).to_be_visible()
            start.click()
            expect(dlg).to_be_visible()
            page.clock.set_system_time(datetime.now(timezone.utc) + timedelta(days=1))
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            expect(dlg).to_have_count(0, timeout=1500)
            page.get_by_label('수거일', exact=True).fill('2000-01-01')
            expect(guest).to_have_count(0)
            expect(page.get_by_text('조회용 QR은 오늘 이후', exact=False).first).to_be_visible()
            print('미등록폰 QR 선택 범위·화면 QR 복원·수동/자동 전환·날짜/화면 변경·중복 방지 통과')
        finally:
            browser.close()


if __name__ == '__main__':
    main()
