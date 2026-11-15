"""Source/packaged checker actually probes HTTP fixtures without business state or Python PATH."""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))
from dgpc import __version__


class Fixture(BaseHTTPRequestHandler):
    server_version, sys_version = "dgpc", ""

    def do_GET(self):
        bodies = {
            "/api/state": ("application/json", b'{"setup_needed":true,"logged_in":false}'),
            "/": ("text/html; charset=utf-8", '<title>동구 대형폐기물 PC 업무</title><script src="/start.js"></script>'.encode()),
            "/start.js": ("text/javascript", b'import "./pc.js";'),
            "/pc.js": ("text/javascript", b'import "./core.js";'),
            "/core.js": ("text/javascript", b'export const ready = true;'),
        }
        mime, body = bodies.get(self.path, ("text/plain", b"missing"))
        self.send_response(200 if self.path in bodies else 404)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-DG-Version", self.server.version)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path)
    args = parser.parse_args()
    exe = args.exe.resolve() if args.exe else None
    if exe:
        helper = exe.parent / "PC 접속 점검.cmd"
        assert helper.read_bytes() == b'@echo off\r\nstart "" "%~dp0Donggu-PC.exe" --check-connection\r\n'
        manifest = json.loads((exe.parent / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["files"][helper.name]["sha256"] == hashlib.sha256(helper.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="dg checker fixture ") as temporary:
        directory = Path(temporary)
        env = {**os.environ, "LOCALAPPDATA": str(directory / "appdata"), "DG_STATE": str(directory / "state"),
               "DG_OUT": str(directory / "out"), "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring"}
        for name in ("DG_DB_KEY", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
            env.pop(name, None)
        if exe:
            env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32") + ";" + os.environ["SystemRoot"]
        command = [str(exe)] if exe else [sys.executable, str(ROOT / "pc/desktop.py")]
        with ThreadingHTTPServer(("127.0.0.1", 0), Fixture) as server:
            server.version = __version__
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/"
                for version, expected, code in ((__version__, "ready", 0), ("0.0.0-fixture", "version_mismatch", 1)):
                    server.version = version
                    report = directory / (expected + ".json")
                    result = subprocess.run(command + ["--check-connection", url, "--check-output", str(report)],
                                            cwd=directory, env=env, capture_output=True, timeout=30)
                    assert result.returncode == code, (result.returncode, result.stderr.decode(errors="replace")[:300])
                    info = json.loads(report.read_text(encoding="utf-8"))
                    assert info["code"] == expected, info
                    assert info["scope"] == "this_device", info
                    for text in (str(directory), "DG_DB_KEY", "cookies", "password"):
                        assert text not in report.read_text(encoding="utf-8"), text
                    assert not (directory / "state").exists(), "checker created business state"
                    assert not (directory / "out").exists(), "checker created business output"
                    assert not (directory / "appdata").exists(), "checker read/wrote launcher configuration"
                    print(f"checker actual process: {expected}; explicit report only; state/config/key/server initialization absent", flush=True)
            finally:
                server.shutdown()
                thread.join(timeout=3)


if __name__ == "__main__":
    main()
