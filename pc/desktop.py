"""동구 PC 실행 앱: 준비 상태·자료 위치·업무 화면·정상 종료를 하나의 창에서 관리."""
from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import webbrowser

from desktop_runtime import (APP_TITLE, DesktopController, DesktopError, child_main, create_shortcut,
                             load_state_choice, resolve_db_key, save_state_choice)

EXISTING_CHECK_SECONDS = 3.0


def launcher_command(state, profile):
    from desktop_connection import profile_arguments
    if getattr(sys, "frozen", False):
        command = [sys.executable]
    else:
        interpreter = Path(sys.executable)
        windowed = interpreter.with_name("pythonw.exe")
        command = [str(windowed if os.name == "nt" and windowed.is_file() else interpreter), str(Path(__file__).resolve())]
    return command + ["--state", str(state)] + profile_arguments(profile)


def open_browser(url, on_error):
    try:
        if webbrowser.open(url):
            return True
    except Exception:
        pass
    on_error("브라우저를 열지 못했습니다. 접속 주소를 복사해 브라우저에 붙여 넣거나 ‘업무 시작’을 다시 누르세요.\n" + url)
    return False


def resolve_initial_profile(options):
    from desktop_connection import ConnectionProfile, normalize_url, resolve_profile
    try:
        profile = resolve_profile(options)
        if profile.mode not in {"local", "lan"} or type(profile.port) is not int or not 1 <= profile.port <= 65535:
            raise ValueError("invalid profile")
        normalize_url(profile.url)
        return profile, None
    except (ValueError, TypeError):
        explicit_port = getattr(options, "port", None)
        port = explicit_port if type(explicit_port) is int and 1 <= explicit_port <= 65535 else 8765
        return ConnectionProfile(port=port), "저장된 접속 설정을 읽지 못했습니다. 서버를 시작하지 않았습니다. ‘접속 설정’에서 확인 후 저장하세요."


