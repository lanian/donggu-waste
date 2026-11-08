"""검토 회귀: 실제 브라우저 저장소·PIN 잠금과 응답 유실 뒤 재시도.

실행: python tests/test_review_ui.py [--suite storage|retry|requests|cleanup|all]
모든 자료는 가상 자료이며 PC 서버·브라우저 자료는 임시 폴더에만 둔다.
"""
from __future__ import annotations

import argparse
import contextlib
import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from functools import partial
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PIN = "246810"
passed = 0


def ok(condition, message):
    global passed
    if not condition:
        raise AssertionError("실패: " + message)
    passed += 1
    print("  ✓", message, flush=True)


@contextlib.contextmanager
def mobile_server():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        # Windows MIME 등록값(.js=text/plain)에 좌우되지 않게 한다.
        extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                          ".js": "text/javascript", ".mjs": "text/javascript"}

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(ROOT / "web")))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join()


def unlock(page):
    page.get_by_placeholder("PIN", exact=True).fill(PIN)
    page.get_by_role("button", name="잠금 해제", exact=True).click()
    expect(page.get_by_placeholder("PIN", exact=True)).to_have_count(0)


def storage_regressions(browser):
    with mobile_server() as url:
        ctx = browser.new_context(service_workers="block")
        page = ctx.new_page()
        try:
            page.goto(url)
            expect(page.get_by_placeholder("숫자 6자리 이상")).to_be_visible()
            page.get_by_placeholder("숫자 6자리 이상").fill(PIN)
            page.get_by_placeholder("한 번 더", exact=True).fill(PIN)
            page.get_by_role("button", name="PIN 설정", exact=True).click()
            expect(page.get_by_role("button", name="이 휴대폰만으로 사용")).to_be_visible()
            page.evaluate("""async () => {
                const C = await import('./core.js');
                C.setSt({ ...C.emptyState(), mode: 'SOLO' });
                await C.commit();
                await C.render();
                const put = IDBObjectStore.prototype.put;
                window.__reviewPut = put;
                window.__reviewFailState = true;
                IDBObjectStore.prototype.put = function(value, key) {
                    if (window.__reviewFailState && key === 's:state')
                        throw new DOMException('시험용 공간 부족', 'QuotaExceededError');
                    return put.apply(this, arguments);
                };
                await C.addEvent({ request_id: 'review-stop', req_ver: 1, assign_ver: 1 },
                    'CONTACT', { result: 'NO_ANSWER' });
            }""")
            expect(page.locator("#save-fail")).to_be_visible()
            page.evaluate("async () => (await import('./core.js')).lockNow()")
            expect(page.get_by_placeholder("PIN", exact=True)).to_be_visible()
            locked = page.evaluate("""async () => {
                const C = await import('./core.js'), S = await import('./store.js');
                let denied = false;
                try { await S.getSecure('state'); } catch (e) { denied = e.code === 'LOCKED'; }
                return C.st === null && !S.isUnlocked() && denied;
            }""")
            ok(locked, "저장 실패 뒤에도 PIN 잠금: 평문 상태·데이터 키를 비우고 복호화 거부")
            unlock(page)
            restored = page.evaluate("async () => (await import('./core.js')).st.events.length")
            ok(restored == 1, "저장 실패 뒤 잠금·PIN 해제해도 미저장 현장 기록 복원")
            expect(page.locator("#save-fail")).to_be_visible()
            page.evaluate("""async () => {
                window.__reviewFailState = false;
                await (await import('./core.js')).commit();
            }""")
            expect(page.locator("#save-fail")).to_have_count(0)
            page.reload()
            unlock(page)
            ok(page.evaluate("async () => (await import('./core.js')).st.events.length") == 1,
               "저장 공간 정상화 뒤 다시 저장·새로고침해도 복구 기록 유지")
            # 삭제 알림을 받으면 앱은 의도대로 새로고침한다. 저장소 수명 검사는 같은 출처의 빈 문서에서 한다.
            page.route("**/review-storage.html", lambda route: route.fulfill(content_type="text/html", body="<main></main>"))
            page.goto(url + "review-storage.html")
            page.evaluate("async pin => (await import('./store.js')).unlock(pin)", PIN)
            wiped = page.evaluate("""async () => {
                const S = await import('./store.js');
                const put = IDBObjectStore.prototype.put;
                IDBObjectStore.prototype.put = function(value, key) {
                    if (key === 's:state') throw new DOMException('시험용 공간 부족', 'QuotaExceededError');
                    return put.apply(this, arguments);
                };
                try { await S.putSecure('state', { private: '가상 복구 자료' }); } catch {}
                IDBObjectStore.prototype.put = put;
                await S.wipeAll();
                await S.setupPin('135790');
                return (await S.getSecure('state')) === null;
            }""")
            ok(wiped, "모든 자료 삭제·새 PIN 설정 뒤 이전 미저장 기록을 복구하지 않음")
            stale = page.evaluate("""async () => {
                const S = await import('./store.js');
                const encrypt = crypto.subtle.encrypt.bind(crypto.subtle);
                let release, ready;
                const waiting = new Promise(resolve => { ready = resolve; });
                crypto.subtle.encrypt = async (...args) => {
                    const ciphertext = await encrypt(...args);
                    ready();
                    return new Promise(resolve => { release = () => resolve(ciphertext); });
                };
                const oldWrite = S.putSecure('state', { private: '이전 세대 가상 자료' }).catch(() => {});
                await waiting;
                crypto.subtle.encrypt = encrypt;
                await S.wipeAll();
                await S.setupPin('135790');
                release();
                await oldWrite;
                return (await S.getSecure('state')) === null;
            }""")
            ok(stale, "삭제 전 시작한 늦은 암호화 저장이 새 저장소에 이전 자료를 되살리지 않음")
        finally:
            ctx.close()


