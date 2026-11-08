"""Windows 더블클릭 실행기 회귀시험: 실제 SQLCipher, 임시 자료·시험 서버만 사용."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import threading
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parent.parent
PASSED = 0


def ok(condition, message):
    global PASSED
    if not condition:
        raise AssertionError(message)
    PASSED += 1
    print("  ✓", message)


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def launch(repo, state, port, *, env=None, via_cmd=False):
    variables = os.environ.copy()
    variables.update(DG_STATE=str(state), DG_OUT=str(state / "out"),
                     DG_DB_KEY="launcher-test-key-not-for-production", PYTHONUTF8="1")
    if env:
        variables.update(env)
    # PS 5.1의 숨김 자식이 상속한 파이프는 서버 종료까지 닫히지 않는다.
    # 파일로 받으면 실행기의 종료와 백그라운드 서버 수명을 독립적으로 확인할 수 있다.
    token = uuid.uuid4().hex
    output, error = state.parent / f"{token}.out", state.parent / f"{token}.err"
    command = (["cmd.exe", "/d", "/c", str(repo / "Donggu-PC.cmd"), "-NoBrowser", "-Port", str(port)]
               if via_cmd else
               ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(repo / "tools" / "start_pc.ps1"), "-NoBrowser", "-Port", str(port)])
    with output.open("wb") as stdout, error.open("wb") as stderr:
        result = subprocess.run(
            command,
            cwd=state.parent, env=variables, stdout=stdout, stderr=stderr, timeout=90,
        )
    return subprocess.CompletedProcess(result.args, result.returncode,
                                       output.read_text(encoding="utf-8", errors="replace"),
                                       error.read_text(encoding="utf-8", errors="replace"))


def ready(result):
    lines = [line for line in result.stdout.splitlines() if line.startswith("DGPC_READY ")]
    if result.returncode != 0 or len(lines) != 1:
        raise AssertionError(f"실행기 준비 실패: {result.stdout}\n{result.stderr}")
    ok(True, "실행기가 준비 완료를 보고함")
    return json.loads(lines[0].partition(" ")[2])


def fetch(port):
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/state", headers={"X-DG-CSRF": "1"})
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.load(response)


def stop_created_server(pid):
    # 이 시험 실행기가 직접 반환한 서버 PID만 정리한다. 다른 포트의 서버는 건드리지 않는다.
    stopped = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                             capture_output=True, timeout=15)
    if stopped.returncode:
        raise AssertionError(f"시험 서버 종료 실패: PID {pid}")


class OtherApp(BaseHTTPRequestHandler):
    def do_GET(self):
        # /api/state 모양만 같은 다른 앱을 동구 서버로 오인하지 않아야 한다.
        body = b'{"setup_needed":true,"logged_in":false}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def main():
    if os.name != "nt":
        print("SKIP: Windows 실행기 시험")
        return
    with tempfile.TemporaryDirectory(prefix="donggu launcher test ") as directory:
        temporary = Path(directory)
        alias = temporary / "repository with spaces"
        subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(ROOT)],
                       check=True, capture_output=True)
        pid = None
        try:
            state, port = temporary / "state with spaces", free_port()
            started = ready(launch(alias, state, port))
            pid = started["started_process_id"]
            listener_pid = started["process_id"]
            ok((ROOT / "Donggu-PC.cmd").is_file(), "더블클릭 진입 파일이 있음")
            ok(started["status"] == "started" and isinstance(listener_pid, int) and listener_pid > 0,
               "공백 경로·저장소 밖 작업 폴더에서 실제 서버 시작")
            ok(fetch(port) == {"setup_needed": True, "logged_in": False},
               "브라우저를 열기 전에 실제 PC API와 첫 설정 상태 확인")
            ok((state / "pc.db").read_bytes()[:16] != b"SQLite format 3\x00",
               "최초 DB가 실제 암호화됨")
            ok(list((state / "logs").glob("*.out.log")) and list((state / "logs").glob("*.err.log")),
               "DG_STATE 아래에 서버 로그 보관")
            again = ready(launch(alias, state, port, via_cmd=True))
            ok(again["status"] == "existing" and again["process_id"] == listener_pid,
               "공백 경로 CMD의 인자 전달·재실행이 동일 PID 서버를 재사용")
            ok(fetch(port)["setup_needed"], "재실행이 관리자 계정을 만들지 않음")
            original = (state / "pc.db").read_bytes()
            # 잘못된 키는 기존 자료를 초기화하거나 덮어쓰면 안 된다.
            stop_created_server(pid)
            pid = None
            failed = launch(alias, state, port, env={"DG_DB_KEY": "wrong-launcher-test-key"})
            ok(failed.returncode != 0 and "DGPC_READY " not in failed.stdout,
               "DB 키 오류를 성공으로 보고하지 않음")
            ok((state / "pc.db").read_bytes() == original, "키 오류 후 기존 암호화 DB 보존")
            with ThreadingHTTPServer(("127.0.0.1", 0), OtherApp) as other:
                thread = threading.Thread(target=other.serve_forever, daemon=True)
                thread.start()
                try:
                    unused = temporary / "must not initialize"
                    refused = launch(alias, unused, other.server_port)
                    ok(refused.returncode != 0 and "DGPC_READY " not in refused.stdout,
                       "같은 모양의 API를 가진 다른 앱의 포트를 거부")
                    ok(not (unused / "pc.db").exists(), "포트 충돌 시 새 DB를 만들지 않음")
                    ok(fetch(other.server_port)["setup_needed"], "다른 앱의 서버를 종료하지 않음")
                finally:
                    other.shutdown()
                    thread.join(timeout=5)
            blocked = temporary / "missing dependency"
            blocked.mkdir()
            (blocked / "sqlcipher3.py").write_text("raise ImportError('test missing SQLCipher')\n", encoding="utf-8")
            missing_state = temporary / "dependency must not initialize"
            missing = launch(alias, missing_state, free_port(), env={
                "PYTHONPATH": str(blocked), "DG_ALLOW_PLAIN_DB": "1",
            })
            ok(missing.returncode != 0 and "-Install" in missing.stdout and "pc/requirements.txt" in missing.stdout,
               "필수 의존성 누락 시 고정 목록·첫 설치 명령 안내")
            ok(not (missing_state / "pc.db").exists(), "SQLCipher 없으면 평문 허용 환경에서도 DB를 만들지 않음")
            concurrent_state, concurrent_port = temporary / "simultaneous first use", free_port()
            with ThreadPoolExecutor(max_workers=2) as pool:
                attempts = [pool.submit(launch, alias, concurrent_state, concurrent_port) for _ in range(2)]
                launches = []
                for attempt in attempts:
                    result = ready(attempt.result(timeout=90))
                    if result["status"] == "started":
                        pid = result["started_process_id"]
                    launches.append(result)
            ok({result["status"] for result in launches} == {"started", "existing"}
               and len({result["process_id"] for result in launches}) == 1,
               "첫 실행 두 개가 동시에 시작되어도 한 번 초기화하고 서버 하나만 실행")
        finally:
            if pid is not None:
                stop_created_server(pid)
            # junction은 링크 자체만 제거한다. 원본 저장소는 삭제하지 않는다.
            os.rmdir(alias)
    print(f"OK: PC 실행기 {PASSED}개 확인")


if __name__ == "__main__":
    main()
