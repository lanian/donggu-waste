"""실제 Tk 실행 앱 화면: 상태·배율·경계·키보드 회귀. 서버나 업무 DB를 만들지 않는다."""
from pathlib import Path
import gc
import os
import socket
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
passed = 0


def ok(condition, message):
    global passed
    assert condition, message
    passed += 1
    print("  ✓", message, flush=True)


def fully_visible(widget):
    left, top = widget.winfo_rootx(), widget.winfo_rooty()
    right, bottom = left + widget.winfo_width(), top + widget.winfo_height()
    parent = widget.master
    while parent is not None:
        x, y = parent.winfo_rootx(), parent.winfo_rooty()
        if not parent.winfo_ismapped() or left < x or top < y or right > x + parent.winfo_width() + 1 or bottom > y + parent.winfo_height() + 1:
            return False
        parent = parent.master
    return True


def make_view(scaling=4 / 3):
    import tkinter as tk
    from desktop_view import DesktopView
    root = tk.Tk()
    root.tk.call("tk", "scaling", scaling)
    root.geometry("720x560")
    actions = []
    callbacks = {name: (lambda key=name: actions.append(key)) for name in ("open", "retry", "reconnect", "password", "stop", "folder", "shortcut", "minimize")}
    view = DesktopView(root, "0.13.3", callbacks=callbacks)
    view.pack(fill="both", expand=True)
    root.update()
    return root, view, actions


def main_thread_tk_cleanup(test):
    """여러 Tk fixture의 순환 참조를 작업 스레드 대신 메인 스레드에서 수거한다."""
    def run():
        enabled = gc.isenabled()
        gc.collect()
        gc.disable()
        try:
            return test()
        finally:
            gc.collect()
            if enabled:
                gc.enable()
    return run


