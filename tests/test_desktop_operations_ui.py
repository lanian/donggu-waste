"""가상 callback과 실제 Tk: 운영 상세·설정·독립 접속 점검의 경계."""
from __future__ import annotations

import gc
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'pc'))
PASSED = 0


def ok(condition, message):
    global PASSED
    assert condition, message
    PASSED += 1
    print('  ✓', message, flush=True)


def pump(root, ready, timeout=4):
    deadline = time.monotonic() + timeout
    while not ready() and time.monotonic() < deadline:
        root.update()
        time.sleep(.01)
    root.update()
    assert ready(), 'Tk 비동기 결과가 정착하지 않음'


def visible(widget, root):
    return (widget.winfo_ismapped() and widget.winfo_rootx() >= root.winfo_rootx()
            and widget.winfo_rooty() >= root.winfo_rooty()
            and widget.winfo_rootx() + widget.winfo_width() <= root.winfo_rootx() + root.winfo_width() + 1
            and widget.winfo_rooty() + widget.winfo_height() <= root.winfo_rooty() + root.winfo_height() + 1)


def view_cases():
    import tkinter as tk
    from desktop_view import DesktopView
    old = ('open', 'retry', 'reconnect', 'password', 'stop', 'folder', 'shortcut', 'minimize')
    optional = ('connection_settings', 'diagnose', 'copy_url', 'open_logs', 'save_report', 'connection_checker')
    for scaling in (4 / 3, 8 / 3):
        for size in ('680x480', '560x400'):
            root = tk.Tk()
            root.tk.call('tk', 'scaling', scaling)
            root.geometry(size)
            actions = []
            callbacks = {name: (lambda key=name: actions.append(key)) for name in (*old, *optional)}
            view = DesktopView(root, '0.14.21', callbacks=callbacks)
            view.pack(fill='both', expand=True)
            try:
                view.set_connection({'mode_label': '이 PC에서만', 'url': 'http://127.0.0.1:8766/', 'peer_url': '', 'note': '다른 기기 접속: 미확인'})
                view.set_diagnostics('이 PC 응답 확인 · 다른 기기 접속 미확인')
                view.set_diagnostic_actions(settings=True, diagnose=True, copy_url=True, open_logs=True, save_report=True, connection_checker=True)
                view.show_state('ready', owned=True)
                root.update()
                ok(visible(view.primary_button, root) and visible(view.minimize_button, root), f'{size}/{scaling:g} 기본 업무/트레이 버튼 가시성')
                ok(view.connection_settings_button.instate(['disabled']), '소유 서버 실행중 접속 설정 금지')
                ok('다른 기기 접속' in view.connection_note_var.get() and '미확인' in view.connection_note_var.get(), '로컬 성공과 다른 기기 미확인 구분')
                view.diagnose_button.invoke()
                ok(actions[-1] == 'diagnose' and view.state == 'ready', '운영 시험은 업무 서버 상태를 대체하지 않음')
                view.set_diagnostics('시험 중…', running=True)
                ok(view.diagnose_button.instate(['disabled']) and view.save_report_button.instate(['disabled']), '시험 중 중복 시험/이전 보고서 방지')
                ok(not view.primary_button.instate(['disabled']), '진단 중 업무 열기 유지')
                view.set_diagnostics('시험 완료 · 다른 기기 접속 미확인')
                view.show_state('existing')
                ok(view.connection_settings_button.instate(['disabled']), '기존 서버 재사용 중 설정 금지')
                view.show_state('stopped')
                ok(not view.connection_settings_button.instate(['disabled']), '정상 종료 뒤 접속 설정 허용')
                view.toggle_details()
                root.update()
                root.focus_force()
                view.save_report_button.focus_set()
                root.update()
                ok(visible(view.save_report_button, root), '작은창/큰글씨 보고서 초점 자동 스크롤')
                view.toggle_details()
                root.update()
                ok(root.focus_get() == view.details_button, '상세 닫기 시 공개 버튼으로 초점 복귀')
                view.set_connection({'mode_label': '같은 망 HTTPS', 'url': 'https://user:SECRET@example.test/?TOKEN=SECRET', 'peer_url': 'https://0.0.0.0:8765/'})
                ok('SECRET' not in view.connection_text.get('1.0', 'end') and view.copy_url_button.instate(['disabled']), '비밀/query/수신용 주소의 표시·복사 차단')
            finally:
                root.destroy()
    root = tk.Tk()
    try:
        view = DesktopView(root, 'old-fixture', callbacks={name: lambda: None for name in old})
        view.pack()
        view.show_state('ready')
        ok(view.connection_settings_button.instate(['disabled']), '기존 8개 callback fixture 호환')
    finally:
        root.destroy()


