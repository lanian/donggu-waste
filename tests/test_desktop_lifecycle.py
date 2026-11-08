"""실행 앱이 서버 준비를 받아 정상 종료할 수 있는 실제 SQLCipher 회귀."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def main():
    with tempfile.TemporaryDirectory(prefix="dg-desktop-lifecycle-") as directory:
        env = {**os.environ, "DG_STATE": str(Path(directory) / "state"), "DG_OUT": str(Path(directory) / "out"),
               "DG_DB_KEY": "desktop-lifecycle-not-production", "DG_NO_SCHEDULER": "1", "PYTHONUTF8": "1"}
        subprocess.run([sys.executable, str(ROOT / "pc/pc_tool.py"), "init"], env=env, check=True, capture_output=True)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        code = r"""
import json, socket, sys, threading, time, urllib.request
sys.path.insert(0, 'pc')
from dgpc import server
server.DesktopServer.connection_timeout = .3
results = []
stop_started, held, timers = [], [], []
work_gate, work_done = threading.Event(), threading.Event()
def ready(srv):
    srv.daemon_threads = False
    def work(job):
        assert work_gate.wait(5)
        time.sleep(.2)  # 종료 요청 뒤에도 진행되는 실제 백그라운드 작업을 모사한다.
        work_done.set()
        return {}
    server.OPS.JOBS.submit('desktop-test', work)
    def client():
        try:
            idle = socket.create_connection(('127.0.0.1', int(sys.argv[1])))
            idle.sendall(b'GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n')
            held.append(idle)
            request = urllib.request.Request('http://127.0.0.1:' + sys.argv[1] + '/api/state', headers={'X-DG-CSRF': '1'})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=5) as response:
                results.append(json.load(response))
        finally:
            stop_started.append(time.monotonic())
            timer = threading.Timer(2, lambda: [connection.close() for connection in held])
            timers.append(timer)
            timer.start()
            srv.shutdown()
            work_gate.set()
    threading.Thread(target=client).start()
server.serve(port=int(sys.argv[1]), on_ready=ready)
assert results == [{'setup_needed': True, 'logged_in': False}], results
assert work_done.is_set(), '진행 중인 작업을 끝내기 전에 서버가 반환함'
assert time.monotonic() - stop_started[0] < 1.5, '미완성 HTTP 헤더 연결 때문에 종료가 지연됨'
for connection in held: connection.close()
for timer in timers: timer.cancel(); timer.join()
print('DESKTOP_GRACEFUL_STOP_OK')
"""
        result = subprocess.run([sys.executable, "-c", code, str(port)], cwd=ROOT, env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "DESKTOP_GRACEFUL_STOP_OK" in result.stdout
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0, "정상 종료 뒤에도 포트가 열려 있음"
        assert (Path(directory) / "state/pc.db").is_file()
        print("실행 앱 준비·API·작업 완료 대기·미완성 연결 제한·정상 종료·DB 보존 통과")


if __name__ == "__main__":
    main()
