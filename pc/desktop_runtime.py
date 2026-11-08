"""실행 앱 수명 관리. GUI와 업무 서버를 분리하고 생성한 자식만 정상 종료한다."""
from __future__ import annotations

import ctypes
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

APP_TITLE = "동구 PC 실행 앱"
SOURCE = Path(__file__).resolve().parent


class DesktopError(RuntimeError):
    pass


def app_data_dir(environment=None):
    environment = os.environ if environment is None else environment
    return Path(environment.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "DongguWaste"


def default_state_dir(environment=None):
    environment = os.environ if environment is None else environment
    if environment.get("DG_STATE"):
        return Path(environment["DG_STATE"]).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return app_data_dir(environment) / "state"
    return SOURCE / "state"


def load_state_choice(explicit=None):
    if explicit or os.environ.get("DG_STATE"):
        return Path(explicit).expanduser().resolve() if explicit else default_state_dir()
    try:
        config = json.loads((app_data_dir() / "desktop.json").read_text(encoding="utf-8"))
        if isinstance(config.get("state"), str) and Path(config["state"]).is_absolute():
            return Path(config["state"])
    except (OSError, ValueError, TypeError):
        pass
    return default_state_dir()


def save_state_choice(state):
    directory = app_data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "desktop.json"
    temporary = directory / ("desktop-" + secrets.token_hex(6) + ".tmp")
    temporary.write_text(json.dumps({"state": str(Path(state).resolve())}, ensure_ascii=False), encoding="utf-8")
    temporary.replace(destination)


def resolve_db_key(new, prompt, *, force_prompt=False):
    """키는 부모 메모리와 자식 파이프로만 전달한다. 저장소 불가 시 GUI 입력을 받는다."""
    if force_prompt:
        return prompt(new)
    if os.environ.get("DG_DB_KEY"):
        return os.environ["DG_DB_KEY"]
    from dgpc import store
    saved = store._keyring_get()
    if saved:
        return saved
    if new:
        generated = secrets.token_urlsafe(32)
        if store._keyring_set(generated):
            return generated
    return prompt(new)


def worker_command(state, port, interpreter=None):
    if getattr(sys, "frozen", False) and interpreter is None:
        command = [sys.executable, "--desktop-child"]
    else:
        command = [str(interpreter or sys.executable), str(SOURCE / "desktop.py"), "--desktop-child"]
    return command + ["--state", str(state), "--port", str(port)]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, destination):
        return None


class _EntryScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attributes):
        if tag == "script":
            source = dict(attributes).get("src")
            if source is not None:
                self.sources.append(source.strip())