def storage_cross_tab_regression(browser):
    with mobile_server() as url:
        ctx = browser.new_context(service_workers="block")
        a = ctx.new_page()
        try:
            a.goto(url)
            expect(a.get_by_placeholder("숫자 6자리 이상")).to_be_visible()
            a.get_by_placeholder("숫자 6자리 이상").fill(PIN)
            a.get_by_placeholder("한 번 더", exact=True).fill(PIN)
            a.get_by_role("button", name="PIN 설정", exact=True).click()
            expect(a.get_by_role("button", name="이 휴대폰만으로 사용")).to_be_visible()
            a.evaluate("""async () => {
                const C = await import('./core.js');
                C.setSt({ ...C.emptyState(), mode: 'SOLO' });
                await C.commit();
                await C.render();
                const put = IDBObjectStore.prototype.put;
                IDBObjectStore.prototype.put = function(value, key) {
                    if (key === 's:state') throw new DOMException('시험용 공간 부족', 'QuotaExceededError');
                    return put.apply(this, arguments);
                };
                await C.addEvent({ request_id: 'cross-a', req_ver: 1, assign_ver: 1 }, 'CONTACT', { result: 'NO_ANSWER' });
                await C.lockNow();
                IDBObjectStore.prototype.put = put;
            }""")
            b = ctx.new_page()
            b.goto(url)
            unlock(b)
            before = b.evaluate("""async () => {
                const C = await import('./core.js'), S = await import('./store.js');
                await C.addEvent({ request_id: 'cross-b', req_ver: 1, assign_ver: 1 }, 'CONTACT', { result: 'REACHED' });
                await C.settleWrites();
                const state = await S.getSecure('state');
                await C.lockNow();
                return state.events.map(e => e.request_id);
            }""")
            ok(before == ['cross-b'], "A창 저장 실패·잠금 뒤 같은 저장소의 B창이 새 현장 기록 저장")
            unlock(a)
            after = b.evaluate("""async pin => {
                const S = await import('./store.js');
                await S.unlock(pin);
                return (await S.getSecure('state')).events.map(e => e.request_id);
            }""", PIN)
            ok(after == ['cross-b'], f"A창의 미저장 기록 복구가 B창의 저장 기록을 덮어쓰지 않음 ({after})")
            own = a.evaluate("""async () => {
                const C = await import('./core.js'), S = await import('./store.js');
                return C.st.events.map(e => e.request_id).join(',') === 'cross-a'
                    && C.saveFailed && S.hasPendingSecure('state');
            }""")
            ok(own, "충돌한 A창은 자기 기록을 복구하되 디스크 저장 성공으로 표시하지 않음")
            expect(a.locator('#save-fail')).to_contain_text('다른 창에서 기록이 바뀌었습니다')
            a.goto(url + '#/backup')
            a.get_by_placeholder('백업 비밀번호 (12자 이상)').fill('review-backup-pass')
            a.get_by_placeholder('한 번 더', exact=True).fill('review-backup-pass')
            with a.expect_download() as download:
                a.get_by_role('button', name='암호화 백업 파일 만들기', exact=True).click()
            backup = list(Path(download.value.path()).read_bytes())
            exported = a.evaluate("""async bytes => {
                const B = await import('./backup.js');
                const obj = await B.openBackup(new Uint8Array(bytes), 'review-backup-pass');
                return obj.state.events.map(e => e.request_id);
            }""", backup)
            persisted = b.evaluate("async () => (await (await import('./store.js')).getSecure('state')).events.map(e => e.request_id)")
            ok(exported == ['cross-a'] and persisted == ['cross-b'], "충돌한 A창의 실제 암호화 백업에 A기록 보존, B창 디스크 기록도 유지")
            expect(a.locator('#save-fail')).to_be_visible()
        finally:
            ctx.close()


