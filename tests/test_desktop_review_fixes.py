"""PC 실행 앱 계층 검토 결함 6건 회귀시험 (가상 자료·임시 폴더·임시 포트만 사용).

1. 기본 포트(443/80)에서 포트 없는 Host 허용, 접속 점검도 브라우저와 같은 Host 사용
2. 자식 서버 오류를 분류 코드·개인정보 없는 안내로 남기고 부모가 분류별 안내
3. 접속 설정 손상 시 서버 시작·설정 덮어쓰기 차단
4. 바로가기가 --state를 고정하지 않고, 설정이 저장된 뒤에는 확인 없이 빈 새 DB를 만들지 않음
5. 기존 서버 주기 점검은 /api/state 하나만, 15초 이상 간격
6. 첫 관리자 설정은 서버 PC 본체(루프백)에서만

실행: python tests/test_desktop_review_fixes.py
결함마다 묶음으로 실행해 실패한 묶음을 모두 보고한다(묶음 안에서는 첫 실패에서 멈춤).
"""
from __future__ import annotations

import errno
import gc
import ipaddress
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))

from dgpc import __version__  # noqa: E402

passed = 0
KEY = "review-fixes-test-key-not-for-production"
WRONG = "review-fixes-wrong-key-not-for-production"


def ok(condition, message):
    global passed
    if not condition:
        raise AssertionError("실패: " + message)
    passed += 1
    print("  ✓", message, flush=True)


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def child_env(directory: Path, state: Path):
    env = {**os.environ, "LOCALAPPDATA": str(directory / "appdata"), "DG_STATE": str(state),
           "DG_OUT": str(directory / "out"), "DG_NO_SCHEDULER": "1", "PYTHONUTF8": "1",
           "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"}
    env.pop("DG_DB_KEY", None)
    return env


def init_db(directory: Path, state: Path):
    env = {**child_env(directory, state), "DG_DB_KEY": KEY}
    subprocess.run([sys.executable, str(ROOT / "pc/pc_tool.py"), "init"], env=env, check=True, capture_output=True, timeout=60)


def run_child(directory: Path, state: Path, payload: str, port: int, *, interpreter=None, timeout=60):
    """실제 실행 앱 진입점(desktop.py --desktop-child)을 부모와 같은 방식(stdin 파이프·로그 파일)으로 실행."""
    logs = directory / "parent-logs"
    logs.mkdir(parents=True, exist_ok=True)
    out, err = logs / f"child-{port}-out.log", logs / f"child-{port}-err.log"
    with out.open("wb") as stdout, err.open("wb") as stderr:
        process = subprocess.Popen([str(interpreter or sys.executable), str(ROOT / "pc/desktop.py"), "--desktop-child",
                                    "--state", str(state), "--port", str(port)],
                                   stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, cwd=ROOT / "pc",
                                   env=child_env(directory, state))
        try:
            process.stdin.write(payload.encode("utf-8"))
            process.stdin.close()
            code = process.wait(timeout=timeout)
        except BaseException:
            process.kill()
            process.wait()
            raise
    child_logs = "".join(p.read_text(encoding="utf-8", errors="replace") for p in (state / "logs").glob("desktop-child-*.log")) \
        if (state / "logs").is_dir() else ""
    return code, out.read_text(encoding="utf-8", errors="replace") + err.read_text(encoding="utf-8", errors="replace"), child_logs


def main_thread_tk_cleanup(test):
    def run(*args):
        enabled = gc.isenabled()
        gc.collect()
        gc.disable()
        try:
            return test(*args)
        finally:
            gc.collect()
            if enabled:
                gc.enable()
    return run


