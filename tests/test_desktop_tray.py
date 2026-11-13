"""실제 Tk 창의 트레이 표시 수명. 업무 서버·주민 자료는 사용하지 않는다."""
import importlib
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

if os.name == "nt":
    import tkinter as tk

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))


def desktop_shell_available():
    if os.name != "nt":
        return False
    user = ctypes.WinDLL("user32")
    user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.FindWindowW.restype = wintypes.HWND
    return bool(user.FindWindowW("Shell_TrayWnd", None))


class FakeIcon:
    instances = []

    def __init__(self, window, title, on_action):
        self.on_action = on_action
        self.visible = False
        self.closed = False
        self.failure = False
        self.instances.append(self)

    def show(self):
        if self.failure:
            raise OSError("가상 트레이 등록 실패")
        self.visible = True

    def hide(self):
        self.visible = False

    def close(self):
        self.hide()
        self.closed = True


@unittest.skipUnless(os.name == "nt", "Windows 트레이")
class TrayTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("desktop_tray"), "트레이 최소화 기능이 아직 없음")
        self.module = importlib.import_module("desktop_tray")
        self.root = tk.Tk()
        self.root.geometry("560x400")
        self.entry = tk.Entry(self.root)
        self.entry.insert(0, "가상 입력 내용")
        self.entry.pack()
        self.root.update()
        self.exits, self.errors = [], []
        FakeIcon.instances = []
        self.native = patch.object(self.module, "_WindowsTray", FakeIcon)
        self.native.start()
        self.tray = self.module.DesktopTray(self.root, "가상 트레이 검증",
                                            on_exit=lambda: self.exits.append(True), on_error=self.errors.append)

    def tearDown(self):
        if hasattr(self, "root"):
            self.root.destroy()
        if hasattr(self, "native"):
            self.native.stop()

    def pump(self):
        self.root.after(180, self.root.quit)
        self.root.mainloop()

    def test_minimize_restore_preserves_window_and_input(self):
        self.tray.minimize()
        self.root.update()
        self.assertEqual(self.root.state(), "withdrawn")
        icon = FakeIcon.instances[0]
        self.assertTrue(icon.visible)
        self.assertEqual(self.exits, [])
        icon.on_action("restore")
        self.pump()
        self.assertEqual(self.root.state(), "normal")
        self.assertEqual(self.entry.get(), "가상 입력 내용")
        self.assertFalse(icon.visible)
        self.assertEqual(self.exits, [])

    def test_titlebar_minimize_uses_tray(self):
        self.root.iconify()
        self.pump()
        self.assertEqual(self.root.state(), "withdrawn")
        self.assertTrue(FakeIcon.instances[0].visible)
        self.assertEqual(self.exits, [])

    def test_external_restore_removes_tray_icon(self):
        self.tray.minimize()
        self.root.update()
        self.root.deiconify()
        self.pump()
        self.assertEqual(self.root.state(), "normal")
        self.assertFalse(FakeIcon.instances[0].visible)

    def test_duplicate_exe_win32_restore_removes_tray_icon(self):
        self.tray.minimize()
        self.root.update()
        user = ctypes.WinDLL("user32")
        user.ShowWindow.argtypes, user.ShowWindow.restype = [wintypes.HWND, ctypes.c_int], wintypes.BOOL
        user.ShowWindow(int(self.root.wm_frame(), 0), 9)
        self.pump()
        self.assertEqual(self.root.state(), "normal")
        self.assertFalse(FakeIcon.instances[0].visible)

    def test_failed_registration_keeps_window_visible(self):
        self.tray.minimize()
        self.tray.restore()
        FakeIcon.instances[0].failure = True
        self.tray.minimize()
        self.root.update()
        self.assertEqual(self.root.state(), "normal")
        self.assertFalse(FakeIcon.instances[0].visible)
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(self.exits, [])

    def test_lost_tray_restores_window(self):
        self.tray.minimize()
        FakeIcon.instances[0].on_action("error")
        self.pump()
        self.assertEqual(self.root.state(), "normal")
        self.assertFalse(FakeIcon.instances[0].visible)
        self.assertEqual(len(self.errors), 1)

    def test_exit_restores_window_and_uses_existing_close_callback(self):
        self.tray.minimize()
        FakeIcon.instances[0].on_action("exit")
        self.pump()
        self.assertEqual(self.root.state(), "normal")
        self.assertEqual(self.exits, [True])
        self.assertFalse(FakeIcon.instances[0].visible)

    def test_destroy_cleans_up_icon_and_poll(self):
        self.tray.minimize()
        icon = FakeIcon.instances[0]
        self.tray.close()
        self.tray.close()
        self.assertTrue(icon.closed)
        self.assertFalse(icon.visible)
        self.assertFalse(self.errors)

    def test_modal_input_stays_available(self):
        modal = tk.Toplevel(self.root)
        modal_entry = tk.Entry(modal)
        modal_entry.pack()
        modal.update()
        modal.grab_set()
        try:
            self.root.iconify()
            self.pump()
            self.assertEqual(self.root.state(), "normal")
            self.assertTrue(modal.winfo_viewable())
            self.assertEqual(FakeIcon.instances, [])
        finally:
            modal.grab_release()
            modal.destroy()


