"""다른 PC에서 실행하는 읽기 전용 접속 점검. DB·키·실행 controller와 독립적이다."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import queue
import re
import tempfile
import threading


MESSAGES = {
    'ready': '이 기기에서 동구 업무 서버와 화면 파일의 응답을 확인했습니다.',
    'invalid_url': '접속 주소를 확인하세요. HTTPS 주소 또는 이 PC의 로컬 주소만 사용할 수 있습니다.',
    'tls_error': '인증서 신뢰·유효기간·접속 이름을 확인하세요. 인증서 검증을 생략하지 않습니다.',
    'access_denied': '이 기기의 접근이 허용되지 않았습니다. 허용 IP 대역과 기관망 설정을 확인하세요.',
    'unreachable': '서버에 연결하지 못했습니다. 서버 실행·주소·포트·기관망 설정을 확인하세요.',
    'wrong_service': '동구 업무 서버로 확인되지 않았습니다. 접속 주소와 사용 중인 포트를 확인하세요.',
    'version_mismatch': '서버와 점검 프로그램의 버전이 다릅니다. 같은 배포본으로 다시 확인하세요.',
    'assets_unavailable': '서버 응답은 있지만 업무 화면 파일을 확인하지 못했습니다. 배포 폴더를 확인하세요.',
}
SCOPE_NOTE = '검사 범위: 이 점검 프로그램을 실행한 기기 → 입력한 서버. 로그인·권한·업무 처리 성공을 확인하는 시험은 아닙니다.'


def _probe(url):
    from desktop_connection import probe_url
    return probe_url(url)


def _normalize(url):
    from desktop_connection import normalize_url
    return normalize_url(url)


def _report(url, result):
    """입력·응답 메시지·예외를 복사하지 않고 허용한 고정 메타데이터만 만든다."""
    from dgpc import __version__
    result = result if isinstance(result, dict) else {}
    code = result.get('code')
    if not isinstance(code, str) or code not in MESSAGES:
        code = 'unreachable'
    version = result.get('version')
    version = version if isinstance(version, str) and re.fullmatch(r'\d{1,3}\.\d{1,3}\.\d{1,3}', version) else ''
    architecture = platform.machine()
    return {'code': code, 'message': MESSAGES[code], 'ok': code == 'ready', 'url': url,
            'version': version, 'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'scope': 'this_device', 'app_version': __version__,
            'platform': platform.system() if platform.system() in {'Windows', 'Linux', 'Darwin'} else 'other',
            'architecture': architecture if re.fullmatch(r'[A-Za-z0-9_.-]{1,32}', architecture) else 'unknown'}


def _write_report(destination, report):
    temporary = None
    try:
        target = Path(destination)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n', dir=target.parent,
                                         prefix='.dg-check-', suffix='.json', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        os.replace(temporary, target)
    except (OSError, ValueError, TypeError):
        raise ValueError('보고서를 저장하지 못했습니다. 선택한 폴더와 쓰기 권한을 확인하세요.') from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def connection_check_report(url, destination):
    """명시한 URL만 점검하고 안전 JSON을 저장한다. Tk/config/DB/controller를 열지 않는다."""
    try:
        safe_url = _normalize(url)
    except (ValueError, TypeError):
        report = _report('', {'code': 'invalid_url'})
    else:
        try:
            result = _probe(safe_url)
        except Exception:
            result = {'code': 'unreachable'}
        report = _report(safe_url, result)
    _write_report(destination, report)
    return report


class ConnectionChecker:
    def __init__(self, root, *, probe=None, initial_url=''):
        import tkinter as tk
        from tkinter import ttk
        self.root, self.probe = root, probe or _probe
        self.closed, self.running, self.report = False, False, None
        self.epoch, self._job, self._timer = 0, None, None
        self.events = queue.Queue()
        root.title('동구 PC 접속 점검 — 이 기기에서 확인')
        root.geometry('680x480')
        root.minsize(560, 400)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        heading = ttk.Frame(root, padding=(16, 12))
        heading.grid(row=0, column=0, sticky='ew')
        heading.columnconfigure(0, weight=1)
        self.scope_label = ttk.Label(heading, text='이 기기에서 서버 접속을 확인합니다.', wraplength=630)
        self.scope_label.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        ttk.Label(heading, text='서버 PC에서 복사한 접속 주소').grid(row=1, column=0, sticky='w')
        self.url_var = tk.StringVar(root, value=initial_url)
        self.url_input = ttk.Entry(heading, textvariable=self.url_var)
        self.url_input.grid(row=2, column=0, sticky='ew', pady=(4, 0))
        self.url_input.bind('<Return>', lambda event: (self.start(), 'break')[1])
        middle = ttk.Frame(root, padding=(16, 0))
        middle.grid(row=1, column=0, sticky='nsew')
        middle.columnconfigure(0, weight=1)
        middle.rowconfigure(0, weight=1)
        self.result_text = tk.Text(middle, width=1, height=4, state='disabled', wrap='word', takefocus=1)
        self.result_text.grid(row=0, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(middle, orient='vertical', command=self.result_text.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self.result_text.configure(yscrollcommand=scrollbar.set)
        self.result_text.bind('<Control-a>', lambda event: (self.result_text.tag_add('sel', '1.0', 'end-1c'), 'break')[1])
        self.result_text.bind('<Control-c>', self._copy_selection)
        self.result_text.bind('<Tab>', lambda event: (self.result_text.tk_focusNext().focus_set(), 'break')[1])
        self.result_text.bind('<Shift-Tab>', lambda event: (self.result_text.tk_focusPrev().focus_set(), 'break')[1])
        footer = ttk.Frame(root, padding=(16, 8, 16, 12))
        footer.grid(row=2, column=0, sticky='ew')
        footer.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(root, value='아직 시험하지 않았습니다. 다른 PC의 접속은 그 PC에서 직접 점검하세요.')
        self.status_label = ttk.Label(footer, textvariable=self.status_var, wraplength=630)
        self.status_label.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 8))
        actions = ttk.Frame(footer)
        actions.grid(row=1, column=0, columnspan=2, sticky='ew')
        self.copy_button = ttk.Button(actions, text='결과 복사', command=self.copy_report, state='disabled')
        self.preview_button = ttk.Button(actions, text='보고서 미리보기', command=self.preview_report, state='disabled')
        self.save_button = ttk.Button(actions, text='보고서 저장', command=self.save_report, state='disabled')
        for column, button in enumerate((self.copy_button, self.preview_button, self.save_button)):
            button.grid(row=0, column=column, padx=(0, 8), pady=(0, 8))
        self.check_button = ttk.Button(footer, text='이 기기에서 연결 시험', command=self.start)
        self.check_button.grid(row=2, column=0, sticky='ew')
        self.close_button = ttk.Button(footer, text='닫기', command=self.close)
        self.close_button.grid(row=2, column=1, padx=(8, 0))
        for button in (self.copy_button, self.preview_button, self.save_button, self.check_button, self.close_button):
            button.bind('<Return>', lambda event, b=button: (b.invoke(), 'break')[1])
        self.url_var.trace_add('write', self._changed)
        root.bind('<Configure>', self._resize, add='+')
        root.protocol('WM_DELETE_WINDOW', self.close)
        self._set_text('서버 실행·DB·로그인 없이 접속과 화면 파일만 읽어 확인합니다.\n' + SCOPE_NOTE)
        self.url_input.focus_set()
        self._timer = root.after(40, self._drain)

    def _resize(self, event=None):
        width = max(160, self.root.winfo_width() - 36)
        self.scope_label.configure(wraplength=width)
        self.status_label.configure(wraplength=width)

    def _set_text(self, text):
        self.result_text.configure(state='normal')
        self.result_text.delete('1.0', 'end')
        self.result_text.insert('1.0', text)
        self.result_text.configure(state='disabled')

    def _changed(self, *_):
        if self.closed:
            return
        self.epoch += 1
        self.report = None
        self._set_text('주소가 바뀌었습니다. 새 주소로 다시 시험하세요.')
        self._buttons()

    def _buttons(self):
        self.check_button.configure(state='disabled' if self.running else 'normal', text='연결 시험 중…' if self.running else '이 기기에서 연결 시험')
        for button in (self.copy_button, self.preview_button, self.save_button):
            button.configure(state='normal' if self.report is not None and not self.running else 'disabled')

    def start(self):
        if self.closed or self.running:
            return
        self.report = None
        self.epoch += 1
        try:
            url = _normalize(self.url_var.get())
        except (ValueError, TypeError):
            self._accept(_report('', {'code': 'invalid_url'}))
            self.url_input.focus_set()
            return
        self.running, self._job = True, self.epoch
        self.status_var.set('이 기기에서 응답을 확인하고 있습니다. 창을 닫아도 서버·업무 자료는 바뀌지 않습니다.')
        self._set_text('연결 시험 중…')
        self._buttons()
        # 작업 스레드는 queue/함수/문자열만 보유한다. Tk 객체와 창의 생명주기를 공유하지 않는다.
        events, probe, job = self.events, self.probe, self._job
        def work():
            try:
                result = probe(url)
            except Exception:
                result = {'code': 'unreachable'}
            events.put((job, url, result))
        threading.Thread(target=work, daemon=True).start()

    def _drain(self):
        if self.closed:
            return
        try:
            while True:
                job, url, result = self.events.get_nowait()
                if job != self._job:
                    continue
                self.running, self._job = False, None
                if job == self.epoch:
                    self._accept(_report(url, result))
                else:
                    self.status_var.set('주소가 바뀌어 이전 시험 결과를 표시하지 않았습니다. 다시 시험하세요.')
                    self._buttons()
        except queue.Empty:
            pass
        self._timer = self.root.after(40, self._drain)

    def _accept(self, report):
        self.report = report
        self.status_var.set('시험 완료 · ' + ('이 기기에서 응답 확인' if report['ok'] else '확인 필요'))
        self._set_text(report['message'] + '\n\n' + self.report_text() + '\n\n' + SCOPE_NOTE)
        self._buttons()

    def report_text(self):
        return json.dumps(self.report, ensure_ascii=False, indent=2) if self.report is not None else ''

    def _copy_selection(self, event=None):
        if not self.result_text.tag_ranges('sel'):
            return self.copy_report()
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.result_text.get('sel.first', 'sel.last'))
        except Exception:
            self.status_var.set('복사하지 못했습니다. 글을 선택해 다시 복사하세요.')
        return 'break'

    def copy_report(self):
        if self.closed or self.report is None or self.running:
            return 'break'
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.report_text())
            self.status_var.set('안전한 점검 결과를 복사했습니다.')
        except Exception:
            self.status_var.set('복사하지 못했습니다. 결과를 선택해 다시 복사하세요.')
        return 'break'

    def preview_report(self):
        if self.closed or self.report is None or self.running:
            return
        import tkinter as tk
        from tkinter import ttk
        window = tk.Toplevel(self.root)
        window.title('접속 점검 보고서 미리보기')
        window.geometry('600x440')
        text = tk.Text(window, wrap='word')
        text.pack(fill='both', expand=True, padx=12, pady=12)
        text.insert('1.0', self.report_text())
        text.configure(state='disabled')
        text.bind('<Tab>', lambda event: (text.tk_focusNext().focus_set(), 'break')[1])
        text.bind('<Shift-Tab>', lambda event: (text.tk_focusPrev().focus_set(), 'break')[1])
        text.bind('<Control-a>', lambda event: (text.tag_add('sel', '1.0', 'end-1c'), 'break')[1])
        close = ttk.Button(window, text='닫기', command=window.destroy)
        close.pack(pady=(0, 12))
        close.bind('<Return>', lambda event: (close.invoke(), 'break')[1])
        return window

    def save_report(self):
        if self.closed or self.report is None or self.running:
            return
        from tkinter import filedialog
        report, epoch, payload = self.report, self.epoch, dict(self.report)
        destination = filedialog.asksaveasfilename(parent=self.root, title='접속 점검 보고서 저장',
                                                  defaultextension='.json', filetypes=[('JSON 보고서', '*.json')], initialfile='동구-PC-접속점검.json')
        if not destination or self.closed or self.running or self.epoch != epoch or self.report is not report:
            return
        try:
            _write_report(destination, payload)
            self.status_var.set('점검 보고서를 저장했습니다.')
        except ValueError:
            self.status_var.set('저장하지 못했습니다. 선택한 폴더와 쓰기 권한을 확인하세요.')

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.epoch += 1
        self.report = None
        if self._timer is not None:
            self.root.after_cancel(self._timer)
            self._timer = None
        self.root.destroy()


def connection_check_main(initial_url=''):
    import tkinter as tk
    root = tk.Tk()
    ConnectionChecker(root, initial_url=initial_url)
    root.mainloop()
    return 0