# ---------------------------------------------------------------- 1. 기본 포트 Host
def defect1_default_port_host():
    from dgpc import server as SV
    import desktop_connection as api

    saved = SV.PORT, SV.LAN

    def allowed(host, port, lan):
        SV.PORT, SV.LAN = port, lan
        return SV.Handler.host_ok(types.SimpleNamespace(headers={"Host": host}))

    lan = {"hostname": "dg-server.example.test", "nets": [ipaddress.ip_network("10.0.0.0/24")]}
    try:
        ok(allowed("dg-server.example.test", 443, lan), "LAN 443: 브라우저가 포트를 뺀 Host(접속 이름)를 허용")
        ok(allowed("DG-Server.Example.Test", 443, lan), "LAN 443: Host 대소문자는 구분하지 않음")
        ok(allowed("dg-server.example.test:443", 443, lan), "LAN 443: 명시 포트 Host도 계속 허용")
        ok(not allowed("dg-server.example.test", 8443, lan), "LAN 8443: 기본 포트가 아니면 포트 없는 Host 거부")
        ok(not allowed("evil.example.test", 443, lan) and not allowed("evil.example.test:443", 443, lan),
           "LAN 443: 다른 이름(DNS rebinding)은 계속 거부")
        ok(not allowed("dg-server.example.test:8443", 443, lan), "LAN 443: 다른 포트를 붙인 Host 거부")
        ok(allowed("127.0.0.1", 80, None) and allowed("localhost", 80, None), "로컬 80: 포트 없는 127.0.0.1·localhost 허용")
        ok(not allowed("dg-server.example.test", 80, None), "로컬 80: LAN 접속 이름은 로컬 방식에서 거부")
        ok(not allowed("127.0.0.1", 8765, None) and allowed("127.0.0.1:8765", 8765, None),
           "로컬 8765: 포트 없는 Host 거부·명시 포트 허용(기존 계약)")
        ok(not allowed("127.0.0.1", 443, None), "로컬 http 방식은 443을 기본 포트로 보지 않음")
        ok(not allowed("", 80, None), "Host 없는 요청 거부")
    finally:
        SV.PORT, SV.LAN = saved
    ok(api._request_host("https://dg-server.example.test:443/") == "dg-server.example.test"
       and api._request_host("http://127.0.0.1:80/") == "127.0.0.1"
       and api._request_host("http://127.0.0.1:8765/") == "127.0.0.1:8765"
       and api._request_host("https://dg-server.example.test:8443/") == "dg-server.example.test:8443",
       "접속 점검은 브라우저처럼 기본 포트일 때 Host에서 포트를 뺌")
    hosts = []

    class Recorder(BaseHTTPRequestHandler):
        def do_GET(self):
            hosts.append(self.headers.get("Host"))
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        api.probe_url(f"http://127.0.0.1:{server.server_port}/")
    finally:
        server.shutdown()
        server.server_close()
    ok(hosts == [f"127.0.0.1:{server.server_port}"], "점검 요청 Host는 한 번만, 비기본 포트를 포함해 보냄: " + repr(hosts))