def dialog_cases():
    import tkinter as tk
    from desktop_view import ConnectionProfileDialog
    root = tk.Tk()
    root.geometry('680x480')
    root.update()
    draft = {'mode': 'lan', 'port': 8766, 'host': '0.0.0.0', 'hostname': 'pc.example.test',
             'cert': 'C:/fake/cert.pem', 'key': 'C:/fake/key.pem', 'allow': '192.168.10.0/24'}
    accepted = []
    def validate(values):
        accepted.append(values)
        return '시험용: 허용 대역을 확인하세요.' if values['allow'] == 'bad' else None
    dialog = ConnectionProfileDialog(root, draft, validate)
    try:
        dialog.window.geometry('560x400')
        dialog.variables['allow'].set('bad')
        dialog.save()
        root.update()
        ok(dialog.window.winfo_exists() and dialog.result is None and dialog.variables['hostname'].get() == draft['hostname'], '설정 검증 오류에 초안/창 유지')
        ok('허용 대역' in dialog.message_var.get(), '설정 오류는 창 내부 안내')
        ok(visible(dialog.save_button, dialog.window) and visible(dialog.cancel_button, dialog.window), '560×400 설정 저장/취소 고정 접근')
        dialog.variables['allow'].set(draft['allow'])
        dialog.save()
        ok(dialog.result == draft and accepted[-1]['port'] == 8766, '명시 저장만 검증된 profile dict 반환')
    finally:
        if dialog.window.winfo_exists():
            dialog.cancel()
        root.destroy()
    root = tk.Tk()
    root.update()
    dialog = ConnectionProfileDialog(root, {'mode': 'local', 'port': 8766, 'host': '127.0.0.1'}, lambda values: None)
    try:
        dialog.variables['mode'].set('lan')
        ok(dialog.variables['host'].get() == '0.0.0.0', 'untouched 로컬 기본값을 LAN 수신주소로 전환')
        dialog.variables['host'].set('192.168.10.50')
        dialog.variables['mode'].set('local')
        dialog.variables['mode'].set('lan')
        ok(dialog.variables['host'].get() == '192.168.10.50', '사용자가 지정한 수신주소는 모드 왕복에도 보존')
    finally:
        dialog.cancel()
        root.destroy()


def compact_cases():
    import tkinter as tk
    from desktop_check import ConnectionChecker
    from desktop_view import ConnectionProfileDialog
    for scaling in (4 / 3, 8 / 3):
        for size in ('680x480', '560x400'):
            root = tk.Tk()
            root.tk.call('tk', 'scaling', scaling)
            checker = ConnectionChecker(root, probe=lambda url: {'code': 'ready'})
            root.geometry(size)
            root.update()
            try:
                ok(all(visible(button, root) for button in (checker.check_button, checker.close_button, checker.preview_button, checker.save_button)), f'{size}/{scaling:g} 점검 창 키보드 행동영역 가시성')
            finally:
                checker.close()
            root = tk.Tk()
            root.tk.call('tk', 'scaling', scaling)
            root.update()
            dialog = ConnectionProfileDialog(root, {'mode': 'lan', 'port': 8766}, lambda values: '허용 IP 대역과 인증서를 확인하세요.')
            dialog.window.geometry(size)
            dialog.save()
            root.update()
            try:
                ok(visible(dialog.save_button, dialog.window) and visible(dialog.cancel_button, dialog.window), f'{size}/{scaling:g} 설정 오류에도 저장/취소 가시성')
            finally:
                dialog.cancel()
                root.destroy()


