"""가상 자료와 실제 임시 PC 서버로 현장 접수 확인·단가 검토를 검증한다."""
from __future__ import annotations

import contextlib
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import expect, sync_playwright
from test_review_ui import review_workdir, stop_pc_server

ROOT = Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def field_server():
    with review_workdir() as work:
        env = {**os.environ, 'DG_STATE': str(Path(work) / 'state'), 'DG_OUT': str(Path(work) / 'out'),
               'DG_DB_KEY': 'test-db-key-not-for-production', 'DG_NO_SCHEDULER': '1', 'PYTHONUTF8': '1'}
        subprocess.run([sys.executable, str(ROOT / 'pc/pc_tool.py'), 'init'], env=env, check=True, capture_output=True)
        seed = """
import json, sys
sys.path.insert(0, sys.argv[1])
from dgpc import store, service as S
con = store.connect()
fee = next(r for r in S.FEE_ROWS.values() if r['status'] == 'CONFIRMED' and r.get('price', 0) > 0 and r['unit_label'] == '개')
out = {}
for key in ('dirty', 'save_reentry', 'save_fail', 'fee_restore', 'save_lost', 'delayed', 'lost', 'pending', 'session_old', 'session_save'):
    item = {'fee_rate_id': fee['id'], 'qty': 2} if key not in ('pending', 'fee_restore') else {'name': fee['name'], 'spec': fee['spec'], 'qty': 2, 'unit': fee['unit'], 'unit_label': fee['unit_label']}
    r = S.create_request(con, {'name': '가상 검토 주민', 'phone': '01000000000', 'address': '가상구 예시로 1', 'dong': '충장동', 'planned_date': S.today_kst(), 'items': [item]})
    con.execute('UPDATE request SET origin_device=? WHERE id=?', ('fixture-phone', r['id']))
    out[key] = r['id']
con.commit()
print(json.dumps({'requests': out, 'fee': fee}, ensure_ascii=False))
con.close()
"""
        seeded = subprocess.run([sys.executable, '-c', seed, str(ROOT / 'pc')], env=env, check=True,
                                capture_output=True, text=True, encoding='utf8')
        fixtures = json.loads(seeded.stdout)
        with socket.socket() as sk:
            sk.bind(('127.0.0.1', 0))
            port = sk.getsockname()[1]
        url = f'http://127.0.0.1:{port}/'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with (Path(work) / 'server.log').open('w', encoding='utf8') as log:
            process = subprocess.Popen([sys.executable, str(ROOT / 'pc/pc_tool.py'), 'serve', '--port', str(port)],
                                       env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise AssertionError('임시 PC 서버가 종료되었습니다')
                    try:
                        with opener.open(url, timeout=1):
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise AssertionError('임시 PC 서버가 시작하지 못했습니다')
                yield url, fixtures
            finally:
                stop_pc_server(process)


def session_reentry(page, url, fixtures, api):
    rid = fixtures['requests']['session_old']
    page.goto(url + '#/request/' + rid)
    check = page.get_by_role('button', name='확인 완료', exact=True)
    expect(check).to_be_visible()
    page.evaluate("""async rid => {
        window.__oldFieldSession = (await import('/core.js')).sessionOwner();
        const native = window.fetch; let hold = true; window.__sessionFieldPosts = 0;
        window.fetch = (path, options) => {
          if (String(path).endsWith('/' + rid + '/field-check')) {
            window.__sessionFieldPosts++;
            if (hold) { hold = false; return new Promise((resolve, reject) => { window.__rejectOldFieldCheck = () => reject(new TypeError('가상 이전 세션 확인 응답 종료')); }); }
          }
          return native(path, options);
        };
    }""", rid)
    check.click()
    expect(check).to_be_disabled()
    page.get_by_role('link', name='로그아웃', exact=True).click()
    page.get_by_placeholder('아이디', exact=True).fill('field_admin')
    page.get_by_placeholder('비밀번호', exact=True).fill('field-review-test-123')
    page.get_by_role('button', name='로그인', exact=True).click()
    expect(page.get_by_role('button', name='확인 완료', exact=True)).to_be_visible()
    assert page.evaluate("async () => window.__oldFieldSession !== (await import('/core.js')).sessionOwner()")
    expect(check).to_be_enabled()
    expect(page.get_by_label('요청 메모', exact=False)).to_be_enabled()
    check.click()
    expect(check).to_be_hidden()
    assert api('requests/' + rid)['field_checked_at']
    memo = page.get_by_label('요청 메모', exact=False)
    memo.fill('같은 계정의 새 인증 세션에서 작성한 가상 초안')
    page.evaluate('window.__rejectOldFieldCheck()')
    page.wait_for_timeout(200)
    expect(memo).to_have_value('같은 계정의 새 인증 세션에서 작성한 가상 초안')
    expect(page.get_by_label('현장 접수 사무실 확인', exact=True)).to_contain_text('사무실 확인 (field_admin)')
    assert page.evaluate('window.__sessionFieldPosts') == 2
    assert api('requests/' + rid)['req_ver'] == 1
    print('PASS: 로그아웃·같은 계정 재로그인 후 이전 확인 작업을 채택하지 않고 늦은 오류도 새 초안을 덮지 않음', flush=True)
    saved_id = fixtures['requests']['session_save']
    page.goto(url + '#/request/' + saved_id)
    expect(page.get_by_role('button', name='확인 완료', exact=True)).to_be_visible()
    expect(page.get_by_label('요청 메모', exact=False)).to_have_value('')
    page.get_by_label('요청 메모', exact=False).fill('이전 인증 세션에서 저장한 가상 내용')
    page.get_by_placeholder('수정 사유 (필수)', exact=True).fill('가상 늦은 저장 성공 응답')
    page.evaluate("""rid => {
        const native = window.fetch;
        window.fetch = async (path, options) => {
          const response = await native(path, options);
          if (String(path).endsWith('/' + rid) && options?.method === 'POST') {
            window.__oldSaveStatus = response.status; window.__oldSaveBody = await response.clone().json();
            return new Promise(resolve => { window.__releaseOldSaveResponse = () => resolve(response); });
          }
          return response;
        };
    }""", saved_id)
    page.get_by_role('button', name='수정 저장', exact=True).click()
    page.wait_for_function("() => typeof window.__releaseOldSaveResponse === 'function'")
    assert api('requests/' + saved_id)['req_ver'] == 2, page.evaluate('({status:window.__oldSaveStatus, body:window.__oldSaveBody})')
    page.get_by_role('link', name='로그아웃', exact=True).click()
    page.get_by_placeholder('아이디', exact=True).fill('field_admin')
    page.get_by_placeholder('비밀번호', exact=True).fill('field-review-test-123')
    page.get_by_role('button', name='로그인', exact=True).click()
    memo = page.get_by_label('요청 메모', exact=False)
    expect(memo).to_be_enabled()
    expect(page.get_by_role('button', name='확인 완료', exact=True)).to_be_enabled()
    memo.fill('늦은 저장 성공 응답에도 남아야 하는 새 세션 초안')
    page.evaluate('window.__releaseOldSaveResponse()')
    page.wait_for_timeout(250)
    expect(memo).to_have_value('늦은 저장 성공 응답에도 남아야 하는 새 세션 초안')
    expect(page.get_by_role('button', name='확인 완료', exact=True)).to_be_disabled()
    stored = api('requests/' + saved_id)
    assert stored['memo'] == '이전 인증 세션에서 저장한 가상 내용' and stored['req_ver'] == 2 and stored['field_checked_at'] is None
    print('PASS: 이전 세션의 실제 저장 성공 응답이 늦게 와도 새 세션의 같은 접수 초안을 덮지 않음', flush=True)


def main(session_only=False):
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-field-review-ui-'))
    (output / 'shots').mkdir(parents=True, exist_ok=True)
    with field_server() as (url, fixtures), sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        context = browser.new_context(service_workers='block', viewport={'width': 1440, 'height': 1000})
        page = context.new_page()
        page.set_default_timeout(12000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def api(path, body=None):
            opts = {'headers': {'X-DG-CSRF': '1'}}
            response = context.request.get(url + 'api/' + path, **opts) if body is None else context.request.post(url + 'api/' + path, data=body, **opts)
            assert response.ok, (path, response.status, response.text())
            return response.json()

        def open_request(key):
            rid = fixtures['requests'][key]
            receipt = api('requests/' + rid)['receipt_no']
            page.goto(url + '#/request/' + rid)
            expect(page.get_by_role('heading', name='접수 ' + receipt, exact=True)).to_be_visible()
            expect(page.get_by_role('button', name='확인 완료', exact=True)).to_be_visible()
            return rid

        try:
            api('setup', {'username': 'field_admin', 'display_name': '가상 현장 검토 담당', 'password': 'field-review-test-123'})
            if session_only:
                session_reentry(page, url, fixtures, api)
                return
            rid = open_request('dirty')
            memo = page.get_by_label('요청 메모', exact=False)
            memo.fill('저장 전에는 사무실 확인할 수 없는 가상 수정')
            check = page.get_by_role('button', name='확인 완료', exact=True)
            # 수정 전 구현은 여기서 실제 DB에 FIELD_CHECK를 남긴다. 비활성 단추에도 핸들러 방어가 필요하다.
            check.dispatch_event('click')
            page.wait_for_timeout(250)
            stored = api('requests/' + rid)
            assert stored['field_checked_at'] is None, '미저장 수정이 있는데 사무실 확인이 DB에 기록됨'
            assert stored['req_ver'] == 1 and stored['memo'] == ''
            expect(memo).to_have_value('저장 전에는 사무실 확인할 수 없는 가상 수정')
            expect(check).to_be_disabled()
            expect(page.get_by_text('수정 내용을 먼저 저장한 뒤 확인 완료를 누르세요.', exact=True)).to_be_visible()
            page.screenshot(path=str(output / 'shots' / 'save-before-confirm.png'), full_page=True)
            print('PASS: 미저장 수정은 실제 DB의 사무실 확인을 막고 초안을 보존함', flush=True)
            reason = page.get_by_placeholder('수정 사유 (필수)', exact=True)
            reason.fill('가상 저장 순서 검증')
            page.route('**/api/requests/' + rid, lambda route: route.fulfill(status=503, content_type='application/json', body='{"error":"가상 수정 저장 실패"}'), times=1)
            page.get_by_role('button', name='수정 저장', exact=True).click()
            expect(page.get_by_text('가상 수정 저장 실패', exact=True)).to_be_visible()
            expect(memo).to_have_value('저장 전에는 사무실 확인할 수 없는 가상 수정')
            expect(reason).to_have_value('가상 저장 순서 검증')
            expect(check).to_be_disabled()
            assert api('requests/' + rid)['field_checked_at'] is None
            with page.expect_response(lambda response: response.url.endswith('/api/requests/' + rid) and response.request.method == 'POST') as saved:
                page.get_by_role('button', name='수정 저장', exact=True).click()
            assert saved.value.ok, saved.value.text()
            expect(reason).to_have_value('')
            expect(check).to_be_enabled()
            stored = api('requests/' + rid)
            assert stored['req_ver'] == 2 and stored['memo'] == memo.input_value() and stored['field_checked_at'] is None
            page.route('**/api/requests/' + rid + '/field-check', lambda route: route.fulfill(status=503, content_type='application/json', body='{"error":"가상 확인 실패"}'), times=1)
            check.click()
            expect(page.get_by_text('가상 확인 실패', exact=True)).to_be_visible()
            expect(memo).to_be_enabled()
            expect(check).to_be_enabled()
            expect(check).to_be_focused()
            assert api('requests/' + rid)['field_checked_at'] is None
            check.click()
            expect(page.get_by_label('현장 접수 사무실 확인', exact=True)).to_contain_text('사무실 확인 (field_admin)')
            expect(check).to_be_hidden()
            expect(page.get_by_label('현장 접수 사무실 확인', exact=True).locator('[tabindex="-1"]')).to_be_focused()
            assert api('requests/' + rid)['req_ver'] == 2
            print('PASS: 수정/확인 실패 후 초안·사유를 보존하고 저장→확인으로 한 번만 개정함', flush=True)

            saving_id = open_request('save_reentry')
            page.get_by_label('요청 메모', exact=False).fill('서버 도착 전 보류한 수정 내용')
            page.get_by_placeholder('수정 사유 (필수)', exact=True).fill('가상 저장 중 재진입')
            page.evaluate("""rid => {
                const native = window.fetch; window.__heldUpdatePosts = 0; window.__reentryFieldPosts = 0;
                window.fetch = (path, options) => {
                  if (String(path).endsWith('/' + rid + '/field-check')) window.__reentryFieldPosts++;
                  if (String(path).endsWith('/' + rid) && options?.method === 'POST') {
                    window.__heldUpdatePosts++;
                    return new Promise((resolve, reject) => { window.__releaseUpdate = () => native(path, options).then(resolve, reject); });
                  }
                  return native(path, options);
                };
            }""", saving_id)
            page.get_by_role('button', name='수정 저장', exact=True).click()
            page.wait_for_function("() => typeof window.__releaseUpdate === 'function'")
            page.evaluate("location.hash = '#/requests'")
            expect(page.get_by_role('heading', name='접수', exact=True)).to_be_visible()
            page.evaluate("rid => { location.hash = '#/request/' + rid; }", saving_id)
            check = page.get_by_role('button', name='확인 완료', exact=True)
            expect(check).to_be_visible()
            check.dispatch_event('click')
            page.wait_for_timeout(250)
            stored = api('requests/' + saving_id)
            assert stored['field_checked_at'] is None, '진행 중인 수정 저장이 있는데 재진입 화면에서 사무실 확인이 기록됨'
            assert stored['req_ver'] == 1 and stored['memo'] == ''
            expect(check).to_be_disabled()
            expect(page.get_by_label('요청 메모', exact=False)).to_be_disabled()
            assert page.evaluate('window.__reentryFieldPosts') == 0
            page.screenshot(path=str(output / 'shots' / 'save-reentry-pending.png'), full_page=True)
            page.evaluate('window.__releaseUpdate()')
            expect(page.get_by_label('요청 메모', exact=False)).to_have_value('서버 도착 전 보류한 수정 내용')
            expect(page.get_by_label('요청 메모', exact=False)).to_be_enabled()
            expect(check).to_be_enabled()
            stored = api('requests/' + saving_id)
            assert stored['req_ver'] == 2 and stored['field_checked_at'] is None
            assert page.evaluate('window.__heldUpdatePosts') == 1
            check.click()
            expect(check).to_be_hidden()
            assert page.evaluate('window.__reentryFieldPosts') == 1
            print('PASS: 저장 POST 전 보류·목록 이동·재진입 시 확인/편집을 잠그고 최신 저장 내용을 본 뒤 확인함', flush=True)

            failed_id = open_request('save_fail')
            page.get_by_label('요청 메모', exact=False).fill('재진입 뒤 저장 실패해도 복구할 수정 초안')
            page.get_by_placeholder('수정 사유 (필수)', exact=True).fill('가상 재진입 실패 복구 사유')
            page.evaluate("""rid => {
                const native = window.fetch; let hold = true;
                window.fetch = (path, options) => {
                  if (hold && String(path).endsWith('/' + rid) && options?.method === 'POST') {
                    hold = false;
                    return new Promise((resolve, reject) => { window.__releaseFailedUpdate = () => native(path, options).then(resolve, reject); });
                  }
                  return native(path, options);
                };
            }""", failed_id)
            page.get_by_role('button', name='수정 저장', exact=True).click()
            page.wait_for_function("() => typeof window.__releaseFailedUpdate === 'function'")
            page.evaluate("location.hash = '#/requests'")
            expect(page.get_by_role('heading', name='접수', exact=True)).to_be_visible()
            page.evaluate("rid => { location.hash = '#/request/' + rid; }", failed_id)
            expect(page.get_by_label('요청 메모', exact=False)).to_be_disabled()
            page.route('**/api/requests/' + failed_id, lambda route: route.fulfill(status=503, content_type='application/json', body='{"error":"가상 재진입 수정 저장 실패"}'), times=1)
            page.evaluate('window.__releaseFailedUpdate()')
            expect(page.get_by_text('가상 재진입 수정 저장 실패', exact=True)).to_be_visible()
            check = page.get_by_role('button', name='확인 완료', exact=True)
            expect(check).to_be_disabled()
            expect(page.locator('.mine-box')).to_contain_text('재진입 뒤 저장 실패해도 복구할 수정 초안')
            page.get_by_role('button', name='내 값 다시 넣기', exact=True).click()
            expect(page.get_by_label('요청 메모', exact=False)).to_have_value('재진입 뒤 저장 실패해도 복구할 수정 초안')
            expect(page.get_by_placeholder('수정 사유 (필수)', exact=True)).to_have_value('가상 재진입 실패 복구 사유')
            expect(check).to_be_disabled()
            assert api('requests/' + failed_id)['req_ver'] == 1 and api('requests/' + failed_id)['field_checked_at'] is None
            page.get_by_role('button', name='수정 저장', exact=True).click()
            expect(page.get_by_placeholder('수정 사유 (필수)', exact=True)).to_have_value('')
            expect(check).to_be_enabled()
            stored = api('requests/' + failed_id)
            assert stored['req_ver'] == 2 and stored['memo'] == '재진입 뒤 저장 실패해도 복구할 수정 초안' and stored['field_checked_at'] is None
            print('PASS: 재진입 뒤 저장 실패 시 원래 수정 초안·사유·오류를 보존하고 확인을 막음', flush=True)

            restore_id = open_request('fee_restore')
            original = api('requests/' + restore_id)['items'][0]
            assert original['fee_rate_id'] is None
            page.locator('.items .it').get_by_role('button', name='삭제', exact=True).click()
            picker = page.get_by_role('region', name='버릴 물건 선택', exact=True)
            picker.get_by_role('searchbox', name='품목명·규격 검색', exact=True).fill(fixtures['fee']['name'] + ' ' + fixtures['fee']['spec'])
            picker.locator('[data-fee-id="' + fixtures['fee']['id'] + '"]').click()
            picker.get_by_role('spinbutton', name='추가 수량', exact=True).fill('2')
            picker.get_by_role('button', name='품목 추가', exact=True).click()
            page.get_by_placeholder('수정 사유 (필수)', exact=True).fill('가상 동일 표시명 정식 품목으로 교체')
            page.evaluate("""rid => {
                const native = window.fetch; let hold = true; window.__restoredItemSubmissions = [];
                window.fetch = (path, options) => {
                  if (String(path).endsWith('/' + rid) && options?.method === 'POST') {
                    window.__restoredItemSubmissions.push(JSON.parse(options.body));
                    if (hold) { hold = false; return new Promise((resolve, reject) => { window.__releaseFeeRestore = () => native(path, options).then(resolve, reject); }); }
                  }
                  return native(path, options);
                };
            }""", restore_id)
            page.get_by_role('button', name='수정 저장', exact=True).click()
            page.wait_for_function("() => typeof window.__releaseFeeRestore === 'function'")
            submitted = page.evaluate('window.__restoredItemSubmissions[0].items')
            assert submitted[0]['fee_rate_id'] == fixtures['fee']['id'] and submitted[0]['qty'] == original['qty']
            assert submitted[0]['name'] == original['name'] and submitted[0]['spec'] == original['spec']
            page.evaluate("location.hash = '#/requests'")
            expect(page.get_by_role('heading', name='접수', exact=True)).to_be_visible()
            page.evaluate("rid => { location.hash = '#/request/' + rid; }", restore_id)
            expect(page.get_by_label('요청 메모', exact=False)).to_be_disabled()
            page.route('**/api/requests/' + restore_id, lambda route: route.fulfill(status=503, content_type='application/json', body='{"error":"가상 품목 교체 저장 실패"}'), times=1)
            page.evaluate('window.__releaseFeeRestore()')
            expect(page.get_by_text('가상 품목 교체 저장 실패', exact=True)).to_be_visible()
            page.get_by_role('button', name='내 값 다시 넣기', exact=True).click()
            page.get_by_role('button', name='수정 저장', exact=True).click()
            expect(page.get_by_placeholder('수정 사유 (필수)', exact=True)).to_have_value('')
            retried = page.evaluate('window.__restoredItemSubmissions[1].items')
            assert retried == submitted, '동일 표시명 품목 교체 복구에서 요금표/단위/행 식별자 payload가 달라짐'
            stored = api('requests/' + restore_id)
            assert stored['items'][0]['fee_rate_id'] == fixtures['fee']['id'] and stored['state']['fee']['pending_count'] == 0
            assert stored['req_ver'] == 2 and stored['field_checked_at'] is None
            print('PASS: 같은 이름·규격·수량의 미확정→정식 품목 교체도 실패 후 전체 품목 payload로 복구·재저장함', flush=True)

            saved_lost_id = open_request('save_lost')
            page.get_by_label('요청 메모', exact=False).fill('실제로 저장됐지만 응답이 유실된 가상 수정')
            page.get_by_placeholder('수정 사유 (필수)', exact=True).fill('가상 저장 응답 유실')
            page.evaluate("""rid => {
                const native = window.fetch;
                window.fetch = async (path, options) => {
                  const response = await native(path, options);
                  if (String(path).endsWith('/' + rid) && options?.method === 'POST') {
                    return new Promise((resolve, reject) => { window.__rejectStoredUpdate = () => reject(new TypeError('가상 저장 응답 유실')); });
                  }
                  return response;
                };
            }""", saved_lost_id)
            page.get_by_role('button', name='수정 저장', exact=True).click()
            page.wait_for_function("() => typeof window.__rejectStoredUpdate === 'function'")
            assert api('requests/' + saved_lost_id)['req_ver'] == 2
            page.evaluate("location.hash = '#/requests'")
            expect(page.get_by_role('heading', name='접수', exact=True)).to_be_visible()
            page.evaluate("rid => { location.hash = '#/request/' + rid; }", saved_lost_id)
            expect(page.get_by_label('요청 메모', exact=False)).to_be_disabled()
            page.evaluate('window.__rejectStoredUpdate()')
            check = page.get_by_role('button', name='확인 완료', exact=True)
            expect(check).to_be_enabled()
            expect(page.locator('.mine-box')).to_have_count(0)
            expect(page.get_by_label('요청 메모', exact=False)).to_have_value('실제로 저장됐지만 응답이 유실된 가상 수정')
            assert api('requests/' + saved_lost_id)['req_ver'] == 2
            print('PASS: 저장 응답 유실 뒤 최신 DB 값이 제출 내용과 같으면 재저장을 요구하거나 개정을 중복 생성하지 않음', flush=True)

            delayed = open_request('delayed')
            page.evaluate("""rid => {
                const native = window.fetch; window.__fieldPosts = 0;
                window.fetch = (path, options) => {
                  if (String(path).endsWith('/' + rid + '/field-check')) {
                    window.__fieldPosts++;
                    return new Promise((resolve, reject) => { window.__releaseFieldCheck = () => native(path, options).then(resolve, reject); });
                  }
                  return native(path, options);
                };
            }""", delayed)
            check = page.get_by_role('button', name='확인 완료', exact=True)
            check.click()
            check.dispatch_event('click')
            expect(check).to_be_disabled()
            expect(page.get_by_label('요청 메모', exact=False)).to_be_disabled()
            page.evaluate("location.hash = '#/requests'")
            expect(page.get_by_role('heading', name='접수', exact=True)).to_be_visible()
            page.evaluate("rid => { location.hash = '#/request/' + rid; }", delayed)
            expect(page.get_by_role('button', name='확인 완료', exact=True)).to_be_disabled()
            expect(page.get_by_text('사무실 확인 처리 중입니다. 중복 전송하지 않고 저장 여부를 확인합니다.', exact=True)).to_be_visible()
            assert page.evaluate('window.__fieldPosts') == 1
            # 다른 접수에서 작성 중인 초안은 이전 확인 응답이 돌아와도 덮지 않는다.
            page.evaluate("rid => { location.hash = '#/request/' + rid; }", rid)
            memo = page.get_by_label('요청 메모', exact=False)
            expect(memo).to_be_enabled()
            memo.fill('다른 접수의 늦은 확인 응답에도 보존하는 가상 초안')
            page.evaluate('window.__releaseFieldCheck()')
            page.wait_for_function("() => !document.querySelector('[aria-label=\"현장 접수 사무실 확인\"]').textContent.includes('처리 중')")
            page.wait_for_timeout(150)
            expect(page).to_have_url(url + '#/request/' + rid)
            expect(memo).to_have_value('다른 접수의 늦은 확인 응답에도 보존하는 가상 초안')
            assert page.evaluate('window.__fieldPosts') == 1
            assert api('requests/' + delayed)['field_checked_at'] and api('requests/' + delayed)['req_ver'] == 1
            page.evaluate("rid => { location.hash = '#/request/' + rid; }", delayed)
            expect(page.get_by_label('현장 접수 사무실 확인', exact=True)).to_contain_text('사무실 확인 (field_admin)')
            expect(page.get_by_label('요청 메모', exact=False)).to_be_enabled()
            print('PASS: 확인 중 재진입·중복 클릭을 막고 늦은 응답은 다른 접수 초안을 덮지 않음', flush=True)

            lost = open_request('lost')
            page.evaluate("""rid => {
                const native = window.fetch; let failRead = true; window.__lostFieldPosts = 0; window.__fieldCommitted = false;
                window.fetch = async (path, options) => {
                  if (String(path).endsWith('/' + rid + '/field-check')) {
                    window.__lostFieldPosts++; await native(path, options); window.__fieldCommitted = true;
                    throw new TypeError('가상 확인 응답 유실');
                  }
                  if (window.__fieldCommitted && failRead && String(path).endsWith('/' + rid) && (!options?.method || options.method === 'GET')) {
                    failRead = false; throw new TypeError('가상 후속 조회 실패');
                  }
                  return native(path, options);
                };
            }""", lost)
            check = page.get_by_role('button', name='확인 완료', exact=True)
            check.click()
            expect(page.get_by_label('현장 접수 사무실 확인', exact=True)).to_contain_text('다시 누르면 저장 여부부터 조회합니다.')
            assert api('requests/' + lost)['field_checked_at']
            expect(check).to_be_enabled()
            check.click()
            expect(check).to_be_hidden()
            assert page.evaluate('window.__lostFieldPosts') == 1
            assert api('requests/' + lost)['req_ver'] == 1
            print('PASS: 실제 저장 뒤 응답·후속 조회가 유실돼도 재시도는 상태 조회로 복구하고 POST를 반복하지 않음', flush=True)

            pending = open_request('pending')
            price_notice = page.get_by_role('status', name='금액 확인 대기', exact=True)
            expect(price_notice).to_contain_text('금액 확인 대기 1건')
            page.get_by_role('button', name='확인 완료', exact=True).click()
            expect(page.get_by_label('현장 접수 사무실 확인', exact=True)).to_contain_text('사무실 확인 (field_admin)')
            expect(price_notice).to_contain_text('사무실 확인과 단가 확정은 별도')
            assert api('requests/' + pending)['state']['fee']['pending_count'] == 1
            memo = page.get_by_label('요청 메모', exact=False)
            memo.fill('수동 단가 적용 중에도 보존하는 가상 초안')
            page.get_by_role('button', name='유사 품목 적용', exact=True).click()
            query = page.get_by_role('searchbox', name='유사 품목 검색', exact=True)
            expected_query = ' '.join(filter(None, [fixtures['fee']['name'], fixtures['fee']['spec']]))
            expect(query).to_have_value(expected_query)
            candidates = page.locator('.linkbox .results button')
            expect(candidates.first).to_be_visible()
            apply = page.locator('.linkbox').get_by_role('button', name='적용', exact=True)
            apply.click()
            expect(page.get_by_text('적용할 품목 행을 고르세요.', exact=True)).to_be_visible()
            assert api('requests/' + pending)['state']['fee']['pending_count'] == 1
            candidates.first.click()
            query.fill('없는가상검색어')
            expect(candidates).to_have_count(0)
            query.fill(expected_query)
            apply.click()
            expect(page.get_by_text('적용할 품목 행을 고르세요.', exact=True)).to_be_visible()
            candidates.first.click()
            apply.click()
            expect(page.get_by_text('적용 근거를 적어 주세요 (예: 같은 재질·크기, 담당 부서 확인).', exact=True)).to_be_visible()
            evidence = page.get_by_placeholder('적용 근거 (예: 같은 재질·크기, 담당 부서 확인)', exact=True)
            reviewer = page.locator('.linkbox').get_by_placeholder('확인자', exact=True)
            evidence.fill('가상 검토: 같은 품목과 규격 확인')
            reviewer.fill('')
            apply.click()
            expect(page.get_by_text('확인자를 적어 주세요.', exact=True)).to_be_visible()
            expect(memo).to_have_value('수동 단가 적용 중에도 보존하는 가상 초안')
            reviewer.fill('가상 수동 확인자')
            page.screenshot(path=str(output / 'shots' / 'manual-price-review.png'), full_page=True)
            apply.click()
            expect(price_notice).to_have_count(0)
            expect(memo).to_have_value('수동 단가 적용 중에도 보존하는 가상 초안')
            stored = api('requests/' + pending)
            line = stored['state']['lines'][0]
            assert stored['req_ver'] == 1 and stored['memo'] == ''
            assert line['name'] == fixtures['fee']['name'] and line['spec'] == fixtures['fee']['spec']
            assert line['link']['reason'] == '가상 검토: 같은 품목과 규격 확인'
            assert line['link']['confirmed_by'] == '가상 수동 확인자'
            assert stored['state']['fee']['pending_count'] == 0
            print('PASS: 단가 대기 별도 표시·검색어 자동 입력·명시적 선택/근거/확인자 유지·수정 초안 보존', flush=True)
            session_reentry(page, url, fixtures, api)
        except BaseException:
            page.screenshot(path=str(output / 'shots' / 'failure.png'), full_page=True)
            print('Browser errors:', errors, flush=True)
            print('Current view:', page.locator('body').inner_text()[-4000:], flush=True)
            raise
        finally:
            context.close()
            browser.close()
        assert not errors, errors
    print('PASS: PC 현장 접수 검토 UI', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--session-only', action='store_true', help='인증 세션 교체 회귀만 실행')
    main(session_only=parser.parse_args().session_only)
