"""실행 앱의 실제 숨김 자식·SQLCipher·소유권·정상 종료 회귀. 사용자 DB는 열지 않는다."""
from __future__ import annotations

import os
import base64
import json
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))
from desktop_runtime import DesktopController, DesktopError, app_ready, create_shortcut, default_state_dir, resolve_db_key  # noqa: E402

PASSED = 0
KEY = "desktop-test-key-not-for-production"


def ok(condition, message):
    global PASSED
    if not condition:
        raise AssertionError(message)
    PASSED += 1
    print("  ✓", message, flush=True)


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def get(url, path="api/state"):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url + path, headers={"X-DG-CSRF": "1"})
    with opener.open(request, timeout=2) as response:
        return response.read()


class OtherApp(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"setup_needed":true,"logged_in":false}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class RacingDonggu(OtherApp):
    server_version = "dgpc"
    sys_version = ""

    def do_GET(self):
        body = (b'{"setup_needed":true,"logged_in":false}' if self.path == "/api/state" else
                "<title>동구 대형폐기물 PC 업무</title>".encode("utf-8"))
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AssetFixture(OtherApp):
    server_version = "dgpc"
    sys_version = ""

    def do_GET(self):
        self.server.requests.append(self.path)
        status, content_type, body, headers = self.server.assets.get(self.path, (404, "text/plain", b"missing", {}))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


def startup_asset_guard(root):
    """실제 로컬 HTTP만 사용해 구버전 서버의 새 진입 파일 누락을 재현한다."""
    title = "<title>동구 대형폐기물 PC 업무</title>"
    defaults = {
        "/api/state": (200, "application/json", b'{"setup_needed":true,"logged_in":false}', {}),
        "/": (200, "text/html; charset=utf-8", (title + '<script src="/start.js" defer></script>').encode(), {}),
        "/start.js": (200, "text/javascript", b"import('./pc.js');", {}),
        "/pc.js": (200, "text/javascript", b"import './core.js';", {}),
        "/core.js": (200, "text/javascript", b"export const ready = true;", {}),
    }
    with ThreadingHTTPServer(("127.0.0.1", 0), AssetFixture) as http, \
            ThreadingHTTPServer(("127.0.0.1", 0), AssetFixture) as external:
        http.assets, http.requests = dict(defaults), []
        external.assets, external.requests = {}, []
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (http, external)]
        for thread in threads:
            thread.start()
        url = f"http://127.0.0.1:{http.server_port}/"
        try:
            ok(app_ready(url), "동일 출처 진입 파일·pc.js·core.js가 실제 제공될 때만 준비 완료")
            for content, label in (
                ((404, "text/plain", b"missing", {}), "HTML은 새 진입을 요구하지만 start.js 404인 기존 서버 거부"),
                ((200, "text/html", b"<html>fallback</html>", {}), "진입 파일 대신 HTML을 반환하는 서버 거부"),
                ((200, "text/javascript", b"", {}), "빈 JavaScript 진입 파일 거부"),
            ):
                http.assets = {**defaults, "/start.js": content}
                ok(not app_ready(url), label)
            for path in ("/pc.js", "/core.js"):
                http.assets = {**defaults, path: (404, "text/plain", b"missing", {})}
                ok(not app_ready(url), f"진입 파일이 있어도 필수 {path} 누락 시 거부")
            external_url = f"http://127.0.0.1:{external.server_port}/outside.js"
            http.assets = {**defaults, "/": (200, "text/html", (title + f'<script src="{external_url}"></script>').encode(), {})}
            ok(not app_ready(url) and not external.requests, "외부 출처 script src는 요청하지 않고 거부")
            http.assets = {**defaults, "/start.js": (302, "text/javascript", b"redirect", {"Location": external_url})}
            ok(not app_ready(url) and not external.requests, "JavaScript redirect를 외부 출처로 따라가지 않음")
            http.assets = {**defaults, "/start.js": (404, "text/plain", b"missing", {})}
            selected = root / "broken assets must not initialize"
            controller = DesktopController(selected, http.server_port)
            message = ""
            try:
                controller.start(lambda new: (_ for _ in ()).throw(AssertionError("불량 기존 서버에서 키 요청")))
            except DesktopError as error:
                message = str(error)
            ok("서버는 실행 중이나 업무 화면 파일을 제공하지 못" in message
               and not controller.owned and not selected.exists(),
               "기존 서버 자산 오류를 포트 충돌과 구분하며 새 자식·DB를 만들지 않음")
        finally:
            for server in (http, external):
                server.shutdown()
            for thread in threads:
                thread.join(timeout=5)