def main():
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--desktop-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--state")
    parser.add_argument("--port", type=int)
    parser.add_argument("--mode", choices=("local", "lan"))
    for option in ("host", "hostname", "cert", "key", "allow"):
        parser.add_argument("--" + option)
    parser.add_argument("--check-connection", nargs="?", const="", metavar="URL", help="자료를 열지 않고 이 PC에서 접속만 점검")
    parser.add_argument("--check-output", type=Path, metavar="JSON", help="점검 창 대신 안전한 JSON 보고서를 지정 파일에 저장")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.check_output is not None and args.check_connection is None:
        parser.error("--check-output은 --check-connection URL과 함께 사용하세요.")
    if args.check_connection is not None:
        if args.check_output is not None:
            from desktop_check import connection_check_report
            try:
                result = connection_check_report(args.check_connection, args.check_output)
                return 0 if result.get("code") == "ready" else 1
            except ValueError:
                return 2
        from desktop_check import connection_check_main
        return connection_check_main(args.check_connection)
    from desktop_connection import ConnectionProfile, save_profile, validate_profile
    from desktop_diagnostics import DiagnosticSnapshot, collect_diagnostics, diagnostics_text, save_report
    profile, profile_error = resolve_initial_profile(args)
    args.port = profile.port
    state = load_state_choice(args.state)
    if args.desktop_child:
        if profile_error:
            raise DesktopError(profile_error)
        return child_main(state, args.port, connection=profile)

    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog
    from desktop_tray import DesktopTray
    from desktop_view import DesktopView, edit_connection_profile
    from dgpc import __version__

    mutex = None
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        mutex = kernel.CreateMutexW(None, False, f"Local\\DongguWasteDesktop-{args.port}")
        if ctypes.get_last_error() == 183:
            user = ctypes.WinDLL("user32", use_last_error=True)
            user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
            user.FindWindowW.restype = wintypes.HWND
            user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user.SetForegroundWindow.argtypes = [wintypes.HWND]
            window = user.FindWindowW(None, APP_TITLE)
            if window:
                user.ShowWindow(window, 9)
                user.SetForegroundWindow(window)
            # 창이 아직 만들어지지 않았어도 서버 초기화를 두 번 시작하지 않는다.
            return 0

    root = tk.Tk()
    root.title(APP_TITLE)
    try:
        from dgpc.paths import resource_root
        root.iconphoto(True, tk.PhotoImage(file=str(resource_root() / "web" / "icons" / "icon-192.png")))
    except (ImportError, OSError, tk.TclError):
        pass
    root.geometry("680x480")
    root.minsize(560, 400)
    events = queue.Queue()
    def new_controller(selected):
        # 기존 local 생성자/시험 fixture 계약도 유지한다.
        return (DesktopController(selected, profile.port, connection=profile) if profile.mode == "lan"
                else DesktopController(selected, profile.port))

    controller = new_controller(state)
    busy = False
    closing = False
    stop_requested = False
    operation_epoch = 0
    existing_probe = False
    existing_failures = 0
    next_existing_check = 0.0
    diagnostic_epoch = 0
    diagnostic_running = False
    last_report = None
    last_connection_info = None
    pending_restart = None
    tray = DesktopTray(root, APP_TITLE, on_exit=lambda: stop(True),
                       on_error=lambda detail: messagebox.showerror(APP_TITLE, detail, parent=root))
    view = DesktopView(root, __version__, callbacks={
        "open": lambda: open_work(), "retry": lambda: begin(), "reconnect": lambda: reconnect(), "password": lambda: begin(True),
        "stop": lambda: stop(), "folder": lambda: select_folder(), "shortcut": lambda: shortcut(),
        "minimize": tray.minimize,
        "connection_settings": lambda: connection_settings(), "diagnose": lambda: diagnose(),
        "copy_url": lambda: copy_url(), "open_logs": lambda: open_logs(),
        "save_report": lambda: export_report(), "connection_checker": lambda: connection_checker(),
    })
    view.pack(fill="both", expand=True)
    view.set_path(state)
    open_button, retry_button, password_button = view.primary_button, view.retry_button, view.password_button
    stop_button, folder_button = view.stop_button, view.folder_button

    def refresh_operations():
        nonlocal last_connection_info
        inactive = busy or closing or stop_requested
        reusable = controller.status == "existing"
        connection_info = {
            "mode_label": "같은 기관망 · HTTPS" if profile.mode == "lan" else "이 PC에서만 접속",
            "url": controller.url, "peer_url": profile.advertised_url if profile.mode == "lan" else "",
            "note": ("기존 서버 사용 중: 실제 접속 설정·자료 폴더는 이 창에서 확인하지 못했습니다."
                     if reusable else "다른 PC 접속은 해당 PC에서 별도로 점검해야 합니다."),
        }
        if connection_info != last_connection_info:
            view.set_connection(connection_info)
            last_connection_info = connection_info
        view.set_diagnostic_actions(settings=not inactive and not controller.owned and not reusable and view.state in {"stopped", "error"},
                                    diagnose=not inactive and not diagnostic_running, copy_url=not closing,
                                    open_logs=not closing, save_report=last_report is not None and not inactive,
                                    connection_checker=not inactive)

    def invalidate_diagnostics():
        nonlocal diagnostic_epoch, diagnostic_running, last_report
        diagnostic_epoch += 1
        diagnostic_running = False
        last_report = None
        view.set_diagnostics("‘연결 점검’을 누르면 이 PC의 연결 상태와 저장공간을 확인합니다.")
        refresh_operations()

    def copy_url():
        if not closing:
            root.clipboard_clear()
            root.clipboard_append(controller.url)

    def open_logs():
        directory = controller.state_dir / "logs"
        if not directory.is_dir():
            messagebox.showinfo(APP_TITLE, "선택한 자료 폴더에 logs 폴더가 아직 없습니다.", parent=root)
            return
        try:
            if os.name == "nt":
                os.startfile(str(directory))
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(directory)])
        except OSError:
            messagebox.showerror(APP_TITLE, "선택한 자료 폴더의 logs를 열지 못했습니다.", parent=root)

    def export_report():
        if last_report is None or busy or closing:
            return
        selected_report, checked_controller = last_report, controller
        checked_epoch, checked_diag = operation_epoch, diagnostic_epoch
        destination = filedialog.asksaveasfilename(title="운영 진단 보고서 저장", parent=root,
            defaultextension=".json", initialfile="동구-PC-운영점검.json", filetypes=[("JSON 보고서", "*.json")])
        if (destination and selected_report is last_report and checked_controller is controller
                and checked_epoch == operation_epoch and checked_diag == diagnostic_epoch
                and not busy and not closing and not stop_requested):
            try:
                save_report(destination, selected_report)
            except OSError:
                messagebox.showerror(APP_TITLE, "보고서를 저장하지 못했습니다. 저장 위치와 권한을 확인하세요.", parent=root)

    def diagnose():
        nonlocal diagnostic_running
        if busy or closing or stop_requested or diagnostic_running:
            return
        checked_controller, checked_epoch, checked_diag = controller, operation_epoch, diagnostic_epoch
        snapshot = DiagnosticSnapshot(profile.mode, profile.port, controller.url, controller.state_dir, controller.status)
        diagnostic_running = True
        view.set_diagnostics("연결과 저장공간을 점검하고 있습니다…", running=True)
        refresh_operations()

        def check():
            try:
                result = collect_diagnostics(snapshot)
            except Exception:
                result = None
            events.put(("diagnosed", checked_controller, checked_epoch, checked_diag, result))
        threading.Thread(target=check, daemon=True).start()

    def connection_checker():
        if busy or closing or stop_requested:
            return
        command = launcher_command(controller.state_dir, profile)
        # 점검기에는 상태/프로필 인자를 넘기지 않는다. 별도 기기에서 실행할 때도 DB 초기화가 없다.
        prefix = command[:1] if getattr(sys, "frozen", False) else command[:2]
        try:
            subprocess.Popen(prefix + ["--check-connection", controller.url],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            messagebox.showerror(APP_TITLE, "접속 점검기를 열지 못했습니다. 프로그램 폴더의 ‘PC 접속 점검.cmd’를 실행하세요.", parent=root)

    def connection_settings():
        nonlocal pending_restart
        if (busy or closing or stop_requested or controller.owned or controller.status == "existing"
                or view.state not in {"stopped", "error"}):
            return

        def validate(values):
            try:
                validate_profile(ConnectionProfile.from_dict(values))
            except ValueError as error:
                return str(error)
            except TypeError:
                return "접속 설정 값을 확인하세요."
            return None

        values = edit_connection_profile(root, profile.to_dict(), validate)
        if (values is None or busy or closing or stop_requested or controller.owned or controller.status == "existing"
                or view.state not in {"stopped", "error"}):
            return
        try:
            selected = ConnectionProfile.from_dict(values)
            validate_profile(selected)
            save_profile(selected)
        except (ValueError, TypeError, OSError):
            messagebox.showerror(APP_TITLE, "접속 설정을 저장하지 못했습니다. 설정 값과 저장 권한을 확인하세요.", parent=root)
            return
        pending_restart = launcher_command(controller.state_dir, selected)
        stop(True)

    def open_work():
        if not busy and not closing and not stop_requested and view.state in {"ready", "existing"}:
            open_browser(controller.url, lambda detail: messagebox.showerror(APP_TITLE, detail, parent=root))

    def shortcut():
        try:
            created = create_shortcut(controller.state_dir, port=controller.port, connection=profile)
            messagebox.showinfo(APP_TITLE, f"바탕화면 바로가기를 만들었습니다.\n{created}", parent=root)
        except (DesktopError, OSError) as error:
            messagebox.showerror(APP_TITLE, str(error), parent=root)

    def prompt(new):
        done, result = threading.Event(), []
        events.put(("password", new, done, result))
        while not done.wait(0.2):
            if closing:
                return None
        return result[0]

    def begin(force_prompt=False):
        nonlocal busy, stop_requested, operation_epoch
        if busy or closing or stop_requested or controller.owned or view.state in {"connection_lost", "reconnecting"}:
            return
        busy = True
        stop_requested = False
        operation_epoch += 1
        invalidate_diagnostics()
        view.start_clock()
        view.show_state("checking")
        for button in (retry_button, password_button, stop_button, open_button, folder_button):
            button.configure(state="disabled")
        refresh_operations()

        def start():
            try:
                result = controller.start(lambda new: resolve_db_key(new, prompt, force_prompt=force_prompt),
                                          on_progress=lambda stage: events.put(("progress", stage)))
                events.put(("started", result))
            except Exception as error:
                events.put(("error", str(error)))
        threading.Thread(target=start, daemon=True).start()

    def launch_existing_check(event):
        nonlocal existing_probe
        existing_probe = True
        checked_controller, checked_epoch = controller, operation_epoch

        def check_existing():
            try:
                healthy = checked_controller.check_existing()
            except Exception:
                healthy = False
            events.put((event, checked_controller, checked_epoch, healthy))

        threading.Thread(target=check_existing, daemon=True).start()

    def reconnect():
        nonlocal busy, operation_epoch
        if (busy or closing or stop_requested or existing_probe or controller.owned
                or controller.status != "existing" or view.state != "connection_lost"):
            return
        busy = True
        operation_epoch += 1
        view.show_state("reconnecting")
        launch_existing_check("reconnected")

    def stop(close=False):
        nonlocal busy, closing, stop_requested, operation_epoch
        if close:
            closing = True
            modal = root.grab_current()
            if isinstance(modal, simpledialog.Dialog):
                modal.cancel()
        if busy:
            view.show_state("stopping" if stop_requested else "closing_start", owned=controller.owned)
            return
        busy = True
        stop_requested = True
        operation_epoch += 1
        invalidate_diagnostics()
        view.start_clock()
        view.show_state("stopping", owned=controller.owned)
        for button in (retry_button, password_button, stop_button, open_button, folder_button):
            button.configure(state="disabled")
        refresh_operations()

        def finish():
            try:
                controller.stop()
                events.put(("stopped", close))
            except Exception as error:
                events.put(("error", str(error)))
        threading.Thread(target=finish, daemon=True).start()

    def select_folder():
        nonlocal controller
        if busy or closing or stop_requested or controller.owned or controller.status == "existing":
            return
        selected = filedialog.askdirectory(title="업무 자료 폴더 선택", initialdir=controller.state_dir, parent=root)
        if not selected:
            return
        try:
            save_state_choice(selected)
        except OSError as error:
            messagebox.showerror(APP_TITLE, f"자료 위치를 기억하지 못했습니다: {error}", parent=root)
            return
        controller = new_controller(selected)
        invalidate_diagnostics()
        view.set_path(controller.state_dir)
        begin()

    def drain():
        nonlocal busy, closing, stop_requested, existing_probe, existing_failures, next_existing_check
        nonlocal diagnostic_running, last_report, operation_epoch
        try:
            while True:
                item = events.get_nowait()
                if item[0] == "progress":
                    if not closing:
                        view.show_progress(item[1])
                elif item[0] == "diagnosed":
                    _, checked_controller, checked_epoch, checked_diag, result = item
                    if (checked_controller is controller and checked_epoch == operation_epoch
                            and checked_diag == diagnostic_epoch and not closing and not stop_requested
                            and (controller.child is None or controller.child.poll() is None)):
                        diagnostic_running = False
                        last_report = result
                        view.set_diagnostics(diagnostics_text(result) if result is not None else "점검을 완료하지 못했습니다. 다시 시도하세요.")
                        refresh_operations()
                elif item[0] == "password":
                    _, new, done, result = item
                    value = None
                    if not closing:
                        tray.restore()
                        view.show_state("password")
                        while True:
                            value = simpledialog.askstring(APP_TITLE, "새 DB 비밀번호 (12자 이상)" if new else "DB 비밀번호",
                                                           show="*", parent=root)
                            if value is None or not new:
                                break
                            if len(value) < 12:
                                messagebox.showerror(APP_TITLE, "12자 이상으로 입력하세요.", parent=root)
                                continue
                            repeated = simpledialog.askstring(APP_TITLE, "새 DB 비밀번호를 한 번 더 입력하세요.", show="*", parent=root)
                            if repeated is None:
                                value = None
                                break
                            if repeated == value:
                                break
                            messagebox.showerror(APP_TITLE, "두 비밀번호가 다릅니다.", parent=root)
                    result.append(value)
                    done.set()
                elif item[0] == "started":
                    busy = False
                    existing_failures = 0
                    next_existing_check = time.monotonic() + EXISTING_CHECK_SECONDS
                    if closing:
                        stop(True)
                        continue
                    view.show_state("existing" if item[1] == "existing" else "ready", owned=controller.owned)
                    open_button.configure(state="normal")
                    stop_button.configure(state="normal" if controller.owned else "disabled")
                    folder_button.configure(state="disabled" if controller.owned or item[1] == "existing" else "normal")
                    if not args.no_browser:
                        open_work()
                    refresh_operations()
                elif item[0] in {"existing_checked", "reconnected"}:
                    existing_probe = False
                    next_existing_check = time.monotonic() + EXISTING_CHECK_SECONDS
                    _, checked_controller, checked_epoch, healthy = item
                    if item[0] == "reconnected":
                        if checked_controller is controller and checked_epoch == operation_epoch:
                            busy = False
                            if closing:
                                stop(True)
                                continue
                            if not stop_requested and controller.status == "existing" and view.state == "reconnecting":
                                existing_failures = 0 if healthy else existing_failures
                                view.show_state("existing" if healthy else "connection_lost")
                                if healthy and not args.no_browser:
                                    open_work()
                        continue
                    if (checked_controller is controller and checked_epoch == operation_epoch
                            and controller.status == "existing" and view.state == "existing"
                            and not busy and not closing and not stop_requested):
                        existing_failures = 0 if healthy else existing_failures + 1
                        if existing_failures >= 2:
                            view.show_state("connection_lost")
                            invalidate_diagnostics()
                elif item[0] == "stopped":
                    busy = False
                    stop_requested = False
                    if closing or item[1]:
                        root.destroy()
                        return
                    view.show_state("stopped")
                    retry_button.configure(state="normal")
                    password_button.configure(state="normal")
                    folder_button.configure(state="normal")
                    refresh_operations()
                elif item[0] == "error":
                    busy = False
                    operation_epoch += 1
                    invalidate_diagnostics()
                    if closing and not controller.owned:
                        root.destroy()
                        return
                    waiting_for_stop = controller.owned and stop_requested and view.state in {"stopping", "stop_waiting"}
                    if not waiting_for_stop:
                        closing = False
                        stop_requested = False
                    view.show_state("stop_waiting" if waiting_for_stop else "error", detail=item[1], owned=controller.owned)
                    retry_button.configure(state="normal" if not controller.owned else "disabled")
                    password_button.configure(state="normal" if not controller.owned else "disabled")
                    stop_button.configure(state="normal" if controller.owned else "disabled")
                    folder_button.configure(state="normal" if not controller.owned else "disabled")
                    refresh_operations()
        except queue.Empty:
            pass
        if controller.status == "ready" and controller.child and controller.child.poll() is not None and not busy:
            completed_stop = stop_requested and controller.child.poll() == 0
            controller.stop()
            if completed_stop:
                events.put(("stopped", closing))
            else:
                closing = False
                stop_requested = False
                events.put(("error", "업무 서버가 종료되었습니다. 자료 폴더의 logs를 확인한 뒤 다시 시도하세요."))
        if (controller.status == "existing" and view.state == "existing" and not controller.owned
                and not busy and not closing and not stop_requested and not existing_probe
                and time.monotonic() >= next_existing_check):
            launch_existing_check("existing_checked")
        refresh_operations()
        root.after(150, drain)

    refresh_operations()
    root.protocol("WM_DELETE_WINDOW", lambda: stop(True))
    root.after(50, drain)
    if profile_error:
        view.show_state("error", detail=profile_error)
        refresh_operations()
    else:
        root.after(100, begin)
    try:
        root.mainloop()
    finally:
        tray.close()
        if mutex:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex))
    if pending_restart:
        try:
            subprocess.Popen(pending_restart, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            # 이전 인스턴스의 mutex를 해제한 뒤에만 새 실행 앱을 만든다.
            messagebox.showerror(APP_TITLE, "설정은 저장했지만 재실행하지 못했습니다. PC 실행 앱을 다시 실행하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