def checker_cases():
    import tkinter as tk
    forbidden = ('desktop_runtime', 'dgpc.store', 'keyring', 'desktop_tray')
    before = {name: sys.modules.get(name) for name in forbidden}
    from desktop_check import ConnectionChecker
    calls, gate = [], threading.Event()
    def probe(url):
        calls.append((url, threading.get_ident()))
        gate.wait(2)
        return {'code': 'ready', 'version': '0.14.21', 'url': 'https://user:SECRET@example.test/?TOKEN=SECRET',
                'message': 'SECRET resident raw response', 'raw': 'private log SECRET', 'checked_at': 'SECRET'}
    root = tk.Tk()
    root.geometry('560x400')
    checker = ConnectionChecker(root, probe=probe, initial_url='https://pc.example.test:8766/')
    root.geometry('560x400')
    try:
        checker.start()
        checker.start()
        pump(root, lambda: bool(calls))
        ok(len(calls) == 1 and calls[0][1] != threading.get_ident(), '점검은 daemon worker에서 한 번만 실행')
        checker.url_var.set('https://changed.example.test:8766/')
        gate.set()
        pump(root, lambda: not checker.running)
        ok(checker.report is None and checker.copy_button.instate(['disabled']), '입력 주소 변경 뒤 늦은 결과/이전 보고서 폐기')
        checker.start()
        pump(root, lambda: checker.report is not None)
        encoded = json.dumps(checker.report, ensure_ascii=False)
        ok('SECRET' not in encoded and 'private log' not in encoded and checker.report['scope'] == 'this_device', '점검 결과는 고정 allowlist/이 PC 범위만')
        checker.copy_button.invoke()
        ok(root.clipboard_get() == checker.report_text(), '점검 결과 복사는 검증된 보고서만')
        preview = checker.preview_report()
        root.update()
        preview.focus_force()
        text = next(widget for widget in preview.winfo_children() if isinstance(widget, tk.Text))
        text.focus_set()
        root.update()
        text.event_generate('<Tab>')
        root.update()
        ok(preview.focus_get() != text, '읽기 전용 보고서 미리보기에서 Tab으로 벗어나기')
        preview.destroy()
        with tempfile.TemporaryDirectory(prefix='dg-checker-report-') as folder:
            destination = Path(folder) / 'report.json'
            with patch('tkinter.filedialog.asksaveasfilename', return_value=str(destination)):
                checker.save_report()
            ok(json.loads(destination.read_text(encoding='utf-8')) == checker.report, '사용자 선택 파일에 안전 보고서 저장')
            other = Path(folder) / 'stale.json'
            def changed_during_save(**options):
                checker.url_var.set('https://new.example.test/')
                return str(other)
            with patch('tkinter.filedialog.asksaveasfilename', side_effect=changed_during_save):
                checker.save_report()
            ok(not other.exists(), '저장 대화창 중 주소 변경은 이전 보고서 저장 금지')
        checker.url_var.set('https://u:SECRET@pc.example.test/?TOKEN=SECRET')
        count = len(calls)
        checker.start()
        root.update()
        ok(len(calls) == count and checker.report['code'] == 'invalid_url' and 'SECRET' not in checker.report_text(), '위험 URL은 worker/네트워크 이전 차단')
        ok(visible(checker.check_button, root) and visible(checker.close_button, root), '560×400 점검/닫기 버튼 가시성')
    finally:
        checker.close()
    late = threading.Event()
    root = tk.Tk()
    checker = ConnectionChecker(root, probe=lambda url: (late.wait(2), {'code': 'ready'})[1], initial_url='https://pc.example.test/')
    checker.start()
    checker.close()
    late.set()
    time.sleep(.04)
    ok(checker.closed and checker.report is None, '닫은 창은 늦은 worker가 갱신하지 않음')
    ok(all(sys.modules.get(name) is before[name] for name in forbidden), '독립 점검에 DB/keyring/실행 controller/tray import 없음')


def report_cases():
    from desktop_check import connection_check_report
    with tempfile.TemporaryDirectory(prefix='dg-check-only-') as folder:
        destination = Path(folder) / 'safe.json'
        with patch('tkinter.Tk', side_effect=AssertionError('noninteractive must not create Tk')):
            with patch('desktop_check._probe', return_value={'code': 'ready', 'version': '0.14.21', 'message': 'SECRET', 'key': 'SECRET'}) as probe:
                result = connection_check_report('https://pc.example.test:8766/', destination)
                ok(result['ok'] and probe.call_count == 1 and json.loads(destination.read_text(encoding='utf-8')) == result, '비GUI 보고서는 서버 시작/Tk 없이 정상 점검 결과만 저장')
                ok('SECRET' not in destination.read_text(encoding='utf-8'), '비GUI 보고서도 고정 allowlist 적용')
                connection_check_report('https://user:SECRET@example.test/?TOKEN=SECRET', destination)
                ok(probe.call_count == 1 and 'SECRET' not in destination.read_text(encoding='utf-8'), '비GUI 위험 URL은 네트워크 이전 차단')
            with patch('desktop_check._probe', side_effect=RuntimeError('SECRET private path')):
                result = connection_check_report('https://pc.example.test/', destination)
                ok(result['code'] == 'unreachable' and 'SECRET' not in destination.read_text(encoding='utf-8'), 'probe 예외의 사적 정보는 고정 오류로 대체')
            try:
                connection_check_report('https://user:SECRET@example.test/', Path(folder) / 'PRIVATE_PATH' / 'result.json')
            except ValueError as error:
                ok('PRIVATE_PATH' not in str(error) and 'SECRET' not in str(error), '보고서 파일 오류도 비밀/경로 비노출')
            else:
                raise AssertionError('없는 폴더에 보고서 저장을 허용함')


def main():
    import tkinter as tk
    # GUI fixture references must be collected on the Tk/main thread.
    enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    try:
        view_cases()
        dialog_cases()
        compact_cases()
        checker_cases()
        report_cases()
    finally:
        gc.collect()
        if enabled:
            gc.enable()
    print(f'실제 Tk 운영 UI/독립 점검 {PASSED}개 통과', flush=True)


if __name__ == '__main__':
    main()
