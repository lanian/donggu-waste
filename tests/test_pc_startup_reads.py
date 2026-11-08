"""PC 시작 조회 예산: 실제 HTTP 헤더/본문 정지·브라우저·재로그인 회귀.

실행: python tests/test_pc_startup_reads.py
조회 정지는 로컬 HTTP fixture, 업무 홈·재로그인은 임시 SQLCipher 서버로 검사한다.
"""
from __future__ import annotations

import contextlib
import http.server
import json
import os
import threading
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from test_review_ui import ROOT, pc_server

passed = 0


def ok(condition, message):
    global passed
    if not condition:
        raise AssertionError("실패: " + message)
    passed += 1
    print("  ✓", message, flush=True)


@contextlib.contextmanager
def read_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.do_GET()

        def do_GET(self):
            path = urlsplit(self.path).path
            if path.startswith('/api/'):
                key = path[5:]
                self.server.counts[key] = self.server.counts.get(key, 0) + 1
                mode = self.server.modes.get(key)
                gate = self.server.gates.setdefault(key, threading.Event())
                body = json.dumps({'setup_needed': False, 'logged_in': False} if key == 'state' else {'value': 1}).encode()
                try:
                    if mode == 'headers':
                        gate.wait(20)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    if mode == 'body':
                        self.wfile.write(body[:1]); self.wfile.flush()
                        self.server.body_started.set()
                        gate.wait(20)
                        self.wfile.write(body[1:])
                    else:
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # 조회 deadline이 실제 연결을 중단한 경우다.
                return
            if path == '/read-test.html':
                body, mime = b'<div id="app"></div>', 'text/html'
            else:
                file = ROOT / ('web/' + path[5:] if path.startswith('/web/') else 'pc/ui/' + (path.lstrip('/') or 'index.html'))
                if not file.is_file():
                    self.send_error(404)
                    return
                body = file.read_bytes()
                mime = {'.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2'}.get(file.suffix, 'text/html')
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.counts, server.modes, server.gates = {}, {}, {}
    server.body_started = threading.Event()
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_address[1]}/', server
    finally:
        for gate in server.gates.values():
            gate.set()
        server.shutdown(); server.server_close(); worker.join()


def start_read(page, path, opts):
    page.evaluate("""({ path, opts }) => {
        window.__readDone = false; window.__readResult = null;
        import('/core.js').then(C => C.api(path, opts)).then(value => { window.__readResult = { value }; })
            .catch(error => { window.__readResult = { error: error.message, code: error.code, name: error.name }; })
            .finally(() => { window.__readDone = true; });
    }""", {'path': path, 'opts': opts})


