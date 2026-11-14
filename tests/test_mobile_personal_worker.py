"""개인폰 작업자: 실제 PIN/IndexedDB·암호화 복원과 이후 기록의 이름만 변경."""
import base64
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright

from test_review_ui import PIN, mobile_server
from test_workflow_mobile import daylist, open_mobile, seed, stop


STORAGE_FIXTURE = """() => {
  const put = IDBObjectStore.prototype.put;
  const complete = Object.getOwnPropertyDescriptor(IDBTransaction.prototype, 'oncomplete');
  IDBObjectStore.prototype.put = function(value, key) {
    if (key === 's:state' && window.__failWorkerSave) throw new DOMException('test worker quota failure', 'QuotaExceededError');
    if (key === 's:state' && window.__holdWorkerSave) this.transaction.__workerSaveHold = true;
    return put.call(this, value, key);
  };
  Object.defineProperty(IDBTransaction.prototype, 'oncomplete', { ...complete, set(fn) {
    complete.set.call(this, typeof fn !== 'function' ? fn : function(event) {
      if (this.__workerSaveHold) window.__releaseWorkerSave = () => fn.call(this, event);
      else fn.call(this, event);
    });
  }});
}"""


def settings(page, url):
    page.goto(url + '#/settings')
    name = page.get_by_role('textbox', name='작업자 이름', exact=True)
    expect(name).to_be_visible(timeout=10000)
    expect(page.get_by_role('textbox', name='작업자 이름', exact=True)).to_have_count(1)
    expect(page.get_by_label('작업자 1 이름', exact=True)).to_have_count(0)
    return name


def unlock(page):
    page.get_by_placeholder('PIN', exact=True).fill(PIN)
    page.get_by_role('button', name='잠금 해제', exact=True).click()
    expect(page.get_by_placeholder('PIN', exact=True)).to_have_count(0)


def state(page):
    return page.evaluate("import('./core.js').then(C=>({worker:C.st.worker, workers:C.st.workers, events:C.st.events}))")


def add_records(page, label):
    return page.evaluate("""async label => {
      const C = await import('./core.js');
      const receipt = C.st.mode === 'SOLO' ? C.st.requests[0] : C.st.daylist.stops[0];
      await C.addEvent(receipt, 'FIELD_MEMO', { text: label });
      await C.addCrewEvent('UNLOAD', { place: '가상 차고지', text: label });
      return C.st.events.slice(-2).map(e=>e.body.worker ?? null);
    }""", label)


