"""실행 앱 수명 관리. GUI와 업무 서버를 분리하고 생성한 자식만 정상 종료한다."""
from __future__ import annotations

import ctypes
import errno
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from dgpc import __version__
from desktop_connection import ConnectionProfile, profile_arguments, probe_connection, probe_url, update_configuration, validate_profile

APP_TITLE = "동구 PC 실행 앱"
SOURCE = Path(__file__).resolve().parent


class DesktopError(RuntimeError):
    def __init__(self, message="", code=None):
        super().__init__(message)
        self.code = code  # 자식 오류 분류(CHILD_FAILURES의 이름) 등. 화면이 분류별로 다르게 안내할 때 쓴다.


class ChildStartError(DesktopError):
    """자식이 서버를 열기 전에 멈춤(접속 설정·시작 정보 등). code는 CHILD_FAILURES의 이름."""

    def __init__(self, code):
        super().__init__(CHILD_FAILURES[code][1], code=code)


# 자식 서버 오류 분류: 이름 → (자식 종료 코드, 부모가 보이는 안내). 안내에는 비밀번호·주민 자료·원시 오류 문구를 넣지 않는다.
# 종료 코드 0(정상 종료)·1(분류 전 예전 자식·잡히지 않은 오류)과 겹치지 않게 20번대를 쓴다.
CHILD_FAILURES = {
    "profile": (20, "접속 설정(HTTPS 인증서·개인키·허용 IP 대역·바인드 주소)이 올바르지 않습니다. ‘접속 설정’에서 확인하세요."),
    "launch_input": (21, "실행 앱이 서버에 시작 정보를 전달하지 못했습니다. 실행 앱을 닫고 다시 실행하세요."),
    "sqlcipher_missing": (22, "SQLCipher 암호화 모듈이 없어 서버를 시작하지 않았습니다. PC 프로그램을 다시 설치하세요(개발 환경은 pc/requirements.txt 설치)."),
    "db_key": (23, "DB 비밀번호가 맞지 않아 자료를 열지 못했습니다. [비밀번호로 다시 열기]로 다시 입력하세요. 자료는 바꾸지 않았습니다."),
    "db_missing": (24, "선택한 자료 폴더에 업무 DB(pc.db)가 없어 새 DB를 만들지 않았습니다. ‘자료 폴더 선택’에서 기존 자료 폴더를 고르세요."),
    "db_newer": (25, "자료 DB가 이 프로그램보다 새 버전입니다. 최신 PC 프로그램으로 여세요. 자료는 바꾸지 않았습니다."),
    "port_in_use": (26, "서버 포트를 다른 프로그램이 이미 사용 중입니다. 그 프로그램을 종료하거나 ‘접속 설정’에서 다른 포트를 고르세요."),
    "port_denied": (27, "이 포트를 열 권한이 없습니다(Windows 예약 포트·보안 프로그램). ‘접속 설정’에서 다른 포트를 고르세요."),
    "bind_address": (28, "서버 바인드 주소가 이 PC의 주소가 아닙니다. ‘접속 설정’의 바인드 주소를 확인하세요."),
    "tls": (29, "HTTPS 인증서·개인키를 읽지 못했습니다. 파일 위치·유효기간·개인키 쌍을 확인하세요."),
    "os_error": (30, "자료 폴더나 파일을 읽고 쓰지 못했습니다. 폴더 권한과 저장공간을 확인하세요."),
    "unexpected": (31, "서버 시작 중 예상하지 못한 오류가 발생했습니다. 오류 기록 폴더를 정보화 담당자에게 전달하세요."),
}
_STORE_EXITS = (("SQLCipher", "sqlcipher_missing"), ("DB를 열 수 없습니다", "db_key"),
                ("PC 자료가 없습니다", "db_missing"), ("DB 구조 버전", "db_newer"))