def shortcut_metadata(root):
    """frozen 실행 파일 경로를 주입한 실제 .lnk 메타데이터 시험. EXE/GUI 실행 시험은 별도다."""
    artifact = ROOT / "dist" / "desktop" / "Donggu-PC" / "Donggu-PC.exe"
    state, port = root / "shortcut state with spaces", 19876
    destination = root / "shortcut with spaces.lnk"
    with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(artifact)), \
            patch.dict(os.environ, {"DG_DB_KEY": KEY}):
        create_shortcut(state, destination, port=port)
    quoted = "'" + str(destination).replace("'", "''") + "'"
    script = ("[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false); "
              "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(" + quoted + "); "
              "@{target=$s.TargetPath; arguments=$s.Arguments; directory=$s.WorkingDirectory}|ConvertTo-Json -Compress")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                             base64.b64encode(script.encode("utf-16le")).decode("ascii")],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                            creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    metadata = json.loads(result.stdout.decode("utf-8-sig"))
    expected_args = subprocess.list2cmdline(["--state", str(state), "--port", str(port)])
    contents = destination.read_bytes()
    ok(Path(metadata["target"]) == artifact and metadata["arguments"] == expected_args
       and Path(metadata["directory"]) == artifact.parent
       and KEY not in json.dumps(metadata) and KEY.encode() not in contents
       and KEY.encode("utf-16le") not in contents,
       "실제 .lnk COM 메타데이터: frozen EXE 대상·공백 자료 경로·사용자 지정 포트 보존·비밀 미포함")


