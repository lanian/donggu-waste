"""실제 source/EXE TLS server + 별도 checker process. 임시 CA·자료·포트만 사용한다."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))
from desktop_connection import ConnectionProfile, probe_url, profile_arguments
from test_desktop_connection import certificates


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path)
    args = parser.parse_args()
    command = [str(args.exe.resolve())] if args.exe else [sys.executable, str(ROOT / "pc/desktop.py")]
    with tempfile.TemporaryDirectory(prefix="dg packaged lan ") as temporary:
        directory = Path(temporary)
        certs = directory / "certificates"
        certs.mkdir()
        cert, key = certificates(certs)["valid"]
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        profile = ConnectionProfile(mode="lan", port=port, host="127.0.0.1", hostname="127.0.0.1",
                                    cert=cert, key=key, allow="127.0.0.1/32")
        state = directory / "server-state"
        env = {**os.environ, "LOCALAPPDATA": str(directory / "server-config"), "DG_STATE": str(state),
               "DG_OUT": str(directory / "server-out"), "DG_NO_SCHEDULER": "1", "SSL_CERT_FILE": str(certs / "ca.pem"),
               "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring", "PYTHONUTF8": "1"}
        for name in ("DG_DB_KEY", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
            env.pop(name, None)
        if args.exe:
            env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32") + ";" + os.environ["SystemRoot"]
        context = ssl.create_default_context(cafile=str(certs / "ca.pem"))
        with (directory / "fixture-process.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command + ["--desktop-child", "--state", str(state)] + profile_arguments(profile),
                                       env=env, cwd=directory, stdin=subprocess.PIPE, stdout=log, stderr=log)
            try:
                process.stdin.write((json.dumps({"key": "fictitious-packaged-lan-test-key-123", "launch_id": "b" * 32}) + "\n").encode())
                process.stdin.flush()
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    assert process.poll() is None, "TLS child exited; inspect fixture process log"
                    result = probe_url(profile.url, ssl_context=context)
                    if result["code"] == "ready":
                        break
                    time.sleep(.1)
                else:
                    raise AssertionError("TLS fixture not ready: " + result["code"])
                assert (state / "pc.db").read_bytes()[:16] != b"SQLite format 3\0"
                assert (state / "logs" / ("desktop-ready-" + "b" * 32 + ".json")).is_file()
                report = directory / "peer-checker-report.json"
                checker_env = {**env, "DG_STATE": str(directory / "checker-state"),
                               "DG_OUT": str(directory / "checker-out"), "LOCALAPPDATA": str(directory / "checker-config")}
                checked = subprocess.run(command + ["--check-connection", profile.url, "--check-output", str(report)],
                                         env=checker_env, cwd=directory, capture_output=True, timeout=30)
                assert checked.returncode == 0, (checked.returncode, checked.stderr.decode(errors="replace")[:200])
                info = json.loads(report.read_text(encoding="utf-8"))
                assert info["code"] == "ready" and info["scope"] == "this_device", info
                for name in ("checker-state", "checker-out", "checker-config"):
                    assert not (directory / name).exists(), name
                print("Actual TLS server + independent checker process: trusted CA/name, encrypted fixture DB, no checker state/config/key initialization", flush=True)
            finally:
                if process.poll() is None:
                    process.stdin.write(b'{"command":"stop"}\n')
                    process.stdin.flush()
                process.stdin.close()
                assert process.wait(timeout=25) == 0, "TLS child failed normal shutdown"
        print("Actual TLS fixture normal shutdown passed; other physical PC connectivity remains unconfirmed", flush=True)


if __name__ == "__main__":
    main()