def classify_child_error(error):
    """자식 오류를 분류 이름으로. 오류 문구는 옮기지 않는다(store가 내는 우리 안내문의 앞부분만 대조)."""
    import ssl
    if isinstance(error, DesktopError) and error.code in CHILD_FAILURES:
        return error.code
    if isinstance(error, SystemExit):
        text = error.code if isinstance(error.code, str) else ""
        return next((code for prefix, code in _STORE_EXITS if text.startswith(prefix)), "unexpected")
    if isinstance(error, ImportError) and "sqlcipher" in (error.name or str(error)).lower():
        return "sqlcipher_missing"
    if isinstance(error, ssl.SSLError):
        return "tls"
    if isinstance(error, OSError):
        number = getattr(error, "winerror", None) or error.errno
        if number in (errno.EADDRINUSE, 10048):
            return "port_in_use"
        if number in (errno.EADDRNOTAVAIL, 10049):
            return "bind_address"
        if number == 10013 or (isinstance(error, PermissionError) and error.filename is None):
            return "port_denied"  # 소켓 권한 오류(파일 권한 오류는 filename이 있다)
        return "os_error"
    return "unexpected"


def child_exit_code(code):
    return CHILD_FAILURES.get(code, CHILD_FAILURES["unexpected"])[0]


def child_failure_code(returncode):
    return next((name for name, (number, _) in CHILD_FAILURES.items() if number == returncode), None)


def child_failure_message(code, logs):
    if code not in CHILD_FAILURES:
        return f"서버를 시작하지 못했습니다. DB 비밀번호와 자료 폴더를 확인하세요. 오류 기록: {logs}"
    return f"서버를 시작하지 못했습니다. {CHILD_FAILURES[code][1]} (오류 분류: {code}) 오류 기록: {logs}"


def child_failure_line(code, error=None):
    """로그 한 줄: 분류·오류 종류·OS 오류 번호만. 비밀번호·원시 요청·주민 자료·경로는 넣지 않는다."""
    detail = type(error).__name__ if error is not None else "-"
    if isinstance(error, OSError) and (getattr(error, "winerror", None) or error.errno):
        detail += f" errno={getattr(error, 'winerror', None) or error.errno}"
    return f"PC 서버 시작/종료 오류 [{code}] {detail}: {CHILD_FAILURES[code][1]}"


def _stderr_stream():
    """windowed(pythonw·PyInstaller --windowed) 자식은 sys.stderr가 None이어도 부모가 준 오류 로그 핸들은 있다."""
    if sys.stderr is not None:
        return sys.stderr
    if os.name != "nt":
        return None
    try:
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel.GetStdHandle.restype = wintypes.HANDLE
        handle = kernel.GetStdHandle(-12 & 0xffffffff)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return None
        return os.fdopen(msvcrt.open_osfhandle(handle, os.O_WRONLY), "w", encoding="utf-8")
    except (OSError, ValueError, AttributeError):
        return None


def report_child_failure(code, error=None):
    """자료 폴더 로그를 열기 전 오류: 부모가 만든 오류 로그(stderr)에 분류만 남긴다. 실패해도 종료 코드는 남는다."""
    stream = _stderr_stream()
    if stream is None:
        return
    try:
        stream.write(child_failure_line(code, error) + "\n")
        stream.flush()
    except (OSError, ValueError):
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
        if isinstance(config, dict) and isinstance(config.get("state"), str) and Path(config["state"]).is_absolute():
            return Path(config["state"])
    except (OSError, ValueError, TypeError):
        pass
    return default_state_dir()


def save_state_choice(state):
    # 저장된 설정을 읽지 못하면 덮어쓰지 않고 ValueError(ConfigurationDamaged)를 낸다.
    update_configuration({"state": str(Path(state).resolve())}, directory=app_data_dir())


def has_saved_configuration(directory=None):
    """첫 설치가 아닌지: 실행 앱 설정 파일이 있으면(손상돼도) 이미 쓰던 PC로 본다."""
    return ((directory or app_data_dir()) / "desktop.json").exists()


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


def worker_command(state, port, interpreter=None, *, connection=None):
    if getattr(sys, "frozen", False) and interpreter is None:
        command = [sys.executable, "--desktop-child"]
    else:
        command = [str(interpreter or sys.executable), str(SOURCE / "desktop.py"), "--desktop-child"]
    arguments = profile_arguments(connection) if connection is not None else ["--port", str(port)]
    return command + ["--state", str(state)] + arguments


def _app_health(url, full=True):
    """Keep legacy health codes while sharing bounded, GET-only checks."""
    code = probe_url(url, assets=full)["code"]
    return code if code in {"ready", "version_mismatch", "assets_unavailable"} else "unrecognized"


