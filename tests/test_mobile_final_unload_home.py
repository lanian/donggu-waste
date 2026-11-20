"""모바일 홈의 최종 하차·복귀 안내를 가상 배차와 실제 암호화 저장소에서 검증한다."""
import os
import re
from urllib.parse import unquote

from playwright.sync_api import expect, sync_playwright

from test_review_ui import mobile_server
from test_workflow_mobile import action, daylist, event, open_mobile, seed, stop


SITE = {
    'final': True, 'depot': {'lat': 35.155915, 'lon': 126.929740, 'label': '가상 차고지 내부 이름'},
    'stop_count': 2, 'lat': 35.154308, 'lon': 126.926742, 'label': '가상 반입지 내부 이름',
    'eta': '10:15', 'minutes': 20, 'volume_m3': 0.4,
}


def picked_up(number):
    return [event(number, 'ITEM_RESULT', line_id=f'line-{number}', qty_confirmed=1, qty_collected=1),
            event(number, 'ATTEMPT', outcome='DONE')]


def batch(day, *, last=SITE):
    return daylist(day, [stop(1, day), stop(2, day, unload_after=last)])


def expect_no_final_action(page):
    card = page.get_by_role('region', name='지금 할 일', exact=True)
    expect(card.get_by_role('button', name='최종 하차 기록', exact=True)).to_have_count(0)
    expect(page.get_by_role('button', name=re.compile(r'^차고지 복귀 길안내'))).to_have_count(0)


def open_nav(page, button, expected_url):
    page.evaluate('() => { window.__opened = []; window.__dgExternal = url => window.__opened.push(url); }')
    button.click()
    page.wait_for_function('() => window.__opened.length === 1')
    urls = page.evaluate('() => window.__opened')
    assert [unquote(url) for url in urls] == [expected_url], urls


def verify_home(page):
    dates = seed(page)
    today, yesterday = dates['today'], dates['yesterday']
    collected = picked_up(1) + picked_up(2)
    receipt = f'{today}/1/workflow-2'
    basis = {'daylist': batch(today), 'navApp': 'geo'}

    seed(page, {**basis, 'events': picked_up(1)})
    action(page, '2번 수거 이어가기')
    expect_no_final_action(page)

    partial = {**daylist(today, [stop(2, today, unload_after=SITE)]), 'printed_since_seq': 0}
    seed(page, {'daylist': partial})
    action(page, '2번 수거 이어가기')
    expect_no_final_action(page)
    seed(page, {'daylist': partial, 'events': picked_up(2)})
    action(page, '오늘 목록 받기')
    expect(page.get_by_role('region', name='자주 쓰는 기능', exact=True)
           .get_by_role('button', name=re.compile(r'^결과 보내기'))).to_be_enabled()
    expect_no_final_action(page)

    old = {key: SITE[key] for key in ('lat', 'lon', 'label', 'eta', 'minutes', 'volume_m3')}
    seed(page, {'daylist': batch(today, last=old), 'events': collected})
    action(page, '결과 보내기')
    expect_no_final_action(page)

    seed(page, {'daylist': batch(yesterday), 'events': collected,
                'finalUnloadReceipt': f'{yesterday}/1/workflow-2'})
    action(page, '결과 보내기')
    expect_no_final_action(page)

    seed(page, {**basis, 'events': collected, 'finalUnloadReceipt': f'{today}/1/workflow-1'})
    card = page.get_by_role('region', name='지금 할 일', exact=True)
    expect(card.get_by_role('button', name='최종 하차 기록', exact=True)).to_be_visible()
    expect(card.locator('button.primary')).to_have_count(1)
    open_nav(page, card.get_by_role('button', name='반입지 길안내 (기본 지도 앱)', exact=True),
             'geo:35.154308,126.926742?q=35.154308,126.926742(반입지)')

    unload = event(2, 'UNLOAD', work_date=today, place=SITE['label'], volume_m3=0.4)
    unload['request_id'] = ''
    seed(page, {**basis, 'events': [*collected, unload], 'finalUnloadReceipt': receipt})
    action(page, '결과 보내기')
    returns = page.get_by_role('region', name='차고지 복귀', exact=True)
    open_nav(page, returns.get_by_role('button', name='차고지 복귀 길안내 (기본 지도 앱)', exact=True),
             'geo:35.155915,126.929740?q=35.155915,126.929740(차고지)')
    assert page.evaluate("async () => (await import('./core.js')).st.events.map(e => e.kind)") == [
        'ITEM_RESULT', 'ATTEMPT', 'ITEM_RESULT', 'ATTEMPT', 'UNLOAD',
    ]


def main():
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        context, page = open_mobile(browser, url)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            verify_home(page)
            assert not errors, errors
        finally:
            context.close()
            browser.close()
    print('모바일 홈 최종 하차·복귀, 종이 QR 일부 수신·구형 QR·지난 작업일 통과')


if __name__ == '__main__':
    main()