def cleanup_workdir(directory):
    """종료 직후의 Windows 공유 위반만 짧게 다시 시도한다. 다른 오류는 숨기지 않는다."""
    expected = Path(directory.name).absolute()
    if expected.resolve() != expected or expected.parent != Path(tempfile.gettempdir()).resolve() or not expected.name.startswith('dg-review-ui-'):
        raise AssertionError('시험 작업 폴더의 정리 경로가 바뀌었습니다')
    for attempt in range(5):
        try:
            directory.cleanup()
            return
        except PermissionError as e:
            if os.name != 'nt' or getattr(e, 'winerror', None) != 32 or attempt == 4:
                raise
            time.sleep(0.05 * 2 ** attempt)  # 50+100+200+400ms, 최대 0.75초


@contextlib.contextmanager
def review_workdir():
    directory = tempfile.TemporaryDirectory(prefix='dg-review-ui-')
    try:
        yield directory.name
    except BaseException as original:
        try:
            cleanup_workdir(directory)
        except BaseException as cleanup:
            raise BaseExceptionGroup('회귀 시험과 작업 폴더 정리가 모두 실패했습니다', [original, cleanup]) from None
        raise
    else:
        cleanup_workdir(directory)


def stop_pc_server(process):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        # venv 실행기와 실제 Python을 종료 전에 찾아 핸들을 잡는다. PID 재사용과 종료 지연을 구별할 수 있다.
        script = f"""$ErrorActionPreference = 'Stop'
        [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
        $owned = @({process.pid}); $frontier = @({process.pid})
        while ($frontier.Count) {{
            $next = @()
            foreach ($serverParent in $frontier) {{
                $next += @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$serverParent" | ForEach-Object {{ [int]$_.ProcessId }})
            }}
            $frontier = @($next | Where-Object {{ $_ -notin $owned }})
            $owned += $frontier
        }}
        ConvertTo-Json -InputObject @($owned) -Compress"""
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                check=True, capture_output=True, text=True, encoding='utf8', timeout=10)
        owned = json.loads(result.stdout)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateProcess.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handles = []
        try:
            for pid in owned:
                handle = kernel.OpenProcess(0x00100001, False, pid)  # SYNCHRONIZE | PROCESS_TERMINATE
                if not handle:
                    error = ctypes.get_last_error()
                    if error == 87:  # 열기 전에 이미 종료된 프로세스
                        continue
                    raise ctypes.WinError(error)
                handles.append((pid, handle))
            for pid, handle in reversed(handles):
                if kernel.WaitForSingleObject(handle, 0) == 0x102 and not kernel.TerminateProcess(handle, 1):
                    error = ctypes.get_last_error()
                    # 자식 종료를 따라 실행기가 막 끝난 경우는 아래 핸들 대기에서 확인한다.
                    if kernel.WaitForSingleObject(handle, 0) != 0:
                        raise ctypes.WinError(error)
            deadline = time.monotonic() + 5
            for pid, handle in handles:
                result = kernel.WaitForSingleObject(handle, max(0, int((deadline - time.monotonic()) * 1000)))
                if result != 0:
                    raise AssertionError(f'회귀 시험 PC 프로세스 {pid}의 종료를 확인하지 못했습니다 (대기 결과 {result})')
        finally:
            for _, handle in handles:
                kernel.CloseHandle(handle)
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def cleanup_regressions():
    if os.name != 'nt':
        return
    directory = tempfile.TemporaryDirectory(prefix='dg-review-ui-')
    handle = (Path(directory.name) / 'locked.db').open('wb')
    release = threading.Timer(0.1, handle.close)
    release.start()
    try:
        cleanup_workdir(directory)
        ok(not Path(directory.name).exists(), '실제 Windows 파일 핸들 해제가 늦어져도 짧은 재시도로 작업 폴더 정리')
    finally:
        release.join()
        handle.close()
        directory.cleanup()

    directory = tempfile.TemporaryDirectory(prefix='dg-review-ui-')
    handle = (Path(directory.name) / 'locked.db').open('wb')
    retained = False
    try:
        try:
            cleanup_workdir(directory)
        except PermissionError as e:
            retained = e.winerror == 32
    finally:
        handle.close()
        directory.cleanup()
    ok(retained, '파일 핸들이 계속 잠겨 있으면 bounded retry 뒤 원래 WinError32 실패 유지')

    directory = tempfile.TemporaryDirectory(prefix='dg-review-ui-')
    denied = PermissionError('시험용 접근 거부')
    denied.winerror = 5
    calls = []

    class DeniedCleanup:
        name = directory.name

        def cleanup(self):
            calls.append(True)
            raise denied

    immediate = False
    try:
        try:
            cleanup_workdir(DeniedCleanup())
        except PermissionError as e:
            immediate = e is denied and len(calls) == 1
    finally:
        directory.cleanup()
    ok(immediate, '공유 위반 이외의 정리 오류는 재시도하거나 숨기지 않음')

    original, cleanup = AssertionError('원래 시험 오류'), PermissionError('정리 오류')
    real_cleanup, created = cleanup_workdir, []

    def fail_cleanup(directory):
        created.append(directory)
        raise cleanup

    globals()['cleanup_workdir'] = fail_cleanup
    grouped = False
    try:
        try:
            with review_workdir():
                raise original
        except BaseExceptionGroup as e:
            grouped = e.exceptions == (original, cleanup)
    finally:
        globals()['cleanup_workdir'] = real_cleanup
        for directory in created:
            directory.cleanup()
    ok(grouped, '시험 assertion과 정리 오류가 함께 나면 두 원인을 모두 보존')