# ---------------------------------------------------------------- 2. 자식 오류 분류
def defect2_child_error_classification(directory: Path):
    import desktop_runtime as runtime

    codes = runtime.CHILD_FAILURES
    ok({"profile", "launch_input", "sqlcipher_missing", "db_key", "db_missing", "db_newer", "port_in_use",
        "port_denied", "tls", "os_error", "unexpected"} <= set(codes), "오류 분류 코드 표 제공")
    exit_codes = [value[0] for value in codes.values()]
    ok(len(set(exit_codes)) == len(exit_codes) and all(code > 1 for code in exit_codes),
       "분류마다 서로 다른 종료 코드(일반 오류 1·정상 0과 겹치지 않음)")
    ok(all(runtime.child_failure_code(value[0]) == name for name, value in codes.items())
       and runtime.child_failure_code(1) is None and runtime.child_failure_code(0) is None,
       "부모가 종료 코드로 분류를 되찾음")
    messages = {name: runtime.child_failure_message(name, directory / "logs") for name in codes}
    ok(len(set(messages.values())) == len(messages) and all(str(directory / "logs") in m for m in messages.values()),
       "분류별로 다른 안내 + 오류 기록 위치")
    ok("비밀번호" in messages["db_key"] and "포트" in messages["port_in_use"] and "SQLCipher" in messages["sqlcipher_missing"]
       and "pc.db" in messages["db_missing"] and "비밀번호" not in messages["port_in_use"],
       "DB 비밀번호 안내는 비밀번호 오류에만, 포트 사용 중은 포트 안내")
    classify = runtime.classify_child_error
    ok(classify(SystemExit("DB를 열 수 없습니다. DB 키(비밀번호)가 맞는지 확인하세요.")) == "db_key"
       and classify(SystemExit("SQLCipher 모듈(sqlcipher3)이 없어 실행을 멈춥니다.")) == "sqlcipher_missing"
       and classify(SystemExit("DB 구조 버전(99)이 이 프로그램(1)보다 새것입니다.")) == "db_newer"
       and classify(SystemExit("PC 자료가 없습니다. 먼저 'python pc_tool.py init'을 실행하세요.")) == "db_missing"
       and classify(SystemExit("가상 주민 홍길동 010-0000-0000")) == "unexpected",
       "store의 우리 안내문(SystemExit)을 분류하고 모르는 문구는 그대로 쓰지 않음")
    inuse = OSError(10048 if os.name == "nt" else errno.EADDRINUSE, "address in use")  # 번호는 운영체제마다 다르다(Linux 98, macOS 48)
    if os.name == "nt":
        inuse.winerror = 10048
    ok(classify(inuse) == "port_in_use", "주소 사용 중 OSError → port_in_use")
    denied = PermissionError(13, "denied")
    ok(classify(denied) == "port_denied" and classify(PermissionError(13, "denied", "C:/가상/pc.db")) == "os_error",
       "소켓 권한 오류와 파일 권한 오류를 구분")
    import ssl
    ok(classify(ssl.SSLError(1, "bad cert")) == "tls", "인증서·키 오류 → tls")
    missing = ModuleNotFoundError("No module named 'sqlcipher3'", name="sqlcipher3")
    ok(classify(missing) == "sqlcipher_missing" and classify(RuntimeError("x")) == "unexpected",
       "sqlcipher3 누락 import 오류 분류")

    state = directory / "child-state"
    init_db(directory, state)
    port = free_port()
    code, parent_log, child_log = run_child(directory, state, json.dumps({"key": WRONG, "launch_id": "c" * 32}) + "\n", port)
    ok(code == codes["db_key"][0], f"실제 자식: 틀린 DB 비밀번호 → db_key 종료 코드 ({code})")
    ok("[db_key]" in child_log and "SystemExit" in child_log, "자식 로그에 분류 코드·오류 종류 기록")
    ok(WRONG not in child_log + parent_log and KEY not in child_log + parent_log, "비밀번호는 로그에 남기지 않음")

    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        busy_port = occupied.getsockname()[1]
        code, parent_log, child_log = run_child(directory, state, json.dumps({"key": KEY, "launch_id": "d" * 32}) + "\n", busy_port)
    ok(code == codes["port_in_use"][0] and "[port_in_use]" in child_log,
       f"실제 자식: 포트 사용 중 → port_in_use ({code})")

    code, parent_log, _ = run_child(directory, state, "{not json\n", free_port())
    ok(code == codes["launch_input"][0] and "[launch_input]" in parent_log,
       f"stdin 시작 정보 JSON 오류도 분류·부모 로그에 흔적 ({code})")
    code, parent_log, _ = run_child(directory, state, json.dumps({"launch_id": "e" * 32}) + "\n", free_port())
    ok(code == codes["launch_input"][0] and "[launch_input]" in parent_log, "키 없는 시작 정보 → launch_input")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if os.name == "nt" and pythonw.is_file():
        code, parent_log, _ = run_child(directory, state, "{not json\n", free_port(), interpreter=pythonw)
        ok(code == codes["launch_input"][0] and "[launch_input]" in parent_log,
           "콘솔 없는(windowed) 자식도 시작 전 오류를 부모 로그에 남김")

    controller = runtime.DesktopController(state, free_port())
    try:
        controller.start(lambda new: WRONG)
    except runtime.DesktopError as error:
        message, code = str(error), getattr(error, "code", None)
    else:
        controller.stop()
        raise AssertionError("틀린 키로 준비 완료")
    ok(code == "db_key" and "비밀번호로 다시 열기" in message, "부모 실행 앱이 비밀번호 오류를 비밀번호 안내로 표시")