def app_ready(url):
    return _app_health(url) == "ready"


class DesktopController:
    def __init__(self, state_dir, port=8765, *, interpreter=None, timeout=45, connection=None):
        self.connection = connection if connection is not None else ConnectionProfile(port=port)
        port = self.connection.port
        if type(port) is not int or not 1 <= port <= 65535:
            raise DesktopError("포트는 1~65535 사이여야 합니다.")
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.port = port
        self.url = self.connection.url
        self.interpreter = interpreter
        self.timeout = timeout
        self.child = None
        self.status = "idle"
        self.created_database = False  # 이번 시작에서 새 DB 생성을 허락받아 만들었는지(첫 설치·사용자 확인)
        self._ready_file = None

    @property
    def owned(self):
        return self.child is not None

    def check_existing(self, *, full=False) -> bool:
        """별도 스레드에서 호출하는 외부 서버 상태 조회. 키·프로세스·상태는 변경하지 않는다.

        주기 점검(full=False)은 /api/state 한 번만, 수동 재연결 확인(full=True)은 업무 화면 파일까지 본다."""
        if self.status != "existing" or self.owned:
            return False
        return self._health(full) == "ready"

    def _health(self, full=True):
        if self.connection.mode == "local":
            return _app_health(self.url, full)
        return probe_connection(self.connection, assets=full)["code"]

    def start(self, password_provider, *, on_progress=None):
        if self.child is not None:
            raise DesktopError("이 실행 앱의 서버가 이미 시작 중이거나 실행 중입니다.")
        progress = on_progress or (lambda phase: None)
        self.status = "starting"
        progress("checking")
        try:
            validate_profile(self.connection)
        except ValueError as error:
            self.status = "error"
            raise DesktopError(str(error)) from None
        try:
            bind = self.connection.host if self.connection.mode == "lan" and self.connection.host != "0.0.0.0" else "127.0.0.1"
            with socket.create_connection((bind, self.port), timeout=0.5):
                occupied = True
        except OSError:
            occupied = False
        if occupied:
            health = self._health()
            if health == "ready":
                self.status = "existing"
                progress("existing")
                return self.status
            self.status = "error"
            if health == "version_mismatch":
                raise DesktopError(f"실행 중인 동구 PC 서버의 버전이 이 실행 앱({__version__})과 다르거나 확인되지 않습니다. 기존 서버를 해당 실행 앱에서 종료한 뒤 다시 실행하세요. 기존 서버는 종료하지 않았고 새 서버도 시작하지 않았습니다.")
            if health == "assets_unavailable":
                raise DesktopError("서버는 실행 중이나 업무 화면 파일을 제공하지 못합니다. 기존 서버를 종료한 뒤 다시 시도하세요. 기존 서버는 종료하지 않았고 새 서버도 시작하지 않았습니다.")
            if health == "tls_error":
                raise DesktopError("기존 HTTPS 서버의 인증서 신뢰·유효기간·접속 이름을 확인하세요. 기존 서버는 종료하지 않았습니다.")
            raise DesktopError(f"포트 {self.port}를 다른 프로그램이 사용 중이거나 동구 서버가 준비되지 않았습니다. 해당 프로그램은 종료하지 않았습니다.")
        progress("unlocking")
        # new=True를 받은 provider가 키를 돌려주면 새 DB 생성을 허락한 것이다(첫 설치 또는 사용자 확인).
        # provider가 DesktopError로 거절하면 폴더·자식·DB를 만들지 않는다.
        new = not (self.state_dir / "pc.db").exists()
        try:
            key = password_provider(new)
        except DesktopError:
            self.status = "error"
            raise
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
                self.child = subprocess.Popen(worker_command(self.state_dir, self.port, self.interpreter,
                                                             connection=self.connection),
                                              stdin=subprocess.PIPE, stdout=output, stderr=error,
                                              env=environment, cwd=SOURCE,
                                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                self.child.stdin.write((json.dumps({"key": key, "launch_id": launch_id, "create": new}) + "\n").encode("utf-8"))
                self.child.stdin.flush()
            except OSError as error:
                self.status = "error"
                raise DesktopError("서버 자식을 시작하지 못했습니다. 실행 앱 파일을 확인하세요.") from error
            finally:
                key = None
        deadline = time.monotonic() + self.timeout
        progress("waiting")
        while time.monotonic() < deadline:
            returncode = self.child.poll()
            if returncode is not None:
                self.child.stdin.close()
                self.child = None
                self.status = "error"
                code = child_failure_code(returncode)
                raise DesktopError(child_failure_message(code, logs), code=code)
            if self._ready_file.is_file() and self._health() == "ready":
                self.created_database = new
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


def child_main(state, port, *, connection=None):
    """자식 서버. 실패는 분류 코드(CHILD_FAILURES)의 종료 코드로 끝내고 부모가 그 코드로 안내를 고른다.

    자료 폴더 로그를 열기 전 실패(접속 설정·시작 정보)는 부모가 만든 오류 로그에 분류를 남기고 ChildStartError를 낸다.
    접속 설정이 틀리면 키를 읽거나 자료 폴더를 만들기 전에 멈춘다."""
    profile = connection if connection is not None else ConnectionProfile(port=port)
    try:
        validate_profile(profile)
    except ValueError:
        report_child_failure("profile")
        raise ChildStartError("profile") from None
    port = profile.port
    try:
        incoming = _stdin_pipe()
        first = json.loads(incoming.readline())
        if not isinstance(first, dict):
            raise ValueError("start message shape")
    except (OSError, ValueError, DesktopError) as error:
        report_child_failure("launch_input", error)
        raise ChildStartError("launch_input") from None
    key = first.get("key")
    launch_id = first.get("launch_id")
    # 예전 형식(키만)은 기존처럼 첫 실행 생성 허용. 실행 앱 부모는 늘 create를 명시한다.
    create = first.get("create", True)
    if not isinstance(key, str) or not key or not isinstance(create, bool):
        report_child_failure("launch_input")
        raise ChildStartError("launch_input")
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
                raise ChildStartError("sqlcipher_missing")
            # 부모에서 선택한 키를 이 자식의 메모리에서만 사용한다. 콘솔 입력은 호출하지 않는다.
            store.db_key = lambda create=False: key
            store._keyring_get = lambda: None
            if not (state / "pc.db").exists():
                if not create:
                    # 설정이 저장된 PC에서 DB가 없는 폴더: 부모 화면의 확인 없이는 빈 새 DB를 만들지 않는다.
                    raise ChildStartError("db_missing")
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

            try:
                lan = server.lan_config(profile.cert, profile.key, profile.allow, profile.hostname) if profile.mode == "lan" else None
            except ValueError:
                raise ChildStartError("profile") from None
            from desktop_osrm import bundled_osrm, bundled_pack_dir
            with bundled_osrm(bundled_pack_dir(), state):
                if lan:
                    server.serve(port, lan=lan, host=profile.host, on_ready=ready)
                else:
                    server.serve(port, on_ready=ready)
            return 0
        except BaseException as error:
            # 비밀번호·원시 요청·주민 자료·원시 오류 문구는 출력하지 않는다. 분류·오류 종류·OS 오류 번호만 남긴다.
            code = classify_child_error(error)
            print(child_failure_line(code, error))
            return child_exit_code(code)


def shortcut_arguments(*, port=8765, connection=None):
    """바로가기 인자. 자료 폴더(--state)는 고정하지 않는다: 바로가기는 늘 저장된 자료 위치를 따른다.
    (고정하면 폴더를 옮긴 뒤 옛 경로가 명시 인자로 우선해 빈 새 DB가 만들어질 수 있었다.)"""
    return profile_arguments(connection) if connection is not None else ["--port", str(port)]


def create_shortcut(state, destination=None, *, port=8765, connection=None):
    """state는 예전 호출 호환용이며 바로가기에 넣지 않는다. 호출자가 미리 save_state_choice로 저장해 둔다."""
    if os.name != "nt":
        raise DesktopError("바탕화면 바로가기는 Windows에서 만들 수 있습니다.")
    import base64
    if destination is None:
        directory = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, directory):
            raise DesktopError("Windows 바탕화면 위치를 찾지 못했습니다.")
        destination = Path(directory.value) / "동구 PC 업무.lnk"
    target = sys.executable
    arguments = shortcut_arguments(port=port, connection=connection)
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