@unittest.skipUnless(os.name == "nt", "Windows 트레이")
class PasswordTrayTests(unittest.TestCase):
    def test_password_request_restores_previously_hidden_launcher(self):
        self.run_password_fixture()

    def test_queued_tray_exit_cancels_modal_and_finishes_shutdown(self):
        self.run_password_fixture(exit_during_prompt=True)

    def run_password_fixture(self, exit_during_prompt=False):
        import desktop
        import desktop_tray
        import desktop_view
        from tkinter import simpledialog
        from desktop_runtime import DesktopError

        windows, observed, expired = [], [], []
        real_view = desktop_view.DesktopView
        FakeIcon.instances = []

        class Controller:
            def __init__(self, state, port):
                self.state_dir, self.port = Path(state), port
                self.url = f"http://127.0.0.1:{port}/"
                self.child, self.status = None, "idle"
                self.owned = False

            def start(self, provider, **kwargs):
                provider(False)
                raise DesktopError("가상 비밀번호 입력 취소")

            def stop(self):
                self.status = "stopped"

        def capture(root, version, **kwargs):
            view = real_view(root, version, **kwargs)
            windows.append(root)
            timers = [root.after(20, view.minimize_button.invoke),
                      root.after(1000, lambda: root.tk.call(root.protocol("WM_DELETE_WINDOW")))]
            destroy = root.destroy

            def clean_destroy():
                for timer in timers:
                    root.after_cancel(timer)
                destroy()

            root.destroy = clean_destroy
            return view

        def dialog(*args, **kwargs):
            observed.append((windows[0].state(), FakeIcon.instances[0].visible))
            if exit_during_prompt:
                class WaitingDialog(simpledialog.Dialog):
                    def body(self, parent):
                        tk.Label(parent, text="가상 입력 대기 · 실제 비밀번호 없음").pack()

                def deadline():
                    expired.append(True)
                    modal = windows[0].grab_current()
                    if isinstance(modal, simpledialog.Dialog):
                        modal.cancel()

                # 아이콘이 제거되기 직전 사용자가 선택한 종료 이벤트가 이미 대기 중인 경우.
                FakeIcon.instances[0].on_action("exit")
                timer = windows[0].after(600, deadline)
                try:
                    WaitingDialog(windows[0], "가상 입력 창")
                finally:
                    windows[0].after_cancel(timer)
            return None

        with tempfile.TemporaryDirectory(prefix="dg-tray-password-") as directory, socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            arguments = ["desktop.py", "--state", directory, "--port", str(probe.getsockname()[1]), "--no-browser"]
            with patch.object(sys, "argv", arguments), patch.object(desktop, "DesktopController", Controller), \
                    patch.object(desktop, "resolve_db_key", side_effect=lambda new, prompt, **kw: prompt(new)), \
                    patch.object(desktop_view, "DesktopView", capture), \
                    patch.object(desktop_tray, "_WindowsTray", FakeIcon), \
                    patch("tkinter.simpledialog.askstring", side_effect=dialog):
                self.assertEqual(desktop.main(), 0)
        self.assertEqual(observed, [("normal", False)], "비밀번호 입력 전에 실행창과 트레이 상태를 복원해야 함")
        self.assertFalse(expired, "트레이 종료는 입력 창의 수동 취소를 기다리지 않고 완료되어야 함")


@unittest.skipUnless(desktop_shell_available(), "실제 Windows Explorer 알림 영역이 필요함")
class NativeTrayTests(unittest.TestCase):
    def test_windows_registers_recreates_and_removes_real_icon(self):
        from desktop_tray import _WindowsTray

        class IconIdentifier(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                        ("uID", wintypes.UINT), ("guidItem", ctypes.c_byte * 16)]

        root = tk.Tk()
        root.update()
        actions = []
        icon = _WindowsTray(int(root.wm_frame(), 0), "가상 트레이 네이티브 검증", actions.append)
        shell = ctypes.WinDLL("shell32")
        shell.Shell_NotifyIconGetRect.argtypes = [ctypes.POINTER(IconIdentifier), ctypes.POINTER(wintypes.RECT)]
        shell.Shell_NotifyIconGetRect.restype = ctypes.c_long
        user = ctypes.WinDLL("user32")
        user.IsWindow.argtypes, user.IsWindow.restype = [wintypes.HWND], wintypes.BOOL
        identifier = IconIdentifier(cbSize=ctypes.sizeof(IconIdentifier), hWnd=icon.window, uID=1)
        rectangle = wintypes.RECT()
        try:
            icon.show()
            icon.show()
            # Windows의 숨겨진 아이콘 영역은 유효한 위치와 함께 S_FALSE(1)를 반환하기도 한다.
            self.assertGreaterEqual(shell.Shell_NotifyIconGetRect(ctypes.byref(identifier), ctypes.byref(rectangle)), 0)
            self.assertGreater(rectangle.right, rectangle.left)
            # 실제 Explorer를 종료하지 않고 아이콘 소실 뒤 재생성 메시지만 전달한다.
            icon.hide()
            icon.visible = True
            icon._window_proc(icon.window, icon._taskbar_created, 0, 0)
            self.assertGreaterEqual(shell.Shell_NotifyIconGetRect(ctypes.byref(identifier), ctypes.byref(rectangle)), 0)
            self.assertGreater(rectangle.right, rectangle.left)
            icon._window_proc(icon.window, icon.CALLBACK, 0, (1 << 16) | 0x400)
            self.assertEqual(actions, ["restore"])
            icon.close()
            icon.close()
            self.assertFalse(user.IsWindow(identifier.hWnd))
            self.assertLess(shell.Shell_NotifyIconGetRect(ctypes.byref(identifier), ctypes.byref(rectangle)), 0)
        finally:
            icon.close()
            root.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