@contextlib.contextmanager
def pc_server():
    with review_workdir() as work:
        env = {**os.environ, "DG_STATE": str(Path(work) / "state"), "DG_OUT": str(Path(work) / "out"),
               "DG_DB_KEY": "test-db-key-not-for-production", "DG_NO_SCHEDULER": "1"}
        subprocess.run([sys.executable, str(ROOT / "pc/pc_tool.py"), "init"], env=env, check=True, capture_output=True)
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            port = sk.getsockname()[1]
        url = f"http://127.0.0.1:{port}/"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with (Path(work) / "server.log").open("w", encoding="utf8") as log:
            process = subprocess.Popen([sys.executable, str(ROOT / "pc/pc_tool.py"), "serve", "--port", str(port)],
                                       env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise AssertionError("회귀 시험 PC 서버가 종료되었습니다")
                    try:
                        with opener.open(url, timeout=1):
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise AssertionError("회귀 시험 PC 서버가 시작하지 못했습니다")
                yield url
            except BaseException as original:
                try:
                    stop_pc_server(process)
                except BaseException as shutdown:
                    raise BaseExceptionGroup('회귀 시험과 PC 서버 종료가 모두 실패했습니다', [original, shutdown]) from None
                raise
            else:
                stop_pc_server(process)


def retry_regressions(browser):
    with pc_server() as url:
        ctx = browser.new_context(service_workers="block")
        page = ctx.new_page()
        try:
            # 공통 요청 도우미의 실제 대화 상자·API를 검사하는 최소 문서. 업무 서버·암호화 DB는 실제로 실행한다.
            page.route("**/review-pc.html", lambda route: route.fulfill(content_type="text/html", body="<div id='app'></div>"))
            page.goto(url + "review-pc.html")
            page.evaluate("""async () => {
                const C = await import('/core.js');
                await C.post('setup', { username: 'reviewadmin', display_name: '가상 검토자', password: 'review-password-123' });
                const request = await C.post('requests', { name: '가상 신청자', phone: '01000001234', address: '가상구 예시로 1',
                    items: [{ name: '가상 시험 품목', qty: 1, unit: 'EA', unit_label: '개' }] });
                window.__reviewRid = request.id;
                window.__reviewPath = `requests/${request.id}/payments`;
                window.__reviewBody = { kind: 'PAY', amount: '1000', method: '기타' };
                window.__reviewRequests = [];
                const fetch = window.fetch;
                window.fetch = (path, options) => {
                    if (path.endsWith('/payments') && options?.method === 'POST')
                        window.__reviewRequests.push(JSON.parse(options.body));
                    return fetch(path, options);
                };
            }""")

            def lose_response(route):
                response = route.fetch()
                if response.status != 200:
                    raise AssertionError("수납 반영이 먼저 성공해야 응답 유실을 시험할 수 있습니다")
                route.abort("failed")

            page.route("**/api/requests/*/payments", lose_response, times=1)
            lost = page.evaluate("""async () => {
                const C = await import('/core.js');
                try { await C.postOnce('review-same', window.__reviewPath, window.__reviewBody); return false; }
                catch { return true; }
            }""")
            ok(lost, "실제 서버 수납 반영 뒤 응답만 유실시켜 불확실 재시도 재현")
            same = page.evaluate("""async () => {
                const C = await import('/core.js');
                await C.postOnce('review-same', window.__reviewPath, window.__reviewBody);
                const r = await C.api('requests/' + window.__reviewRid);
                const [a, b] = window.__reviewRequests;
                return r.payment.records.length === 1 && r.payment.paid === 1000 && a.client_req_id === b.client_req_id;
            }""")
            ok(same, "같은 본문 재시도는 같은 요청 번호로 중복 수납 없이 원래 반영 확인")

            page.evaluate("() => { window.__reviewBody.amount = '1100'; }")
            page.route("**/api/requests/*/payments", lose_response, times=1)
            page.evaluate("""async () => {
                const C = await import('/core.js');
                try { await C.postOnce('review-changed', window.__reviewPath, window.__reviewBody); } catch {}
                window.__reviewBody.amount = '2200';
                window.__reviewChangedDone = false;
                C.postOnce('review-changed', window.__reviewPath, window.__reviewBody)
                    .then(() => { window.__reviewChanged = { success: true }; })
                    .catch(e => { window.__reviewChanged = { error: e.message }; })
                    .finally(() => { window.__reviewChangedDone = true; });
            }""")
            page.wait_for_function("document.querySelector('[role=dialog]') || window.__reviewChangedDone")
            ok(page.get_by_role("dialog").count() == 1,
               "수납 응답 유실 뒤 금액을 바꾸면 과거 응답으로 성공 처리하지 않고 이전 요청 확인")
            sent_before_cancel = page.evaluate("window.__reviewRequests.length")
            page.get_by_role("button", name="돌아가기", exact=True).click()
            page.wait_for_function("window.__reviewChangedDone")
            ok(page.evaluate("window.__reviewRequests.length") == sent_before_cancel
               and page.evaluate("!!window.__reviewChanged.error"), "이전 요청 확인 취소는 새 POST 없이 현재 입력을 미저장으로 유지")
            page.evaluate("""async () => {
                const C = await import('/core.js');
                window.__reviewChangedDone = false;
                C.postOnce('review-changed', window.__reviewPath, window.__reviewBody)
                    .then(() => { window.__reviewChanged = { success: true }; })
                    .catch(e => { window.__reviewChanged = { error: e.message }; })
                    .finally(() => { window.__reviewChangedDone = true; });
            }""")
            page.get_by_role("button", name="이전 요청 확인", exact=True).click()
            page.wait_for_function("window.__reviewChangedDone")
            confirmed = page.evaluate("""async () => {
                const C = await import('/core.js');
                const r = await C.api('requests/' + window.__reviewRid);
                const a = window.__reviewRequests.at(-2), b = window.__reviewRequests.at(-1);
                return !!window.__reviewChanged.error && !window.__reviewChanged.success
                    && window.__reviewChanged.error.includes('현재 변경값은 저장하지 않았습니다')
                    && r.payment.records.length === 2 && r.payment.paid === 2100
                    && a.client_req_id === b.client_req_id && b.amount === '1100';
            }""")
            ok(confirmed, "이전 요청 확인은 원본 1100원·같은 번호로 재전송하고 현재 2200원은 미저장으로 안내")
            saved = page.evaluate("""async () => {
                const C = await import('/core.js');
                await C.postOnce('review-changed', window.__reviewPath, window.__reviewBody);
                const r = await C.api('requests/' + window.__reviewRid);
                const a = window.__reviewRequests.at(-2), b = window.__reviewRequests.at(-1);
                return r.payment.records.length === 3 && r.payment.paid === 4300 && a.client_req_id !== b.client_req_id;
            }""")
            ok(saved, "원래 요청을 확인한 뒤 별도 저장은 새 번호로 현재 금액 반영")
            corrected = page.evaluate("""async () => {
                const C = await import('/core.js');
                try { await C.postOnce('review-invalid', window.__reviewPath, { ...window.__reviewBody, amount: '0' }); } catch {}
                await C.postOnce('review-invalid', window.__reviewPath, { ...window.__reviewBody, amount: '3000' });
                const r = await C.api('requests/' + window.__reviewRid);
                return r.payment.records.length === 4 && r.payment.paid === 7300;
            }""")
            ok(corrected, "서버가 입력 오류로 확정 거절한 요청은 값을 고쳐 바로 다시 저장 가능")
            page.route("**/api/requests/*/payments", lose_response, times=1)
            expired = page.evaluate("""async () => {
                const C = await import('/core.js');
                const body = { ...window.__reviewBody, amount: '4100' };
                try { await C.postOnce('review-expired', window.__reviewPath, body); } catch {}
                const sent = window.__reviewRequests.length, now = Date.now;
                Date.now = () => now() + 24 * 60 * 60 * 1000 + 1;
                let message = '';
                try { await C.postOnce('review-expired', window.__reviewPath, body); } catch (e) { message = e.message; }
                finally { Date.now = now; }
                return window.__reviewRequests.length === sent && message.includes('24시간');
            }""")
            ok(expired, "서버 멱등 보관 기간 24시간이 지난 불확실 요청은 POST 없이 수동 기록 확인 안내")
        finally:
            ctx.close()


def requests_regressions(browser):
    with pc_server() as url:
        ctx = browser.new_context(service_workers='block')
        page = ctx.new_page()
        try:
            page.route('**/review-requests.html', lambda route: route.fulfill(content_type='text/html', body="<div id='app'></div>"))
            page.goto(url + 'review-requests.html#/requests')
            page.evaluate("""async () => {
                const C = await import('/core.js'), R = await import('/screens/requests.js');
                await C.post('setup', { username: 'listreview', display_name: '가상 조회담당', password: 'review-password-123' });
                for (let i = 1; i <= 2; i++) await C.post('dongs', { name: `가상${i}동`, code: `X${i}`, active: true });
                for (let i = 0; i < 6; i++) await C.post('requests', {
                    name: `가상 조회${i}`, phone: `0100000100${i}`, address: `가상구 예시로 ${i + 1}`,
                    dong: i < 2 ? '가상1동' : '가상2동', items: [{ name: '가상 품목', qty: 1, unit: 'EA', unit_label: '개' }],
                });
                C.setBoot(await C.api('bootstrap'));
                window.__mountRequests = async () => document.getElementById('app').replaceChildren(await R.screenRequests());
                window.__listGates = []; window.__holdNextList = false; window.__listRequestCount = 0;
                const fetch = window.fetch;
                window.fetch = async (path, options) => {
                    const list = typeof path === 'string' && path.startsWith('/api/requests?');
                    if (list) window.__listRequestCount++;
                    const hold = list && window.__holdNextList;
                    if (hold) window.__holdNextList = false;
                    const response = await fetch(path, options);
                    if (!list) return response;
                    // 실제 API 본문을 먼저 받아 두어 release 뒤 적용 순서가 네트워크 시간에 좌우되지 않게 한다.
                    const body = await response.text();
                    if (hold) await new Promise(release => window.__listGates.push({ release, path }));
                    return new Response(body, { status: response.status, headers: response.headers });
                };
                await window.__mountRequests();
            }""")
            expect(page.locator('tbody tr.click')).to_have_count(6)
            ok(True, '접수 목록은 처음 mount한 화면에서 빠른 실제 API 응답도 표시')
            page.evaluate('() => { window.__holdNextList = true; }')
            page.locator('#rq-status').select_option('')
            page.wait_for_function('window.__listGates.length === 1')
            selection_blocked = page.get_by_label('이 쪽 모두 고르기').is_disabled()
            page.locator('#rq-dong').select_option('가상1동')
            expect(page.locator('tbody tr.click')).to_have_count(2)
            page.evaluate("""async () => {
                window.__listGates.shift().release();
                await new Promise(resolve => setTimeout(resolve, 0));
            }""")
            expect(page.locator('tbody tr.click')).to_have_count(2)
            ok(True, '최신 행정동 2건 조회 뒤 도착한 이전 6건 응답은 목록을 덮어쓰지 않음')
            page.get_by_label('이 쪽 모두 고르기').check()
            expect(page.get_by_text('2건 선택', exact=True)).to_be_visible()
            ok(selection_blocked, '조회 중 옛 전체선택은 비활성화하고 최신 목록의 2건만 전체선택')
            page.get_by_role('button', name='선택 해제', exact=True).click()
            page.evaluate("""async () => {
                const R = await import('/screens/requests.js');
                R.screenRequests.f = { ...R.screenRequests.f, dong: '', size: 2, page: 3 };
                window.__holdNextList = true;
                await window.__mountRequests();
            }""")
            page.wait_for_function('window.__listGates.length === 1')
            page.evaluate("""async () => {
                const R = await import('/screens/requests.js');
                Object.assign(R.screenRequests.f, { dong: '가상1동', size: 50, page: 1 });
                await window.__mountRequests();
            }""")
            expect(page.locator('tbody tr.click')).to_have_count(2)
            page.evaluate("""async () => {
                window.__listGates.shift().release();
                await new Promise(resolve => setTimeout(resolve, 0));
            }""")
            own_page = page.evaluate("async () => (await import('/screens/requests.js')).screenRequests.f.page")
            ok(own_page == 1 and page.locator('tbody tr.click').count() == 2,
               '떠난 목록 화면의 늦은 3쪽 응답은 재진입한 현재 화면과 공유 page 조건을 변경하지 않음')

            page.evaluate('() => { window.__holdNextList = true; }')
            page.locator('#rq-dong').select_option('가상2동')
            page.wait_for_function('window.__listGates.length === 1')
            abandoned_did_not_load = page.evaluate("""async () => {
                const R = await import('/screens/requests.js');
                const before = window.__listRequestCount;
                window.__abandonedRequests = await R.screenRequests();  // 오래된 render가 만들었다가 버린 미연결 view
                await new Promise(resolve => setTimeout(resolve, 0));
                return window.__listRequestCount === before;
            }""")
            page.evaluate("""async () => {
                window.__listGates.shift().release();
                await new Promise(resolve => setTimeout(resolve, 0));
            }""")
            expect(page.locator('tbody tr.click')).to_have_count(4)
            ok(abandoned_did_not_load, '버려진 미마운트 화면은 조회하지 않고 실제 현재 화면의 정상 응답도 무효화하지 않음')
        finally:
            ctx.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("storage", "retry", "requests", "cleanup", "all"), default="all")
    args = parser.parse_args()
    if args.suite in ('cleanup', 'all'):
        cleanup_regressions()
    if args.suite == 'cleanup':
        print(f"\n통과 {passed}개")
        return
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), headless=True)
        try:
            if args.suite in ("storage", "all"):
                storage_regressions(browser)
                storage_cross_tab_regression(browser)
            if args.suite in ("retry", "all"):
                retry_regressions(browser)
            if args.suite in ('requests', 'all'):
                requests_regressions(browser)
        finally:
            browser.close()
    print(f"\n통과 {passed}개")


if __name__ == "__main__":
    main()
