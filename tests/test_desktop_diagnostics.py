"""운영 점검은 선택 폴더를 수정하거나 업무 DB/로그 원문을 열지 않는다."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("desktop_diagnostics"), "운영 진단 모듈이 필요합니다")
        import desktop_diagnostics
        self.ops = desktop_diagnostics

    def test_read_only_snapshot_distinguishes_device_and_disk_capacity(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir()
            (state / "pc.db").write_bytes(b"private database sentinel")
            (state / "logs").mkdir()
            (state / "logs" / "private.log").write_text("resident and key sentinel")
            before = {str(p.relative_to(state)): p.read_bytes() for p in state.rglob("*") if p.is_file()}
            snapshot = self.ops.DiagnosticSnapshot("local", 8766, "http://127.0.0.1:8766/", state, "existing")
            with patch.object(self.ops.shutil, "disk_usage", return_value=(10000000000, 2000000000, 8000000000)):
                result = self.ops.collect_diagnostics(snapshot, probe=lambda url: {"code": "ready", "message": "not trusted"})
            self.assertEqual(result["storage"]["disk_free_bytes"], 8000000000)
            self.assertEqual(result["probe"]["code"], "ready")
            self.assertFalse(result["connection"]["peer_confirmed"])
            self.assertEqual(result["connection"]["scope"], "this_device")
            self.assertFalse(result["server"]["configuration_verified"])
            self.assertEqual(before, {str(p.relative_to(state)): p.read_bytes() for p in state.rglob("*") if p.is_file()})
            report = self.ops.report_text(result)
            for private in (str(state), "sentinel", "not trusted"):
                self.assertNotIn(private, report)
            self.assertIn("다른 PC", self.ops.diagnostics_text(result))

    def test_missing_folder_is_not_created_and_worker_failure_is_sanitized(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "not-created"
            snapshot = self.ops.DiagnosticSnapshot("lan", 9443, "https://waste.example:9443/", state, "stopped")
            def failing_probe(url):
                raise OSError("private password username path")
            result = self.ops.collect_diagnostics(snapshot, probe=failing_probe)
            self.assertFalse(state.exists())
            self.assertEqual(result["probe"]["code"], "check_failed")
            self.assertEqual(result["storage"]["folder_access"], "not_created")
            self.assertNotIn("private", self.ops.report_text(result))

    def test_report_allows_only_fixed_metadata_and_safe_urls(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = self.ops.DiagnosticSnapshot("lan", 9443, "https://waste.example:9443/", Path(temporary), "ready")
            result = self.ops.collect_diagnostics(snapshot, probe=lambda url: {
                "code": "tls_error", "message": "secret certificate path", "raw": "resident"})
            result["password"] = "resident"
            result["storage"]["raw_path"] = "C:/Users/private"
            result["probe"]["message"] = "private"
            result["connection"]["url"] = "https://admin:password@waste.example:9443/?token=private"
            report = self.ops.report_text(result)
            parsed = json.loads(report)
            self.assertEqual(parsed["connection"]["url"], "")
            for private in ("resident", "private", "password", "certificate path"):
                self.assertNotIn(private, report)
            self.assertEqual(parsed["probe"], {"code": "tls_error"})

    def test_report_save_is_atomic_utf8_and_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "점검.json"
            snapshot = self.ops.DiagnosticSnapshot("local", 8766, "http://127.0.0.1:8766/", Path(temporary), "stopped")
            result = self.ops.collect_diagnostics(snapshot, probe=lambda url: {"code": "unreachable"})
            self.ops.save_report(destination, result)
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["probe"]["code"], "unreachable")
            self.assertEqual(list(Path(temporary).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