# ---------------------------------------------------------------- 3. 접속 설정 손상
def defect3_damaged_configuration(directory: Path):
    import desktop_connection as api
    import desktop_runtime as runtime

    home = directory / "damaged-home"
    config = home / "DongguWaste" / "desktop.json"
    config.parent.mkdir(parents=True)
    config.write_text("{broken", encoding="utf-8")
    try:
        api.update_configuration({"state": str(directory / "x")}, directory=config.parent)
    except ValueError:
        pass
    else:
        raise AssertionError("손상 설정 위에 덮어씀")
    ok(config.read_text(encoding="utf-8") == "{broken", "update_configuration은 손상 설정을 {}로 보고 덮어쓰지 않음")
    with patch.dict(os.environ, {"LOCALAPPDATA": str(home)}):
        try:
            runtime.save_state_choice(directory / "x")
        except ValueError:
            pass
        else:
            raise AssertionError("자료 위치 저장이 손상 설정을 덮어씀")
    ok(config.read_text(encoding="utf-8") == "{broken", "자료 폴더 저장도 손상 설정(LAN 설정)을 지우지 않음")
    backup = api.update_configuration({"connection": {"mode": "local", "port": 8766}}, directory=config.parent, replace_damaged=True)
    ok(backup is not None and Path(backup).read_text(encoding="utf-8") == "{broken"
       and json.loads(config.read_text(encoding="utf-8")) == {"connection": {"mode": "local", "port": 8766}},
       "명시적 복구 저장만 손상 파일을 보관한 뒤 새 설정 저장")
    config.write_text("{broken", encoding="utf-8")
    for path in config.parent.glob("desktop.json.damaged-*"):
        path.unlink()
    gui_damaged_configuration(home, config, directory)


@main_thread_tk_cleanup
def gui_damaged_configuration(home: Path, config: Path, directory: Path):
    import tkinter.filedialog
    import tkinter.messagebox
    import desktop
    import desktop_view
    from desktop_runtime import DesktopError

    real_view = desktop_view.DesktopView
    starts, errors, failures = [], [], []
    chosen = directory / "chosen-folder"
    chosen.mkdir()

    class Controller:
        def __init__(self, state, port, **_):
            self.state_dir, self.port = Path(state), port
            self.url = f"http://127.0.0.1:{port}/"
            self.child, self.status = None, "idle"

        @property
        def owned(self):
            return False

        def start(self, password_provider, **_):
            starts.append(self.state_dir)
            raise DesktopError("가상 시작")

        def stop(self):
            self.status = "stopped"

    def capture(root, version, **kwargs):
        view = real_view(root, version, **kwargs)

        def inspect():
            if view.state != "error":
                failures.append("손상 설정 안내 상태가 아님: " + view.state)
            for name in ("retry", "password", "folder"):
                view.callbacks[name]()
            root.after(300, close)

        def close():
            if view.state != "error" or "접속 설정" not in view.detail_text.get("1.0", "end"):
                failures.append("다시 시도 뒤 손상 설정 안내가 사라짐")
            root.tk.call(root.protocol("WM_DELETE_WINDOW"))

        root.after(80, inspect)
        return view

    port = free_port()
    with patch.dict(os.environ, {"LOCALAPPDATA": str(home), "DG_STATE": str(directory / "gui-state")}), \
            patch.object(sys, "argv", ["desktop.py", "--port", str(port), "--no-browser"]), \
            patch.object(desktop, "DesktopController", Controller), \
            patch.object(desktop_view, "DesktopView", capture), \
            patch.object(tkinter.filedialog, "askdirectory", return_value=str(chosen)), \
            patch.object(tkinter.messagebox, "showerror", side_effect=lambda *a, **k: errors.append(a)):
        result = desktop.main()
    ok(result == 0 and not failures, "손상 설정 실행 앱이 정상 종료 " + repr(failures))
    ok(not starts, "설정을 못 읽은 상태의 [다시 시도]·[비밀번호로 다시 열기]·[자료 폴더 선택]은 기본(로컬) 서버를 띄우지 않음")
    ok(config.read_text(encoding="utf-8") == "{broken", "자료 폴더 선택이 손상 설정 파일을 덮어쓰지 않음")