def main():
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-personal-worker-'))
    shots = output / 'shots'
    shots.mkdir(parents=True, exist_ok=True)
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        context, page = open_mobile(browser, url)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            dates = seed(page)
            receipt = stop(1, dates['today'])
            historic = {'request_id': receipt['request_id'], 'kind': 'FIELD_MEMO', 'body': {'text': '가상 이전 기록', 'worker': '가상 과거'}}
            seed(page, {'workers': ['가상 이전', '가상 본인'], 'worker': '가상 본인',
                        'setupHidden': False, 'daylist': daylist(dates['today'], [receipt]), 'events': [historic]})
            before_event = state(page)['events'][0]
            name = settings(page, url)
            expect(name).to_have_value('가상 본인')
            print('PASS: 여러 이름의 기존 설정은 선택한 이름을 단일 입력에 우선 표시함', flush=True)
            page.goto(url + '#/home')
            expect(page.get_by_role('group', name='작업자 바꾸기', exact=True)).to_have_count(0)
            current = page.get_by_role('group', name='현재 작업자', exact=True)
            expect(current).to_contain_text('가상 본인')
            expect(current.get_by_role('button')).to_have_count(0)
            expect(current.get_by_role('link', name='이름 변경', exact=True)).to_be_visible()
            assert page.evaluate("import('./screens/stoplist.js').then(M=>M.setupSteps().find(x=>x[0]==='worker')[3])")
            assert add_records(page, '이름 변경 전') == ['가상 본인', '가상 본인']
            name = settings(page, url)
            sms = page.get_by_role('textbox', name='문자 문구:', exact=False).first
            sms.fill('가상 저장하지 않은 문자 초안')
            name.fill('  가상 새이름')
            page.get_by_role('button', name='작업자 저장', exact=True).click()
            expect(page.get_by_text('작업자 이름을 저장했습니다.', exact=True)).to_be_visible()
            expect(name).to_have_value('가상 새이름')
            expect(sms).to_have_value('가상 저장하지 않은 문자 초안')
            assert state(page)['workers'] == ['가상 새이름']
            assert add_records(page, '이름 변경 후') == ['가상 새이름', '가상 새이름']
            assert state(page)['events'][0] == before_event
            assert [event['body']['worker'] for event in state(page)['events'][1:3]] == ['가상 본인', '가상 본인']
            worker_card = page.get_by_role('region', name='작업자 설정', exact=True)
            worker_card.evaluate("el => el.scrollIntoView({block:'center'})")
            worker_card.screenshot(path=str(shots / 'personal-worker-card-390.png'))
            page.screenshot(path=str(shots / 'personal-worker-settings-390.png'), full_page=True)
            page.reload()
            expect(page.get_by_placeholder('PIN', exact=True)).to_be_visible()
            unlock(page)
            name = settings(page, url)
            expect(name).to_have_value('가상 새이름')
            assert state(page)['events'][0] == before_event
            print('PASS: 개인 이름은 이후 접수·하차 기록에만 자동 적용되고 예전 기록·다른 설정 초안·재실행 PIN을 보존함', flush=True)

            page.evaluate('(' + STORAGE_FIXTURE + ')()')
            disk_before = page.evaluate("import('./store.js').then(S=>S.getRaw('s:state')).then(JSON.stringify)")
            name.fill('가상 실패이름')
            page.evaluate('window.__failWorkerSave=true')
            page.get_by_role('button', name='작업자 저장', exact=True).click()
            expect(page.get_by_text('작업자 이름을 저장하지 못했습니다. 입력한 이름을 유지했으니 다시 저장해 주세요.', exact=True)).to_be_visible()
            expect(name).to_have_value('가상 실패이름')
            expect(name).to_be_enabled()
            expect(page.get_by_text('작업자 이름을 저장했습니다.', exact=True)).to_have_count(0)
            assert page.evaluate("import('./store.js').then(S=>S.hasPendingSecure('state'))")
            assert page.evaluate("import('./store.js').then(S=>S.getRaw('s:state')).then(JSON.stringify)") == disk_before
            assert state(page)['worker'] == '가상 실패이름', '대기 암호문과 메모리의 설정 의도를 따로 되돌리지 않음'
            page.evaluate('window.__failWorkerSave=false')
            page.get_by_role('button', name='작업자 저장', exact=True).click()
            expect(page.get_by_text('작업자 이름을 저장했습니다.', exact=True)).to_be_visible()
            assert not page.evaluate("import('./store.js').then(S=>S.hasPendingSecure('state'))")
            print('PASS: 실제 IndexedDB 저장 실패는 성공으로 안내하지 않고 입력·대기 암호문을 유지하며 재저장함', flush=True)

            name.fill('가상 지연이름')
            page.evaluate('window.__holdWorkerSave=true; window.__releaseWorkerSave=null')
            page.get_by_role('button', name='작업자 저장', exact=True).click()
            expect(name).to_be_disabled()
            expect(page.get_by_role('button', name='작업자 저장', exact=True)).to_be_disabled()
            page.wait_for_function('() => typeof window.__releaseWorkerSave === "function"')
            sms = page.get_by_role('textbox', name='문자 문구:', exact=False).first
            sms.fill('가상 지연 응답에도 보존할 문자 초안')
            page.evaluate('window.__holdWorkerSave=false; window.__releaseWorkerSave()')
            expect(name).to_be_enabled()
            expect(sms).to_have_value('가상 지연 응답에도 보존할 문자 초안')
            name.fill('가상 이전화면')
            page.evaluate('window.__holdWorkerSave=true; window.__releaseWorkerSave=null')
            page.get_by_role('button', name='작업자 저장', exact=True).click()
            page.wait_for_function('() => typeof window.__releaseWorkerSave === "function"')
            page.goto(url + '#/home')
            name = settings(page, url)
            name.fill('가상 재진입초안')
            page.evaluate('window.__holdWorkerSave=false; window.__releaseWorkerSave()')
            page.wait_for_timeout(150)
            expect(name).to_have_value('가상 재진입초안')
            assert state(page)['worker'] == '가상 이전화면'
            print('PASS: 지연 저장 완료는 다른 설정 입력과 재진입한 화면의 새 이름 초안을 덮지 않음', flush=True)

            # 실제 암호화 백업 UI 복원은 loadState를 다시 거치지 않는다.
            seed(page, {'mode': 'SOLO', 'requests': [receipt], 'worker': '가상 현재', 'workers': ['가상 현재']}, registered=False)
            backup = page.evaluate("""async () => {
              const B = await import('./backup.js'), C = await import('./core.js');
              const legacy = { ...C.emptyState(), mode: 'SOLO', setupHidden: false, storagePersisted: true,
                mapEnabled: false, worker: '', workers: ['', '가상 복원본인', '가상 복원다른'], requests: C.st.requests,
                events: [{ event_id: crypto.randomUUID(), seq: 1, request_id: C.st.requests[0].request_id,
                  kind: 'FIELD_MEMO', ts: new Date().toISOString(), body: { worker: '가상 복원과거', text: '복원 전 기록' } }], seq: 1 };
              const bytes = await B.makeBackup({ format: 'dg.backup/1', created_at: new Date().toISOString(),
                app_version: '0.14.16', state: legacy, photos: {} }, 'worker-backup-test-123');
              return btoa(String.fromCharCode(...bytes));
            }""")
            page.goto(url + '#/backup')
            page.locator('input[type=file]').set_input_files({'name': 'personal-worker-legacy.dgb', 'mimeType': 'application/octet-stream', 'buffer': base64.b64decode(backup)})
            page.get_by_placeholder('백업 비밀번호', exact=True).fill('worker-backup-test-123')
            page.get_by_role('button', name='백업에서 복원', exact=True).click()
            page.get_by_role('button', name='복원하기', exact=True).click()
            expect(page).to_have_url(url + '#/home')
            expect(page.get_by_role('group', name='현재 작업자', exact=True)).to_contain_text('가상 복원본인')
            expect(page.get_by_role('group', name='작업자 바꾸기', exact=True)).to_have_count(0)
            assert add_records(page, '복원 직후') == ['가상 복원본인', '가상 복원본인']
            assert state(page)['events'][0]['body']['worker'] == '가상 복원과거'
            assert page.evaluate("import('./screens/stoplist.js').then(M=>M.setupSteps().find(x=>x[0]==='worker')[3])")
            name = settings(page, url)
            expect(name).to_have_value('가상 복원본인')
            name.fill('')
            page.get_by_role('button', name='작업자 저장', exact=True).click()
            expect(page.get_by_text('작업자 이름을 비웠습니다.', exact=True)).to_be_visible()
            assert state(page)['worker'] == '' and state(page)['workers'] == []
            assert add_records(page, '이름 비운 뒤') == [None, None]
            assert not page.evaluate("import('./screens/stoplist.js').then(M=>M.setupSteps().find(x=>x[0]==='worker')[3])")
            page.reload()
            unlock(page)
            name = settings(page, url)
            expect(name).to_have_value('')
            page.goto(url + '#/home')
            expect(page.get_by_role('group', name='현재 작업자', exact=True)).to_have_count(0)
            print('PASS: PC·단독 호환, 실제 백업 복원 직후 이전 목록 이름 사용, 명시적 비우기 후 이름 부활 없음', flush=True)
            name = settings(page, url)
            name.fill('가상개인휴대폰작업자')
            page.get_by_role('button', name='작업자 저장', exact=True).click()
            expect(page.get_by_text('작업자 이름을 저장했습니다.', exact=True)).to_be_visible()
            for width in (360, 430):
                page.set_viewport_size({'width': width, 'height': 844})
                page.evaluate("async()=>{const C=await import('./core.js');C.st.textSize='xl';C.applyTextSize();await C.render();}")
                name = settings(page, url)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                worker_card = page.get_by_role('region', name='작업자 설정', exact=True)
                worker_card.evaluate("el => el.scrollIntoView({block:'center'})")
                worker_card.screenshot(path=str(shots / f'personal-worker-card-{width}-large.png'))
                page.screenshot(path=str(shots / f'personal-worker-{width}-large.png'), full_page=True)
                page.goto(url + '#/home')
                expect(page.get_by_role('group', name='현재 작업자', exact=True)).to_contain_text('가상개인휴대폰작업자')
                expect(page.get_by_role('group', name='현재 작업자', exact=True).get_by_role('button')).to_have_count(0)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(shots / f'personal-worker-home-{width}-large.png'), full_page=True)
            assert not errors, errors
            print('PASS: 개인폰 작업자 전체 회귀', flush=True)
        except BaseException:
            page.screenshot(path=str(shots / 'personal-worker-failure.png'), full_page=True)
            raise
        finally:
            context.close()
            browser.close()


if __name__ == '__main__':
    main()
