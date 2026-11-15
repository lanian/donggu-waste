"""운영 모드 진입·재실행·브라우저 실패의 실제 실행 앱 연결 회귀."""
from __future__ import annotations

from pathlib import Path
import os
import gc
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))
import desktop


class LauncherOperationsTests(unittest.TestCase):
    def test_invalid_saved_profile_recovers_without_automatic_server_start(self):
        self.assertTrue(hasattr(desktop, "resolve_initial_profile"), "깨진 설정의 복구 흐름이 필요합니다")
        from types import SimpleNamespace
        with patch("desktop_connection.resolve_profile", side_effect=ValueError("private config path")):
            profile, error = desktop.resolve_initial_profile(SimpleNamespace(port=8766))
        self.assertEqual(profile.mode, "local")
        self.assertEqual(profile.port, 8766)
        self.assertIn("접속 설정", error)
        self.assertNotIn("private config path", error)

    def test_checker_routes_before_profile_state_key_controller(self):
        import types
        checked = []
        checker = types.SimpleNamespace(connection_check_main=lambda url: checked.append(url) or 0)
        with patch.dict(sys.modules, {"desktop_check": checker}), \
                patch.object(sys, "argv", ["desktop.py", "--check-connection", "https://waste.example:9443/"]), \
                patch.object(desktop, "load_state_choice", side_effect=AssertionError("checker opened state")), \
                patch.object(desktop, "DesktopController", side_effect=AssertionError("checker launched server")), \
                patch.object(desktop, "resolve_db_key", side_effect=AssertionError("checker opened keyring")):
            self.assertEqual(desktop.main(), 0)
        self.assertEqual(checked, ["https://waste.example:9443/"])

    def test_headless_checker_report_routes_before_state(self):
        import types
        calls = []
        checker = types.SimpleNamespace(connection_check_report=lambda url, path: calls.append((url, path)) or {"code": "ready"})
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            with patch.dict(sys.modules, {"desktop_check": checker}), \
                    patch.object(sys, "argv", ["desktop.py", "--check-connection", "https://waste.example:9443/", "--check-output", str(report)]), \
                    patch.object(desktop, "load_state_choice", side_effect=AssertionError("checker opened state")), \
                    patch.object(desktop, "DesktopController", side_effect=AssertionError("checker launched server")):
                self.assertEqual(desktop.main(), 0)
            self.assertEqual(calls, [("https://waste.example:9443/", report)])

    def test_explicit_local_port_is_preserved_in_restart_command(self):
        self.assertTrue(hasattr(desktop, "launcher_command"), "재실행 명령 생성기가 필요합니다")
        from desktop_connection import ConnectionProfile
        with tempfile.TemporaryDirectory() as temporary:
            profile = ConnectionProfile(port=8766)
            command = desktop.launcher_command(Path(temporary), profile)
            self.assertIn("--mode", command)
            self.assertEqual(command[command.index("--mode") + 1], "local")
            self.assertEqual(command[command.index("--port") + 1], "8766")
            self.assertEqual(command[command.index("--state") + 1], temporary)
            self.assertNotIn("--desktop-child", command)

    def test_browser_false_and_error_leave_copyable_address(self):
        self.assertTrue(hasattr(desktop, "open_browser"), "브라우저 실패 처리가 필요합니다")
        for failure in (False, OSError("private browser path")):
            errors = []
            options = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
            with patch.object(desktop.webbrowser, "open", **options):
                result = desktop.open_browser("http://127.0.0.1:8766/", errors.append)
            self.assertFalse(result)
            self.assertEqual(len(errors), 1)
            self.assertIn("http://127.0.0.1:8766/", errors[0])
            self.assertNotIn("private browser path", errors[0])

    def test_new_profiles_do_not_change_current_port_mutex_in_place(self):
        self.assertTrue(hasattr(desktop, "launcher_command"), "재실행 명령 생성기가 필요합니다")
        from desktop_connection import ConnectionProfile
        profile = ConnectionProfile(port=8766)
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", "C:/Program Files/Donggu-PC.exe"):
            command = desktop.launcher_command(Path("C:/test state"), profile)
        self.assertEqual(command[0], "C:/Program Files/Donggu-PC.exe")
        self.assertEqual(command[command.index("--port") + 1], "8766")
        self.assertEqual(profile.port, 8766)

    @unittest.skipUnless(os.name == "nt", "Windows Tk integration")
    def test_main_discards_late_diagnostics_after_stop(self):
        self._main_flow("late_diagnostics")

    @unittest.skipUnless(os.name == "nt", "Windows Tk integration")
    def test_main_saves_profile_and_restarts_after_window_cleanup(self):
        self._main_flow("restart")

    @unittest.skipUnless(os.name == "nt", "Windows Tk integration")
    def test_main_discards_diagnostics_after_unexpected_child_exit(self):
        self._main_flow("unexpected_exit")

    @unittest.skipUnless(os.name == "nt", "Windows Tk integration")
    def test_report_save_modal_rechecks_invalidated_snapshot(self):
        self._main_flow("save_dialog")

    def _main_flow(self, case):
        import desktop_diagnostics
        import desktop_view
        import tkinter as tk
        from desktop_connection import ConnectionProfile
        released, checking = threading.Event(), threading.Event()
        captured, spawned, failures, saved, reports = {}, [], [], [], []
        original_view = desktop_view.DesktopView

        class Controller:
            def __init__(self, state, port):
                self.state_dir, self.port = Path(state), port
                self.url = f"http://127.0.0.1:{port}/"
                self.status, self.child = "idle", None
                captured["controller"] = self
            @property
            def owned(self):
                return self.child is not None
            def start(self, *_args, **_kwargs):
                self.status = "ready"
                if case == "unexpected_exit":
                    self.child = type("Child", (), {"code": None, "poll": lambda child: child.code})()
                return "ready"
            def stop(self):
                self.status = "stopped"
                self.child = None

        def diagnostic(snapshot):
            checking.set()
            if case != "save_dialog":
                released.wait(3)
            return {"late": "must not be rendered"}

        def capture(root, version, callbacks):
            view = original_view(root, version, callbacks=callbacks)
            captured.update(root=root, view=view)
            original_destroy = root.destroy
            def clean_destroy():
                for timer in root.tk.call("after", "info"):
                    root.tk.call("after", "cancel", timer)
                original_destroy()
            root.destroy = clean_destroy
            root.report_callback_exception = lambda kind, value, trace: failures.append(value) or root.destroy()
            phase = [0]
            def advance():
                try:
                    if phase[0] == 0 and view.state == "ready":
                        callbacks["connection_settings"]()  # running view must reject settings
                        self.assertEqual(saved, [])
                        phase[0] = 1
                        if case in {"late_diagnostics", "unexpected_exit", "save_dialog"}:
                            callbacks["diagnose"]()
                        else:
                            callbacks["stop"]()
                    elif phase[0] == 1:
                        if case == "late_diagnostics" and checking.is_set():
                            phase[0] = 2
                            callbacks["stop"]()
                            released.set()
                        elif case == "unexpected_exit" and checking.is_set():
                            phase[0] = 2
                            captured["controller"].child.code = 1
                        elif case == "save_dialog" and str(view.save_report_button["state"]) == "normal":
                            phase[0] = 2
                            callbacks["save_report"]()
                        elif case == "restart" and view.state == "stopped":
                            phase[0] = 2
                            callbacks["connection_settings"]()
                    elif phase[0] == 2 and case == "unexpected_exit" and view.state == "error":
                        phase[0] = 3
                        released.set()
                        root.after(350, lambda: root.tk.call(root.protocol("WM_DELETE_WINDOW")))
                    elif phase[0] == 2 and case in {"late_diagnostics", "save_dialog"} and view.state == "stopped":
                        phase[0] = 3
                        root.after(350, lambda: root.tk.call(root.protocol("WM_DELETE_WINDOW")))
                except Exception as error:
                    failures.append(error)
                    root.destroy()
                    return
                root.after(30, advance)
            root.after(200, advance)
            root.after(5000, lambda: failures.append(AssertionError("main operations timeout")) or root.destroy())
            return view

        def spawn(command, **kwargs):
            # 再실행 전에 실제 기존 Tk 창이 파괴되어 있어야 한다.
            self.assertRaises(tk.TclError, captured["root"].winfo_exists)
            spawned.append(command)

        def save_dialog(**kwargs):
            captured["view"].callbacks["stop"]()
            return str(Path(temporary) / "report.json")

        enabled = gc.isenabled()
        gc.collect()
        gc.disable()
        try:
            with tempfile.TemporaryDirectory() as temporary, \
                    patch.dict(os.environ, {"LOCALAPPDATA": temporary, "DG_STATE": str(Path(temporary) / "state")}), \
                    patch.object(sys, "argv", ["desktop.py", "--mode", "local", "--port", "49161", "--no-browser"]), \
                    patch.object(desktop, "DesktopController", Controller), \
                    patch.object(desktop_view, "DesktopView", side_effect=capture), \
                    patch.object(desktop_view, "edit_connection_profile", return_value=ConnectionProfile(port=49162).to_dict()) as edit, \
                    patch("desktop_connection.save_profile", side_effect=lambda profile: saved.append(profile)), \
                    patch.object(desktop_diagnostics, "collect_diagnostics", side_effect=diagnostic), \
                    patch.object(desktop_diagnostics, "diagnostics_text", **({"return_value": "completed"} if case == "save_dialog" else {"side_effect": AssertionError("stale result rendered")})), \
                    patch.object(desktop_diagnostics, "save_report", side_effect=lambda *args: reports.append(args)), \
                    patch("tkinter.filedialog.asksaveasfilename", side_effect=save_dialog), \
                    patch.object(desktop.subprocess, "Popen", side_effect=spawn):
                self.assertEqual(desktop.main(), 0)
            self.assertEqual(failures, [])
            if case == "restart":
                self.assertEqual(edit.call_count, 1)
                self.assertEqual(saved[0].port, 49162)
                self.assertEqual(spawned[0][spawned[0].index("--port") + 1], "49162")
            else:
                self.assertEqual(edit.call_count, 0)
                self.assertEqual(spawned, [])
                self.assertEqual(reports, [])
        finally:
            released.set()
            captured.clear()
            gc.collect()
            if enabled:
                gc.enable()


if __name__ == "__main__":
    unittest.main()