def main():
    with tempfile.TemporaryDirectory(prefix="dg desktop app ") as directory:
        root = Path(directory).resolve()
        if os.name == "nt":
            shortcut_metadata(root)
        ok(default_state_dir({"DG_STATE": str(root / "explicit")}) == root / "explicit",
           "명시 DG_STATE를 우선하며 사용자 자료를 이동하지 않음")
        with patch.dict(os.environ, {"DG_DB_KEY": "other-folder-key"}):
            chosen = resolve_db_key(False, lambda new: KEY, force_prompt=True)
            ok(chosen == KEY and os.environ["DG_DB_KEY"] == "other-folder-key",
               "명시 비밀번호 재입력이 기존 환경 키를 이번 실행에서만 우회")
        startup_asset_guard(root)
        for interpreter in [None] + ([Path(sys.executable).with_name("pythonw.exe")] if os.name == "nt" else []):
            state = root / ("windowed state" if interpreter else "normal state")
            controller = DesktopController(state, free_port(), interpreter=interpreter)
            other = None
            try:
                phases = []
                def provide_key(new):
                    phases.append("provider")
                    return KEY
                result = controller.start(provide_key, on_progress=phases.append)
                ok(phases == ["checking", "unlocking", "provider", "starting", "waiting", "ready"],
                   "진짜 키 요청·자식 시작·준비 순서대로 비밀 없는 진행 단계 제공")
                ok(result == "ready" and controller.owned and controller.child.poll() is None,
                   "콘솔 없는 pythonw 자식 준비" if interpreter else "실제 앱 자식 준비·소유권 확인")
                ok(b'"setup_needed": true' in get(controller.url), "실제 PC API 준비 뒤 성공 보고")
                ok(b"import('./pc.js')" in get(controller.url, "start.js"), "실제 PC 첫 화면 자산 제공")
                ok((state / "pc.db").read_bytes()[:16] != b"SQLite format 3\x00", "실제 SQLCipher DB만 생성")
                other = DesktopController(root / "unused selected directory", controller.port)
                reused_phases = []
                ok(other.start(lambda new: (_ for _ in ()).throw(AssertionError("재사용 중 키 요청")),
                               on_progress=reused_phases.append) == "existing"
                   and not other.owned, "기존 동구 서버는 키 요청·중복 자식 없이 재사용")
                ok(reused_phases == ["checking", "existing"], "서버 재사용은 키 요청·시작·대기 단계를 내보내지 않음")
                ok(other.check_existing() and other.status == "existing" and not other.owned,
                   "기존 외부 서버의 정체성·업무 파일 상태를 변경 없이 확인")
                owned_child = controller.child
                with patch("desktop_runtime._app_health", side_effect=AssertionError("소유 서버 재사용 조회")):
                    ok(not controller.check_existing() and controller.status == "ready" and controller.child is owned_child,
                       "소유 서버에는 외부 서버 조회를 실행하지 않고 상태·핸들 보존")
                other.stop()
                ok(get(controller.url) and not other.state_dir.exists(), "외부 서버 종료·선택 폴더 초기화하지 않음")
                process = controller.child
                pid = process.pid
                controller.stop()
                ok(process.returncode == 0, "정상 종료 자식이 성공 종료 코드를 반환")
                ok(not other.check_existing() and other.status == "existing" and not other.owned,
                   "재사용하던 실제 외부 서버가 종료되면 False를 반환하고 재시작·상태 변경하지 않음")
                ok(controller.child is None and not controller.owned and (state / "pc.db").exists(),
                   f"본인이 만든 자식 PID {pid}를 stdin stop으로 정상 종료하고 DB 보존")
                saved = (state / "pc.db").read_bytes()
                failed = DesktopController(state, controller.port)
                try:
                    failed.start(lambda new: "wrong-desktop-key")
                except DesktopError:
                    pass
                else:
                    raise AssertionError("잘못된 키를 준비 완료로 처리함")
                finally:
                    failed.stop()
                ok((state / "pc.db").read_bytes() == saved, "잘못된 키 오류 후 DB 원본 보존")
                logs = b"".join(p.read_bytes() for p in (state / "logs").glob("desktop-*.log"))
                ok(KEY.encode() not in logs and b"wrong-desktop-key" not in logs, "비밀번호를 자식 로그에 남기지 않음")
                ok(controller.start(lambda new: KEY) == "ready" and get(controller.url),
                   "정상 종료 뒤 같은 자료·포트에서 서버를 다시 시작")
                restarted = controller.child
                restarted.stdin.close()
                ok(restarted.wait(timeout=20) == 0, "부모 입력 파이프 EOF도 정상 종료로 처리")
                controller.stop()
            finally:
                if other:
                    other.stop()
                controller.stop()
        cancelled = DesktopController(root / "cancelled before init", free_port())
        try:
            cancelled.start(lambda new: None)
        except DesktopError:
            pass
        else:
            raise AssertionError("취소 뒤 초기화를 진행함")
        ok(not cancelled.state_dir.exists(), "비밀번호 입력 취소 시 새 DB·폴더를 만들지 않음")
        racing = DesktopController(root / "another app wins port race", free_port())
        competitor = None

        def occupy_after_initial_check(new):
            nonlocal competitor
            competitor = ThreadingHTTPServer(("127.0.0.1", racing.port), RacingDonggu)
            threading.Thread(target=competitor.serve_forever, daemon=True).start()
            return KEY

        try:
            try:
                racing.start(occupy_after_initial_check)
            except DesktopError:
                pass
            else:
                raise AssertionError("포트 경합에서 다른 동구 서버의 응답을 본인 자식 준비로 오인함")
            ok(get(racing.url), "시작 도중 포트를 먼저 연 다른 동구 서버도 종료하지 않음")
        finally:
            racing.stop()
            if competitor:
                competitor.shutdown()
                competitor.server_close()
        with ThreadingHTTPServer(("127.0.0.1", 0), OtherApp) as http:
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            try:
                conflict = DesktopController(root / "port conflict", http.server_port)
                try:
                    conflict.start(lambda new: KEY)
                except DesktopError:
                    pass
                else:
                    raise AssertionError("다른 앱을 동구 서버로 오인함")
                conflict.stop()
                ok(get(conflict.url) and not conflict.state_dir.exists(), "포트 충돌 앱·자료를 건드리지 않음")
            finally:
                http.shutdown()
                thread.join(timeout=5)
    print(f"OK: 실행 앱 {PASSED}개 확인")


if __name__ == "__main__":
    main()
