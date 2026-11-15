"""실행 앱의 Tk 화면. 서버·비밀번호·자료 상태는 다루지 않고 전달받은 상태만 표시한다."""
from __future__ import annotations

import time
import math
import tkinter as tk
from tkinter import font as tkfont, ttk

NAVY, GREEN, INK = "#16324f", "#146c51", "#192b40"
BG, WHITE, MUTED, LINE = "#f4f6fa", "#ffffff", "#5b6b7f", "#dfe5ee"
BUSY = {"checking", "unlocking", "starting", "waiting", "password", "reconnecting", "stopping", "closing_start", "stop_waiting"}
STATES = {
    "checking": ("PC 업무를 준비하고 있습니다", "실행 환경·사용 중인 포트 확인", "암호화 자료와 이 PC의 서버 상태를 확인합니다."),
    "unlocking": ("암호화 자료를 확인하고 있습니다", "DB 키·비밀번호 확인", "비밀번호는 설정이나 실행 인자에 저장하지 않습니다."),
    "starting": ("PC 서버를 시작하고 있습니다", "암호화 자료를 열고 서버 시작", "자료 확인과 서버 시작이 끝날 때까지 이 창을 열어 두세요."),
    "waiting": ("PC 서버 응답을 기다리고 있습니다", "PC 서버의 준비 응답 확인", "서버에서 준비 응답을 보내면 업무 화면을 열 수 있습니다."),
    "password": ("DB 비밀번호 입력이 필요합니다", "입력 창에서 비밀번호 확인", "열린 입력 창에서 비밀번호를 입력하거나 취소할 수 있습니다."),
    "ready": ("PC 서버 준비 완료", "이 PC의 서버 응답 확인 완료", "업무 화면을 열어 사용하세요."),
    "existing": ("기존 서버 사용 중", "기존 PC 서버 응답 확인 완료", "현재 자료 위치는 기존 실행 프로그램에서 확인하세요. 종료도 그 창에서 할 수 있습니다."),
    "connection_lost": ("기존 서버와 연결이 끊겼습니다", "서버 응답을 연속해서 확인하지 못했습니다", "기존 실행 프로그램에서 서버를 다시 실행한 뒤 연결을 확인하세요. 자료 위치는 그 프로그램에서 확인할 수 있습니다."),
    "reconnecting": ("기존 서버 연결을 확인하고 있습니다", "기존 PC 서버의 응답 확인", "새 서버를 시작하지 않고 기존 서버의 응답만 확인합니다."),
    "stopping": ("진행 중인 작업을 마치고 있습니다", "수락한 요청·백그라운드 작업 완료 대기", "이 앱이 시작한 서버를 정상 종료합니다. 업무 자료는 그대로 보관합니다."),
    "closing_start": ("시작 작업이 끝나면 종료합니다", "시작 작업 종료 대기", "시작 중인 작업이 끝나면 이 앱이 만든 서버만 정상 종료합니다."),
    "stop_waiting": ("서버 종료를 기다리고 있습니다", "진행 중인 작업 완료 대기", "진행 중인 작업을 강제로 끊지 않았습니다. 잠시 뒤 종료를 다시 확인할 수 있습니다."),
    "stopped": ("PC 서버를 종료했습니다", "이 앱이 시작한 서버 종료", "업무 시작을 누르면 다시 열립니다. 자료는 그대로 보관됩니다."),
    "error": ("PC 업무를 시작하거나 종료하지 못했습니다", "오류 내용을 확인한 뒤 다시 시도하세요", "아래 내용을 확인하세요. 업무 자료를 자동 이동하거나 삭제하지 않았습니다."),
}


def _rounded(canvas, x, y, right, bottom, radius, **options):
    return canvas.create_polygon(
        x + radius, y, right - radius, y, right, y, right, y + radius,
        right, bottom - radius, right, bottom, right - radius, bottom,
        x + radius, bottom, x, bottom, x, bottom - radius, x, y + radius, x, y,
        smooth=True, splinesteps=24, **options)


class Surface(tk.Canvas):
    """장식 표면만 그린다. 내용과 조작은 기존 네이티브 위젯을 사용한다."""
    def __init__(self, parent, padding=20):
        super().__init__(parent, background=BG, highlightthickness=0, width=1, height=1, takefocus=0)
        self.padding = padding
        self.body = ttk.Frame(self, style="DG.Card.TFrame")
        self._body_window = self.create_window(padding, padding, window=self.body, anchor="nw")
        self.bind("<Configure>", self._layout)
        self.body.bind("<Configure>", self._layout)

    def _layout(self, event=None):
        width = max(1, self.winfo_width())
        self.itemconfigure(self._body_window, width=max(1, width - self.padding * 2))
        height = self.body.winfo_reqheight() + self.padding * 2
        if int(self.cget("height")) != height:
            self.configure(height=height)
        self.delete("surface")
        _rounded(self, 1, 1, width - 1, height - 1, 14, fill=WHITE, outline=LINE, width=1, tags="surface")
        self.tag_lower("surface")