# ---------------------------------------------------------------- 4. 바로가기·빈 새 DB
def defect4_shortcut_and_new_database(directory: Path):
    import desktop
    import desktop_runtime as runtime
    from desktop_connection import ConnectionProfile

    arguments = runtime.shortcut_arguments(port=8766)
    ok("--state" not in arguments and arguments[-2:] == ["--port", "8766"], "바로가기는 자료 폴더(--state)를 고정하지 않음")
    lan = ConnectionProfile(mode="lan", port=8443, host="10.0.0.2", hostname="dg.example.test", cert="c", key="k", allow="10.0.0.0/24")
    ok("--state" not in runtime.shortcut_arguments(connection=lan), "LAN 바로가기도 --state 없음")

    home = directory / "new-db-home"
    asked = []
    with patch.dict(os.environ, {"LOCALAPPDATA": str(home)}):
        ok(not runtime.has_saved_configuration(), "설정 파일이 없으면 첫 설치")
        ok(desktop.new_database_allowed(lambda: asked.append(1) or False) and not asked, "첫 설치는 묻지 않고 새 DB 허용")
        runtime.save_state_choice(directory / "remembered")
        ok(runtime.has_saved_configuration(), "설정 저장 뒤에는 첫 설치가 아님")
        ok(not desktop.new_database_allowed(lambda: asked.append(1) or False) and asked == [1],
           "설정이 저장된 뒤 pc.db 없는 폴더는 사용자 확인을 묻고, 거절하면 만들지 않음")
        ok(desktop.new_database_allowed(lambda: True), "사용자가 확인하면 새 DB 허용")

    state = directory / "missing-db"
    code, _, child_log = run_child(directory, state, json.dumps({"key": KEY, "launch_id": "f" * 32, "create": False}) + "\n", free_port())
    ok(code == runtime.CHILD_FAILURES["db_missing"][0] and not (state / "pc.db").exists(),
       f"자식은 생성 허락(create) 없이 pc.db 없는 폴더에 새 DB를 만들지 않음 ({code})")

    declined = directory / "declined"
    controller = runtime.DesktopController(declined, free_port())

    def refuse(new):
        if new:
            raise runtime.DesktopError("새 DB를 만들지 않았습니다.", code="db_missing")
        return KEY
    try:
        controller.start(refuse)
    except runtime.DesktopError as error:
        code = getattr(error, "code", None)
    else:
        raise AssertionError("거절했는데 시작")
    ok(code == "db_missing" and controller.status == "error" and not controller.owned and not declined.exists(),
       "새 DB 거절 시 자식·폴더·DB를 만들지 않음")
    gui_new_database_confirmation(directory)


