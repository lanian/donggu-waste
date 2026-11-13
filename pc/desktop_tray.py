"""Tk 창의 트레이 숨김·복원. 서버 제어는 기존 종료 콜백에 위임한다."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import queue
import tkinter as tk


class DesktopTray:
    def __init__(self, root, title, *, on_exit, on_error):
        self.root, self.title = root, title
        self.on_exit, self.on_error = on_exit, on_error
        self._icon = None
        self._events = queue.Queue()
        self._hidden = self._closed = False
        self._timer = root.after(100, self._poll)
        root.bind("<Unmap>", self._unmap, add="+")
        root.bind("<Map>", self._map, add="+")
        root.bind("<Destroy>", self._destroy, add="+")

    def minimize(self):
        if self._closed or self._hidden:
            return
        modal = self.root.grab_current()
        if modal is not None:
            self.root.deiconify()
            modal.winfo_toplevel().lift()
            modal.focus_set()
            return
        try:
            if self._icon is None:
                self._icon = _WindowsTray(int(self.root.wm_frame(), 0), self.title, self._events.put)
            self._icon.show()
        except OSError:
            self.restore()
            self.on_error("트레이 아이콘을 표시하지 못해 실행창을 열어 두었습니다. 잠시 후 다시 시도하세요.")
            return
        self._hidden = True
        self.root.withdraw()

    def restore(self):
        if self._closed:
            return
        self._hidden = False
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        if self._icon:
            self._icon.hide()

    def _unmap(self, event):
        if event.widget is self.root and not self._hidden and not self._closed and self.root.state() == "iconic":
            self.minimize()

    def _map(self, event):
        # 이미 실행 중인 EXE를 다시 열어 Windows가 창을 복원한 경우에도 아이콘을 정리한다.
        if event.widget is self.root and self._hidden:
            self.restore()

    def _destroy(self, event):
        if event.widget is self.root:
            self.close()

    def _poll(self):
        self._timer = None
        if self._closed:
            return
        try:
            while True:
                action = self._events.get_nowait()
                self.restore()
                if action == "exit":
                    self.on_exit()
                elif action == "error":
                    self.on_error("트레이 아이콘을 다시 표시하지 못해 실행창을 복원했습니다.")
                if self._closed:
                    return
        except queue.Empty:
            pass
        self._timer = self.root.after(100, self._poll)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._timer is not None:
            self.root.after_cancel(self._timer)
            self._timer = None
        if self._icon:
            self._icon.close()


class _WindowsTray:
    """Tk와 같은 스레드의 숨김 Win32 창으로 트레이 메시지를 수신한다."""
    CALLBACK = 0x8001

    def __init__(self, window, title, on_action):
        if os.name != "nt":
            raise OSError("Windows 트레이가 필요합니다.")
        self.on_action = on_action
        self.window = self.icon = None
        self.atom = 0
        self.visible = False
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM)

        class WindowClass(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", callback_type),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        class Guid(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        class NotifyData(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                        ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                        ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                        ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                        ("dwInfoFlags", wintypes.DWORD), ("guidItem", Guid),
                        ("hBalloonIcon", wintypes.HICON)]

        signatures = [
            (self.kernel.GetModuleHandleW, [wintypes.LPCWSTR], wintypes.HMODULE),
            (self.user.RegisterClassW, [ctypes.POINTER(WindowClass)], wintypes.WORD),
            (self.user.UnregisterClassW, [wintypes.LPCWSTR, wintypes.HINSTANCE], wintypes.BOOL),
            (self.user.CreateWindowExW, [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HWND),
            (self.user.DefWindowProcW, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], ctypes.c_ssize_t),
            (self.user.SendMessageW, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], ctypes.c_ssize_t),
            (self.user.PostMessageW, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
            (self.user.DestroyWindow, [wintypes.HWND], wintypes.BOOL),
            (self.user.CopyIcon, [wintypes.HICON], wintypes.HICON),
            (self.user.DestroyIcon, [wintypes.HICON], wintypes.BOOL),
            (self.user.LoadIconW, [wintypes.HINSTANCE, wintypes.LPCWSTR], wintypes.HICON),
            (self.user.RegisterWindowMessageW, [wintypes.LPCWSTR], wintypes.UINT),
            (self.user.CreatePopupMenu, [], wintypes.HMENU),
            (self.user.AppendMenuW, [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR], wintypes.BOOL),
            (self.user.SetMenuDefaultItem, [wintypes.HMENU, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
            (self.user.GetCursorPos, [ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            (self.user.SetForegroundWindow, [wintypes.HWND], wintypes.BOOL),
            (self.user.TrackPopupMenu, [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, wintypes.HWND, ctypes.c_void_p], wintypes.UINT),
            (self.user.DestroyMenu, [wintypes.HMENU], wintypes.BOOL),
            (self.shell.Shell_NotifyIconW, [wintypes.DWORD, ctypes.POINTER(NotifyData)], wintypes.BOOL),
        ]
        for function, arguments, result in signatures:
            function.argtypes, function.restype = arguments, result
        self._taskbar_created = self.user.RegisterWindowMessageW("TaskbarCreated")
        self._instance = self.kernel.GetModuleHandleW(None)
        self._class_name = f"DongguWasteTray-{os.getpid()}-{id(self)}"
        self._callback = callback_type(self._window_proc)
        definition = WindowClass(lpfnWndProc=self._callback, hInstance=self._instance, lpszClassName=self._class_name)
        self.atom = self.user.RegisterClassW(ctypes.byref(definition))
        if not self.atom:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            # 일반 숨김 최상위 창이어야 Explorer의 TaskbarCreated 방송을 받는다.
            self.window = self.user.CreateWindowExW(0, self._class_name, "", 0, 0, 0, 0, 0,
                                                    None, None, self._instance, None)
            if not self.window:
                raise ctypes.WinError(ctypes.get_last_error())
            source = (self.user.SendMessageW(window, 0x7F, 2, 0)
                      or self.user.SendMessageW(window, 0x7F, 1, 0)
                      or self.user.LoadIconW(None, ctypes.cast(ctypes.c_void_p(32512), wintypes.LPCWSTR)))
            self.icon = self.user.CopyIcon(source)
            if not self.icon:
                raise ctypes.WinError(ctypes.get_last_error())
            self._data = NotifyData(cbSize=ctypes.sizeof(NotifyData), hWnd=self.window, uID=1,
                                    uFlags=0x87, uCallbackMessage=self.CALLBACK, hIcon=self.icon,
                                    szTip=title[:127], uVersion=4)
        except BaseException:
            self.close()
            raise

    def show(self):
        if self.visible:
            return
        if not self.shell.Shell_NotifyIconW(0, ctypes.byref(self._data)):
            raise OSError("Windows 트레이에 아이콘을 등록하지 못했습니다.")
        self.visible = True
        self.shell.Shell_NotifyIconW(4, ctypes.byref(self._data))

    def hide(self):
        if self.visible:
            self.shell.Shell_NotifyIconW(2, ctypes.byref(self._data))
            self.visible = False

    def _window_proc(self, window, message, wparam, lparam):
        try:
            if self._taskbar_created and message == self._taskbar_created and self.visible:
                self.visible = False
                self.show()
            elif message == self.CALLBACK:
                event = lparam & 0xffff
                if event in {0x202, 0x203, 0x400, 0x401}:  # 클릭·더블클릭·키보드 선택
                    self.on_action("restore")
                elif event in {0x205, 0x7B}:  # 오른쪽 클릭·키보드 메뉴
                    self._menu(window)
            else:
                return self.user.DefWindowProcW(window, message, wparam, lparam)
        except Exception:
            # ctypes 콜백 밖으로 예외를 내보내지 않고 Tk에서 창을 복원한다.
            self.on_action("error")
        return 0

    def _menu(self, window):
        menu = self.user.CreatePopupMenu()
        if not menu:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.user.AppendMenuW(menu, 0, 1, "실행창 열기")
            self.user.AppendMenuW(menu, 0x800, 0, None)
            self.user.AppendMenuW(menu, 0, 2, "실행 앱 종료")
            self.user.SetMenuDefaultItem(menu, 1, False)
            point = wintypes.POINT()
            self.user.GetCursorPos(ctypes.byref(point))
            self.user.SetForegroundWindow(window)
            selected = self.user.TrackPopupMenu(menu, 0x182, point.x, point.y, 0, window, None)
            self.user.PostMessageW(window, 0, 0, 0)
            if selected in {1, 2}:
                self.on_action("restore" if selected == 1 else "exit")
        finally:
            self.user.DestroyMenu(menu)

    def close(self):
        self.hide()
        if self.window:
            self.user.DestroyWindow(self.window)
            self.window = None
        if self.atom:
            self.user.UnregisterClassW(self._class_name, self._instance)
            self.atom = 0
        if self.icon:
            self.user.DestroyIcon(self.icon)
            self.icon = None