def _button_surface(root, color):
    """9분할 확대 가능한 작은 모서리. 클릭·포커스·키보드는 ttk가 처리한다."""
    image = tk.PhotoImage(master=root, width=20, height=20)
    radius = 8
    for y in range(20):
        edge = min(y, 19 - y)
        inset = math.ceil(radius - .5 - math.sqrt(radius ** 2 - (radius - edge - .5) ** 2)) if edge < radius else 0
        image.put(color, to=(max(0, inset), y, 20 - max(0, inset), y + 1))
    return image


class DesktopView(ttk.Frame):
    def __init__(self, root, version, *, callbacks):
        super().__init__(root, style="DG.App.TFrame", padding=0)
        self.root, self.callbacks = root, callbacks
        self.started_at = None
        self._clock_running, self._timer = False, None
        self.state = "checking"
        self._owned = False
        self.details_open = False
        self.fonts = {name: tkfont.Font(root=root, family="맑은 고딕", size=size, weight=weight)
                      for name, size, weight in (("title", 14, "bold"), ("status", 18, "bold"), ("body", 10, "normal"),
                                                ("strong", 10, "bold"), ("action", 11, "bold"), ("small", 9, "normal"))}
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("DG.App.TFrame", background=BG)
        style.configure("DG.Card.TFrame", background=WHITE)
        style.configure("DG.Header.TFrame", background=WHITE)
        style.configure("DG.Header.TLabel", background=WHITE, foreground=NAVY, font=self.fonts["title"])
        style.configure("DG.Subtitle.TLabel", background=WHITE, foreground=MUTED, font=self.fonts["small"])
        style.configure("DG.Version.TLabel", background=BG, foreground=MUTED, font=self.fonts["small"], padding=(9, 4))
        style.configure("DG.Body.TLabel", background=WHITE, foreground=INK, font=self.fonts["body"])
        style.configure("DG.Muted.TLabel", background=WHITE, foreground=MUTED, font=self.fonts["small"])
        style.configure("DG.Strong.TLabel", background=WHITE, foreground=INK, font=self.fonts["strong"])
        style.configure("DG.Status.TLabel", background=WHITE, foreground=INK, font=self.fonts["status"])
        style.configure("DG.TButton", background=WHITE, foreground=NAVY, bordercolor=LINE, lightcolor=WHITE, darkcolor=WHITE,
                        focuscolor="#2866b1", font=self.fonts["strong"], padding=(12, 7), borderwidth=1)
        style.map("DG.TButton", background=[("active", "#edf2f8")], bordercolor=[("focus", "#2866b1")], foreground=[("disabled", "#8290a0")])
        self._button_images = [_button_surface(root, color) for color in (NAVY, "#22496e", "#10263c", "#e2e8f0")]
        style.element_create("DG.Primary.surface", "image", self._button_images[0],
                             ("disabled", self._button_images[3]), ("pressed", self._button_images[2]),
                             ("active", self._button_images[1]), border=8, sticky="nsew")
        style.layout("DG.Primary.TButton", [("DG.Primary.surface", {"sticky": "nsew", "children": [
            ("Button.focus", {"sticky": "nsew", "children": [("Button.padding", {"sticky": "nsew", "children": [
                ("Button.label", {"sticky": "nsew"})]})]})]})])
        style.configure("DG.Primary.TButton", background=NAVY, foreground=WHITE, focuscolor=WHITE, font=self.fonts["action"], padding=(16, 5), borderwidth=0)
        style.map("DG.Primary.TButton", background=[("disabled", "#e2e8f0"), ("active", "#22496e")],
                  foreground=[("disabled", "#637285"), ("!disabled", WHITE)])
        style.configure("DG.Stop.TButton", background=BG, foreground="#87424d", borderwidth=0, focuscolor="#2866b1", font=self.fonts["small"], padding=(9, 6))
        style.map("DG.Stop.TButton", background=[("active", "#f8e9ec")], foreground=[("disabled", "#8290a0")])
        style.configure("DG.Minimize.TButton", background=WHITE, foreground=NAVY, bordercolor=LINE,
                        focuscolor="#2866b1", font=self.fonts["small"], padding=(9, 6), borderwidth=1)
        style.map("DG.Minimize.TButton", background=[("active", "#edf2f8")])
        style.configure("DG.Disclosure.TButton", background=BG, foreground=MUTED, borderwidth=0, focuscolor="#2866b1", font=self.fonts["strong"], padding=(4, 7))
        style.map("DG.Disclosure.TButton", background=[("active", "#e8edf5")])
        style.configure("DG.Horizontal.TProgressbar", background="#356a98", troughcolor="#e9eef5", borderwidth=0, thickness=4)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.header = ttk.Frame(self, style="DG.Header.TFrame", padding=(24, 9))
        self.header.grid(row=0, column=0, sticky="ew")
        self.header.columnconfigure(1, weight=1)
        logo = tk.Canvas(self.header, width=36, height=36, background=WHITE, highlightthickness=0, takefocus=0)
        logo.grid(row=0, column=0, rowspan=2, padx=(0, 12))
        _rounded(logo, 0, 0, 36, 36, 10, fill=NAVY, outline="")
        logo.create_text(18, 17, text="동", fill=WHITE, font=self.fonts["title"])
        self.brand = ttk.Label(self.header, text="동구 PC 업무", style="DG.Header.TLabel")
        self.brand.grid(row=0, column=1, sticky="w")
        ttk.Label(self.header, text="대형폐기물 접수·수거 관리", style="DG.Subtitle.TLabel").grid(row=1, column=1, sticky="w", pady=(2, 0))
        self.version = ttk.Label(self.header, text="v" + version, style="DG.Version.TLabel")
        self.version.grid(row=0, column=2, rowspan=2, sticky="e", padx=(12, 0))

        scroller = ttk.Frame(self, style="DG.App.TFrame")
        scroller.grid(row=1, column=0, sticky="nsew", padx=24, pady=(14, 8))
        scroller.rowconfigure(0, weight=1)
        scroller.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(scroller, background=BG, highlightthickness=0, width=1, height=1)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(scroller, orient="vertical", command=self.canvas.yview)
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self._scroll_changed)
        self.content = ttk.Frame(self.canvas, style="DG.App.TFrame")
        self.content.columnconfigure(0, weight=1)
        self._content_window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")

        self.status_card = Surface(self.content)
        self.status_card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        card = self.status_card.body
        card.columnconfigure(1, weight=1)
        self.status_mark = tk.Canvas(card, width=44, height=44, background=WHITE, highlightthickness=0, takefocus=0)
        self.status_mark.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 12))
        ttk.Label(card, text="서버 실행 상태", style="DG.Muted.TLabel").grid(row=0, column=1, sticky="w")
        self.badge_var = tk.StringVar()
        self.badge = tk.Label(card, textvariable=self.badge_var, font=self.fonts["small"], borderwidth=0, padx=8, pady=3)
        self.badge.grid(row=0, column=2, sticky="e", padx=(8, 0))
        self.status_var, self.stage_var = tk.StringVar(), tk.StringVar()
        self.elapsed_var = tk.StringVar(value="경과 00:00")
        self.status_label = ttk.Label(card, textvariable=self.status_var, style="DG.Status.TLabel")
        self.status_label.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(5, 0))
        self.stage_label = ttk.Label(card, textvariable=self.stage_var, style="DG.Strong.TLabel")
        self.stage_label.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Label(card, textvariable=self.elapsed_var, style="DG.Muted.TLabel").grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.progress = ttk.Progressbar(card, mode="indeterminate", style="DG.Horizontal.TProgressbar", length=120)
        self.progress.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.description_var = tk.StringVar()
        self.description_label = ttk.Label(card, textvariable=self.description_var, style="DG.Body.TLabel")
        self.description_label.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        self.recovery = ttk.Frame(card, style="DG.Card.TFrame")
        self.recovery.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        self.recovery.columnconfigure(0, weight=1)
        self.detail_text = self._text_area(self.recovery, height=3, row=0)
        self.recovery_buttons = ttk.Frame(self, style="DG.App.TFrame")
        self.recovery_buttons.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 8))
        self.retry_button = self._button(self.recovery_buttons, "다시 시도", "retry")
        self.password_button = self._button(self.recovery_buttons, "비밀번호로 다시 열기", "password")

        self.details_button = ttk.Button(self.content, text="설정·상세 ▸", style="DG.Disclosure.TButton", command=self.toggle_details)
        self.details_button.grid(row=1, column=0, sticky="w", pady=(0, 8))
        self.details_button.bind("<Return>", lambda event: (self.details_button.invoke(), "break")[1])
        self.details = Surface(self.content)
        self.details.grid(row=2, column=0, sticky="ew")
        path_card = self.details.body
        path_card.columnconfigure(0, weight=1)
        self._diagnostic_running = False
        self._diagnostic_requested = {name: callable(callbacks.get(name)) for name in
                                      ("connection_settings", "diagnose", "copy_url", "open_logs", "save_report", "connection_checker")}
        self._diagnostic_requested["save_report"] = False
        self._connection_has_url = False
        self.operation_rows = []
        self.connection_mode_var = tk.StringVar(value="접속 설정 확인 전")
        ttk.Label(path_card, text="접속·운영 정보", style="DG.Strong.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(path_card, textvariable=self.connection_mode_var, style="DG.Body.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 4))
        self.connection_text = self._text_area(path_card, height=3, row=2)
        self.connection_note_var = tk.StringVar(value="다른 기기 접속: 미확인 · 해당 기기에서 접속 점검을 실행하세요.")
        self.connection_note_label = ttk.Label(path_card, textvariable=self.connection_note_var, style="DG.Body.TLabel")
        self.connection_note_label.grid(row=3, column=0, sticky="ew", pady=(6, 8))
        for row, specs in ((4, (("접속 설정", "connection_settings"), ("다른 PC 접속 점검", "connection_checker"))),
                           (5, (("이 PC에서 연결 시험", "diagnose"), ("업무 주소 복사", "copy_url"))),
                           (8, (("선택한 로그 폴더 열기", "open_logs"), ("진단 보고서 저장", "save_report")))):
            group = ttk.Frame(path_card, style="DG.Card.TFrame")
            group.grid(row=row, column=0, sticky="ew", pady=(0, 8))
            buttons = []
            for label, action in specs:
                button = self._button(group, label, action)
                setattr(self, action + "_button", button)
                buttons.append(button)
            self.operation_rows.append((group, tuple(buttons)))
        ttk.Label(path_card, text="연결 시험·운영 사양", style="DG.Strong.TLabel").grid(row=6, column=0, sticky="w", pady=(6, 4))
        self.diagnostics_text = self._text_area(path_card, height=5, row=7)
        self._set_text(self.diagnostics_text, "아직 시험하지 않았습니다. 이 PC의 응답과 다른 기기의 접속은 별도로 확인합니다.")
        ttk.Separator(path_card).grid(row=9, column=0, sticky="ew", pady=(8, 12))
        ttk.Label(path_card, text="선택한 업무 자료 폴더", style="DG.Strong.TLabel").grid(row=10, column=0, sticky="w", pady=(0, 6))
        self.path_text = self._text_area(path_card, height=2, row=11)
        self.path_buttons = ttk.Frame(path_card, style="DG.Card.TFrame")
        self.path_buttons.grid(row=12, column=0, sticky="ew", pady=(10, 0))
        self.folder_button = self._button(self.path_buttons, "자료 폴더 선택", "folder")
        self.shortcut_button = self._button(self.path_buttons, "바탕화면 바로가기 만들기", "shortcut")
        self.details.grid_remove()

        self.primary_button = self._button(self, "업무 화면 열기", "open", style="DG.Primary.TButton")
        self.primary_button.configure(command=self._primary_action)
        self.primary_button.grid(row=3, column=0, sticky="ew", padx=24, pady=(3, 8))
        self.footer = ttk.Frame(self, style="DG.App.TFrame", padding=(24, 0, 24, 12))
        self.footer.grid(row=4, column=0, sticky="ew")
        self.footer.columnconfigure(0, weight=1)
        self.footer_var = tk.StringVar()
        self.footer_label = ttk.Label(self.footer, textvariable=self.footer_var, background=BG, foreground=MUTED, font=self.fonts["small"])
        self.footer_label.grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.minimize_button = self._button(self.footer, "최소화", "minimize", style="DG.Minimize.TButton")
        self.minimize_button.grid(row=0, column=1, sticky="e", padx=(0, 8))
        self.stop_button = self._button(self.footer, "서버 종료", "stop", style="DG.Stop.TButton")
        self.stop_button.grid(row=0, column=2, sticky="e")
        self.canvas.bind("<Configure>", self._resize)
        self.content.bind("<Configure>", lambda event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.bind("<Configure>", self._resize)
        root.bind("<MouseWheel>", self._wheel, add="+")
        root.bind("<FocusIn>", self._see_focus, add="+")
        self.show_state("checking")

    def _button(self, parent, text, action, style="DG.TButton"):
        button = ttk.Button(parent, text=text, style=style, command=self.callbacks.get(action, lambda: None))
        button.bind("<Return>", lambda event: (button.invoke(), "break")[1])
        return button

    def _text_area(self, parent, *, height, row):
        frame = ttk.Frame(parent, style="DG.Card.TFrame")
        frame.grid(row=row, column=0, sticky="ew")
        frame.columnconfigure(0, weight=1)
        text = tk.Text(frame, height=height, width=1, wrap="char", state="disabled", background=WHITE,
                       foreground=INK, font=self.fonts["body"], relief="flat", borderwidth=0,
                       highlightthickness=1, highlightbackground=LINE, highlightcolor="#2d64b0", takefocus=1)
        text.grid(row=0, column=0, sticky="ew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scrollbar.set)
        text.bind("<Control-a>", lambda event: (text.tag_add("sel", "1.0", "end-1c"), "break")[1])
        text.bind("<Control-c>", lambda event: self._copy(text))
        text.bind("<Tab>", lambda event: (text.tk_focusNext().focus_set(), "break")[1])
        text.bind("<Shift-Tab>", lambda event: (text.tk_focusPrev().focus_set(), "break")[1])
        return text

    def _copy(self, text):
        selected = text.get("sel.first", "sel.last") if text.tag_ranges("sel") else text.get("1.0", "end-1c")
        self.root.clipboard_clear()
        self.root.clipboard_append(selected)
        return "break"

    @staticmethod
    def _set_text(widget, value):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def set_path(self, path):
        self._set_text(self.path_text, str(path))

    def set_connection(self, info):
        from desktop_connection import normalize_url
        urls = []
        for key, label in (("url", "업무 화면"), ("peer_url", "같은 망의 다른 PC")):
            value = info.get(key, "")
            if not value:
                continue
            try:
                safe = normalize_url(value)
            except (ValueError, TypeError):
                continue
            urls.append(f"{label}: {safe}")
        self.connection_mode_var.set(str(info.get("mode_label") or "접속 설정 확인 전"))
        self._connection_has_url = bool(urls)
        self._set_text(self.connection_text, "\n".join(urls) or "사용할 접속 주소가 없습니다.")
        note = str(info.get("note") or "").strip()
        scope = "다른 기기 접속: 미확인 · 해당 기기에서 접속 점검을 실행하세요."
        self.connection_note_var.set((note + "\n" if note and note != scope else "") + scope)
        self._apply_diagnostic_actions()
        self._resize()

    def set_diagnostics(self, text, running=False):
        self._diagnostic_running = bool(running)
        self._set_text(self.diagnostics_text, str(text))
        self.diagnose_button.configure(text="연결 시험 중…" if running else "이 PC에서 연결 시험")
        self._apply_diagnostic_actions()

    def set_diagnostic_actions(self, *, settings=None, diagnose=None, copy_url=None, open_logs=None, save_report=None, connection_checker=None):
        for name, value in (("connection_settings", settings), ("diagnose", diagnose), ("copy_url", copy_url),
                            ("open_logs", open_logs), ("save_report", save_report), ("connection_checker", connection_checker)):
            if value is not None:
                self._diagnostic_requested[name] = bool(value)
        self._apply_diagnostic_actions()

    def _apply_diagnostic_actions(self):
        busy = self.state in BUSY
        for name, requested in self._diagnostic_requested.items():
            enabled = requested and callable(self.callbacks.get(name))
            if name == "connection_settings":
                enabled = enabled and self.state in {"stopped", "error"} and not self._owned and not self._diagnostic_running
            elif name in {"diagnose", "save_report"}:
                enabled = enabled and not busy and not self._diagnostic_running
            elif name == "copy_url":
                enabled = enabled and self._connection_has_url
            else:
                enabled = enabled and not busy
            getattr(self, name + "_button").configure(state="normal" if enabled else "disabled")

    def toggle_details(self):
        self.details_open = not self.details_open
        if self.details_open:
            self.details.grid()
        else:
            focused = self.focus_get()
            if focused and str(focused).startswith(str(self.details) + "."):
                self.details_button.focus_set()
            self.details.grid_remove()
        self.details_button.configure(text="설정·상세 ▾" if self.details_open else "설정·상세 ▸")
        self._resize()

    def _primary_action(self):
        if self.state in {"ready", "existing"}:
            self.callbacks["open"]()
        elif self.state == "connection_lost":
            self.callbacks["reconnect"]()
        elif self.state in {"stopped", "error"} and not self._owned:
            self.callbacks["retry"]()

    def _scroll_changed(self, first, last):
        self.scrollbar.set(first, last)
        if float(last) - float(first) >= 0.999:
            self.scrollbar.grid_remove()
        else:
            self.scrollbar.grid()

    def _resize(self, event=None):
        if not hasattr(self, "stop_button"):
            return
        width = max(120, self.canvas.winfo_width())
        self.canvas.itemconfigure(self._content_window, width=width)
        self.status_label.configure(wraplength=max(80, width - 100))
        for label in (self.stage_label, self.description_label):
            label.configure(wraplength=max(80, width - 44))
        self.connection_note_label.configure(wraplength=max(80, width - 44))
        self.footer_label.configure(wraplength=max(80, self.winfo_width() - self.stop_button.winfo_reqwidth()
                                                   - self.minimize_button.winfo_reqwidth() - 72))
        for parent, buttons in ((self.path_buttons, (self.folder_button, self.shortcut_button)),
                                (self.recovery_buttons, (self.retry_button, self.password_button)), *self.operation_rows):
            available = max(120, self.winfo_width() - 48) if parent is self.recovery_buttons else width - 40
            stacked = sum(button.winfo_reqwidth() for button in buttons) + 10 > available
            for i, button in enumerate(buttons):
                button.grid(row=i if stacked else 0, column=0 if stacked else i, sticky="w",
                            padx=(0, 0 if stacked else 8), pady=(0 if i == 0 else (6 if stacked else 0), 0))
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _wheel(self, event):
        if not isinstance(event.widget, tk.Text):
            self.canvas.yview_scroll(-int(event.delta / 120), "units")

    def _see_focus(self, event):
        widget = event.widget
        if not str(widget).startswith(str(self.content) + "."):
            return
        top = self.canvas.canvasy(0)
        y = widget.winfo_rooty() - self.content.winfo_rooty()
        bottom = y + widget.winfo_height()
        height = self.canvas.winfo_height()
        target = y if y < top else bottom - height if bottom > top + height else None
        if target is not None:
            self.canvas.yview_moveto(max(0, target) / max(1, self.content.winfo_height()))

    def start_clock(self):
        self.started_at, self._clock_running = time.monotonic(), True
        self.elapsed_var.set("경과 00:00")
        if self._timer is None:
            self._tick()

    def _tick(self):
        self._timer = None
        if self._clock_running:
            seconds = int(max(0, time.monotonic() - self.started_at))
            self.elapsed_var.set(f"경과 {seconds // 60:02d}:{seconds % 60:02d}")
            self._timer = self.after(200, self._tick)

    def show_progress(self, stage):
        if stage in {"checking", "unlocking", "starting", "waiting"}:
            self.show_state(stage)
        elif stage in {"ready", "existing"}:
            self.stage_var.set(STATES[stage][1])  # 버튼 활성화는 started 결과를 받은 뒤에만 한다.

    def _show_badge(self, state):
        if state in {"ready", "existing"}:
            text, accent, tint, mark = ("실행 중" if state == "ready" else "기존 서버"), GREEN, "#e9f5ef", "check"
        elif state in {"error", "connection_lost"}:
            text, accent, tint, mark = ("확인 필요" if state == "error" else "연결 끊김"), "#9b3c47", "#faecee", "alert"
        elif state in {"stopping", "closing_start", "stop_waiting"}:
            text, accent, tint, mark = "종료 대기", "#825b13", "#fbf2de", "server"
        elif state == "stopped":
            text, accent, tint, mark = "종료됨", MUTED, "#edf0f5", "stop"
        else:
            text, accent, tint, mark = ("입력 대기" if state == "password" else "연결 확인" if state == "reconnecting" else "준비 중"), "#285e91", "#eaf1f9", "server"
        self.badge_var.set(text)
        self.badge.configure(background=tint, foreground=accent)
        canvas = self.status_mark
        canvas.delete("all")
        canvas.create_oval(0, 0, 44, 44, fill=tint, outline="")
        if mark == "check":
            canvas.create_line(12, 22, 19, 29, 32, 15, fill=accent, width=3, capstyle="round", joinstyle="round")
        elif mark == "alert":
            canvas.create_line(22, 12, 22, 25, fill=accent, width=3, capstyle="round")
            canvas.create_oval(20, 30, 24, 34, fill=accent, outline="")
        elif mark == "stop":
            _rounded(canvas, 15, 15, 29, 29, 3, fill=accent, outline="")
        else:
            for y in (12, 20, 28):
                _rounded(canvas, 12, y, 32, y + 5, 2, fill="", outline=accent, width=1.4)
                canvas.create_line(15, y + 2, 17, y + 2, fill=accent, width=1.5)

    def show_state(self, state, *, detail="", owned=False):
        self.state = state
        self._owned = owned
        title, stage, description = STATES[state]
        self.status_var.set(title)
        self.stage_var.set(stage)
        self.description_var.set(description)
        self._show_badge(state)
        if state in {"error", "ready", "existing"}:
            self.stage_label.grid_remove()
        else:
            self.stage_label.grid()
        if state == "error":
            self.description_label.grid_remove()
        else:
            self.description_label.grid()
        busy = state in BUSY
        if busy:
            if not self._clock_running:
                self.start_clock()
            self.progress.grid()
            self.progress.start(50)
        else:
            self._clock_running = False
            self.progress.stop()
            self.progress.grid_remove()
        ready = state in {"ready", "existing"}
        recover = state in {"error", "stop_waiting"}
        can_start = state in {"stopped", "error", "connection_lost"} and not owned
        self.primary_button.configure(state="normal" if ready or can_start else "disabled",
                                      text="업무 화면 열기" if ready else "기존 서버 연결 확인" if state in {"connection_lost", "reconnecting"} else "업무 시작")
        self.folder_button.configure(state="normal" if not busy and not owned and state not in {"existing", "connection_lost"} else "disabled")
        self.stop_button.configure(state="normal" if owned and (not busy or state == "stop_waiting") else "disabled")
        for button in (self.retry_button, self.password_button):
            button.configure(state="normal" if recover and not owned else "disabled")
        if recover:
            self.recovery.grid()
            self._set_text(self.detail_text, detail or description)
        else:
            self.recovery.grid_remove()
        if state == "error":
            self.recovery_buttons.grid()
        else:
            self.recovery_buttons.grid_remove()
        self.footer_var.set("기존 서버 사용 중 · 이 앱에서 종료 불가" if state == "existing" else
                            "기존 서버의 연결만 다시 확인합니다." if state == "connection_lost" else
                            "이 앱이 시작한 서버만 종료합니다." if owned else "업무 자료는 보관됩니다.")
        self._apply_diagnostic_actions()
        self._resize()

    def destroy(self):
        self.progress.stop()
        if self._timer is not None:
            self.after_cancel(self._timer)
            self._timer = None
        super().destroy()


class ConnectionProfileDialog:
    """초안만 다루는 설정 창. 저장·서버 재실행은 호출자가 담당한다."""

    MODES = {"local": "이 PC에서만", "lan": "같은 망의 다른 PC (HTTPS)"}

    def __init__(self, root, values, validate):
        from tkinter import filedialog
        self.parent, self.validate = root, validate
        self.result = None
        self._opener = root.focus_get()
        self.window = window = tk.Toplevel(root)
        window.title("접속 설정 — 저장 후 다시 실행")
        window.geometry("640x600")
        window.minsize(560, 400)
        window.transient(root)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)
        self.variables = {name: tk.StringVar(window, value=str(values.get(name, default))) for name, default in
                          (("mode", "local"), ("port", 8765), ("host", "127.0.0.1"), ("hostname", "127.0.0.1"), ("cert", ""), ("key", ""), ("allow", ""))}
        self._last_mode = self.variables["mode"].get()
        self._host_edited = self._last_mode == "lan"
        self._setting_host = False
        self.variables["host"].trace_add("write", self._host_changed)
        self.mode_choice = tk.StringVar(window)
        title = ttk.Label(window, text="검증 후 설정을 저장하고 다시 실행합니다.\n방화벽·인증서 신뢰는 자동 변경하지 않습니다.", wraplength=600, padding=(16, 12))
        title.grid(row=0, column=0, sticky="ew")
        panel = ttk.Frame(window)
        panel.grid(row=1, column=0, sticky="nsew", padx=16)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(panel, highlightthickness=0, width=1, height=1, takefocus=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(panel, orient="vertical", command=self.canvas.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=bar.set)
        body = ttk.Frame(self.canvas)
        body.columnconfigure(0, weight=1)
        body_window = self.canvas.create_window((0, 0), window=body, anchor="nw")
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(body_window, width=event.width))
        body.bind("<Configure>", lambda event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        ttk.Label(body, text="사용 범위").grid(row=0, column=0, sticky="w")
        self.mode_input = ttk.Combobox(body, textvariable=self.mode_choice, values=list(self.MODES.values()), state="readonly")
        self.mode_input.grid(row=1, column=0, sticky="ew", pady=(4, 12))
        self.mode_input.bind("<<ComboboxSelected>>", lambda event: self.variables["mode"].set(next(key for key, label in self.MODES.items() if label == self.mode_choice.get())))
        self.entries = {}

        def field(parent, row, name, label, *, file=False):
            frame = ttk.Frame(parent)
            frame.grid(row=row, column=0, sticky="ew", pady=(0, 12))
            frame.columnconfigure(0, weight=1)
            ttk.Label(frame, text=label).grid(row=0, column=0, columnspan=2, sticky="w")
            entry = ttk.Entry(frame, textvariable=self.variables[name])
            entry.grid(row=1, column=0, sticky="ew", pady=(4, 0))
            entry.bind("<Return>", lambda event: (self.save(), "break")[1])
            self.entries[name] = entry
            if file:
                def choose():
                    path = filedialog.askopenfilename(parent=window, title=label)
                    if path:
                        self.variables[name].set(path)
                    entry.focus_set()
                button = ttk.Button(frame, text="찾기", command=choose)
                button.grid(row=1, column=1, padx=(8, 0), pady=(4, 0))
                button.bind("<Return>", lambda event: (button.invoke(), "break")[1])

        field(body, 2, "port", "포트 (1~65535)")
        self.lan_fields = ttk.Frame(body)
        self.lan_fields.grid(row=3, column=0, sticky="ew")
        self.lan_fields.columnconfigure(0, weight=1)
        self.lan_note = ttk.Label(self.lan_fields, text="수신 주소 0.0.0.0은 모든 네트워크 장치에서 받되 TLS·허용 대역으로 제한한다는 뜻입니다. 다른 PC의 접속 주소로는 사용하지 않습니다. 접속 이름은 인증서에 등록된 DNS 이름/IP로 입력하세요.", wraplength=500)
        self.lan_note.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        for row, (name, label) in enumerate((("host", "서버 수신 IPv4 (접속 주소와 다름)"), ("hostname", "접속 DNS 이름 또는 IP (인증서와 일치)"),
                                             ("allow", "허용 PC 대역 (예: 192.168.0.0/24, 쉼표로 구분)"), ("cert", "기관에서 신뢰하는 서버 인증서 파일"), ("key", "인증서의 개인키 파일 (내용을 저장하지 않음)")), start=1):
            field(self.lan_fields, row, name, label, file=name in {"cert", "key"})
        self.variables["mode"].trace_add("write", self._mode_changed)
        self._mode_changed()
        footer = ttk.Frame(window, padding=16)
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.message_var = tk.StringVar(window)
        self.message_label = ttk.Label(footer, textvariable=self.message_var, foreground="#9b3c47", wraplength=500)
        self.message_label.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.save_button = ttk.Button(footer, text="저장 후 다시 실행", command=self.save)
        self.save_button.grid(row=1, column=0, sticky="ew")
        self.cancel_button = ttk.Button(footer, text="취소", command=self.cancel)
        self.cancel_button.grid(row=1, column=1, padx=(8, 0))
        for button in (self.save_button, self.cancel_button):
            button.bind("<Return>", lambda event, b=button: (b.invoke(), "break")[1])
        window.bind("<Escape>", lambda event: (self.cancel(), "break")[1])
        window.bind("<FocusIn>", self._see_focus, add="+")
        window.bind("<MouseWheel>", lambda event: self.canvas.yview_scroll(-int(event.delta / 120), "units"))
        def resize(event=None):
            width = max(160, window.winfo_width() - 40)
            self.message_label.configure(wraplength=width)
            title.configure(wraplength=width)
            self.lan_note.configure(wraplength=max(160, self.canvas.winfo_width() - 12))
        window.bind("<Configure>", resize)
        window.protocol("WM_DELETE_WINDOW", self.cancel)
        self.mode_input.focus_set()

    def _mode_changed(self, *_):
        mode = self.variables["mode"].get()
        if mode == "lan" and self._last_mode == "local" and not self._host_edited and self.variables["host"].get() == "127.0.0.1":
            self._setting_host = True
            self.variables["host"].set("0.0.0.0")
            self._setting_host = False
        self._last_mode = mode
        self.mode_choice.set(self.MODES.get(mode, self.MODES["local"]))
        self.lan_fields.grid() if mode == "lan" else self.lan_fields.grid_remove()

    def _host_changed(self, *_):
        if not self._setting_host:
            self._host_edited = True

    def _see_focus(self, event):
        widget = event.widget
        if widget not in self.entries.values() and widget is not self.mode_input:
            return
        top = self.canvas.canvasy(0)
        y = widget.winfo_rooty() - self.canvas.winfo_rooty() + top
        bottom = y + widget.winfo_height()
        height = self.canvas.winfo_height()
        target = y if y < top else bottom - height if bottom > top + height else None
        if target is not None:
            region = self.canvas.bbox("all")
            self.canvas.yview_moveto(max(0, target) / max(1, region[3] if region else 1))

    def save(self):
        values = {name: value.get().strip() for name, value in self.variables.items()}
        try:
            values["port"] = int(values["port"])
            if not 1 <= values["port"] <= 65535:
                raise ValueError
        except ValueError:
            self.message_var.set("포트는 1~65535의 정수로 입력하세요.")
            self.entries["port"].focus_set()
            return
        try:
            error = self.validate(values)
        except Exception:
            error = "설정을 확인하지 못했습니다. 입력값을 다시 확인하세요."
        if error is not None:
            self.message_var.set(error[:300] if isinstance(error, str) else "설정값을 다시 확인하세요.")
            return
        self.result = values
        self._finish()

    def cancel(self):
        self.result = None
        self._finish()

    def _finish(self):
        try:
            self.window.grab_release()
            self.window.destroy()
            if self._opener and self._opener.winfo_exists():
                self._opener.focus_set()
        except tk.TclError:
            pass


def edit_connection_profile(root, values, validate):
    dialog = ConnectionProfileDialog(root, values, validate)
    dialog.window.wait_visibility()
    dialog.window.grab_set()
    root.wait_window(dialog.window)
    return dialog.result