@main_thread_tk_cleanup
def main_queue_regressions():
    """실제 Tk queue·버튼을 사용하되 종료 시간과 프로세스 상태만 가상 controller로 제어한다."""
    import desktop
    import desktop_view
    from desktop_runtime import DesktopError

    real_view = desktop_view.DesktopView
    outcomes = []
    for case in ("stop_wait", "close_wait", "unexpected", "restart", "close_start", "existing"):
        history, failures, controllers = [], [], []
        phase = [0]
        release = threading.Event()

        class Child:
            code = None

            def poll(self):
                return self.code

        class Controller:
            def __init__(self, state, port):
                self.state_dir, self.port = Path(state), port
                self.url = f"http://127.0.0.1:{port}/"
                self.child, self.status = None, "idle"
                self.starts = 0
                controllers.append(self)

            @property
            def owned(self):
                return self.child is not None

            def start(self, password_provider, *, on_progress=None):
                self.starts += 1
                if case == "existing":
                    self.status = "existing"
                    return "existing"
                password_provider(False)
                if case == "close_start":
                    if on_progress:
                        on_progress("waiting")
                    assert release.wait(3), "가상 시작 응답 해제 신호 없음"
                self.child, self.status = Child(), "ready"
                if on_progress:
                    on_progress("ready")
                return "ready"

            def stop(self):
                if self.child and self.child.poll() is None:
                    raise DesktopError("서버가 진행 중인 작업을 마치고 있습니다. 잠시 기다렸다가 다시 종료하세요.")
                self.child, self.status = None, "stopped"

        def capture(root, version, **kwargs):
            view = real_view(root, version, **kwargs)
            original = view.show_state
            timers = []

            def show(state, **options):
                history.append(state)
                return original(state, **options)

            view.show_state = show

            def request_close():
                root.tk.call(root.protocol("WM_DELETE_WINDOW"))

            def inspect():
                if case == "existing" and view.state == "existing":
                    if not view.folder_button.instate(["disabled"]):
                        failures.append("기존 서버 재사용 중 선택 폴더와 실제 자료 위치가 달라질 수 있음")
                    if "기존 실행 프로그램에서 확인" not in view.description_var.get():
                        failures.append("기존 서버의 실제 자료 위치를 알 수 없다는 안내 누락")
                    phase[0] = 3
                    request_close()
                    return
                if case == "close_start" and view.state == "waiting" and phase[0] == 0:
                    phase[0] = 1
                    request_close()
                    release.set()
                elif view.state == "ready" and phase[0] == 0:
                    phase[0] = 1
                    if case == "unexpected":
                        controllers[0].child.code = 0
                    elif case == "close_wait":
                        request_close()
                    else:
                        view.stop_button.invoke()
                elif view.state == "stop_waiting" and phase[0] == 1:
                    starts = controllers[0].starts
                    view.primary_button.invoke()
                    view.retry_button.invoke()
                    if controllers[0].starts != starts:
                        failures.append("종료 대기 중 중복 시작")
                    phase[0] = 2
                    controllers[0].child.code = 0
                elif view.state == "error":
                    if case != "unexpected":
                        failures.append("요청한 정상 종료를 오류로 표시함")
                    phase[0] = 3
                    request_close()
                    return
                elif view.state == "stopped":
                    if case == "close_wait":
                        failures.append("창 닫기 의도를 잃고 창을 남김")
                    if case == "restart" and phase[0] == 2:
                        phase[0] = 3
                        view.primary_button.invoke()
                        view.primary_button.invoke()
                    else:
                        phase[0] = 3
                        request_close()
                        return
                elif case == "restart" and view.state == "ready" and phase[0] == 3:
                    if controllers[0].starts != 2:
                        failures.append("업무 시작의 반복 클릭이 두 서버 시작으로 이어짐")
                    controllers[0].child.code = 0
                    request_close()
                    return
                timers.append(root.after(20, inspect))

            def deadline():
                failures.append("종료 상태 전환 제한시간 초과")
                if controllers[0].child:
                    controllers[0].child.code = 0
                request_close()

            timers.append(root.after(20, inspect))
            timers.append(root.after(5000, deadline))
            destroy = root.destroy

            def clean_destroy():
                for timer in timers:
                    root.after_cancel(timer)
                destroy()

            root.destroy = clean_destroy
            return view

        with tempfile.TemporaryDirectory(prefix="dg-ui-stop-intent-") as temporary, socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            arguments = ["desktop.py", "--state", temporary, "--port", str(port), "--no-browser"]
            with patch.object(sys, "argv", arguments), patch.object(desktop, "DesktopController", Controller), \
                    patch.object(desktop, "resolve_db_key", return_value="fictitious-ui-only-key"), \
                    patch.object(desktop_view, "DesktopView", capture):
                result = desktop.main()
            outcomes.append((case, result, failures, history, phase[0]))
    for case, result, failures, history, phase in outcomes:
        if case == "existing":
            ok(result == 0 and not failures, "기존 서버 재사용 중 자료 폴더 변경을 막고 실제 위치 확인 안내 유지")
        elif case == "restart":
            ok(result == 0 and not failures and history.count("ready") == 2,
               "종료 후 주 동작으로 다시 시작하며 준비 중 반복 클릭은 막음")
        elif case == "close_start":
            ok(result == 0 and not failures and "closing_start" in history and "ready" not in history,
               "시작 중 닫기 요청 뒤 늦은 ready는 업무 열기로 되살아나지 않음")
        elif case == "unexpected":
            ok(result == 0 and not failures and "error" in history, "종료를 요청하지 않은 자발적 프로세스 종료는 오류로 표시")
        else:
            ok(result == 0 and not failures and "stop_waiting" in history and "error" not in history
               and (case == "close_wait" or "stopped" in history),
               "종료 대기 뒤 정상 exit0은 창 닫기 의도를 보존해 자동 닫음" if case == "close_wait"
               else "종료 대기 뒤 정상 exit0은 오류가 아닌 종료 완료로 표시")


