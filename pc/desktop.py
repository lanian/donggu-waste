"""동구 PC 실행 앱: 준비 상태·자료 위치·업무 화면·정상 종료를 하나의 창에서 관리."""
from __future__ import annotations

import argparse
import ctypes
import os
import queue
import sys
import threading
import time
import webbrowser

from desktop_runtime import (APP_TITLE, DesktopController, DesktopError, child_main, create_shortcut,
                             load_state_choice, resolve_db_key, save_state_choice)

EXISTING_CHECK_SECONDS = 3.0


def main():
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--desktop-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--state")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    state = load_state_choice(args.state)
    if args.desktop_child:
        return child_main(state, args.port)

    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog
    from desktop_view import DesktopView
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
    controller = DesktopController(state, args.port)
    busy = False
    closing = False
    stop_requested = False
    operation_epoch = 0
    existing_probe = False
    existing_failures = 0
    next_existing_check = 0.0
    view = DesktopView(root, __version__, callbacks={
        "open": lambda: open_work(), "retry": lambda: begin(), "reconnect": lambda: reconnect(), "password": lambda: begin(True),
        "stop": lambda: stop(), "folder": lambda: select_folder(), "shortcut": lambda: shortcut(),
    })
    view.pack(fill="both", expand=True)
    view.set_path(state)
    open_button, retry_button, password_button = view.primary_button, view.retry_button, view.password_button
    stop_button, folder_button = view.stop_button, view.folder_button

    def open_work():
        if not busy and not closing and not stop_requested and view.state in {"ready", "existing"}:
            webbrowser.open(controller.url)

    def shortcut():
        try:
            created = create_shortcut(controller.state_dir, port=controller.port)
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
        view.start_clock()
        view.show_state("checking")
        for button in (retry_button, password_button, stop_button, open_button, folder_button):
            button.configure(state="disabled")

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
        nonlocal busy, closing, stop_requested
        if close:
            closing = True
        if busy:
            view.show_state("stopping" if stop_requested else "closing_start", owned=controller.owned)
            return
        busy = True
        stop_requested = True
        view.start_clock()
        view.show_state("stopping", owned=controller.owned)
        for button in (retry_button, password_button, stop_button, open_button, folder_button):
            button.configure(state="disabled")

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
        controller = DesktopController(selected, args.port)
        view.set_path(controller.state_dir)
        begin()

    def drain():
        nonlocal busy, closing, stop_requested, existing_probe, existing_failures, next_existing_check
        try:
            while True:
                item = events.get_nowait()
                if item[0] == "progress":
                    if not closing:
                        view.show_progress(item[1])
                elif item[0] == "password":
                    _, new, done, result = item
                    value = None
                    if not closing:
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
                        webbrowser.open(controller.url)
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
                                    webbrowser.open(controller.url)
                        continue
                    if (checked_controller is controller and checked_epoch == operation_epoch
                            and controller.status == "existing" and view.state == "existing"
                            and not busy and not closing and not stop_requested):
                        existing_failures = 0 if healthy else existing_failures + 1
                        if existing_failures >= 2:
                            view.show_state("connection_lost")
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
                elif item[0] == "error":
                    busy = False
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
        root.after(150, drain)

    root.protocol("WM_DELETE_WINDOW", lambda: stop(True))
    root.after(50, drain)
    root.after(100, begin)
    try:
        root.mainloop()
    finally:
        if mutex:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