@main_thread_tk_cleanup
def gui_new_database_confirmation(directory: Path):
    import tkinter.messagebox
    import desktop
    import desktop_view

    home = directory / "gui-new-db-home"
    folder = directory / "moved-away"
    config = home / "DongguWaste" / "desktop.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"state": str(folder)}), encoding="utf-8")
    real_view = desktop_view.DesktopView
    questions, failures = [], []

    def capture(root, version, **kwargs):
        view = real_view(root, version, **kwargs)
        began = time.monotonic()

        def inspect():
            if view.state == "ready":
                failures.append("확인 없이 새 DB로 서버를 시작함")
            elif view.state != "error" and time.monotonic() - began < 20:
                root.after(50, inspect)
                return
            root.tk.call(root.protocol("WM_DELETE_WINDOW"))

        root.after(100, inspect)
        return view

    def ask(*args, **kwargs):
        questions.append((args, kwargs))
        return False

    environment = {"LOCALAPPDATA": str(home), "DG_DB_KEY": KEY, "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"}
    with patch.dict(os.environ, environment), \
            patch.object(sys, "argv", ["desktop.py", "--port", str(free_port()), "--no-browser"]), \
            patch.object(desktop_view, "DesktopView", capture), \
            patch.object(tkinter.messagebox, "askyesno", side_effect=ask), \
            patch.object(tkinter.messagebox, "showerror", return_value=None):
        os.environ.pop("DG_STATE", None)
        result = desktop.main()
    ok(result == 0 and not failures, "설정 저장 PC에서 pc.db 없는 폴더 실행이 오류 상태로 멈춤 " + repr(failures))
    ok(len(questions) == 1 and str(folder) in " ".join(map(str, questions[0][0])),
       "실행 앱 화면에서 해당 폴더를 보여 주며 새 DB 생성을 한 번 확인")
    ok(questions and questions[0][1].get("default") == "no", "새 DB 확인 기본값은 [아니요]")
    ok(not (folder / "pc.db").exists(), "거절하면 빈 새 DB를 만들지 않음")


# ---------------------------------------------------------------- 5. 기존 서버 점검 부하
def defect5_light_existing_check(directory: Path):
    import desktop
    import desktop_runtime as runtime

    ok(desktop.EXISTING_CHECK_SECONDS >= 15, f"기존 서버 주기 점검 간격 15초 이상 ({desktop.EXISTING_CHECK_SECONDS})")
    requests = []
    assets = {
        "/api/state": ("application/json", b'{"setup_needed":false,"logged_in":false}'),
        "/": ("text/html; charset=utf-8", '<title>동구 대형폐기물 PC 업무</title><script type="module" src="start.js"></script>'.encode()),
        "/start.js": ("text/javascript", b"import('./pc.js')"),
        "/pc.js": ("text/javascript", b"export {}"),
        "/core.js": ("text/javascript", b"export {}"),
    }

    class Donggu(BaseHTTPRequestHandler):
        server_version, sys_version = "dgpc", ""

        def do_GET(self):
            requests.append(self.path)
            ctype, body = assets.get(self.path, ("text/plain", b""))
            self.send_response(200 if self.path in assets else 404)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-DG-Version", __version__)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Donggu)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        controller = runtime.DesktopController(directory / "existing-unused", server.server_port)
        controller.status = "existing"
        ok(controller.check_existing() and requests == ["/api/state"], "주기 점검은 /api/state 하나만 GET: " + repr(requests))
        requests.clear()
        ok(controller.check_existing(full=True) and set(requests) == set(assets), "수동 점검은 업무 화면 자산까지 확인")
        requests.clear()
        assets["/api/state"] = ("application/json", b"{}")
        ok(not controller.check_existing() and requests == ["/api/state"], "가벼운 점검도 서버 정체 확인 실패는 실패로 봄")
    finally:
        server.shutdown()
        server.server_close()
    ok(not (directory / "existing-unused").exists(), "점검은 자료 폴더를 만들지 않음")