def _origin(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("인증 정보가 있는 자산 주소")
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


def _app_health(url):
    """API 정체성과 HTML이 요구하는 실제 진입 파일을 함께 확인한다. 다른 출처로는 요청하지 않는다."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        request = urllib.request.Request(url + "api/state", headers={"X-DG-CSRF": "1"})
        with opener.open(request, timeout=1) as response:
            if response.status != 200 or response.headers.get("Server", "").strip() != "dgpc":
                return "unrecognized"
            state = json.load(response)
        if not all(isinstance(state.get(key), bool) for key in ("setup_needed", "logged_in")):
            return "unrecognized"
    except (OSError, ValueError, AttributeError):
        return "unrecognized"
    try:
        with opener.open(url, timeout=1) as response:
            if response.status != 200:
                return "assets_unavailable"
            page = response.read().decode("utf-8")
        if "<title>동구 대형폐기물 PC 업무</title>" not in page:
            return "assets_unavailable"
        entries = _EntryScripts()
        entries.feed(page)
        if not entries.sources or any(not source for source in entries.sources):
            return "assets_unavailable"
        resources = [urllib.parse.urljoin(url, source) for source in entries.sources + ["pc.js", "core.js"]]
        expected_origin = _origin(url)
        if any(_origin(resource) != expected_origin for resource in resources):
            return "assets_unavailable"
        for resource in dict.fromkeys(resources):
            with opener.open(resource, timeout=1) as response:
                if (response.status != 200 or response.headers.get_content_type() not in
                        {"text/javascript", "application/javascript", "text/ecmascript", "application/ecmascript"}
                        or not response.read().strip()):
                    return "assets_unavailable"
        return "ready"
    except (OSError, ValueError, AttributeError):
        return "assets_unavailable"


def app_ready(url):
    return _app_health(url) == "ready"


class DesktopController:
    def __init__(self, state_dir, port=8765, *, interpreter=None, timeout=45):
        if not 1 <= port <= 65535:
            raise DesktopError("포트는 1~65535 사이여야 합니다.")
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.port = port
        self.url = f"http://127.0.0.1:{port}/"
        self.interpreter = interpreter
        self.timeout = timeout
        self.child = None
        self.status = "idle"
        self._ready_file = None

    @property
    def owned(self):
        return self.child is not None

    def check_existing(self) -> bool:
        """별도 스레드에서 호출하는 외부 서버 상태 조회. 키·프로세스·상태는 변경하지 않는다."""
        if self.status != "existing" or self.owned:
            return False
        return app_ready(self.url)

    def start(self, password_provider, *, on_progress=None):
        if self.child is not None:
            raise DesktopError("이 실행 앱의 서버가 이미 시작 중이거나 실행 중입니다.")
        progress = on_progress or (lambda phase: None)
        self.status = "starting"
        progress("checking")
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                occupied = True
        except OSError:
            occupied = False
        if occupied:
            health = _app_health(self.url)
            if health == "ready":
                self.status = "existing"
                progress("existing")
                return self.status
            self.status = "error"
            if health == "assets_unavailable":
                raise DesktopError("서버는 실행 중이나 업무 화면 파일을 제공하지 못합니다. 기존 서버를 종료한 뒤 다시 시도하세요. 기존 서버는 종료하지 않았고 새 서버도 시작하지 않았습니다.")
            raise DesktopError(f"포트 {self.port}를 다른 프로그램이 사용 중이거나 동구 서버가 준비되지 않았습니다. 해당 프로그램은 종료하지 않았습니다.")
        progress("unlocking")
        key = password_provider(not (self.state_dir / "pc.db").exists())
        if not key:
            self.status = "error"
            raise DesktopError("DB 비밀번호 입력을 취소했습니다. 자료는 초기화하지 않았습니다.")
        logs = self.state_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        launch_id = secrets.token_hex(16)
        self._ready_file = logs / f"desktop-ready-{launch_id}.json"
        stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
        environment = os.environ.copy()
        environment.update(DG_STATE=str(self.state_dir), DG_OUT=str(self.state_dir / "out"),
                           PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        with (logs / f"desktop-{stamp}-out.log").open("ab") as output, (logs / f"desktop-{stamp}-err.log").open("ab") as error:
            try:
                progress("starting")
                self.child = subprocess.Popen(worker_command(self.state_dir, self.port, self.interpreter),
                                              stdin=subprocess.PIPE, stdout=output, stderr=error,
                                              env=environment, cwd=SOURCE,
                                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                self.child.stdin.write((json.dumps({"key": key, "launch_id": launch_id}) + "\n").encode("utf-8"))
                self.child.stdin.flush()
            except OSError as error:
                self.status = "error"
                raise DesktopError("서버 자식을 시작하지 못했습니다. 실행 앱 파일을 확인하세요.") from error
            finally:
                key = None
        deadline = time.monotonic() + self.timeout
        progress("waiting")
        while time.monotonic() < deadline:
            if self.child.poll() is not None:
                self.child.stdin.close()
                self.child = None
                self.status = "error"
                raise DesktopError(f"서버를 시작하지 못했습니다. DB 비밀번호와 자료 폴더를 확인하세요. 오류 기록: {logs}")
            if self._ready_file.is_file() and app_ready(self.url):
                self.status = "ready"
                progress("ready")
                return self.status
            time.sleep(0.25)
        self.stop()
        self.status = "error"
        raise DesktopError(f"서버 준비 시간이 초과되었습니다. 오류 기록: {logs}")

    def stop(self):
        child = self.child
        if child is None:
            return
        if child.stdin and not child.stdin.closed:
            try:
                child.stdin.write(b'{"command":"stop"}\n')
                child.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                child.stdin.close()
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired as error:
            raise DesktopError("서버가 진행 중인 작업을 마치고 있습니다. 잠시 기다렸다가 다시 종료하세요.") from error
        self.child = None
        self.status = "stopped"
        if self._ready_file and self._ready_file.exists():
            self._ready_file.unlink()


def _stdin_pipe():
    if sys.stdin is not None:
        return sys.stdin
    if os.name != "nt":
        raise DesktopError("실행 앱의 입력 파이프가 없습니다.")
    # PyInstaller --windowed와 pythonw는 유효한 OS 파이프가 있어도 sys.stdin을 None으로 둔다.
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    handle = kernel.GetStdHandle(-10 & 0xffffffff)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise DesktopError("실행 앱의 입력 파이프를 찾지 못했습니다.")
    descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    return os.fdopen(descriptor, "r", encoding="utf-8")


def child_main(state, port):
    incoming = _stdin_pipe()
    first = json.loads(incoming.readline())
    key = first.get("key")
    launch_id = first.get("launch_id")
    if not isinstance(key, str) or not key:
        raise DesktopError("DB 키가 전달되지 않았습니다.")
    state = Path(state).resolve()
    os.environ.update(DG_STATE=str(state), DG_OUT=str(state / "out"))
    logs = state / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / ("desktop-child-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4) + ".log")
    with log.open("a", encoding="utf-8", buffering=1) as output:
        sys.stdout = sys.stderr = output
        try:
            from dgpc import devices, server, store
            if not store.ENCRYPTED:
                raise DesktopError("SQLCipher가 없어 실행을 멈춥니다.")
            # 부모에서 선택한 키를 이 자식의 메모리에서만 사용한다. 콘솔 입력은 호출하지 않는다.
            store.db_key = lambda create=False: key
            store._keyring_get = lambda: None
            if not (state / "pc.db").exists():
                connection = store.connect(create=True)
                try:
                    devices.ensure_pc_keys(connection)
                finally:
                    connection.close()

            def ready(srv):
                srv.daemon_threads = False
                if isinstance(launch_id, str) and len(launch_id) == 32 and all(c in "0123456789abcdef" for c in launch_id):
                    (logs / f"desktop-ready-{launch_id}.json").write_text(
                        json.dumps({"pid": os.getpid(), "port": port}), encoding="utf-8")

                def controls():
                    try:
                        while True:
                            line = incoming.readline()
                            if not line or json.loads(line).get("command") == "stop":
                                break
                    except (OSError, ValueError):
                        pass
                    srv.shutdown()

                threading.Thread(target=controls, name="desktop-stop", daemon=True).start()

            server.serve(port, on_ready=ready)
            return 0
        except BaseException as error:
            # 비밀번호·원시 요청·주민 자료는 출력하지 않는다.
            print(f"PC 서버 시작/종료 오류: {type(error).__name__}")
            return 1


def create_shortcut(state, destination=None, *, port=8765):
    if os.name != "nt":
        raise DesktopError("바탕화면 바로가기는 Windows에서 만들 수 있습니다.")
    import base64
    if destination is None:
        directory = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, directory):
            raise DesktopError("Windows 바탕화면 위치를 찾지 못했습니다.")
        destination = Path(directory.value) / "동구 PC 업무.lnk"
    target = sys.executable
    arguments = ["--state", str(state), "--port", str(port)]
    if not getattr(sys, "frozen", False):
        pythonw = Path(target).with_name("pythonw.exe")
        target = str(pythonw if pythonw.exists() else target)
        arguments.insert(0, str(SOURCE / "desktop.py"))
    quote = lambda text: "'" + str(text).replace("'", "''") + "'"
    script = ("$ErrorActionPreference='Stop'; $s=(New-Object -ComObject WScript.Shell).CreateShortcut("
              + quote(destination) + "); $s.TargetPath=" + quote(target) + "; $s.Arguments="
              + quote(subprocess.list2cmdline(arguments)) + "; $s.WorkingDirectory=" + quote(Path(target).parent)
              + "; $s.Description='동구 PC 업무 실행'; $s.Save()")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                             base64.b64encode(script.encode("utf-16le")).decode("ascii")],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW, timeout=20)
    if result.returncode:
        raise DesktopError("바탕화면 바로가기를 만들지 못했습니다. 폴더 쓰기 권한을 확인하세요.")
    return Path(destination)
