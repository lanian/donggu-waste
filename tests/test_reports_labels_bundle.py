"""Python 없는 PATH의 실제 Windows 배포 EXE에서 보고서·번호표 업무를 검증한다."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.request

import test_reports_labels_e2e as flow


@contextmanager
def frozen_server(exe):
    with tempfile.TemporaryDirectory(prefix="dg reports bundle ") as directory:
        state = Path(directory) / "state"
        env = {**os.environ, "DG_STATE": str(state), "DG_OUT": str(Path(directory) / "out"),
               "DG_NO_SCHEDULER": "1", "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"}
        for name in ("DG_DB_KEY", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
            env.pop(name, None)
        env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32") + ";" + os.environ["SystemRoot"]
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        url = f"http://127.0.0.1:{port}/"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with (Path(directory) / "process.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen([str(exe), "--desktop-child", "--state", str(state), "--port", str(port)],
                env=env, cwd=directory, stdin=subprocess.PIPE, stdout=log, stderr=log, text=True, encoding="utf-8")
            try:
                process.stdin.write(json.dumps({"key": "reports-bundle-test-only-123"}) + "\n")
                process.stdin.flush()
                until = time.monotonic() + 30
                while time.monotonic() < until:
                    assert process.poll() is None, (Path(directory) / "process.log").read_text(encoding="utf-8")
                    try:
                        request = urllib.request.Request(url + "api/state", headers={"X-DG-CSRF": "1"})
                        with opener.open(request, timeout=1) as response:
                            assert json.load(response)["setup_needed"] is True
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise AssertionError("배포 서버 준비 시간 초과")
                for name in ("pc.js", "pc.css", "doc.css", "screens/home.js", "screens/request.js", "screens/reports.js", "screens/numbered_labels.js"):
                    with opener.open(url + name, timeout=10) as response:
                        assert response.read() == (flow.ROOT / "pc/ui" / name).read_bytes(), "과거 배포 자산: " + name
                yield url
                assert (state / "pc.db").read_bytes()[:16] != b"SQLite format 3\0"
            finally:
                if process.poll() is None:
                    process.stdin.write(json.dumps({"command": "stop"}) + "\n")
                    process.stdin.flush()
                process.stdin.close()
                try:
                    code = process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                    raise
                assert code == 0, f"배포 서버 종료 코드 {code}"
                assert (state / "pc.db").is_file()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    exe = parser.parse_args().exe.resolve()
    assert exe.is_file(), exe
    flow.pc_server = lambda: frozen_server(exe)
    flow.main()
    print("실제 배포 EXE 보고서·배출표: 새 자산·Python 없는 PATH·암호화 DB·정상 종료 통과")