@main_thread_tk_cleanup
def existing_probe_regressions():
    """실제 Tk 이벤트 큐에서 외부 서버 확인의 소실·중복·늦은 결과를 검증한다."""
    import desktop
    import desktop_view

    real_view = desktop_view.DesktopView
    outcomes = []
    for case in ("lost", "transient", "single", "stale_restart", "stale_close"):
        history, failures, controllers, keys, opened = [], [], [], [], []
        open_baseline = [None]
        started, release, completed = threading.Event(), threading.Event(), threading.Event()
        phase = [0]

        class Child:
            def poll(self):
                return None

        class Controller:
            def __init__(self, state, port):
                self.state_dir, self.port = Path(state), port
                self.url = f"http://127.0.0.1:{port}/"
                self.child, self.status = None, "idle"
                self.starts, self.probes, self.stops = 0, 0, 0
                controllers.append(self)

            @property
            def owned(self):
                return self.child is not None

            def start(self, password_provider, *, on_progress=None):
                self.starts += 1
                if self.starts > 1:
                    password_provider(False)
                self.status = "existing" if self.starts == 1 else "ready"
                self.child = Child() if self.status == "ready" else None
                return self.status

            def check_existing(self):
                self.probes += 1
                started.set()
                if case not in {"lost", "transient"}:
                    assert release.wait(2), "가상 상태 확인 해제 신호 없음"
                completed.set()
                return case == "transient" and (self.probes == 2 or self.probes >= 5)

            def stop(self):
                self.stops += 1
                self.child, self.status = None, "stopped"

        def capture(root, version, **kwargs):
            view = real_view(root, version, **kwargs)
            show = view.show_state
            timers = []

            def record(state, **options):
                history.append(state)
                return show(state, **options)

            view.show_state = record

            def close():
                root.tk.call(root.protocol("WM_DELETE_WINDOW"))

            def finish_race():
                if case == "single" and controllers[0].probes != 1:
                    failures.append("외부 서버 확인이 겹쳐 실행됨")
                if case == "stale_restart" and view.state != "ready":
                    failures.append("이전 확인 결과가 다시 시작한 화면을 덮어씀")
                release.set()
                close()

            def inspect():
                if case in {"lost", "transient"} and view.state == "connection_lost" and phase[0] == 0:
                    # 화면 그리기와 20ms 가상 확인 간격의 실행 순서에 의존하지 않는다.
                    # 초기 자동 열기도 보존하여, 수동 실패의 잘못된 열기를 숨기지 않는다.
                    if opened != [controllers[0].url]:
                        failures.append("초기 업무 화면을 한 번 열지 않거나 자동 확인 중 추가로 열었음")
                    open_baseline[0] = len(opened)
                    expected = 4 if case == "transient" else 2
                    if controllers[0].probes != expected or controllers[0].starts != 1 or controllers[0].stops:
                        failures.append("연결 실패를 확인하며 서버를 자동 시작하거나 종료함")
                    if view.primary_button.cget("text") != "기존 서버 연결 확인" or view.primary_button.instate(["disabled"]):
                        failures.append("연결 끊김 뒤 기존 서버만 다시 확인할 수 없음")
                    view.primary_button.invoke()
                    view.primary_button.invoke()
                    phase[0] = 2
                elif case in {"lost", "transient"} and phase[0] == 2 and (
                        view.state == "existing" if case == "transient" else view.state == "connection_lost"):
                    if controllers[0].starts != 1 or keys or controllers[0].stops:
                        failures.append("재연결 확인이 새 서버 시작·키 확인·종료를 호출함")
                    if case == "lost" and (len(opened) != open_baseline[0] or "기존 실행 프로그램" not in view.description_var.get()):
                        failures.append("실패한 재연결에서 브라우저를 열거나 원래 실행 프로그램 안내를 잃음")
                    if case == "transient" and len(opened) != open_baseline[0] + 1:
                        failures.append("재연결 성공 뒤 업무 화면을 한 번 열지 않음")
                    close()
                    return
                elif case not in {"lost", "transient"} and started.is_set() and phase[0] == 0:
                    phase[0] = 1
                    if case == "stale_close":
                        close()
                        release.set()
                        return
                    if case == "stale_restart":
                        view.callbacks["retry"]()  # 이미 큐에 들어온 재시도 콜백과 확인 완료의 경합.
                        release.set()
                    timers.append(root.after(400, finish_race))
                timers.append(root.after(15, inspect))

            def deadline():
                failures.append("외부 서버 확인 상태 전환 제한시간 초과")
                release.set()
                close()

            timers.extend((root.after(15, inspect), root.after(5000, deadline)))
            destroy = root.destroy

            def clean_destroy():
                release.set()
                for timer in timers:
                    root.after_cancel(timer)
                destroy()

            root.destroy = clean_destroy
            return view

        with tempfile.TemporaryDirectory(prefix="dg-ui-existing-probe-") as temporary, socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            arguments = ["desktop.py", "--state", temporary, "--port", str(probe.getsockname()[1])]
            with patch.object(sys, "argv", arguments), patch.object(desktop, "DesktopController", Controller), \
                    patch.object(desktop, "EXISTING_CHECK_SECONDS", 0.02, create=True), \
                    patch.object(desktop, "resolve_db_key", side_effect=lambda *a, **kw: keys.append(True) or "가상 키"), \
                    patch.object(desktop.webbrowser, "open", side_effect=opened.append), \
                    patch.object(desktop_view, "DesktopView", capture):
                result = desktop.main()
            outcomes.append((case, result, failures, history))
    for case, result, failures, history in outcomes:
        ok(result == 0 and not failures and ("connection_lost" in history if case in {"lost", "transient"} else "connection_lost" not in history),
           {"lost": "연결 확인 실패는 새 서버·키를 사용하지 않고 원래 프로그램 안내 유지",
            "transient": "일시 실패 횟수를 초기화하고 수동 재연결 성공 때 업무 화면만 한 번 열기",
            "single": "외부 서버 확인 중에도 Tk가 응답하며 확인 작업은 한 번만 실행",
            "stale_restart": "늦은 외부 서버 확인 결과가 재시작 상태를 덮어쓰지 않음",
            "stale_close": "창을 닫은 뒤 외부 서버 확인 결과가 화면을 되살리지 않음"}[case] + str(failures))


