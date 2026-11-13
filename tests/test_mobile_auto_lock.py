"""실제 PIN/IndexedDB: 자동잠금 토글·시간/화면전환·실패/지연저장·수동/PIN/다른탭 보호."""
import os
from pathlib import Path
import tempfile
from playwright.sync_api import expect, sync_playwright
from test_review_ui import PIN, mobile_server
from test_workflow_mobile import seed, stop

STORAGE_FIXTURE = """() => {
  const put = IDBObjectStore.prototype.put;
  const complete = Object.getOwnPropertyDescriptor(IDBTransaction.prototype, 'oncomplete');
  IDBObjectStore.prototype.put = function(value, key) {
    if (key === 'auto-lock' && window.__failAutoLock) throw new DOMException('test quota failure', 'QuotaExceededError');
    if (key === 'auto-lock' && window.__holdAutoLock) this.transaction.__autoLockHold = true;
    return put.call(this, value, key);
  };
  Object.defineProperty(IDBTransaction.prototype, 'oncomplete', { ...complete, set(fn) {
    complete.set.call(this, typeof fn !== 'function' ? fn : function(event) {
      if (this.__autoLockHold) window.__releaseAutoLock = () => fn.call(this, event);
      else fn.call(this, event);
    });
  }});
}"""


def unlock(page):
    page.get_by_placeholder('PIN', exact=True).fill(PIN)
    page.get_by_role('button', name='잠금 해제', exact=True).click()
    expect(page.get_by_placeholder('PIN', exact=True)).to_have_count(0)


def settings(page, url):
    page.goto(url + '#/settings')
    toggle = page.get_by_role('switch', name='자동 잠금', exact=True)
    expect(toggle).to_be_visible(timeout=15000)
    return toggle


def visibility(page, hidden):
    page.evaluate("(hidden)=>{Object.defineProperty(document,'hidden',{configurable:true,value:hidden});document.dispatchEvent(new Event('visibilitychange'));}", hidden)