def api_read_regressions(browser):
    with read_server() as (url, server):
        ctx = browser.new_context(service_workers='block')
        page = ctx.new_page()
        try:
            page.goto(url + 'read-test.html')
            page.evaluate("async () => { await import('/core.js'); }")
            for phase in ('headers', 'body'):
                key = 'hold-' + phase
                server.modes[key] = phase
                server.body_started.clear()
                start_read(page, key, {'timeoutMs': 150})
                page.wait_for_function('window.__readDone', timeout=1800)
                result = page.evaluate('window.__readResult')
                ok(result.get('code') == 'READ_TIMEOUT' and '응답' in result.get('error', ''),
                   f'GET {phase} 미완료는 실제 fetch/body 읽기를 중단하고 한글 조회 시간 초과로 반환')
                if phase == 'body':
                    ok(server.body_started.is_set(), 'HTTP 200·일부 JSON 본문을 받은 뒤에도 본문 deadline 유지')
                server.gates[key].set()

            for method, opts in (('GET', {}), ('POST', {'method': 'POST', 'body': {}, 'timeoutMs': 30})):
                key = 'unbounded-' + method
                server.modes[key] = 'headers'
                start_read(page, key, opts)
                page.wait_for_timeout(200)
                ok(not page.evaluate('window.__readDone'), f'{method}: 명시하지 않은 GET·기존 POST에는 새 제한 시간을 적용하지 않음')
                server.gates[key].set()
                page.wait_for_function('window.__readDone')
                ok(page.evaluate('window.__readResult.value.value') == 1, f'{method}: 늦은 정상 응답도 원래 결과로 반환')

            # 성공·실패 시 timer가 남지 않는지 표준 timer API의 활성 handle만 추적한다.
            cleaned = page.evaluate("""async () => {
                const C = await import('/core.js'), set = window.setTimeout, clear = window.clearTimeout;
                const active = new Set();
                window.setTimeout = (fn, ms, ...args) => { const id = set(fn, ms, ...args); if (ms === 150) active.add(id); return id; };
                window.clearTimeout = id => { active.delete(id); return clear(id); };
                try { await C.api('state', { timeoutMs: 150 }); return active.size === 0; }
                finally { window.setTimeout = set; window.clearTimeout = clear; }
            }""")
            ok(cleaned, '정상 응답 뒤 조회 timer를 finally에서 정리')
            server.modes['failure-cleanup'] = 'headers'
            cleaned = page.evaluate("""async () => {
                const C = await import('/core.js'), set = window.setTimeout, clear = window.clearTimeout;
                const active = new Set();
                window.setTimeout = (fn, ms, ...args) => { const id = set(fn, ms, ...args); if (ms === 150) active.add(id); return id; };
                window.clearTimeout = id => { active.delete(id); return clear(id); };
                try {
                    try { await C.api('failure-cleanup', { timeoutMs: 150 }); }
                    catch (error) { return error.code === 'READ_TIMEOUT' && active.size === 0; }
                    return false;
                } finally { window.setTimeout = set; window.clearTimeout = clear; }
            }""")
            server.gates['failure-cleanup'].set()
            ok(cleaned, '조회 시간 초과 뒤에도 timer를 finally에서 정리')
            server.modes['caller-abort'] = 'body'
            server.body_started.clear()
            page.evaluate("""() => {
                window.__callerAbort = new AbortController(); window.__readDone = false;
                import('/core.js').then(C => C.api('caller-abort', { timeoutMs: 1000, signal: window.__callerAbort.signal }))
                    .then(value => { window.__readResult = { value }; })
                    .catch(error => { window.__readResult = { name: error.name, code: error.code }; })
                    .finally(() => { window.__readDone = true; });
            }""")
            assert server.body_started.wait(2), '호출자 취소 전에 실제 HTTP 200·부분 본문을 받아야 합니다'
            page.evaluate('window.__callerAbort.abort()')
            page.wait_for_function('window.__readDone')
            result = page.evaluate('window.__readResult')
            ok(result.get('name') == 'AbortError' and result.get('code') != 'READ_TIMEOUT', '호출자 signal을 body 읽기까지 전파하고 취소를 시간 초과로 바꾸지 않음')
            server.gates['caller-abort'].set()
            before = server.counts.get('state', 0)
            page.evaluate("""async () => {
                const L = await import('/screens/login.js');
                document.getElementById('app').replaceChildren(await L.screenLogin({ setup_needed: false, logged_in: false }));
            }""")
            expect(page.get_by_placeholder('아이디', exact=True)).to_be_visible()
            ok(server.counts.get('state', 0) == before, 'screenLogin(initialState)는 받은 상태를 재사용하고 state를 다시 요청하지 않음')
        finally:
            ctx.close()


def initial_read_ui_regressions(browser):
    for phase in ('headers', 'body'):
        with read_server() as (url, server):
            server.modes['state'] = phase
            ctx = browser.new_context(service_workers='block')
            page = ctx.new_page()
            try:
                page.clock.install()
                page.goto(url, wait_until='domcontentloaded')
                # 실제 요청 시작을 확인한 뒤 브라우저 timer만 10초 진행한다. 응답은 계속 미완료다.
                for _ in range(500):
                    if server.counts.get('state'):
                        break
                    page.wait_for_timeout(10)
                ok(server.counts.get('state') == 1, f'시작 {phase}: 첫 state 조회 1회가 실제 서버에 도착')
                page.clock.fast_forward(10_050)
                expect(page.get_by_role('button', name='다시 시도', exact=True)).to_be_visible()
                ok('응답' in page.locator('#app').inner_text(), f'시작 {phase}: 무기한 불러오는 중 대신 조회 오류·수동 재시도 표시')
                server.modes.pop('state')
                server.gates['state'].set()
                page.get_by_role('button', name='다시 시도', exact=True).click()
                expect(page.get_by_placeholder('아이디', exact=True)).to_be_visible()
                ok(server.counts['state'] == 2, f'시작 {phase}: 수동 재시도 한 번 후 추가 중복 state 없이 로그인 표시')
            finally:
                ctx.close()