# ---------------------------------------------------------------- 6. 첫 관리자 설정
def defect6_setup_loopback_only(directory: Path):
    from dgpc import server as SV, service as S, store

    ok(SV.loopback_client("127.0.0.1") and SV.loopback_client("::1") and SV.loopback_client("::ffff:127.0.0.1")
       and not SV.loopback_client("10.0.0.5") and not SV.loopback_client("") and not SV.loopback_client("::ffff:10.0.0.5"),
       "루프백 판정(IPv4·IPv6·IPv4 대응 IPv6)")
    ok(SV.same_machine("10.0.0.2", "10.0.0.2") and SV.same_machine("::ffff:10.0.0.2", "10.0.0.2") and SV.same_machine("127.0.0.1", "10.0.0.2")
       and not SV.same_machine("10.0.0.5", "10.0.0.2") and not SV.same_machine("", "") and not SV.same_machine("0.0.0.0", "0.0.0.0"),
       "기관망 주소로만 열린 서버에서도 본체(접속한 쪽 주소 = 서버가 받은 주소)는 첫 설정 가능, 다른 PC는 불가")
    saved_env = {k: os.environ.get(k) for k in ("DG_STATE", "DG_DB_KEY")}
    saved = SV.CON, SV.PORT, SV.LAN
    os.environ.update(DG_STATE=str(directory / "setup-state"), DG_DB_KEY=KEY)
    con = store.connect(create=True)
    servers = []
    try:
        class Remote(SV.Handler):
            def setup(self):
                self.client_address = ("10.0.0.5", 50000)
                super().setup()

        port = free_port()
        SV.CON, SV.PORT = con, port
        SV.LAN = {"hostname": "127.0.0.1", "nets": [ipaddress.ip_network("10.0.0.0/24")]}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def post(path, body):
            request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), method="POST",
                                             headers={"X-DG-CSRF": "1", "Content-Type": "application/json"})
            try:
                with opener.open(request, timeout=20) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())

        body = {"username": "admin", "display_name": "가상 관리자", "password": "first-admin-pass-01"}
        for handler, expected in ((Remote, 403), (SV.Handler, 200)):
            srv = SV.Server(("127.0.0.1", port), handler)
            servers.append(srv)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            status, data = post("/api/setup", body)
            srv.shutdown()
            srv.server_close()
            if handler is Remote:
                ok(status == 403 and "PC 본체" in data.get("error", "") and not S.users_exist(con),
                   f"LAN의 다른 PC는 첫 관리자를 만들 수 없음 ({status} {data.get('error')})")
            else:
                ok(status == 200 and S.users_exist(con), f"서버 PC 본체(127.0.0.1)에서는 첫 관리자 생성 ({status})")
    finally:
        con.close()
        SV.CON, SV.PORT, SV.LAN = saved
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main():
    failed = []
    with tempfile.TemporaryDirectory(prefix="dg-desktop-review-") as temporary:
        root = Path(temporary).resolve()
        sections = (("1. 기본 포트 Host", lambda: defect1_default_port_host()),
                    ("2. 자식 오류 분류", lambda: defect2_child_error_classification(root / "d2")),
                    ("3. 접속 설정 손상", lambda: defect3_damaged_configuration(root / "d3")),
                    ("4. 바로가기·빈 새 DB", lambda: defect4_shortcut_and_new_database(root / "d4")),
                    ("5. 기존 서버 가벼운 점검", lambda: defect5_light_existing_check(root / "d5")),
                    ("6. 첫 관리자 루프백", lambda: defect6_setup_loopback_only(root / "d6")))
        for title, run in sections:
            print(title, flush=True)
            folder = root / ("d" + title[0])
            folder.mkdir(parents=True, exist_ok=True)
            try:
                run()
            except Exception as error:  # noqa: BLE001 - 묶음별 실패를 모두 보고
                failed.append(title)
                print(f"  ✗ {title}: {type(error).__name__}: {error}", flush=True)
    print(f"통과 {passed}건" + (f", 실패 묶음: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