def main():
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-auto-lock-'))
    output.mkdir(parents=True, exist_ok=True)
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        context = browser.new_context(viewport={'width': 390, 'height': 844}, service_workers='block')
        page = context.new_page()
        page.clock.install()
        page.add_init_script('(' + STORAGE_FIXTURE + ')()')
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto(url)
            page.get_by_placeholder('숫자 6자리 이상').fill(PIN)
            page.get_by_placeholder('한 번 더', exact=True).fill(PIN)
            page.get_by_role('button', name='PIN 설정', exact=True).click()
            expect(page.get_by_role('button', name='PC와 연결해서 사용', exact=True)).to_be_visible()
            today = page.evaluate("import('./core.js').then(C=>C.todayKST())")
            receipt = stop(1, today)
            seed(page, {'daylist': {'work_date': today, 'crew': '가상 팀', 'stops': [receipt]},
                        'events': [{'request_id': receipt['request_id'], 'kind': 'CONTACT', 'body': {'result': 'REACHED'}}]}, registered=False)
            toggle = settings(page, url)
            expect(toggle).to_be_checked()
            page.get_by_role('button', name='5분', exact=True).click()
            expect(page.get_by_role('button', name='5분', exact=True)).to_have_attribute('aria-pressed', 'true')
            before = page.evaluate("import('./core.js').then(C=>JSON.stringify([C.st.daylist,C.st.events,C.st.requests,C.st.intakeDraft]))")
            toggle.click()
            expect(toggle).not_to_be_checked()
            expect(page.get_by_role('button', name='5분', exact=True)).to_be_disabled()
            assert page.evaluate("import('./store.js').then(S=>S.getRaw('auto-lock')).then(v=>v.enabled)") is False
            assert page.evaluate("import('./core.js').then(C=>JSON.stringify([C.st.daylist,C.st.events,C.st.requests,C.st.intakeDraft]))") == before
            page.clock.fast_forward('20:00')
            assert page.evaluate("import('./store.js').then(S=>S.isUnlocked())")
            visibility(page, True)
            expect(page.locator('html')).to_have_class('veil')
            page.clock.fast_forward('03:00')
            visibility(page, False)
            expect(page.locator('html')).not_to_have_class('veil')
            expect(page.get_by_placeholder('PIN', exact=True)).to_have_count(0)
            page.evaluate('scrollTo(0,0)')
            page.screenshot(path=str(output / 'auto-lock-off.png'), full_page=True)
            page.locator('.auto-lock-controls').screenshot(path=str(output / 'auto-lock-off-card.png'))
            page.goto(url + '#/home')
            page.get_by_role('button', name='잠금', exact=True).click()
            expect(page.get_by_placeholder('PIN', exact=True)).to_be_visible()
            unlock(page)
            toggle = settings(page, url)
            expect(toggle).not_to_be_checked()
            page.reload()
            expect(page.get_by_placeholder('PIN', exact=True)).to_be_visible()
            unlock(page)
            toggle = settings(page, url)
            expect(toggle).not_to_be_checked()
            page.evaluate('window.__failAutoLock=true')
            toggle.click()
            expect(page.get_by_role('alert').filter(has_text='자동 잠금 설정을 저장하지 못했습니다')).to_be_visible()
            expect(toggle).not_to_be_checked()
            assert page.evaluate("import('./store.js').then(S=>S.getRaw('auto-lock')).then(v=>v.enabled)") is False
            assert not page.evaluate("import('./store.js').then(S=>S.hasPendingSecure('state'))")
            page.evaluate('window.__failAutoLock=false;window.__holdAutoLock=true')
            toggle.click()
            expect(toggle).to_be_disabled()
            for _ in range(100):
                if page.evaluate('typeof window.__releaseAutoLock === "function"'):
                    break
                page.wait_for_timeout(30)
            assert page.evaluate('typeof window.__releaseAutoLock === "function"'), '설정 저장 완료 응답이 보류됨'
            page.goto(url + '#/home')
            toggle = settings(page, url)
            expect(toggle).to_be_disabled()
            page.get_by_label('작업자 1 이름', exact=True).fill('가상 임시수정')
            page.evaluate('window.__holdAutoLock=false;window.__releaseAutoLock()')
            expect(toggle).to_be_checked()
            expect(toggle).to_be_enabled()
            expect(page.get_by_label('작업자 1 이름', exact=True)).to_have_value('가상 임시수정')
            page.clock.fast_forward('04:00')
            expect(page.get_by_placeholder('PIN', exact=True)).to_have_count(0)
            page.clock.fast_forward('02:00')
            expect(page.get_by_placeholder('PIN', exact=True)).to_be_visible()
            unlock(page)
            visibility(page, True)
            page.clock.fast_forward('03:00')
            visibility(page, False)
            expect(page.get_by_placeholder('PIN', exact=True)).to_be_visible()
            unlock(page)
            toggle = settings(page, url)
            toggle.click()
            expect(toggle).not_to_be_checked()
            page.evaluate("new BroadcastChannel('dg-session').postMessage({type:'active',id:'test-other-tab'})")
            expect(page.get_by_placeholder('PIN', exact=True)).to_be_visible()
            unlock(page)
            toggle = settings(page, url)
            expect(toggle).not_to_be_checked()
            page.evaluate("async()=>{const S=await import('./store.js');await S.putRaw('auto-lock',{enabled:'false'});}")
            page.reload()
            unlock(page)
            toggle = settings(page, url)
            expect(toggle).to_be_checked()
            page.evaluate('window.__holdAutoLock=true')
            toggle.click()
            for _ in range(100):
                if page.evaluate('typeof window.__releaseAutoLock === "function"'):
                    break
                page.wait_for_timeout(30)
            assert page.evaluate('typeof window.__releaseAutoLock === "function"')
            # 해시 변경만으로도 PIN 화면이 먼저 보인다. 실제 새 문서의 load까지 기다린다.
            with page.expect_event('load'):
                page.evaluate("async()=>{const S=await import('./store.js');await S.wipeAll();window.__holdAutoLock=false;window.__releaseAutoLock();}")
            # 초기화 알림은 앱을 새로 연다. 새 저장소에 이전 OFF 요청이 살아나지 않는다.
            expect(page.get_by_role('button', name='PIN 설정', exact=True)).to_be_visible()
            assert page.evaluate("import('./store.js').then(S=>S.getRaw('auto-lock'))") is None
            page.get_by_placeholder('숫자 6자리 이상').fill(PIN)
            page.get_by_placeholder('한 번 더', exact=True).fill(PIN)
            page.get_by_role('button', name='PIN 설정', exact=True).click()
            page.get_by_role('button', name='PC와 연결해서 사용', exact=True).click()
            expect(page.get_by_role('button', name='빠른 수수료 계산', exact=True)).to_be_visible()
            toggle = settings(page, url)
            expect(toggle).to_be_checked()
            assert not errors, errors
            print('모바일 자동잠금: 기본ON·OFF저장·무조작/백그라운드OFF·가림·수동/재실행PIN·저장실패/지연재진입·기록/초안보존·재활성화시간·다른탭보호·비정상설정ON·초기화중옛설정차단 통과')
        finally:
            context.close()
            browser.close()


if __name__ == '__main__':
    main()