def real_server_read_regressions(browser):
    with pc_server() as url:
        ctx = browser.new_context(service_workers='block')
        ctx.route('https://**/*', lambda route: route.abort())
        page = ctx.new_page()
        try:
            page.route('**/read-test.html', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body='<div id="app"><input id="draft" value="가상 미저장 초안"></div>'))
            page.goto(url + 'read-test.html')
            page.evaluate("""async () => {
                const C = await import('/core.js');
                await C.post('setup', { username: 'startupadmin', display_name: '가상 시작 검토자', password: 'startup-password-123' });
                C.setBoot(await C.api('bootstrap')); await C.post('logout');
            }""")
            start_read(page, 'me', {'timeoutMs': 150})
            dialog = page.get_by_role('dialog', name='다시 로그인', exact=True)
            expect(dialog).to_be_visible()
            page.wait_for_timeout(250)
            ok(not page.evaluate('window.__readDone') and dialog.is_visible(), '재로그인 사람 입력 대기는 GET 조회 예산으로 끊지 않음')
            dialog.get_by_label('비밀번호', exact=True).fill('startup-password-123')
            dialog.get_by_role('button', name='로그인하고 계속', exact=True).click()
            page.wait_for_function('window.__readDone')
            result = page.evaluate('window.__readResult')
            ok(not result.get('error') and page.locator('#draft').input_value() == '가상 미저장 초안',
               '재로그인 뒤 새 조회 예산으로 원래 GET을 이어서 보내고 초안을 유지')

            page.goto(url)
            expect(page.get_by_role('heading', name='오늘 현황', exact=True)).to_be_visible()
            page.clock.install()
            paths = ('close?*', 'batches?*', 'access/status', 'backup-status', 'pending', 'readiness', 'ops-status', 'requests?*')
            finished = set()
            page.on('requestfinished', lambda request: finished.add(urlsplit(request.url).path.removeprefix('/api/')))
            for path in paths:
                held = []
                pattern = '**/api/' + path
                handler = lambda route: held.append(route)
                page.route(pattern, handler)
                finished.clear()
                try:
                    page.evaluate("""() => {
                        window.__homeReadDone = false;
                        import('/core.js').then(C => C.render(true)).finally(() => { window.__homeReadDone = true; });
                    }""")
                    expected = {item.split('?')[0] for item in paths if item != path}
                    # 정상 응답 7개의 실제 body 수신이 끝난 뒤, 의도적으로 멈춘 읽기의 timer만 진행한다.
                    for _ in range(500):
                        if held and expected <= finished:
                            break
                        page.wait_for_timeout(10)
                    ok(bool(held) and expected <= finished, f'홈 {path}: 해당 요청만 보류하고 나머지 응답은 실제 수신 완료')
                    page.clock.fast_forward(10_050)
                    page.wait_for_function('window.__homeReadDone', timeout=2000)
                    if path in ('close?*', 'batches?*'):
                        expect(page.get_by_role('button', name='다시 시도', exact=True)).to_be_visible()
                        ok(True, f'홈 필수 {path}: 조회 시간 초과를 오류와 재시도로 표시')
                    else:
                        expect(page.get_by_role('heading', name='오늘 현황', exact=True)).to_be_visible()
                        if path == 'requests?*':
                            expect(page.locator('.workflow-primary')).to_contain_text('미배정 현황 확인')
                        ok(True, f'홈 선택 {path}: 제한 시간 뒤 기존 null 처리로 현황 표시')
                finally:
                    page.unroute(pattern, handler)
                    for route in held:
                        with contextlib.suppress(Exception):
                            route.continue_()
                page.evaluate("async () => { await (await import('/core.js')).render(true); }")
                expect(page.get_by_role('heading', name='오늘 현황', exact=True)).to_be_visible()
        finally:
            ctx.close()


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME', '/usr/bin/google-chrome'), headless=True)
        try:
            api_read_regressions(browser)
            initial_read_ui_regressions(browser)
            real_server_read_regressions(browser)
        finally:
            browser.close()
    print(f'\n통과 {passed}개')


if __name__ == '__main__':
    main()