def main():
    if os.name != "nt":
        print("SKIP Windows UI")
        return
    ok((ROOT / "pc/desktop_view.py").is_file(), "독립된 실제 Tk 실행 화면이 있음")
    sys.path.insert(0, str(ROOT / "pc"))
    root, view, actions = make_view()
    try:
        view.show_state("waiting")
        root.update()
        ok(not view.path_text.winfo_ismapped() and not view.shortcut_button.winfo_ismapped(),
           "첫 화면에서는 자료 위치와 바로가기 설정을 접어 주 동작에 집중")
        ok("서버" in view.status_var.get() and view.primary_button.instate(["disabled"]),
           "시작 중에는 실제 서버 응답 대기를 표시하고 업무 열기를 막음")
        ok(str(view.progress.cget("mode")) == "indeterminate", "측정할 수 없는 진행률을 백분율로 꾸미지 않음")
        begun = view.started_at
        for stage in ("unlocking", "starting", "waiting"):
            view.show_progress(stage)
        ok(view.started_at == begun, "실제 준비 단계가 바뀌어도 이번 시도의 경과시간을 초기화하지 않음")
        view.show_state("password")
        ok("비밀번호" in view.stage_var.get(), "비밀번호 입력 대기를 서버 완료로 표시하지 않음")
        view.show_state("ready", owned=True)
        root.update()
        ok(view.status_var.get() == "PC 서버 준비 완료" and view.primary_button.instate(["!disabled"])
           and view.folder_button.instate(["disabled"]) and view.stop_button.instate(["!disabled"]),
           "서버 준비 완료에서 업무 열기·소유 서버 종료만 가능하고 자료 위치 변경은 잠금")
        ok(hasattr(view, "minimize_button"), "서버 종료 옆에 독립된 최소화 버튼을 제공")
        view.minimize_button.invoke()
        ok(actions == ["minimize"], "최소화 버튼은 종료나 업무 열기 대신 전용 콜백만 호출")
        actions.clear()
        ok(not view.canvas.master.grid_slaves(row=0, column=1), "내용이 창 안에 들어가면 불필요한 세로 스크롤바를 숨김")
        root.focus_force()
        view.primary_button.focus_set()
        view.primary_button.event_generate("<Return>")
        root.update()
        ok(actions == ["open"], "주 동작은 실제 Tk Enter 이벤트로 한 번만 실행")
        view.show_state("existing", owned=False)
        root.update()
        view.stop_button.invoke()
        ok(view.stop_button.instate(["disabled"]) and actions == ["open"] and "이 앱에서 종료 불가" in view.footer_var.get(),
           "재사용 중인 외부 서버는 종료 버튼으로 종료하지 않음")
        path = "D:\\가상 업무 자료\\" + "자료폴더이름이긴경로\\" * 20
        view.set_path(path)
        root.geometry("640x440")
        view.details_button.invoke()
        root.update()
        ok(bool(view.canvas.master.grid_slaves(row=0, column=1)), "설정 내용이 넘칠 때만 세로 스크롤바를 표시")
        view.path_text.focus_set()
        view.path_text.event_generate("<Control-a>")
        view.path_text.event_generate("<Control-c>")
        root.update()
        ok(root.clipboard_get() == path and view.path_text.get("1.0", "end-1c") == path,
           "긴 자료 경로를 생략하지 않고 키보드로 전체 선택·복사")
        view.details_button.invoke()
        root.update()
        ok(not view.path_text.winfo_ismapped() and view.path_text.get("1.0", "end-1c") == path,
           "설정·상세를 닫아도 선택 경로와 긴 경로 원문은 보존")
        error = "가상 오류: 자료 폴더를 확인하세요. " * 80
        view.show_state("error", detail=error, owned=False)
        root.update()
        ok(view.detail_text.get("1.0", "end-1c") == error and str(view.detail_text.cget("state")) == "disabled"
           and view.retry_button.instate(["!disabled"]) and view.password_button.instate(["!disabled"]),
           "긴 오류 전문을 제한 높이 읽기 영역에 유지하고 기존 재시도·비밀번호 동작 제공")
        view.retry_button.invoke()
        view.password_button.invoke()
        ok(actions[-2:] == ["retry", "password"], "재시도와 비밀번호 재입력은 서로 다른 기존 콜백 유지")
        ok(view.recovery.winfo_ismapped() and not view.path_text.winfo_ismapped(),
           "설정이 접혀 있어도 오류와 비밀번호 복구 동작은 직접 표시")
        root.geometry("640x440")
        root.update()
        ok(fully_visible(view.retry_button) and fully_visible(view.password_button),
           "작은 기본 창에서도 오류 복구 버튼을 스크롤 없이 직접 사용")
        for state in ("stopped", "error"):
            view.show_state(state)
            view.primary_button.invoke()
            ok(str(view.primary_button.cget("text")) == "업무 시작" and actions[-1] == "retry",
               f"{state}에서는 주 동작 한 번으로 업무를 다시 시작")
        before = len(actions)
        view.show_state("stop_waiting", owned=True)
        view.primary_button.invoke()
        view.retry_button.invoke()
        ok(view.primary_button.instate(["disabled"]) and len(actions) == before,
           "종료 대기 중에는 주 동작과 재시도로 중복 시작하지 않음")
    finally:
        root.destroy()

    for scaling in (4 / 3, 2.0, 8 / 3):
        root, view, _ = make_view(scaling)
        try:
            for width, height in ((640, 440), (560, 400), (720, 560), (620, 500)):
                root.geometry(f"{width}x{height}")
                view.set_path("E:\\가상 경로\\" + "매우긴자료폴더\\" * 30)
                for expanded in (False, True):
                    if view.details_open != expanded:
                        view.details_button.invoke()
                    for state in ("waiting", "ready", "existing", "error", "stopping", "stopped"):
                        view.show_state(state, detail="가상 설명이 긴 오류와 상태 " * 60, owned=state in ("ready", "stopping"))
                        root.update()
                        for widget in (view.header, view.canvas, view.primary_button, view.footer, view.minimize_button, view.stop_button):
                            x = widget.winfo_rootx() - root.winfo_rootx()
                            y = widget.winfo_rooty() - root.winfo_rooty()
                            assert x >= 0 and y >= 0 and x + widget.winfo_width() <= root.winfo_width() + 1
                            assert y + widget.winfo_height() <= root.winfo_height() + 1, (scaling, width, state, widget)
                        assert view.canvas.winfo_height() >= 40
                        assert view.detail_text.winfo_reqheight() < root.winfo_height()
                        if state == "error":
                            assert fully_visible(view.retry_button) and fully_visible(view.password_button)
                        for widget in (view.brand, view.version, view.status_label, view.stage_label, view.description_label,
                                       view.folder_button, view.shortcut_button, view.retry_button, view.password_button):
                            if widget.winfo_ismapped():
                                right = widget.winfo_rootx() - root.winfo_rootx() + widget.winfo_width()
                                assert right <= root.winfo_width() + 1, (scaling, width, state, widget)
                ok(True, f"{round(scaling * 75)}% 배율 {width}×{height}: 고정 주 동작·종료와 스크롤 정보 영역이 창 경계 안에 있음")
            if scaling == 8 / 3:
                view.show_state("error", detail="가상 오류 " * 100)
                root.update()
                view.canvas.yview_moveto(0)
                root.focus_force()
                view.shortcut_button.focus_set()
                root.update()
                y = view.shortcut_button.winfo_rooty() - view.canvas.winfo_rooty()
                ok(0 <= y and y + view.shortcut_button.winfo_height() <= view.canvas.winfo_height() + 1,
                   "큰 글씨에서 정보 영역의 키보드 포커스가 화면 밖에 숨지 않음")
        finally:
            root.destroy()

    root, view, _ = make_view()
    try:
        view.show_state("checking")
        root.after(1150, root.quit)
        root.mainloop()
        ok(view.elapsed_var.get() != "경과 00:00", "실제 Tk 타이머에서 준비 경과시간이 흐름")
        view.show_state("ready", owned=True)
        frozen = view.elapsed_var.get()
        root.after(300, root.quit)
        root.mainloop()
        ok(view.elapsed_var.get() == frozen, "서버 준비 완료 후 이번 준비 경과시간을 고정")
    finally:
        root.destroy()
    main_queue_regressions()
    existing_probe_regressions()
    print(f"실행 앱 실제 Tk UI {passed}개 통과")


if __name__ == "__main__":
    main()
