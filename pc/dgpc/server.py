"""PC 업무 화면 서버. 이 PC 안(127.0.0.1)에서만 열리며 휴대폰이나 외부에서 접속하는 서버가 아니다.

보호
- 127.0.0.1에만 바인딩, Host 헤더가 127.0.0.1/localhost가 아니면 거부(DNS 리바인딩 방지)
- 업무 비밀번호 로그인(scrypt), 세션 쿠키(HttpOnly, SameSite=Strict), 30분 무조작 시 만료
- 모든 API 요청에 사용자 지정 헤더(X-DG-CSRF) 필수 → 다른 웹사이트가 이 PC의 API를 부를 수 없음
- 응답에 CSP·nosniff·no-referrer·no-store
- 브라우저가 직접 여는 외부 연결은 바탕 지도 조각뿐(개인정보 미포함). v0.11부터 이 서버가 설정된 경우에만 대신 묻는다:
  행안부 도로명주소 API(검색어·건물 식별 번호만), 카카오모빌리티 길찾기(좌표만) — external.py. 서버 로그에는 검색어가 남지 않는다
  (경로 틀만 남기고 조건(query)은 버림).
"""
from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import html as _h
import ipaddress
import json
import hashlib
import logging
import logging.handlers
import os
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Callable

import contextlib
import queue

from . import area as A, backup as BK, dong_work as DW, external as X, finance as FN, geo, ops as OPS, privacy as PV, routing, service as S, store, users as U
from .paths import resource_root
from . import numbered_labels as NL, label_auto as LA, official_forms as OF, operation_print as OP, pc_collection as PC, reports as RP, route_depots as DP, vworld as VW

ROOT = resource_root()
UI = ROOT / "pc" / "ui"
WEB = ROOT / "web"
WEB_ALLOWED = {"fees.js", "fees-data.js", "fee-calculator.js", "fee-calculator.css", "labels-data.js", "map.js", "proto.js", "nav.js", "vendor/jsQR.js", "vendor/maplibre/maplibre-gl.mjs", "vendor/maplibre/maplibre-gl-worker.mjs",
               "vendor/maplibre/maplibre-gl.css"}
UI_FILES = {"/": "index.html", "/index.html": "index.html", "/start.js": "start.js", "/pc.js": "pc.js", "/core.js": "core.js", "/widgets.js": "widgets.js", "/date_filters.js": "date_filters.js", "/pc.css": "pc.css",
            # 화면 모듈(pc/ui/screens/*.js): 폴더에 있는 파일만, 이름 그대로
            **{f"/screens/{f.name}": f"screens/{f.name}" for f in sorted((UI / "screens").glob("*.js"))}, "/print.css": "print.css", "/print.js": "print.js", "/doc.css": "doc.css",
            # 화면 글꼴: Pretendard GOV(범정부 디자인 시스템 권장, SIL OFL) 일부 글자 묶음. 이 PC 안에서만 내려줌(외부 글꼴 서버 없음)
            **{f"/fonts/PretendardGOV-{w}.subset.woff2": f"fonts/PretendardGOV-{w}.subset.woff2" for w in ("Regular", "SemiBold", "Bold")}}
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".woff2": "font/woff2"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob: https://tiles.openfreemap.org; "
       "connect-src 'self' https://tiles.openfreemap.org; worker-src 'self' blob:; media-src 'self' blob: mediastream:; object-src 'none'; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")
RAW_CSP = ("default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; "
           "frame-ancestors 'none'")
# 세션 무조작 만료(30분). 자동 시험에서만 DG_SESSION_IDLE_S 로 줄일 수 있다(60초 이상).
IDLE_S = max(60, int(os.environ.get("DG_SESSION_IDLE_S") or 30 * 60))
# 세션 절대 만료(조작이 이어져도 로그인 뒤 10시간). 시험에서만 DG_SESSION_ABS_S 로 줄일 수 있다(60초 이상).
ABS_S = max(60, int(os.environ.get("DG_SESSION_ABS_S") or 10 * 3600))
MAX_BODY = 40 * 1024 * 1024
# 화면 창 주소(/bundle/·/print/batch/·/qrtest/)의 짧은 ID: 화면이 UUID 앞 8자리로 만든다. LIKE 패턴(_ %)이 섞이지 않게 형식을 고정
SHORT_ID_RE = re.compile(r"[0-9a-f]{8}")
SLOW_MS = 1000  # 이보다 오래 걸린 요청은 WARNING 으로 남긴다(경로 틀·ms만)

LOCK = threading.Lock()
CON = None
SESSIONS: dict[str, dict] = {}
FAILS: dict[str, dict] = {}  # 아이디별 로그인 실패 {"n": 횟수, "until": 잠금 끝 시각, "at": 마지막 실패}
PRINTS: dict[str, dict] = {}  # 고른 접수 인쇄: 잠깐 쓰는 표(주소에 접수 ID를 길게 싣지 않게)
PORT = 8765
LAN: dict | None = None  # LAN 모드 설정 (기본 꺼짐): {"hostname", "nets", "cert", "key"}
LOG = logging.getLogger("dgpc")
IP_FAILS: dict[str, dict] = {}  # LAN 모드: 접속 PC(IP)별 로그인 실패 {"times": [...], "until": 잠금 끝}
FAIL_AGG: dict[tuple[str, str], int] = {}  # 로그인 실패 변경 이력은 분마다 모아 한 줄: (분, 아이디 표시) → 횟수
RPOOL: queue.LifoQueue = queue.LifoQueue()  # 조회 전용 DB 연결(WAL 읽기, 잠금 없음)
RPOOL_MAX = 4
STATE = {"scheduler_tick": None, "started_at": None}
IP_FAIL_MAX, IP_FAIL_WINDOW, IP_BLOCK_S = 20, 600, 600


def login_ip(client_ip: str) -> str | None:
    """접속 위치별 로그인 제한에 쓸 IP. LAN 모드에서만(다른 PC가 각자 IP로 들어옴). 이 PC에서만 여는 기본 방식은
    모든 사용자가 127.0.0.1 한 곳이라, 위치별 제한을 걸면 누군가 20번 틀릴 때 이 PC의 모든 로그인이 막힌다 — 그래서 None(제한 없음).
    아이디별 제한(login_blocked)은 두 방식 모두 그대로."""
    return client_ip if LAN else None


def ip_blocked(ip: str | None) -> int:
    """LAN 모드에서 이 PC(IP)가 잠겨 있으면 남은 초. ip 가 None(기본 방식)이거나 LAN 모드가 아니면 0."""
    if not LAN or not ip:
        return 0
    f = IP_FAILS.get(ip)
    return max(0, int(f["until"] - time.time()) + 1) if f and f["until"] > time.time() else 0


def ip_failed(ip: str | None):
    """LAN 모드: 한 PC에서 10분에 20번 넘게 틀리면(아이디를 바꿔 가며 시도) 그 PC를 10분 잠근다. 기본 방식(ip None)은 세지 않는다."""
    if not LAN or not ip:
        return
    now_ = time.time()
    if len(IP_FAILS) > 1000:
        for k in [k for k, v in IP_FAILS.items() if v["until"] < now_ and (not v["times"] or now_ - v["times"][-1] > IP_FAIL_WINDOW)]:
            IP_FAILS.pop(k, None)
    f = IP_FAILS.setdefault(ip, {"times": [], "until": 0.0})
    f["times"] = [t for t in f["times"] if now_ - t < IP_FAIL_WINDOW] + [now_]
    if len(f["times"]) >= IP_FAIL_MAX:
        f["until"] = now_ + IP_BLOCK_S


def note_login_fail(who: str, at: float | None = None):
    m = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(at or time.time()))
    FAIL_AGG[(m, who)] = FAIL_AGG.get((m, who), 0) + 1


def flush_login_fails(con, force: bool = False) -> int:
    """지난 분의 로그인 실패를 아이디마다 한 줄(횟수)로 변경 이력에 남긴다(실패마다 한 줄씩 쌓지 않음). 잠금은 부르는 쪽."""
    cur = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())
    keys = [k for k in list(FAIL_AGG) if force or k[0] < cur]
    if not keys:
        return 0
    with S.tx(con):
        for m, who in keys:
            n = FAIL_AGG.pop((m, who), 0)
            tok = S.ACTOR.set(who)
            try:
                S.audit(con, "user", "-", "LOGIN_FAIL", {"count": n, "minute": m})
            finally:
                S.ACTOR.reset(tok)
    return len(keys)


@contextlib.contextmanager
def read_con():
    """조회 전용 연결 하나 빌리기(없으면 새로 연다). 이 연결에서 생긴 기록(접속기록·이력)은 S.WRITE_SINK로 잠금 안에서 쓴다."""
    try:
        rc = RPOOL.get_nowait()
    except queue.Empty:
        rc = store.connect(readonly=True)
    tok = S.WRITE_SINK.set(_locked_write)
    try:
        yield rc
    finally:
        S.WRITE_SINK.reset(tok)
        if rc.in_transaction:
            try:
                rc.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
        if RPOOL.qsize() < RPOOL_MAX:
            RPOOL.put(rc)
        else:
            rc.close()


def _locked_write(fn):
    with LOCK:
        return fn(CON)


def _is_ro_error(e: Exception) -> bool:
    t = str(e).lower()
    return "readonly" in t or "read-only" in t or "attempt to write" in t


def _fail_key(username: str) -> str:
    return (username or "").strip().lower()[:30] or "?"


def login_blocked(username: str) -> int:
    """이 아이디가 잠겨 있으면 남은 초, 아니면 0. (다른 아이디의 실패는 영향 없음)"""
    f = FAILS.get(_fail_key(username))
    return max(0, int(f["until"] - time.time()) + 1) if f and f["until"] > time.time() else 0


def login_failed(username: str):
    """5번째 실패부터 30초·60초·120초… (최대 30분) 잠근다."""
    now_ = time.time()
    if len(FAILS) > 1000:  # 아무 아이디나 넣어 메모리를 채우지 못하게 오래된 기록 정리
        for k in [k for k, v in FAILS.items() if now_ - v["at"] > 3600]:
            FAILS.pop(k, None)
    f = FAILS.setdefault(_fail_key(username), {"n": 0, "until": 0.0, "at": now_})
    f["n"] += 1
    f["at"] = now_
    if f["n"] >= 5:
        f["until"] = now_ + min(30 * 2 ** (f["n"] - 5), 1800)


def login_succeeded(username: str):
    FAILS.pop(_fail_key(username), None)


FAIL_LOCK = threading.Lock()  # FAILS·IP_FAILS·LOGIN_INFLIGHT 를 함께 보고 고치는 동안
LOGIN_INFLIGHT: set[str] = set()  # 지금 비밀번호를 확인하는 중인 아이디(·LAN 모드 IP)


@contextlib.contextmanager
def login_attempt(username: str, ip: str | None):
    """로그인 한 번의 잠금 검사 → 비밀번호 확인 → 결과 기록을 같은 아이디(LAN 모드는 같은 IP도)에 대해 한 번에 하나씩.
    비밀번호 확인(scrypt)이 끝나기 전에 실패 횟수를 세므로, 이렇게 막지 않으면 동시에 수십 번 보내 잠금 검사를 모두 통과할 수 있었다.
    같은 아이디로 확인 중이면 세지 않고 바로 거절한다(사람은 동시에 두 번 로그인하지 않음). yield 받은 함수로 결과를 기록한다."""
    keys = {"u:" + _fail_key(username)} | ({"ip:" + ip} if LAN and ip else set())
    with FAIL_LOCK:
        wait = login_blocked(username) or ip_blocked(ip)
        if wait:
            raise S.UserError(f"여러 번 틀렸습니다. {wait}초 뒤 다시 시도하세요.")
        if keys & LOGIN_INFLIGHT:
            raise S.UserError("같은 아이디로 로그인을 확인하는 중입니다. 잠시 뒤 다시 시도하세요.")
        LOGIN_INFLIGHT.update(keys)

    def record(good: bool):
        with FAIL_LOCK:  # 다음 시도가 들어오기 전에(자리를 비우기 전에) 결과를 센다
            if good:
                login_succeeded(username)
            else:
                login_failed(username)
                ip_failed(ip)
    try:
        yield record
    finally:
        with FAIL_LOCK:
            LOGIN_INFLIGHT.difference_update(keys)


# ------------------------------------------------------------------ 역할과 API 경로 표
# 역할 묶음 (운영 관리자는 모든 경로를 쓴다)
#  INTAKE(접수 담당자): 접수·배정·전송·결과·충돌·수납·마감 기록
#  FIELD(수거팀): 조회(자기 팀 범위), 결과·사진 가져오기, 하차 기록, QR 실측
#  LEAD(팀장·검토자): 모든 조회(변경 이력 포함) + 마감 확인·환불 결재. 사용자·설정 관리와 업무 변경은 못 함
ALL = frozenset({"ADMIN", "INTAKE", "FIELD", "LEAD"})
VIEW = frozenset({"ADMIN", "INTAKE", "LEAD"})     # 사무실 조회 (수거팀 제외)
WORK = frozenset({"ADMIN", "INTAKE"})             # 업무 변경
CREW = frozenset({"ADMIN", "INTAKE", "FIELD"})    # 현장 결과·하차·QR 실측
REVIEW = frozenset({"ADMIN", "LEAD"})             # 검토·결재·이력·개인정보 점검
ADMIN = frozenset({"ADMIN"})
SELF = ALL | {"DONG"}  # 자기 정보만 명시적으로 허용. 기존 전역 역할 집합은 확대하지 않는다.
LOCKS = {"read": "조회 전용 연결(잠금 없음, 쓰기 필요 시 잠금 잡고 다시)", "write": "DB 잠금 + 쓰기 연결", "none": "처리 함수가 스스로(잠금 밖 검증·계산, 반영만 잠금)"}


@dataclasses.dataclass(frozen=True)
class Route:
    method: str
    pattern: str                 # "requests/{id}/cancel": {id} 는 '/' 없는 한 마디
    roles: frozenset
    lock: str                    # LOCKS 키
    fn: Callable
    must_change_ok: bool         # 임시 비밀번호 상태(비밀번호 변경 전)에도 쓸 수 있는지
    access: str                  # 접속기록 동작(privacy.ACCESS_ACTIONS 키). 빈 값 = 경로에서는 남기지 않음
    access_fn: Callable | None   # fn(handler, out, qs, *params) -> (대상, 건수, 메모) | None
    regex: re.Pattern


ROUTES: list[Route] = []


def endpoint(method: str, pattern: str, roles, lock: str | None = None, must_change_ok: bool = False, access=None):
    """API 경로 등록. 정의한 순서대로 맞춰 본다(먼저 쓴 것이 이김). lock 을 안 주면 GET=read, POST=write.
    access=(동작, fn): 응답 보내기 전에 접속기록(건수·메모만, 내용 없음)."""
    lock = lock or ("read" if method == "GET" else "write")
    assert lock in LOCKS, lock
    rx = re.compile("^" + "/".join("([^/]*)" if seg == "{id}" else re.escape(seg) for seg in pattern.split("/")) + "$")

    def deco(fn):
        assert not any(r.method == method and r.pattern == pattern for r in ROUTES), (method, pattern)
        ROUTES.append(Route(method, pattern, frozenset(roles), lock, fn, must_change_ok, access[0] if access else "", access[1] if access else None, rx))
        return fn
    return deco


def match(method: str, p: str):
    for r in ROUTES:
        if r.method == method:
            m = r.regex.match(p)
            if m:
                return r, m.groups()
    return None, ()


def allowed(role: str, method: str, p: str) -> bool:
    """경로 표의 roles 로 판단. 표에 없는 주소는 관리자, 또는 같은 방법·같은 첫 마디의 경로를 쓸 수 있는 역할만 통과(→ 404 없음)."""
    r, _ = match(method, p)
    if r is not None:
        return role in r.roles
    if role == "ADMIN":
        return True
    head = p.split("/")[0]
    return any(role in x.roles for x in ROUTES if x.method == method and x.pattern.split("/")[0] == head)


@dataclasses.dataclass
class Raw:
    """처리 함수가 돌려주는 JSON 아닌 응답(파일 내려받기·글·사진) 또는 상태 코드가 있는 JSON."""
    body: bytes | None = None
    ctype: str = ""
    status: int = 200
    headers: dict | None = None
    json: object = None


def csv_file(body: bytes, filename: str) -> Raw:
    return Raw(body, "text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _file_component(value: str) -> str:
    """다운로드/ZIP의 이름 한 부분. 동 이름을 디렉터리 경로로 해석하지 않는다."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f\ud800-\udfff\ufffe\uffff]', '_', value)
    name = re.sub(r'\.{2,}', '_', name).strip(' .')[:80].rstrip(' .')
    return name or '행정동'


def json_status(obj, status: int) -> Raw:
    return Raw(json=obj, status=status)


# ------------------------------------------------------------------ 서버 로그 (state/logs/server.log, 5MB × 10개 돌려 쓰기)
# 남기는 것: 방법·경로 틀(ID 가림)·상태 코드·걸린 시간·사용자 아이디. 남기지 않는 것: 주소 뒤 조건(query)·본문·이름·연락처·주소.
_PHONE_RE = re.compile(r"(?<!\d)0\d{1,2}[-\s.]?\d{3,4}[-\s.]?\d{4}(?!\d)")
_ADDR_RE = re.compile(r"[가-힣A-Za-z0-9·.]+(?:대로|로|길)\s*\d+(?:\s*-\s*\d+)?(?:\s*번?길\s*\d+)?")


def scrub(text: str) -> str:
    """로그에 남길 글에서 전화번호·도로명 주소처럼 보이는 값을 가린다."""
    return _ADDR_RE.sub("<주소>", _PHONE_RE.sub("<전화>", text or ""))


def path_template(path: str) -> str:
    out = []
    for seg in path.split("?")[0].split("/"):
        if (len(seg) >= 6 and re.search(r"\d", seg)) or len(seg) >= 20:
            out.append(":id")
        else:
            out.append(seg)
    return "/".join(out)


def setup_logging(state: Path | None = None) -> Path:
    d = (state or store.state_dir()) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "server.log"
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) and getattr(h, "baseFilename", "") == str(f) for h in LOG.handlers):
        h = logging.handlers.RotatingFileHandler(f, maxBytes=5 * 1024 * 1024, backupCount=10, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOG.addHandler(h)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False  # 콘솔에는 접근 기록을 내지 않는다
    return f


# ------------------------------------------------------------------ LAN 모드 (기본 꺼짐, 여러 접수 PC용)
def lan_config(cert: str | None, key: str | None, allow: str | None, hostname: str | None = None) -> dict:
    """LAN 모드 설정 검사. HTTPS 인증서·키, 허용 IP 대역은 반드시 있어야 한다."""
    if not cert or not key:
        raise ValueError("LAN 모드는 HTTPS 전용입니다. --cert 인증서와 --key 개인키 파일을 지정하세요.")
    if not allow:
        raise ValueError("LAN 모드는 접속을 허용할 PC의 IP 대역(--allow 10.0.0.0/24,…)을 지정해야 합니다.")
    for f in (cert, key):
        if not Path(f).is_file():
            raise ValueError(f"파일이 없습니다: {f}")
    nets = []
    for part in allow.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = ipaddress.ip_network(part, strict=False)
        except ValueError:
            raise ValueError(f"허용 IP 대역 형식이 올바르지 않습니다: {part}")
        if n.prefixlen == 0:
            raise ValueError("모든 주소(0.0.0.0/0) 허용은 쓸 수 없습니다. 업무 PC가 있는 대역만 지정하세요.")
        nets.append(n)
    if not nets:
        raise ValueError("허용 IP 대역이 비어 있습니다.")
    return {"cert": cert, "key": key, "nets": nets, "hostname": (hostname or socket.gethostname()).lower()}


def ip_allowed(ip: str, nets) -> bool:
    try:
        a = ipaddress.ip_address((ip or "").split("%")[0])
    except ValueError:
        return False
    if a.is_loopback:
        return True  # 서버 PC 자신
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return any(a in n for n in nets)


class Handler(BaseHTTPRequestHandler):
    server_version = "dgpc"
    sys_version = ""
    # 소켓 읽기·쓰기 한 번마다 시간 제한(초). 보내다 멈춘 접속·응답을 받지 않는 접속이 작업 스레드를 끝없이 붙잡지 않게
    # (한 번의 읽기·쓰기 기준이라 큰 파일도 조금씩이라도 오가면 끊기지 않는다. 쉬는 keep-alive 연결은 이 시간 뒤 닫힘)
    timeout = 60

    def setup(self):
        # 서버가 접속에 이미 더 짧은 시간 제한을 걸었으면(실행 앱 DesktopServer: 빨리 끝내기 위한 짧은 제한) 그것을 지킨다.
        # StreamRequestHandler.setup 은 위 timeout 으로 덮어쓰므로 미리 읽어 두었다가 둘 중 짧은 쪽으로 다시 건다.
        pre = self.request.gettimeout()
        super().setup()
        if pre is not None and (self.timeout is None or pre < self.timeout):
            self.connection.settimeout(pre)

    def log_message(self, fmt, *args):  # 콘솔 접근 로그는 남기지 않는다(파일 로그는 route()에서 ID를 가려 남김)
        pass

    def send_response(self, code, message=None):
        self._status = code
        super().send_response(code, message)

    # ---------------------------------------------------------- 공통
    def _headers(self, status, ctype, length, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("X-DG-Version", __import__("dgpc").__version__)
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def send_json(self, obj, status=200, extra=None):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self._headers(status, "application/json; charset=utf-8", len(body), extra)
        self.wfile.write(body)

    def send_bytes(self, body: bytes, ctype: str, status=200, extra=None):
        self._headers(status, ctype, len(body), extra)
        self.wfile.write(body)

    def host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        ok_ = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        if LAN:
            ok_ |= {f"{LAN['hostname']}:{PORT}"}
        return host in ok_

    def cookie_flags(self) -> str:
        return "; Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if LAN else "")

    def session(self) -> str | None:
        m = re.search(r"(?:^|;\s*)dg_s=([A-Za-z0-9_-]+)", self.headers.get("Cookie") or "")
        if not m:
            return None
        t = m.group(1)
        sess = SESSIONS.get(t)
        if not sess or sess["exp"] < time.time() or time.time() - sess.get("created", 0) > ABS_S:
            SESSIONS.pop(t, None)
            return None
        sess["exp"] = time.time() + IDLE_S
        return t

    def body(self) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise S.UserError("요청 길이가 올바르지 않습니다.")
        if n < 0:  # 음수면 연결이 끊길 때까지 읽어 크기 제한을 넘는다
            raise S.UserError("요청 길이가 올바르지 않습니다.")
        if n > MAX_BODY:
            raise S.UserError("파일이 너무 큽니다.")
        if getattr(self, "body_cache", None) is None:
            self.body_cache = self.rfile.read(n) if n else b""
        return self.body_cache

    def json_body(self) -> dict:
        if getattr(self, "json_body_cache", None) is not None:
            return self.json_body_cache
        b = self.body()
        self.json_body_cache = json.loads(b.decode() or "{}") if b else {}
        return self.json_body_cache

    def crew_scope(self):
        """수거팀 계정이면 연결된 팀 ID(없으면 빈 문자열 → 아무것도 안 보임), 그 외 역할은 None."""
        u = self.user or {}
        if u.get("role") != "FIELD":
            return None
        return u.get("crew_id") or ""

    def require_crew_request(self, rid: str):
        cs = self.crew_scope()
        if cs is not None and not (cs and S.request_in_crew(self.c, rid, cs)):
            raise S.UserError("내 팀에 배정된 접수만 볼 수 있습니다.")

    def me(self) -> dict:
        u = self.user
        return {"username": u["username"], "display_name": u["display_name"], "role": u["role"], "role_label": S.ROLES[u["role"]], "crew_id": u.get("crew_id"),
                "must_change": bool(u.get("must_change")), "prefs": S.user_prefs(self.c, u["user_id"]),
                "dong_ids": U.user_dong_ids(self.c, u["user_id"]) if u["role"] == "DONG" else []}

    def _refresh_session(self, token, con):
        """세션 토큰은 유지하고 현재 DB의 계정 사용·역할을 읽는다. 자료 범위는 전용 서비스가 매번 검사한다."""
        sess = SESSIONS.get(token or "")
        current = S.one(con, "SELECT username,display_name,role,active,must_change,crew_id FROM app_user WHERE id=?", (sess["user_id"],)) if sess else None
        if not current or not current["active"]:
            SESSIONS.pop(token, None)
            return None
        sess.update({k: current[k] for k in ("username", "display_name", "role", "must_change", "crew_id")})
        return sess

    # ---------------------------------------------------------- 진입점
    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        t0 = time.time()
        self._status = 0
        try:
            self._route(method)
        finally:
            u = (self.user or {}).get("username") or "-"
            ms = int((time.time() - t0) * 1000)
            LOG.info("%s %s %s %dms user=%s client=%s", method, path_template(self.path), self._status, ms, u,
                     self.client_address[0] if LAN else "local")
            if ms > SLOW_MS:
                LOG.warning("느린 요청 %s %s %dms", method, path_template(self.path), ms)

    def _route(self, method):
        self.json_body_cache = None
        self.body_cache = None
        self.user = None
        self.c = None
        S.ACTOR.set("system")
        PV.CLIENT.set(self.client_address[0] if LAN else "local")
        if LAN and not ip_allowed(self.client_address[0], LAN["nets"]):
            return self.send_json({"error": "허용되지 않은 PC입니다."}, 403)
        if not self.host_ok():
            return self.send_json({"error": "허용되지 않은 주소입니다."}, 403)
        url = urllib.parse.urlsplit(self.path)
        path, qs = url.path, dict(urllib.parse.parse_qsl(url.query))
        try:
            if method == "GET" and path in UI_FILES:
                f = UI / UI_FILES[path]
                return self.send_bytes(f.read_bytes(), TYPES[f.suffix])
            if method == "GET" and path.startswith("/web/"):
                rel = path[5:]
                if rel not in WEB_ALLOWED:
                    return self.send_json({"error": "없음"}, 404)
                f = WEB / rel
                return self.send_bytes(f.read_bytes(), TYPES[f.suffix])
            if path.startswith("/api/"):
                if self.headers.get("X-DG-CSRF") != "1":
                    return self.send_json({"error": "잘못된 요청입니다."}, 403)
                return self.api(method, path[5:], qs)
            if method == "GET" and path.startswith(("/bundle/", "/print/", "/qrtest/")):
                token = self.session()
                with read_con() as rc:
                    sess = self._refresh_session(token, rc)
                if not sess:
                    return self.send_json({"error": "로그인이 필요합니다."}, 401)
                if sess["role"] not in ALL:
                    return self.send_json({"error": "이 출력 작업은 권한이 없습니다."}, 403)
                if sess.get("must_change"):
                    return self.send_json({"error": "비밀번호를 먼저 바꿔 주세요.", "must_change": True}, 403)
            if method == "GET" and path.startswith(("/bundle/", "/print/batch/", "/qrtest/")):
                # 화면용 창(목록 QR·배차표·QR 실측): API와 같게 로그인·임시 비밀번호 변경을 확인하고, 주소는 화면이 만드는 8자리 16진수만
                sess = SESSIONS.get(self.session() or "")
                if not sess:
                    return self.send_json({"error": "로그인이 필요합니다."}, 401)
                if sess.get("must_change"):
                    return self.send_json({"error": "비밀번호를 먼저 바꿔 주세요.", "must_change": True}, 403)
                if not SHORT_ID_RE.fullmatch(path.rsplit("/", 1)[1]):
                    return self.send_json({"error": "없음"}, 404)
            if method == "GET" and path.startswith("/bundle/"):
                return self.bundle_page(path[8:])
            if method == "GET" and path.startswith("/print/batch/"):
                sess = SESSIONS.get(self.session() or "")
                if not sess:  # 위에서 확인한 사이에 로그아웃·만료 정리로 사라짐
                    return self.send_json({"error": "로그인이 필요합니다."}, 401)
                S.ACTOR.set(sess["username"])
                with LOCK:
                    if sess["role"] == "FIELD":
                        b = S.one(CON, "SELECT crew_id FROM assignment_batch WHERE id LIKE ?", (path[13:] + "%",))
                        if not b or b["crew_id"] != sess.get("crew_id"):
                            return self.send_json({"error": "내 팀의 배차표만 볼 수 있습니다."}, 403)
                    return self.print_page(path[13:])
            if method == "GET" and path.startswith("/print/requests/"):
                sess = SESSIONS.get(self.session() or "")
                if not sess:
                    return self.send_json({"error": "로그인이 필요합니다."}, 401)
                job = PRINTS.pop(path[16:], None)
                if not job or job["exp"] < time.time() or job["user"] != sess["user_id"]:
                    return self.send_json({"error": "인쇄 자료가 없거나 시간이 지났습니다. 목록에서 다시 인쇄하세요."}, 404)
                return self.print_requests_page(job["data"])
            if method == "GET" and path.startswith("/print/doc/"):
                sess = SESSIONS.get(self.session() or "")
                if not sess:
                    return self.send_json({"error": "로그인이 필요합니다."}, 401)
                job = PRINTS.pop(path[11:], None)
                if not job or job["exp"] < time.time() or job["user"] != sess["user_id"]:
                    return self.send_json({"error": "인쇄 자료가 없거나 시간이 지났습니다. 화면에서 다시 인쇄하세요."}, 404)
                return self.send_bytes(job["html"].encode(), "text/html; charset=utf-8")
            if method == "GET" and path.startswith("/qrtest/"):
                return self.qrtest_page(path[8:])
            return self.send_json({"error": "없음"}, 404)
        except S.StaleError as e:  # 다른 곳에서 먼저 바뀜(낙관적 동시성) → 409 + 지금 버전
            return self.send_json({"error": str(e), "stale": True, **e.info}, 409)
        except DW.ScopeError as e:
            return self.send_json({"error": str(e)}, 403)
        except S.UserError as e:
            return self.send_json({"error": str(e)}, 400)
        except Exception:  # 오류 내용에 개인정보가 섞일 수 있어 화면에는 일반 문구만, 로그에는 전화·주소 모양 값을 가린 추적 기록
            tb = scrub(traceback.format_exc())
            LOG.error("오류 %s %s\n%s", method, path_template(self.path), tb)
            sys.stderr.write(tb)
            return self.send_json({"error": "처리 중 오류가 발생했습니다."}, 500)

    # ---------------------------------------------------------- API
    def api(self, method, p, qs):
        if p == "state" and method == "GET":
            with read_con() as rc:
                need = not S.users_exist(rc)
            return self.send_json({"setup_needed": need, "logged_in": bool(self.session())})
        if p == "setup" and method == "POST":
            d = self.json_body()
            with LOCK:
                if S.users_exist(CON):
                    raise S.UserError("이미 설정되어 있습니다.")
                S.ACTOR.set(d.get("username") or "admin")
                S.create_user(CON, d.get("username") or "", d.get("display_name") or "운영 관리자", "ADMIN", d.get("password") or "")
                u = S.check_login(CON, d.get("username") or "", d.get("password") or "")
            return self.login_ok(u)
        if p == "login" and method == "POST":
            return self.login()
        tok = self.session()
        if not tok:
            return self.send_json({"error": "로그인이 필요합니다.", "login": True}, 401)
        if p == "logout" and method == "POST":
            SESSIONS.pop(tok, None)
            return self.send_json({"ok": True}, extra={"Set-Cookie": "dg_s=; Max-Age=0" + self.cookie_flags()})
        sess = SESSIONS.get(tok)
        if not sess:  # session() 확인과 이 사이에 다른 요청(로그아웃·비밀번호 재설정·만료 정리)이 지움
            return self.send_json({"error": "로그인이 필요합니다.", "login": True}, 401)
        r, params = match(method, p)
        if not allowed(sess["role"], method, p):
            return self.send_json({"error": "이 작업은 권한이 없습니다."}, 403)
        with read_con() as rc:
            sess = self._refresh_session(tok, rc)
        if not sess:
            return self.send_json({"error": "로그인이 필요합니다.", "login": True}, 401)
        if not allowed(sess["role"], method, p):
            return self.send_json({"error": "이 작업은 권한이 없습니다."}, 403)
        if sess.get("must_change") and not (r is not None and r.must_change_ok):
            return self.send_json({"error": "비밀번호를 먼저 바꿔 주세요.", "must_change": True}, 403)
        self.user = sess
        S.ACTOR.set(sess["username"])
        if r is None:
            return self.send_json({"error": "없음"}, 404)
        return self.dispatch(r, params, qs)

    def login(self):
        """scrypt는 잠금 밖에서, 계정 변경 확인·최신 권한 조회·세션 생성은 같은 잠금 안에서."""
        d = self.json_body()
        ip = login_ip(self.client_address[0])
        evidence = {}
        with login_attempt(d.get("username") or "", ip) as record:
            with read_con() as rc:
                good = S.verify_login(rc, d.get("username") or "", d.get("password") or "", evidence=evidence)
                un = (d.get("username") or "").strip()[:30]
                known = bool(un and S.one(rc, "SELECT 1 AS x FROM app_user WHERE username=?", (un,)))
            with LOCK:
                if good:
                    good = U.current_login(CON, good["id"], evidence.get("credential", ""))
                record(bool(good))
                if good:
                    S.ACTOR.set(good["username"])
                    with S.tx(CON):
                        CON.execute("UPDATE app_user SET last_login_at=? WHERE id=?", (S.now(), good["id"]))
                        S.audit(CON, "user", good["id"], "LOGIN", None)
                else:
                    # 아이디 칸에 비밀번호를 잘못 넣는 일이 있어, 있는 아이디일 때만 그대로 적고 아니면 가린다. 분마다 모아 한 줄로 남긴다.
                    note_login_fail(un if known else "(없는 아이디)")
                flush_login_fails(CON)
                if good:
                    return self.login_ok(good)
        if not good:
            raise S.UserError("아이디 또는 비밀번호가 맞지 않습니다.")

    def idem(self, endpoint: str, fn):
        """client_req_id가 있으면 같은 요청을 한 번만 처리(24시간 안 다시 오면 그때 응답). 잠금 안에서 부른다."""
        d = self.json_body()
        key = d.get("client_req_id") if isinstance(d, dict) else None
        return S.idempotent(self.c, self.user["username"], endpoint, key, fn)

    def job(self, kind: str, **kw) -> dict | Raw:
        with LOCK:
            error = self._write_session_error()
            if error:
                return error
            jid = OPS.JOBS.submit(kind, OPS.job_fn(kind, CON, LOCK, lambda: store.connect(readonly=True), **kw), self.user["username"],
                                  self.client_address[0] if LAN else "local")
        return {"job_id": jid, "status": "QUEUED", "kind": kind}

    def login_ok(self, u):
        t = secrets.token_urlsafe(32)
        SESSIONS[t] = {"exp": time.time() + IDLE_S, "created": time.time(), "user_id": u["id"], "username": u["username"], "role": u["role"],
                       "display_name": u["display_name"], "must_change": u.get("must_change", False), "crew_id": u.get("crew_id")}
        return self.send_json({"ok": True}, extra={"Set-Cookie": f"dg_s={t}" + self.cookie_flags()})

    # ---------------------------------------------------------- API 처리 함수 (경로 표: endpoint)
    # 처리 함수는 fn(self, qs, *경로 마디) → dict/list(JSON 200) | Raw(파일·상태 코드) | None(이미 보냄).
    # self.c: read → 조회 전용 연결, write → 쓰기 연결(잠금 안), none → None(함수가 read_con()/LOCK 을 스스로).
    def _write_session_error(self) -> Raw | None:
        """실제 반영 직전 LOCK 안에서 현재 세션·권한을 다시 확인한다. 응답은 호출자가 잠금 밖에서 보낸다."""
        sess = SESSIONS.get(self.session() or "")
        self.user = sess
        S.ACTOR.set(sess["username"] if sess else "system")
        r = self._dispatch_route
        if not sess:
            return json_status({"error": "로그인이 필요합니다.", "login": True}, 401)
        if sess["role"] not in r.roles:
            return json_status({"error": "이 작업은 권한이 없습니다."}, 403)
        if sess.get("must_change") and not r.must_change_ok:
            return json_status({"error": "비밀번호를 먼저 바꿔 주세요.", "must_change": True}, 403)
        sess = self._refresh_session(self.session(), CON)
        self.user = sess
        S.ACTOR.set(sess["username"] if sess else "system")
        if not sess:
            return json_status({"error": "로그인이 필요합니다.", "login": True}, 401)
        if sess["role"] not in r.roles:
            return json_status({"error": "이 작업은 권한이 없습니다."}, 403)
        if sess.get("must_change") and not r.must_change_ok:
            return json_status({"error": "비밀번호를 먼저 바꿔 주세요.", "must_change": True}, 403)
        return None

    def dispatch(self, r: Route, params: tuple, qs: dict):
        self._dispatch_route = r
        if r.lock == "read":
            # 조회는 DB 잠금 없이 조회 전용 연결(WAL)로. 접속기록·이력은 잠금 안에서 쓰기 연결로 넘어간다.
            try:
                with read_con() as rc:
                    self.c = rc
                    return self.reply(r, r.fn(self, qs, *params), qs, params)
            except Exception as e:  # noqa: BLE001 — 조회 중 쓰기가 필요했던 경우(드묾): 잠금 잡고 쓰기 연결로 다시
                if not _is_ro_error(e):
                    raise
            with LOCK:
                self.c = CON
                out = r.fn(self, qs, *params)
                self.log_access(r, out, qs, params)
            return self.send_out(out)
        if r.lock == "write":
            # 요청 본문은 DB 잠금을 잡기 전에 다 읽고, 응답은 잠금을 놓은 뒤 보낸다: 느리거나 멈춘 접속 하나가
            # 잠금을 붙잡아 다른 사람의 저장이 모두 멈추지 않게
            self.body()
            with LOCK:
                out = self._write_session_error()
                if out is None:
                    self.c = CON
                    out = r.fn(self, qs, *params)
                    self.log_access(r, out, qs, params)
            return self.send_out(out)
        return self.reply(r, r.fn(self, qs, *params), qs, params)

    def reply(self, r: Route, out, qs: dict, params: tuple):
        self.log_access(r, out, qs, params)
        return self.send_out(out)

    def log_access(self, r: Route, out, qs: dict, params: tuple):
        """접속기록(있으면). 쓰기 경로에서는 잠금 안에서 부른다."""
        if r.access_fn is not None:
            rec = r.access_fn(self, out, qs, *params)
            if rec:
                if self.c is not None:
                    PV.record_access(self.c, r.access, *rec)
                else:
                    with read_con() as rc:
                        PV.record_access(rc, r.access, *rec)

    def send_out(self, out):
        if out is None:
            return None
        if isinstance(out, Raw):
            if out.body is None:
                return self.send_json(out.json, out.status)
            return self.send_bytes(out.body, out.ctype, out.status, out.headers)
        return self.send_json(out)

    # ---- 조회 (GET)
    @endpoint("GET", "bootstrap", SELF, lock="write", must_change_ok=True)
    def ep_bootstrap(self, qs):
        """화면 시작 자료: 설정·팀·기기·오늘·나·수수료표·동·코드 목록."""
        c = self.c
        if self.user["role"] == "DONG":
            u = DW.actor(c, self.user)
            return {"settings": DW.safe_settings(c), "crews": DW.safe_crews(c), "devices": [], "today": S.today_kst(), "me": self.me(),
                    "fee": S.fee_table_for_phone(c), "dongs": DW.safe_dongs(c, u), "channels": S.CHANNELS,
                    "cancel_codes": S.CANCEL_CODES, "postpone_codes": S.POSTPONE_CODES, "contact_kinds": S.CONTACT_KINDS,
                    "session_idle_s": IDLE_S, "roles": {"DONG": S.ROLES["DONG"]}, "audit_entities": {}, "addr_count": 0,
                    "version": __import__("dgpc").__version__, "quick_filters": [], "reduction_round_units": list(S.ROUND_UNITS), "api": 13}
        return {"settings": S.settings(c), "crews": S.list_crews(c), "devices": S.list_devices(c),
                "today": S.today_kst(), "me": self.me(), "fee": S.fee_table_for_phone(c),
                "dongs": A.list_dongs(c), "channels": S.CHANNELS, "cancel_codes": S.CANCEL_CODES, "postpone_codes": S.POSTPONE_CODES,
                "contact_kinds": S.CONTACT_KINDS, "session_idle_s": IDLE_S, "roles": S.ROLES, "audit_entities": S.AUDIT_ENTITIES,
                "addr_count": S.addr_stats(c)["count"], "version": __import__("dgpc").__version__,
                "quick_filters": list(S.QUICK), "reduction_round_units": list(S.ROUND_UNITS), "api": 13}

    # 동별 조회는 현재 범위 확인과 원문 읽기를 같은 짧은 잠금으로 묶는다.
    # 기존 조회 전용 autocommit 연결에서 검사 뒤 동 이관·권한 변경이 끼어드는 것을 막는다.
    @endpoint("GET", "dong-work", DW.READ_ROLES, lock="write")
    def ep_dong_work(self, qs):
        """현재 허용된 동의 기준일 현황과 안전한 배정 정보."""
        return DW.overview(self.c, self.user, qs.get("date"), qs.get("dong") or "all")

    @endpoint("GET", "dong-work/requests", DW.READ_ROLES, lock="write",
              access=("LIST", lambda h, out, qs: ("", len(out["rows"]), "동별 접수 목록")))
    def ep_dong_work_requests(self, qs):
        """현재 범위·기준일·처리 종류로 50건씩 접수 조회."""
        return DW.search(self.c, self.user, qs)

    @endpoint("GET", "dong-work/requests/{id}", DW.READ_ROLES, lock="write",
              access=("VIEW", lambda h, out, qs, rid: (rid, 1, "동별 접수 상세")))
    def ep_dong_work_request(self, qs, rid):
        """다른 동 접수의 원문을 포함하지 않는 상세 권한 검사."""
        return DW.detail(self.c, self.user, rid)

    @endpoint("GET", "dong-work/duplicates", DW.READ_ROLES, lock="write",
              access=("SEARCH", lambda h, out, qs: ("", len(out), "동별 중복 후보") if out else None))
    def ep_dong_work_duplicates(self, qs):
        """허용 범위 안의 접수만 중복 후보로 제공."""
        return DW.duplicates(self.c, self.user, qs)

    @endpoint("GET", "dong-work/geocode", DW.READ_ROLES, lock="write")
    def ep_dong_work_geocode(self, qs):
        """선택 동을 검사한 뒤 공개 주소 자료의 위치 찾기(외부 호출 없음)."""
        return DW.geocode(self.c, self.user, qs)

    @endpoint("GET", "dong-work/assignments", DW.READ_ROLES, lock="write")
    def ep_dong_work_assignments(self, qs):
        """선택 범위의 배정만 표시하고 혼합 배정은 shared로 안내."""
        return DW.assignments(self.c, self.user, qs.get("date"), qs.get("dong") or "all")

    @endpoint("GET", "dong-work/summary.csv", DW.READ_ROLES, lock="write")
    def ep_dong_work_summary_csv(self, qs):
        """주민 정보 없는 동별 현황 CSV."""
        return csv_file(DW.summary_csv(self.c, self.user, qs.get("date"), qs.get("dong") or "all"), "dong-summary.csv")

    @endpoint("POST", "dong-work/requests", DW.WRITE_ROLES)
    def ep_dong_work_create(self, qs):
        """현재 담당 동으로 접수 등록(멱등 재시도도 최신 범위 검사)."""
        return DW.create(self.c, self.user, self.json_body())

    @endpoint("POST", "dong-work/requests/{id}", DW.WRITE_ROLES)
    def ep_dong_work_update(self, qs, rid):
        """원래 동과 이동할 동을 모두 검사한 접수 수정."""
        return DW.update(self.c, self.user, rid, self.json_body())

    @endpoint("POST", "dong-work/requests/{id}/cancel", DW.WRITE_ROLES)
    def ep_dong_work_cancel(self, qs, rid):
        """담당 접수만 버전·사유 코드 계약으로 취소."""
        return DW.cancel(self.c, self.user, rid, self.json_body())

    @endpoint('POST', 'dong-work/requests/{id}/collection', DW.WRITE_ROLES)
    def ep_dong_collection(self, qs, rid):
        """현재 담당 동 접수의 PC 직접 수거 입력(멱등 재시도 전에도 범위 검사)."""
        return DW.collect(self.c, self.user, rid, self.json_body())

    @endpoint("POST", "dong-work/route", DW.WRITE_ROLES, lock="none")
    def ep_dong_work_route(self, qs):
        """준비만 잠금, 노선 계산은 잠금 밖, 반환 직전 최신 범위 재검사."""
        data = self.json_body()
        with LOCK:
            error = self._write_session_error()
            if error:
                return error
            prepared = DW.prepare_route(CON, self.user, data)
        result = DW.calculate(prepared)
        with LOCK:
            error = self._write_session_error()
            if error:
                return error
            DW.validate_route_result(CON, self.user, prepared)
            if prepared["method"] == "KAKAO":
                mi = result.get("matrix_info") or {}
                with S.tx(CON):
                    S.audit(CON, "settings", "kakao", "ROUTE_MATRIX", {"calls": mi.get("api_calls", 0), "estimated": mi.get("fallback_pairs", 0)})
            PV.record_access(CON, "LIST", "", len(result["requests"]), "동별 노선 계산")
        return result

    @endpoint("POST", "dong-work/assign", DW.WRITE_ROLES)
    def ep_dong_work_assign(self, qs):
        """최신 계정·동·예정일·계산 기준·혼합 배정을 검사한 명시적 확정."""
        return DW.assign(self.c, self.user, self.json_body())

    @endpoint("GET", "calendar", VIEW)
    def ep_calendar(self, qs):
        """수거 가능일 달력(수용량·휴무일)."""
        start = qs.get("start") or S.today_kst()
        dong = qs.get("dong") or None
        ex = qs.get("exclude") or None
        out = {"days": A.calendar(self.c, start, min(31, int(qs.get("days") or 14)), dong, ex),
               "earliest": A.earliest_available(self.c, start, dong, ex)}
        if qs.get("date"):
            out["warnings"] = A.capacity_warnings(self.c, qs["date"], dong, ex)
        return out

    @endpoint("GET", "dong-detect", VIEW)
    def ep_dong_detect(self, qs):
        """주소로 행정동 찾기 + 주소 경고."""
        return {**A.detect_dong(self.c, qs.get("address", "")), "warnings": S.address_warnings(self.c, qs.get("address", ""))}

    @endpoint("GET", "dongs", VIEW)
    def ep_dongs(self, qs):
        """행정동 목록."""
        return A.list_dongs(self.c)

    @endpoint("GET", "holidays", VIEW)
    def ep_holidays(self, qs):
        """휴무일 목록."""
        return A.list_holidays(self.c, qs.get("start", ""), qs.get("end", ""))

    @endpoint("GET", "requests/import-template", ALL)
    def ep_import_template(self, qs):
        """접수 일괄 등록 CSV 서식."""
        return csv_file(S.import_template(), "donggu-intake-template.csv")

    @endpoint("GET", "audit.csv", REVIEW)
    def ep_audit_csv(self, qs):
        """변경 이력 CSV."""
        return csv_file(S.audit_csv(self.c, qs), "donggu-audit.csv")

    @endpoint("GET", "stats", VIEW)
    def ep_stats(self, qs):
        """통계."""
        return S.stats(self.c, qs.get("start", ""), qs.get("end", ""), qs.get("basis") or "planned")

    @endpoint("GET", "reports/daily", VIEW)
    def ep_daily_report(self, qs):
        """일일 작업일지 집계(원자료 변경 없음)."""
        return RP.daily_report(self.c, qs.get("date", ""), qs.get("basis") or "planned")

    @endpoint("GET", "reports/monthly", VIEW)
    def ep_monthly_report(self, qs):
        """월보 집계(원자료 변경 없음)."""
        return RP.monthly_report(self.c, qs.get("month", ""), qs.get("basis") or "planned")

    @endpoint("GET", "reports/daily.csv", VIEW, access=("EXPORT", lambda h, out, qs: ("", 0, "일일 작업일지 집계 CSV")))
    def ep_daily_report_csv(self, qs):
        """개인정보 없는 일일 작업일지 전체 집계 CSV."""
        return csv_file(RP.daily_report_csv(self.c, qs.get("date", ""), qs.get("basis") or "planned"), "donggu-daily-report.csv")

    @endpoint("GET", "reports/monthly.csv", VIEW, access=("EXPORT", lambda h, out, qs: ("", 0, "월보 집계 CSV")))
    def ep_monthly_report_csv(self, qs):
        """개인정보 없는 월보 전체 집계 CSV."""
        return csv_file(RP.monthly_report_csv(self.c, qs.get("month", ""), qs.get("basis") or "planned"), "donggu-monthly-report.csv")

    @endpoint("GET", "label-usage", VIEW)
    def ep_label_usage(self, qs):
        """수거 수량 기준 배출표 사용 장수 자동 집계(전체 기간 누적)."""
        return NL.usage_page(self.c, qs)

    @endpoint("GET", "reports/forms", VIEW)
    def ep_official_forms(self, qs):
        """동별 원본 서식의 실제 수거·재고 집계 미리보기(개인정보 제외)."""
        return OF.preview(self.c, qs.get("dong_id"), qs.get("date"))

    @endpoint("POST", "reports/forms/download", WORK)
    def ep_official_forms_download(self, qs):
        """개인정보를 포함한 원본 XLSX 월보·ODT 일일 대장 내려받기."""
        from io import BytesIO
        from zipfile import ZipFile, ZIP_DEFLATED
        d = self.json_body()
        kind = d.get("format")
        if kind not in ("xlsx", "odt", "zip"):
            raise S.UserError("내려받을 파일 형식을 골라 주세요.")
        # 일자를 먼저 검사하고, 월보는 선택일이 속한 달 전체로 작성한다.
        daily = OF.daily_data(self.c, d.get("dong_id"), d.get("date"))
        dong_name = _file_component(daily['dong'])
        monthly = OF.monthly_data(self.c, d.get("dong_id"), daily["end"][:7]) if kind != "odt" else None
        files, ids = [], set()
        if monthly is not None:
            files.append((f"{monthly['period']}_{dong_name}_대형폐기물_월보.xlsx", OF.xlsx_file(monthly)))
            ids.update(monthly["request_ids"])
        if kind != "xlsx":
            files.append((f"{daily['period']}_{dong_name}_신고처리_배출표수불대장.odt", OF.odt_file(daily)))
            ids.update(daily["request_ids"])
        if kind == "zip":
            output = BytesIO()
            with ZipFile(output, "w", ZIP_DEFLATED) as archive:
                for name, body in files:
                    archive.writestr(name, body)
            filename, body = f"{daily['period']}_{dong_name}_업무서식.zip", output.getvalue()
        else:
            filename, body = files[0]
        PV.record_access(self.c, "EXPORT", "", len(ids), f"원본서식 {kind} dong_id={daily['dong_id']} date={daily['end']}")
        mime = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "odt": "application/vnd.oasis.opendocument.text", "zip": "application/zip"}[kind]
        return Raw(body, mime, headers={"Content-Disposition": f"attachment; filename=\"donggu-forms.{kind}\"; filename*=UTF-8''{urllib.parse.quote(filename, safe='')}"})

    @endpoint("GET", "label-auto", VIEW)
    def ep_label_auto(self, qs):
        """동별 배출표 자동 배정·부족 장수와 재고 조회."""
        dong_id = qs.get("dong_id") or None
        if dong_id:
            NL._stock_dong(self.c, dong_id)
        return LA.summary(self.c, dong_id)

    @endpoint("POST", "label-auto/sync", WORK)
    def ep_label_auto_sync(self, qs):
        """확정 수거량을 동별 입고 번호에 자동 배정."""
        data = self.json_body()
        dong_id = data.get("dong_id") or None
        if dong_id:
            NL._stock_dong(self.c, dong_id)
        return self.idem("label-auto/sync", lambda: LA.sync(self.c, dong_id=dong_id))

    @endpoint("POST", "numbered-labels/stock-dong", WORK)
    def ep_numbered_label_stock_dong(self, qs):
        """기존 번호 구간의 재고 동·수령일 지정."""
        d = self.json_body()
        return self.idem("numbered-labels/stock-dong", lambda: NL.assign_stock_dong(self.c, d.get("start"), d.get("end"), d.get("dong_id"), d.get("received_on")))

    @endpoint("GET", "numbered-labels", VIEW)
    def ep_numbered_labels(self, qs):
        """숫자 배출표 재고 원장."""
        return NL.label_page(self.c, qs)

    @endpoint("GET", "numbered-labels/{id}/history", VIEW)
    def ep_numbered_label_history(self, qs, number):
        """숫자 배출표 최근 변경 이력."""
        return NL.label_history(self.c, number)

    @endpoint("POST", "numbered-labels/receive", WORK)
    def ep_numbered_label_receive(self, qs):
        """숫자 배출표 구간 입고."""
        d = self.json_body()
        return self.idem("numbered-labels/receive", lambda: NL.receive_range(self.c, d.get("start"), d.get("end"), dong_id=d.get("dong_id"), received_on=d.get("received_on")))

    @endpoint("POST", "numbered-labels/distribute", WORK)
    def ep_numbered_label_distribute(self, qs):
        """숫자 배출표 구간 배부."""
        d = self.json_body()
        return self.idem("numbered-labels/distribute", lambda: NL.distribute_range(self.c, d.get("start"), d.get("end"), d.get("destination"), dong_id=d.get("dong_id"), distributed_on=d.get("distributed_on")))

    @endpoint("POST", "numbered-labels/{id}/use", WORK)
    def ep_numbered_label_use(self, qs, number):
        """유효 접수·품목에 숫자 배출표 사용."""
        d = self.json_body()
        return self.idem(f"numbered-labels/{number}/use", lambda: NL.use_number(self.c, number, d.get("request_id"), d.get("line_id")))

    @endpoint("POST", "numbered-labels/{id}/void", WORK)
    def ep_numbered_label_void(self, qs, number):
        """사유를 남기고 숫자 배출표 무효."""
        d = self.json_body()
        return self.idem(f"numbered-labels/{number}/void", lambda: NL.void_number(self.c, number, d.get("reason")))

    @endpoint("POST", "numbered-labels/{id}/print", VIEW)
    def ep_numbered_label_print(self, qs, number):
        """번호 확인용 표지 출력. 배출표 상태는 변경하지 않는다."""
        from pc_tool import qr_png_data_uri
        label = NL.label_history(self.c, number)["label"]
        qr = qr_png_data_uri(label["number"], "M")[0]
        body = OP.numbered_label_body(label, qr)
        with S.tx(self.c):
            S.audit(self.c, "numbered_label", label["number_key"], "PRINT_NUMBERED_LABEL", {"number": label["number"]})
        return {"token": self.put_print(self._doc("배출표 번호 " + label["number"], body, "labels"))}

    @endpoint("GET", "pilot", VIEW)
    def ep_pilot(self, qs):
        """시범 운영 비교 설정·보고."""
        ps = S.pilot_settings(self.c)
        st_, en_ = qs.get("start") or ps["start"], qs.get("end") or ps["end"]
        return {"settings": ps, "base_keys": S.PILOT_BASE,
                "report": S.pilot_report(self.c, st_, en_, qs.get("crew")) if st_ and en_ and st_ <= en_ else None}

    @endpoint("GET", "pilot.csv", VIEW)
    def ep_pilot_csv(self, qs):
        """시범 운영 비교 CSV."""
        return csv_file(S.pilot_csv(self.c, qs.get("start") or None, qs.get("end") or None, qs.get("crew")), "donggu-pilot.csv")

    @endpoint("GET", "stats.csv", VIEW)
    def ep_stats_csv(self, qs):
        """통계 CSV(종류별)."""
        kind = qs.get("kind") if qs.get("kind") in ("days", "dongs", "items", "categories", "months", "reductions", "channels") else "requests"
        body = S.stats_csv(self.c, qs.get("start", ""), qs.get("end", ""), kind, qs.get("basis") or "planned")
        return csv_file(body, f'donggu-{kind}-{qs.get("start")}_{qs.get("end")}.csv')

    @endpoint("GET", "geocode", VIEW)
    def ep_geocode(self, qs):
        """주소 자료로 좌표 찾기."""
        return S.geocode(self.c, qs.get("address", "")) or {"none": True}

    @endpoint("GET", "addr-search", VIEW)
    def ep_addr_search(self, qs):
        """PC 주소 자료 검색."""
        return S.addr_search(self.c, qs.get("q", ""))

    @endpoint("GET", "juso-search", WORK)
    def ep_juso_search(self, qs):
        """행안부 주소 검색(서버가 대신 물음). 검색어는 로그·접속기록에 남기지 않는다."""
        return S.juso_search(self.c, qs.get("q", ""), qs.get("name", ""))

    @endpoint("GET", "juso-coord", WORK)
    def ep_juso_coord(self, qs):
        """행안부 검색 결과의 출입구 좌표: 이 PC 주소 위치 자료 먼저(외부 전송 없음), 없고 좌표 승인키가 있으면 행안부 좌표 API."""
        return S.juso_coord(self.c, {k: qs.get(k, "") for k in (*X.REF_RE, *X.REF_LOCAL)})

    @endpoint("GET", "privacy", REVIEW)
    def ep_privacy(self, qs):
        """개인정보 파기 미리보기·이력·보유기간·백업 상태."""
        c = self.c
        return {"preview": PV.purge_preview(c), "log": PV.purge_log(c), "retention_years": PV.retention_years(c),
                "access_retention_years": PV.access_retention_years(c), "backup": BK.status(c)}

    @endpoint("GET", "access", REVIEW)
    def ep_access(self, qs):
        """접속기록 조회."""
        return PV.access_page(self.c, qs)

    @endpoint("GET", "access.csv", REVIEW)
    def ep_access_csv(self, qs):
        """접속기록 CSV."""
        return csv_file(PV.access_csv(self.c, qs), "donggu-access.csv")

    @endpoint("GET", "access/status", REVIEW)
    def ep_access_status(self, qs):
        """접속기록 월 점검 상태."""
        return {**PV.check_status(self.c), "checks": PV.list_checks(self.c)}

    @endpoint("GET", "integrity", REVIEW)
    def ep_integrity(self, qs):
        """무결성 빠른 검사(검사점부터 새 행만). 전체 검사는 POST integrity/full (백그라운드 작업)."""
        res = PV.verify_all(self.c, full=False)
        S.audit(self.c, "access", "verify", "VERIFY", {"ok": res["ok"], "incremental": True})
        return res

    @endpoint("GET", "jobs/{id}", ALL)
    def ep_job(self, qs, jid):
        """백그라운드 작업 상태(본인 작업만, 관리자는 전부)."""
        j = OPS.JOBS.get(jid)
        if not j or (j["actor"] != self.user["username"] and self.user["role"] != "ADMIN"):
            return json_status({"error": "작업을 찾을 수 없습니다."}, 404)
        return j

    @endpoint("GET", "imports/recent", ALL)
    def ep_imports_recent(self, qs):
        """최근 가져오기(수거팀은 자기 팀 휴대폰만)."""
        return OPS.recent_imports(self.c, int(qs.get("limit") or 30), self.crew_scope())

    @endpoint("GET", "imports/{id}/field-review", VIEW)
    def ep_import_field_review(self, qs, iid):
        """가져온 현장 접수·추가 품목 확인(사무실 조회 전용, 개인정보 본문 제외)."""
        result = OPS.import_field_review(self.c, iid)
        return result if result is not None else json_status({"error": "결과 가져오기를 찾을 수 없습니다."}, 404)

    @endpoint("GET", "pending", VIEW)
    def ep_pending(self, qs):
        """처리할 일(대시보드)."""
        return OPS.pending(self.c)

    @endpoint("GET", "readiness", VIEW)
    def ep_readiness(self, qs):
        """운영 준비 점검."""
        return OPS.readiness(self.c)

    @endpoint("GET", "ops-status", VIEW)
    def ep_ops_status(self, qs):
        """예약 작업 마지막 실행."""
        return OPS.ops_status(self.c)

    @endpoint("GET", "diag", ADMIN)
    def ep_diag(self, qs):
        """진단(DB 크기·표 건수·예약 작업)."""
        return {**OPS.diag(self.c), "scheduler_tick": STATE["scheduler_tick"], "started_at": STATE["started_at"],
                "sessions": len(SESSIONS), "jobs_queued": OPS.JOBS.q.qsize()}

    @endpoint("GET", "assign/current", VIEW)
    def ep_assign_current(self, qs):
        """그날 확정 배정."""
        return S.current_assignment(self.c, qs.get("date") or S.today_kst())

    @endpoint("GET", "requests/{id}/notice", ALL)
    def ep_request_notice(self, qs, rid):
        """주민 안내 글(이름·연락처·주소 없음)."""
        self.require_crew_request(rid)
        return Raw(S.notice_text(self.c, rid).encode("utf-8"), "text/plain; charset=utf-8")

    @endpoint("GET", "backup-status", REVIEW)
    def ep_backup_status(self, qs):
        """자동 백업 상태."""
        return BK.status(self.c)

    @endpoint("GET", "revenue", VIEW)
    def ep_revenue(self, qs):
        """세입 일계표·월계표."""
        return FN.revenue(self.c, qs.get("start", ""), qs.get("end", ""), qs.get("unit", "day"))

    @endpoint("GET", "revenue.csv", VIEW)
    def ep_revenue_csv(self, qs):
        """세입 CSV."""
        unit, kind = qs.get("unit", "day"), qs.get("kind", "method")
        return csv_file(FN.revenue_csv(self.c, qs.get("start", ""), qs.get("end", ""), unit, kind), f"donggu-revenue-{kind}.csv")

    @endpoint("GET", "recon-template", VIEW)
    def ep_recon_template(self, qs):
        """대사(납부 자료) CSV 서식."""
        return csv_file(FN.recon_template(), "donggu-recon-template.csv")

    @endpoint("GET", "recon-history", VIEW)
    def ep_recon_history(self, qs):
        """대사 가져오기 이력."""
        return FN.recon_history(self.c)

    @endpoint("GET", "refunds", VIEW)
    def ep_refunds(self, qs):
        """환불 목록."""
        return FN.list_refunds(self.c, qs.get("status", ""))

    @endpoint("GET", "addr-stats", REVIEW)
    def ep_addr_stats(self, qs):
        """주소 자료 건수."""
        return S.addr_stats(self.c)

    @endpoint("GET", "fee-table", ALL)
    def ep_fee_table(self, qs):
        """운영 수수료표 상태·검토 행·개정 목록."""
        return {"state": S.fee_state(self.c), "review_rows": S.review_rows(), "revisions": S.list_fee_revisions(self.c),
                "source": {"label": S.FEE_TABLE["label"], "checked_on": S.FEE_TABLE["source_checked_on"], "rows": len(S.FEE_ROWS)}}

    @endpoint("GET", "me", SELF, lock="write", must_change_ok=True)
    def ep_me(self, qs):
        """나(사용자·역할·화면 설정)."""
        return self.me()

    @endpoint("GET", "users", REVIEW)
    def ep_users(self, qs):
        """사용자 목록."""
        return S.list_users(self.c)

    @endpoint("GET", "backups", ADMIN)
    def ep_backups(self, qs):
        """백업 파일 목록."""
        return {"dir": str(S.backup_dir(self.c)), "files": S.list_backups(self.c)}

    @endpoint("GET", "photo-purge", ADMIN)
    def ep_photo_purge_preview(self, qs):
        """사진 정리 대상 미리보기."""
        cand = S.purge_candidates(self.c)
        return {"count": len(cand), "bytes": sum(x["bytes"] or 0 for x in cand), "days": S.get_setting(self.c, "photo_retention_days")}

    @endpoint("GET", "audit", REVIEW)
    def ep_audit(self, qs):
        """변경 이력 조회."""
        return S.audit_page(self.c, qs)

    @endpoint("GET", "requests", ALL,
              access=("LIST", lambda h, out, qs: ("", len(out["rows"]), f"전체 {out['total']}건 · " + PV.filter_summary(qs))))  # 건수·조건만 (내용 없음)
    def ep_requests(self, qs):
        """접수 검색(수거팀 계정: 자기 팀에 배정된 접수만)."""
        scope = S.crew_request_ids(self.c, self.crew_scope()) if self.crew_scope() is not None else None
        return S.search_requests(self.c, qs, scope)

    @endpoint("GET", "requests/{id}", ALL, access=("VIEW", lambda h, out, qs, rid: (rid, 1, "접수 상세")))
    def ep_request(self, qs, rid):
        """접수 상세."""
        self.require_crew_request(rid)
        return S.get_request(self.c, rid)

    @endpoint("GET", "duplicates", VIEW, access=("SEARCH", lambda h, out, qs: ("", len(out), "중복 후보 조회") if out else None))
    def ep_duplicates(self, qs):
        """중복 후보(전화·주소)."""
        return S.find_duplicates(self.c, qs.get("phone", ""), qs.get("address", ""), qs.get("exclude") or None)

    @endpoint("GET", "batches", ALL, access=("LIST", lambda h, out, qs: (
        "", sum(b.get("count") or len(b.get("stops") or []) for b in out), f"배정 목록 {qs['date']}") if out else None))  # 이름·주소 포함: 건수만
    def ep_batches(self, qs):
        """그날 배정 목록(수거팀은 자기 팀만)."""
        out = S.list_batches(self.c, qs["date"])
        if self.crew_scope() is not None:
            out = [b for b in out if b["crew_id"] == self.crew_scope()]
        return out

    @endpoint("GET", "conflicts", VIEW, access=("LIST", lambda h, out, qs: ("", len(out), "충돌함·현장 요청") if out else None))
    def ep_conflicts(self, qs):
        """충돌함(현장 접수·정정 요청 원문 = 개인정보 포함)."""
        return S.list_conflicts(self.c)

    @endpoint("GET", "field-intakes", VIEW, access=("SEARCH", lambda h, out, qs: ("", len(out), "현장 접수 확인 목록") if out else None))
    def ep_field_intakes(self, qs):
        """현장 접수 확인 목록."""
        return S.field_intakes(self.c)

    @endpoint("GET", "close", ALL, access=("LIST", lambda h, out, qs: (
        "", len(out.get("no_result") or []) + len(out.get("revisit") or []), f"마감 점검 {qs['date']}")))
    def ep_close(self, qs):
        """마감 점검 요약(수거팀은 자기 팀 범위)."""
        return S.day_summary(self.c, qs["date"], self.crew_scope())

    @endpoint("GET", "registration-qr", ADMIN)
    def ep_registration_qr(self, qs):
        """기기 등록 QR 이미지."""
        from pc_tool import qr_png_data_uri
        return {"image": qr_png_data_uri(S.registration_text(self.c), "M")[0]}

    @endpoint("GET", "unloads", ALL)
    def ep_unloads(self, qs):
        """그날 하차 기록(수거팀은 자기 팀만)."""
        return [u for u in S.list_unloads(self.c, qs["date"]) if self.crew_scope() is None or u["crew_id"] == self.crew_scope()]

    @endpoint("GET", "volumes", VIEW)
    def ep_volumes(self, qs):
        """자주 쓴 품목의 부피·무게 추정과 보정값."""
        ov = S.volume_overrides(self.c)
        ids = list(dict.fromkeys(S.fee_usage(self.c, 20) + list(ov)))
        rows = []
        for i in ids:
            if i not in S.FEE_ROWS:
                continue
            fr = S.FEE_ROWS[i]
            one = {"name": fr["name"], "spec": fr["spec"], "qty": 1}
            rows.append({"fee_rate_id": i, "name": fr["name"], "spec": fr["spec"], "unit_label": fr["unit_label"],
                         "estimate": routing.volume_of(one), "kg": routing.weight_of(one),
                         "tags": [routing.TAG_LABEL[t] for t in routing.tags_of([one])], "override": ov.get(i)})
        return rows

    @endpoint("GET", "load-check", VIEW)
    def ep_load_check(self, qs):
        """적재 점검(기간)."""
        return S.load_check(self.c, qs.get("start") or "", qs.get("end") or "")

    @endpoint("GET", "fee-usage", ALL)
    def ep_fee_usage(self, qs):
        """자주 쓴 품목."""
        return S.fee_usage(self.c)

    @endpoint("GET", "qrtests", ALL)
    def ep_qrtests(self, qs):
        """QR 실측 목록."""
        return S.list_qr_tests(self.c)

    @endpoint("GET", "photo/{id}", ALL, access=("PHOTO", lambda h, out, qs, pid: (S.photo_request(h.c, pid) or "", 1, "현장 사진") if out.body else None))
    def ep_photo(self, qs, pid):
        """현장 사진(수거팀은 자기 팀 접수만)."""
        self.require_crew_request(S.photo_request(self.c, pid) or "")
        b = S.photo_bytes(self.c, pid)
        return Raw(b, "image/jpeg") if b else json_status({"error": "없음"}, 404)

    # ---- 잠금 밖에서 검증·계산하는 POST (lock="none"): 파일 검증·복호·외부 호출·노선 계산은 잠금 밖, 반영만 잠금 안
    def _import_package(self, qs, kind: str):
        body = self.body()
        with read_con() as rc:
            if self.crew_scope() is not None and S.device_crew(rc, body) != self.crew_scope():
                raise S.UserError("내 팀에 연결된 휴대폰의 파일만 가져올 수 있습니다.")
            prep = S.prepare_result(rc, body) if kind == "result" else S.prepare_photos(rc, body)
        with LOCK:
            error = self._write_session_error()
            if error:
                return error
            if self.crew_scope() is not None and S.device_crew(CON, body) != self.crew_scope():
                raise S.UserError("내 팀에 연결된 휴대폰의 파일만 가져올 수 있습니다.")
            if kind == "result":
                return S.import_result(CON, body, "QR" if qs.get("channel") == "QR" else "MAIL", prepared=prep)
            return S.import_photos(CON, body, prepared=prep)

    @endpoint("POST", "import/result", CREW, lock="none")
    def ep_import_result(self, qs):
        """휴대폰 결과 파일 가져오기(수거팀은 자기 팀 휴대폰 파일만)."""
        return self._import_package(qs, "result")

    @endpoint("POST", "import/photos", CREW, lock="none")
    def ep_import_photos(self, qs):
        """휴대폰 사진 묶음 가져오기."""
        return self._import_package(qs, "photos")

    @endpoint("POST", "import/solo", WORK, lock="none")
    def ep_import_solo(self, qs):
        """단독 휴대폰 백업 옮기기(접수를 만든다). PBKDF2 60만 회는 잠금 밖."""
        pw = base64.b64decode(self.headers.get("X-DG-Pass") or "").decode("utf-8", "replace")
        body = self.body()
        opened = S.prepare_solo(body, pw)
        if qs.get("preview") == "1":
            with read_con() as rc:
                return S.import_solo(rc, body, pw, qs.get("crew") or None, preview=True, opened=opened)
        with LOCK:
            error = self._write_session_error()
            if error:
                return error
            return S.import_solo(CON, body, pw, qs.get("crew") or None, opened=opened)

    @endpoint("POST", "requests/import", WORK, lock="none")
    def ep_requests_import(self, qs):
        """접수 CSV 일괄 등록(preview=1 은 조회 전용 연결에서 미리보기)."""
        body = self.body()
        if qs.get("preview") == "1":
            with read_con() as rc:
                return S.import_requests(rc, body, preview=True)
        with LOCK:
            error = self._write_session_error()
            if error:
                return error
            return S.import_requests(CON, body, preview=False, skip_duplicates=qs.get("skip_dup") == "1")

    @endpoint("POST", "recon", WORK, lock="none")
    def ep_recon(self, qs):
        """세외수입 납부 자료 대사(preview=1 은 미리보기)."""
        body = self.body()
        if qs.get("preview") == "1":
            with read_con() as rc:
                return FN.import_recon(rc, body, preview=True)
        with LOCK:
            error = self._write_session_error()
            if error:
                return error
            return FN.import_recon(CON, body, preview=False)

    @endpoint("POST", "addr-import", ADMIN, lock="none")
    def ep_addr_import(self, qs):
        """주소 자료 가져오기(큰 파일 읽기는 잠금 밖)."""
        cols = geo.parse_columns_arg(qs["columns"]) if qs.get("columns") else None
        body = self.body()
        parsed = S.parse_addr_points(body, qs.get("region", ""), cols)
        if qs.get("preview") == "1":
            with read_con() as rc:
                return S.import_addr_points(rc, body, qs.get("region", ""), cols, preview=True, parsed=parsed)
        with LOCK:
            error = self._write_session_error()
            if error:
                return error
            return S.import_addr_points(CON, body, qs.get("region", ""), cols, parsed=parsed)

    @endpoint("POST", "external/test", ADMIN, lock="none")
    def ep_external_test(self, qs):
        """외부 서비스 연결 시험(잠금 밖)."""
        data = self.json_body()
        if not isinstance(data, dict):
            raise S.UserError("시험할 외부 서비스를 확인하세요.")
        if data.get("service") == "vworld":
            return self._vworld_lookup({"address": "광주광역시 동구 서남로 1", "type": "ROAD"}, testing=True)
        with read_con() as rc:
            return S.external_test(rc, data.get("service", ""))

    @endpoint("POST", "vworld/locate", WORK, lock="none")
    def ep_vworld_locate(self, qs):
        """VWorld 위치 참고 조회. 결과 비저장, 외부 호출은 잠금 밖, 전후 세션 재검사."""
        return self._vworld_lookup(self.json_body())

    def _vworld_lookup(self, data, testing=False):
        """참고 조회와 연결 시험 모두 현재 세션·설정으로 실행한다."""
        with LOCK:
            error = self._write_session_error()
            if error:
                return error
            prepared = VW.prepare(CON, data)
        result = VW.locate(prepared) if prepared else VW.unavailable()
        with LOCK:
            error = self._write_session_error()
            if error:
                return error
            if prepared and VW.config_token(CON) != prepared["token"]:
                result = VW.unavailable("SETTINGS_CHANGED", "VWorld 설정이 변경되었습니다. 다시 조회하세요.")
        return {"ok": not result.get("none"), "message": result.get("message") or "VWorld 위치 참고 조회 정상 (결과 저장 안 함)"} if testing else result

    @endpoint("POST", "route", WORK, lock="none",
              access=("LIST", lambda h, out, qs: None if isinstance(out, Raw) else ("", len(out.get("requests") or {}), "노선 계산(이름·주소 포함 결과)")))
    def ep_route(self, qs):
        """노선 계산(OR-Tools·카카오 길찾기는 잠금 밖)."""
        return self.route_plan(self.json_body())

    # 오래 걸리는 관리 작업 → 백그라운드 작업 번호
    @endpoint("POST", "backup", ADMIN, lock="none")
    def ep_backup(self, qs):
        """수동 백업(백그라운드 작업)."""
        pw = self.json_body().get("passphrase", "")
        if len(pw or "") < 12:
            raise S.UserError("백업 비밀번호는 12자 이상으로 정해 주세요.")
        return self.job("backup", passphrase=pw)

    @endpoint("POST", "backup/auto", ADMIN, lock="none")
    def ep_backup_auto(self, qs):
        """자동 백업 지금 실행(백그라운드 작업)."""
        return self.job("backup_auto")

    @endpoint("POST", "backup/retention", ADMIN, lock="none")
    def ep_backup_retention(self, qs):
        """백업 보관 정리(백그라운드 작업)."""
        return self.job("backup_retention")

    @endpoint("POST", "privacy/purge", ADMIN, lock="none")
    def ep_privacy_purge(self, qs):
        """개인정보 파기(백그라운드 작업)."""
        return self.job("pii_purge")

    @endpoint("POST", "photo-purge", ADMIN, lock="none")
    def ep_photo_purge(self, qs):
        """사진 보관 기간 정리(백그라운드 작업)."""
        return self.job("photo_purge")

    @endpoint("POST", "refunds/sync", ADMIN, lock="none")
    def ep_refunds_sync(self, qs):
        """자동 환불 건 맞추기(백그라운드 작업)."""
        return self.job("refund_sync")

    @endpoint("POST", "integrity/full", ADMIN, lock="none")
    def ep_integrity_full(self, qs):
        """무결성 전체 검사(백그라운드 작업)."""
        return self.job("verify_full")

    # ---- 쓰기 (POST, DB 잠금 안)
    @endpoint("POST", "requests", WORK)
    def ep_request_create(self, qs):
        """접수 등록(client_req_id 로 한 번만)."""
        d = self.json_body()
        return self.idem("requests", lambda: S.create_request(self.c, d))

    @endpoint("POST", "requests/bulk", WORK)
    def ep_requests_bulk(self, qs):
        """일괄 처리: 예정일 변경·취소·동 지정."""
        return self.bulk(self.json_body())

    @endpoint("POST", "requests/export", WORK)
    def ep_requests_export(self, qs):
        """고른 접수 CSV(개인정보 포함은 접수 담당자·관리자만)."""
        d = self.json_body()
        pii = bool(d.get("include_pii"))
        if pii and self.user["role"] not in ("ADMIN", "INTAKE"):
            return json_status({"error": "개인정보가 든 목록은 접수 담당자·관리자만 내보낼 수 있습니다."}, 403)
        return csv_file(S.export_requests(self.c, d.get("ids"), pii), "donggu-selected.csv")

    @endpoint("POST", "requests/print", WORK)
    def ep_requests_print(self, qs):
        """고른 접수 인쇄(잠깐 쓰는 인쇄 번호)."""
        data = S.print_requests(self.c, self.json_body().get("ids"))
        t = secrets.token_urlsafe(12)
        for k in [k for k, v in PRINTS.items() if v["exp"] < time.time()]:
            PRINTS.pop(k, None)
        PRINTS[t] = {"exp": time.time() + 120, "data": data, "user": self.user["user_id"]}
        return {"token": t}

    @endpoint("POST", "requests/{id}/reduction", WORK)
    def ep_request_reduction(self, qs, rid):
        """감면 지정·해제."""
        return FN.set_reduction(self.c, rid, self.json_body())

    @endpoint("POST", "requests/{id}/receipt-print", WORK)
    def ep_receipt_print(self, qs, rid):
        """접수증 인쇄."""
        return {"token": self.put_print(self.receipt_html(rid))}

    @endpoint("POST", "requests/labels", WORK)
    def ep_labels(self, qs):
        """배출 표지 인쇄."""
        return {"token": self.put_print(self.labels_html(self.json_body().get("ids")))}

    @endpoint("POST", "stats/print", VIEW)
    def ep_stats_print(self, qs):
        """월보 인쇄."""
        d = self.json_body()
        return {"token": self.put_print(self.monthly_html(d.get("start", ""), d.get("end", ""), d.get("basis") or "planned"))}

    @endpoint("POST", "reports/print", VIEW)
    def ep_operations_report_print(self, qs):
        """일일 작업일지·월보 기본 서식 인쇄. 마감·이월을 수행하지 않는다."""
        d = self.json_body()
        basis = d.get("basis") or "planned"
        if d.get("kind") == "daily":
            report = RP.daily_report(self.c, d.get("date", ""), basis)
        elif d.get("kind") == "monthly":
            report = RP.monthly_report(self.c, d.get("month", ""), basis)
        else:
            raise S.UserError("일일 작업일지 또는 월보를 선택하세요.")
        title, body = OP.report_body(report)
        with S.tx(self.c):
            S.audit(self.c, "report", report["kind"], "PRINT", {"start": report["start"], "end": report["end"], "basis": report["basis"]})
            PV.record_access(self.c, "PRINT", "", 0, title + " 집계")
        return {"token": self.put_print(self._doc(title, body))}

    @endpoint("POST", "access/check", REVIEW)
    def ep_access_check(self, qs):
        """접속기록 월 점검 기록."""
        return PV.record_check(self.c, self.json_body().get("note", ""))

    @endpoint("POST", "refunds/request", WORK)
    def ep_refund_request(self, qs):
        """환불 신청."""
        return self.idem("refunds/request", lambda: {"id": FN.request_refund(self.c, self.json_body())})

    @endpoint("POST", "refunds/{id}/approve", REVIEW)
    def ep_refund_approve(self, qs, cid):
        """환불 결재(승인)."""
        FN.approve_refund(self.c, cid, True, self.json_body().get("note", ""))
        return {"ok": True}

    @endpoint("POST", "refunds/{id}/reject", REVIEW)
    def ep_refund_reject(self, qs, cid):
        """환불 결재(반려)."""
        FN.approve_refund(self.c, cid, False, self.json_body().get("note", ""))
        return {"ok": True}

    @endpoint("POST", "refunds/{id}/pay", WORK)
    def ep_refund_pay(self, qs, cid):
        """환불 지급 기록."""
        return FN.pay_refund(self.c, cid, self.json_body())

    @endpoint("POST", "refunds/{id}/void", WORK)
    def ep_refund_void(self, qs, cid):
        """환불 취소."""
        FN.cancel_refund(self.c, cid, self.json_body().get("reason", ""))
        return {"ok": True}

    @endpoint("POST", "backup-key", ADMIN)
    def ep_backup_key(self, qs):
        """자동 백업 키 쌍 만들기(개인키는 한 번만 내려줌)."""
        pem, info = BK.generate_key(self.c, self.json_body().get("password", ""))
        return {"pem": pem.decode(), **info}

    @endpoint("POST", "backups/verify-auto", ADMIN)
    def ep_backup_verify_auto(self, qs):
        """자동 백업 파일 열어 확인."""
        d = self.json_body()
        return BK.verify_auto(self.c, d.get("name", ""), (d.get("key") or "").encode(), d.get("password", ""))

    @endpoint("POST", "requests/{id}", WORK)
    def ep_request_update(self, qs, rid):
        """접수 수정(이전 내용은 버전으로)."""
        d = self.json_body()
        return S.update_request(self.c, rid, d, d.get("reason", ""))

    @endpoint("POST", "requests/{id}/field-check", WORK)
    def ep_field_check(self, qs, rid):
        """현장 접수 확인."""
        return S.check_field_intake(self.c, rid)

    @endpoint('POST', 'requests/{id}/collection', WORK)
    def ep_pc_collection(self, qs, rid):
        """실제 수거일·품목별 추가 수거량 입력과 배출표 자동 배정을 함께 저장."""
        return PC.save(self.c, rid, self.json_body())

    @endpoint("POST", "requests/{id}/cancel", WORK)
    def ep_request_cancel(self, qs, rid):
        """접수 취소(사유 코드)."""
        d = self.json_body()
        return S.cancel_request(self.c, rid, d.get("reason", ""), d.get("code") or None, d.get("req_ver"))

    @endpoint("POST", "requests/{id}/contacts", WORK)
    def ep_request_contact(self, qs, rid):
        """민원·통화 기록."""
        return self.idem(f"requests/{rid}/contacts", lambda: {"id": S.add_contact(self.c, rid, self.json_body())})

    @endpoint("POST", "requests/{id}/location", WORK)
    def ep_request_location(self, qs, rid):
        """위치 직접 지정."""
        d = self.json_body()
        S.set_location(self.c, rid, d["lat"], d["lon"])
        return {"ok": True}

    @endpoint("POST", "assign", WORK)
    def ep_assign(self, qs):
        """배정 확정."""
        d = self.json_body()
        if d.get('depot_basis'):
            self.check_depot_basis(self.c, d['depot_basis'], d['date'], d['plan'])
        return S.confirm_plan(self.c, d["date"], d["plan"], d.get("adjust_reason", ""), bool(d.get("adjusted")))

    @endpoint("POST", "assign/edit", WORK)
    def ep_assign_edit(self, qs):
        """확정 배정 부분 수정(넣기·빼기·순서·팀 옮기기)."""
        d = self.json_body()
        return S.edit_assignment(self.c, d.get("date", ""), d.get("op", ""), d)

    @endpoint("POST", "imports/{id}/confirm", CREW)
    def ep_import_confirm(self, qs, iid):
        """휴대폰 반영 확인."""
        return OPS.confirm_import(self.c, iid)

    @endpoint("POST", "conflicts/bulk", WORK)
    def ep_conflicts_bulk(self, qs):
        """충돌 일괄 처리(접수별·가져오기별)."""
        d = self.json_body()
        return S.resolve_conflicts_bulk(self.c, d.get("action", ""), d.get("reason", ""), d.get("request_id") or "", d.get("import_id") or "")

    @endpoint("POST", "bundles", WORK)
    def ep_bundle(self, qs):
        """휴대폰 목록 묶음 만들기."""
        b = S.make_bundle(self.c, self.json_body()["batch_id"])
        return {k: b[k] for k in ("bundle_id", "bytes", "count", "work_date", "assign_ver")} | {"frames": len(b["frames"])}

    @endpoint("POST", "bundles/{id}/receipt", WORK)
    def ep_bundle_receipt(self, qs, bid):
        """휴대폰 수신 확인 코드."""
        S.confirm_receipt(self.c, bid, self.json_body().get("code", ""))
        return {"ok": True}

    @endpoint("POST", "conflicts/{id}", WORK)
    def ep_conflict(self, qs, eid):
        """충돌 하나 처리."""
        d = self.json_body()
        S.resolve_conflict(self.c, eid, d.get("action", ""), d.get("reason", ""))
        return {"ok": True}

    @endpoint("POST", "pilot", WORK)
    def ep_pilot_save(self, qs):
        """시범 운영 비교 설정 저장."""
        return S.save_pilot(self.c, self.json_body())

    @endpoint("POST", "close", WORK)
    def ep_close_day(self, qs):
        """마감 기록(담당자)."""
        d = self.json_body()
        return self.idem("close", lambda: S.close_day(self.c, d.get("date", ""), d.get("note", ""), d.get("close_secs")))

    @endpoint("POST", "close/approve", REVIEW)
    def ep_close_approve(self, qs):
        """마감 확인(팀장)."""
        d = self.json_body()
        return S.approve_close(self.c, d.get("date", ""), d.get("note", ""))

    @endpoint("POST", "dongs", ADMIN)
    def ep_dong_save(self, qs):
        """행정동 저장."""
        return {"id": A.save_dong(self.c, self.json_body())}

    @endpoint("POST", "dongs/order", ADMIN)
    def ep_dong_order(self, qs):
        """관리자가 전체 행정동의 직제순을 한 번에 저장. 기존 순서와 다르면 충돌 반환."""
        data = self.json_body()
        return {"dongs": A.reorder_dongs(self.c, data.get("ids"), data.get("base_ids"))}

    @endpoint("POST", "dongs/centers", ADMIN)
    def ep_dong_centers(self, qs):
        """관리자가 담당팀이 연결된 동의 비어 있는 차고지만 공식 행정복지센터로 보충."""
        from dgpc import dong_centers
        return self.idem('dongs/centers', lambda: dong_centers.apply_missing(self.c))

    @endpoint("POST", "dongs/{id}/delete", ADMIN)
    def ep_dong_delete(self, qs, did):
        """행정동 지우기."""
        A.delete_dong(self.c, did)
        return {"ok": True}

    @endpoint("POST", "holidays", ADMIN)
    def ep_holiday_add(self, qs):
        """휴무일 추가."""
        d = self.json_body()
        A.add_holiday(self.c, d.get("day", ""), d.get("label", ""))
        return {"ok": True}

    @endpoint("POST", "holidays/{id}/delete", ADMIN)
    def ep_holiday_delete(self, qs, day):
        """휴무일 지우기."""
        A.delete_holiday(self.c, day)
        return {"ok": True}

    @endpoint("POST", "crews", ADMIN)
    def ep_crew_save(self, qs):
        """수거팀 저장."""
        return {"id": S.save_crew(self.c, self.json_body())}

    @endpoint("POST", "devices/register", ADMIN)
    def ep_device_register(self, qs):
        """휴대폰 등록(지문 확인 대기)."""
        d = self.json_body()
        return S.register_device(self.c, d.get("code", ""), d.get("label", ""))

    @endpoint("POST", "devices/{id}/confirm", ADMIN)
    def ep_device_confirm(self, qs, dev):
        """휴대폰 지문 확인."""
        S.confirm_device(self.c, dev)
        return {"ok": True}

    @endpoint("POST", "devices/{id}/status", ADMIN)
    def ep_device_status(self, qs, dev):
        """휴대폰 상태(분실·중지)."""
        S.set_device_status(self.c, dev, self.json_body().get("status", ""))
        return {"ok": True}

    @endpoint("POST", "requests/{id}/link", WORK)
    def ep_link(self, qs, rid):
        """유사 품목 연결."""
        d = self.json_body()
        return {"id": S.link_similar(self.c, rid, d.get("line_id", ""), d.get("fee_rate_id", ""), d.get("reason", ""), d.get("confirmed_by", ""))}

    @endpoint("POST", "links/{id}/void", WORK)
    def ep_link_void(self, qs, lid):
        """유사 품목 연결 취소."""
        S.unlink_similar(self.c, lid, self.json_body().get("reason", ""))
        return {"ok": True}

    @endpoint("POST", "requests/{id}/payments", WORK)
    def ep_payment(self, qs, rid):
        """수납·환불 기록."""
        return self.idem(f"requests/{rid}/payments", lambda: S.add_payment(self.c, rid, self.json_body()))

    @endpoint("POST", "payments/{id}/void", WORK)
    def ep_payment_void(self, qs, pid):
        """수납 기록 취소."""
        return S.void_payment(self.c, pid, self.json_body().get("reason", ""))

    @endpoint("POST", "unloads", CREW)
    def ep_unload(self, qs):
        """하차 기록(수거팀은 자기 팀만)."""
        d = self.json_body()
        if self.crew_scope() is not None and d.get("crew_id") != self.crew_scope():
            raise S.UserError("내 팀의 하차만 기록할 수 있습니다.")
        return self.idem("unloads", lambda: {"id": S.add_unload(self.c, d)})

    @endpoint("POST", "volumes", ADMIN)
    def ep_volume_save(self, qs):
        """품목 부피 보정 저장."""
        d = self.json_body()
        S.save_volume_override(self.c, d.get("fee_rate_id", ""), d.get("m3"))
        return {"ok": True}

    @endpoint("POST", "qrtest", CREW)
    def ep_qrtest(self, qs):
        """QR 실측 시험 만들기."""
        d = self.json_body()
        return S.make_qr_test(self.c, d.get("device_id", ""), int(d.get("count", 20)), d.get("addr", "mixed"),
                              int(d.get("chunk", 700)), d.get("ec", "M"), int(d.get("fps", 3)))

    @endpoint("POST", "qrtest/{id}/result", CREW)
    def ep_qrtest_result(self, qs, tid):
        """QR 실측 결과 기록."""
        d = self.json_body()
        return S.record_qr_test(self.c, tid, d.get("elapsed_s") or 0, d.get("reads") or 0, d.get("check", ""), d.get("note", ""))

    @endpoint("POST", "users", ADMIN)
    def ep_user_create(self, qs):
        """사용자 만들기(임시 비밀번호)."""
        d = self.json_body()
        return {"id": S.create_user(self.c, d.get("username", ""), d.get("display_name", ""), d.get("role", ""),
                                    d.get("password", ""), must_change=True, crew_id=d.get("crew_id") or None, dong_ids=d.get("dong_ids"))}

    @endpoint("POST", "users/{id}/dongs", ADMIN)
    def ep_user_dongs(self, qs, uid):
        """동 담당자 계정의 한 개·여러 담당 동 지정(기존 세션에도 즉시 적용)."""
        U.set_user_dongs(self.c, uid, self.json_body().get("dong_ids"))
        return {"ok": True}

    @endpoint("POST", "users/{id}/crew", ADMIN)
    def ep_user_crew(self, qs, uid):
        """수거팀 계정의 팀 연결(열린 세션에도 바로 반영)."""
        S.set_user_crew(self.c, uid, self.json_body().get("crew_id") or None)
        for v in list(SESSIONS.values()):  # 다른 요청이 로그인·로그아웃하는 동안에도 깨지지 않게 복사본으로
            if v["user_id"] == uid:
                v["crew_id"] = self.json_body().get("crew_id") or None
        return {"ok": True}

    @endpoint("POST", "users/{id}/active", ADMIN)
    def ep_user_active(self, qs, uid):
        """사용자 사용·중지(중지하면 세션 끊음)."""
        S.set_user_active(self.c, uid, bool(self.json_body().get("active")), self.user["user_id"])
        if not self.json_body_cache.get("active"):
            for k in [k for k, v in list(SESSIONS.items()) if v["user_id"] == uid]:
                SESSIONS.pop(k, None)
        return {"ok": True}

    @endpoint("POST", "users/{id}/password", ADMIN)
    def ep_user_password(self, qs, uid):
        """임시 비밀번호 다시 정하기(세션 끊음)."""
        S.set_password(self.c, uid, self.json_body().get("password", ""), must_change=True)
        for k in [k for k, v in list(SESSIONS.items()) if v["user_id"] == uid]:
            SESSIONS.pop(k, None)
        return {"ok": True}

    @endpoint("POST", "geocode/missing", WORK)
    def ep_geocode_missing(self, qs):
        """좌표 없는 접수에 주소 자료로 좌표 채우기."""
        return S.geocode_missing(self.c, self.json_body().get("date", ""))

    @endpoint("POST", "fee-revisions", ADMIN)
    def ep_fee_draft(self, qs):
        """수수료표 개정안 작성(관리자)."""
        return {"id": S.save_fee_draft(self.c, self.json_body())}

    @endpoint("POST", "fee-revisions/{id}/approve", WORK)
    def ep_fee_approve(self, qs, rev):
        """수수료표 개정 승인(작성자 아닌 담당자)."""
        S.approve_fee_revision(self.c, rev, True, self.json_body().get("reason", ""))
        return {"ok": True}

    @endpoint("POST", "fee-revisions/{id}/reject", WORK)
    def ep_fee_reject(self, qs, rev):
        """수수료표 개정 반려."""
        S.approve_fee_revision(self.c, rev, False, self.json_body().get("reason", ""))
        return {"ok": True}

    @endpoint("POST", "backups/verify", ADMIN)
    def ep_backup_verify(self, qs):
        """수동 백업 파일 열어 확인."""
        d = self.json_body()
        return S.verify_backup(self.c, d.get("name", ""), d.get("passphrase", ""))

    @endpoint("POST", "prefs", SELF, must_change_ok=True)
    def ep_prefs(self, qs):
        """본인 화면 설정(테마·글자 크기)."""
        return S.save_prefs(self.c, self.user["user_id"], self.json_body())

    @endpoint("POST", "password", SELF, must_change_ok=True)
    def ep_password(self, qs):
        """본인 비밀번호 변경."""
        d = self.json_body()
        S.set_password(self.c, self.user["user_id"], d.get("password", ""), old=d.get("old", ""))
        self.user["must_change"] = False
        return {"ok": True}

    @endpoint("POST", "settings", ADMIN)
    def ep_settings(self, qs):
        """설정 저장 → 새 설정 묶음."""
        S.save_settings(self.c, self.json_body())
        return S.settings(self.c)

    def route_inputs(self, con, date, request_ids, crew_ids):
        versions = {}
        ids = list(request_ids)
        for start in range(0, len(ids), 800):
            chunk = ids[start:start + 800]
            placeholders = ','.join('?' for _ in chunk)
            events = {}
            for event in S.rows(con, f'SELECT * FROM result_event WHERE request_id IN ({placeholders}) ORDER BY event_id', chunk):
                events.setdefault(event['request_id'], []).append(event)
            for row in S.rows(con, f'SELECT * FROM request WHERE id IN ({placeholders})', chunk):
                versions[row['id']] = hashlib.sha256(json.dumps([row, events.get(row['id'], [])], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        profiles = [{k: crew.get(k) for k in ('id', 'name', 'active', 'capacity_m3', 'capacity_kg', 'shift_start', 'shift_end')}
                    for crew in S.list_crews(con) if crew['id'] in crew_ids]
        st = S.settings(con)
        options = {k: st.get(k) for k in ('depot', 'end_site', 'osrm_url', 'service_min_base', 'service_min_per_item',
            'unload_site', 'unload_min', 'max_unloads', 'unload_hours', 'lunch', 'base_first', 'capacity')}
        options.update(crews=sorted(profiles, key=lambda c: c['id']), volumes=S.volume_overrides(con), travel_method=S.travel_method(con))
        signature = hashlib.sha256(json.dumps(options, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        revisions = {r['crew_id']: r['v'] for r in S.rows(con, 'SELECT crew_id,MAX(assign_ver) AS v FROM assignment_batch WHERE work_date=? GROUP BY crew_id', (date,)) if r['crew_id'] in crew_ids}
        return {'requests': versions, 'profiles': signature, 'revisions': {cid: revisions.get(cid, 0) for cid in crew_ids}}

    def check_depot_basis(self, con, value, date, plan=None, *, refresh_inputs=False):
        try:
            proof = DW._read_proof(value)
            if proof['kind'] != 'depot' or proof['date'] != date:
                raise ValueError()
            if plan is not None and (not set(plan).issubset(proof['crew_ids']) or any(s['request_id'] not in proof['request_ids'] for stops in plan.values() for s in stops)):
                raise ValueError()
            if refresh_inputs:
                return  # 재계산은 같은 승인 범위에서 최신 자료를 읽고 새 증명을 만든다. 확정은 엄격하게 검사한다.
            current = DP.current(con, proof['request_ids'], proof['crew_ids'])
            if current['signature'] != proof['signature']:
                raise ValueError()
            if 'inputs' in proof and proof['inputs'] != self.route_inputs(con, date, proof['request_ids'], proof['crew_ids']):
                raise ValueError()
        except (KeyError, TypeError, ValueError, S.UserError):
            raise S.StaleError('접수·차고지·수거팀·배정 또는 계산 설정이 바뀌었습니다. 노선을 다시 계산해 주세요.')

    def route_plan(self, d):
        """노선 계산: 자료 모으기는 잠금 안(짧게), 계산(OR-Tools·카카오 길찾기)은 잠금 밖, 이력 저장만 다시 잠금 안(카카오 이동시간은 저장하지 않음)."""
        date = d["date"]
        ids = set(d.get("request_ids") or [])
        with LOCK:
            denied = self._write_session_error()
            if denied:
                return denied
            c = CON
            crews = [x for x in S.list_crews(c) if x["id"] in set(d.get("crew_ids") or []) and x["active"]]
            fixed = d.get('fixed_plan')
            if fixed is not None:
                if not isinstance(fixed, dict) or any(not isinstance(ids, list) or any(not isinstance(rid, str) for rid in ids) for ids in fixed.values()):
                    raise S.UserError('수거팀별 접수 순서를 확인하세요.')
                self.check_depot_basis(c, d.get('depot_basis'), date, {cid: [{'request_id': rid} for rid in ids] for cid, ids in fixed.items()}, refresh_inputs=True)
                proof = DW._read_proof(d['depot_basis'])
                if set(proof['crew_ids']) != {crew['id'] for crew in crews}:
                    raise S.StaleError('수거팀 선택이 바뀌었습니다. 자동 추천을 다시 계산하세요.')
            reqs = []
            candidates = S.search_requests(c, {'date': date, 'size': 500, 'page': 1}, scope_ids=ids or None, with_counts=False)
            collected = candidates['rows']
            for page in range(2, candidates['pages'] + 1):
                collected.extend(S.search_requests(c, {'date': date, 'size': 500, 'page': page}, scope_ids=ids or None, with_counts=False)['rows'])
            for r in collected:
                if r["status"] in ("CANCELLED", "DONE") or (ids and r["id"] not in ids):
                    continue
                r["waiting_days"] = max(0, (time.time() - time.mktime(time.strptime(r["received_at"][:10], "%Y-%m-%d"))) // 86400)
                if r["state"]["has_field_records"]:  # 재방문: 남은 품목만 적재·작업시간에 반영
                    r["items"] = [{"name": l["name"], "spec": l["spec"], "qty": l["remaining"], "fee_rate_id": l["fee_rate_id"]} for l in r["state"]["lines"] if l["remaining"]]
                reqs.append(r)
            st = S.settings(c)
            dongs = {x["name"]: x for x in A.list_dongs(c)}
            method = S.travel_method(c)
            kakao_key = S.secret(c, "kakao_rest_key") if method == "KAKAO" else ""
            ov = S.volume_overrides(c)
            context = DP.resolve(reqs, crews, list(dongs.values()), st['depot'], st['end_site'])
            basis = DW._proof({'kind': 'depot', 'date': date, 'request_ids': [r['id'] for r in reqs],
                               'crew_ids': [c['id'] for c in crews], 'signature': context['signature'], 'at': time.time(),
                               'inputs': self.route_inputs(c, date, [r['id'] for r in reqs], [crew['id'] for crew in crews])})
        pref = {r["id"]: dongs[r["dong"]]["crew_id"] for r in reqs if r.get("dong") in dongs and dongs[r["dong"]]["crew_id"]} \
            if st["capacity"]["prefer_dong_crew"] else {}
        res = routing.plan(reqs, crews, st["depot"], osrm_url=st["osrm_url"], service_base=st["service_min_base"],
                           travel_method=method, kakao_key=kakao_key,
                           service_per_item=st["service_min_per_item"], time_limit_s=routing.clamp_time_limit(d.get("time_limit") or min(20, max(5, len(reqs) // 10))),
                           volume_overrides=ov, unload_site=st["unload_site"], unload_min=st["unload_min"],
                           max_unloads=st["max_unloads"], end_site=st["end_site"], crew_pref=pref,
                           unload_hours=st["unload_hours"], lunch=st["lunch"], base_first=st["base_first"],
                           crew_depots=context['crew_depots'], crew_required=context['crew_required'], fixed_plan=fixed)
        res['fixed_order'] = fixed is not None
        res["prefer_dong_crew"] = bool(st["capacity"]["prefer_dong_crew"])
        with LOCK:
            denied = self._write_session_error()
            if denied:
                return denied
            self.check_depot_basis(CON, basis, date)
        res['depot_basis'] = basis
        if method == "KAKAO":  # 좌표만 보냈다는 기록(건수만). 받은 이동시간은 저장하지 않는다(다음 계산은 다시 묻는다)
            mi = res.get("matrix_info") or {}
            with LOCK, S.tx(CON):
                S.audit(CON, "settings", "kakao", "ROUTE_MATRIX", {"calls": mi.get("api_calls", 0), "estimated": mi.get("fallback_pairs", 0)})
        res["requests"] = {r["id"]: {"receipt_no": r["receipt_no"], "name": r["name"], "address": r["address"], "lat": r["lat"],
                                     "lon": r["lon"], "must_today": r["must_today"], "appt": [r["appt_start"], r["appt_end"]],
                                     "status": r["status"], "dong": r["dong"], "pref_crew_id": pref.get(r["id"]), 'required_crew_id': context['crew_required'].get(r['id']),
                                     "items": [f'{i["name"]} {i["spec"]} {i["qty"]}' for i in r.get("items", [])],
                                     "m3": round(sum(routing.volume_of(i, ov) for i in r.get("items", [])), 2),
                                     "kg": int(sum(routing.weight_of(i) for i in r.get("items", []))),
                                     "tags": routing.tags_of(r.get("items", []))} for r in reqs}
        return res

    def bulk(self, d: dict) -> dict:
        """일괄 처리: 예정일 변경·취소·동 지정 (한 트랜잭션, 건별 이력)."""
        act, ids = d.get("action"), d.get("ids")
        if act == "reschedule":
            return S.reschedule_requests(CON, ids, d.get("date", ""), d.get("code") or None, d.get("text", ""))
        if act == "cancel":
            return S.cancel_requests(CON, ids, d.get("code") or None, d.get("text", ""))
        if act == "dong":
            return S.set_requests_dong(CON, ids, d.get("dong", ""), d.get("reason", ""))
        raise S.UserError("알 수 없는 일괄 처리입니다.")

    def raw_page(self, page: bytes):
        """QR 표시 화면(자체 CSP 메타 + 인라인 스크립트). 응답 머리에도 같은 범위의 CSP와 틀 막기·캐시 금지를 붙인다."""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Content-Security-Policy", RAW_CSP)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.end_headers()
        self.wfile.write(page)

    def qrtest_page(self, short: str):
        from pc_tool import display_html, qr_png_data_uri
        with read_con() as rc:
            r = S.rows(rc, "SELECT * FROM qr_test WHERE id LIKE ?", (short + "%",))
        if len(r) != 1:
            return self.send_json({"error": "없음"}, 404)
        t = r[0]
        pr = json.loads(t["params_json"])
        imgs = [qr_png_data_uri(f, pr["ec"])[0] for f in json.loads(t["frames_json"])]
        label = f"QR 실측 · 가상 {pr['count']}건 · 주소 {pr['addr']} · 조각 {pr['chunk']}B · 오류복원 {pr['ec']}"
        self.raw_page(display_html(imgs, t["id"][:8], "실측", 0, pr["count"], pr["fps"], label).encode())

    def print_page(self, batch_short: str):
        b = S.one(CON, "SELECT id FROM assignment_batch WHERE id LIKE ?", (batch_short + "%",))
        if not b or len(batch_short) < 8:
            return self.send_json({"error": "없음"}, 404)
        d = S.dispatch_sheet(CON, b["id"])
        e = _h.escape
        rows_html = []
        for s in d["stops"]:
            done_note = {"DONE": "수거 완료", "PARTIAL": "일부 수거 (남은 품목)", "NOT_COLLECTED": "미수거 (재방문)", "ON_HOLD": "보류"}.get(s.get("status"), "")
            items = (f"<b>{e(done_note)}</b><br>" if done_note else "") + "<br>".join(e(f'{i["name"]} {i["spec"]} {i["qty"]}{i["unit_label"]}' + (" (재방문 남은 수량)" if s.get("revisit") else "")) for i in s["items"])
            tg = routing.tags_of(s["items"])
            if tg:
                items += f'<br><small class="tags">취급: {e(" · ".join(routing.TAG_LABEL[t] for t in tg))}</small>'
            amt = sum((i.get("amount") or 0) for i in s["items"])
            pend = any(i.get("price_status") != "CONFIRMED" for i in s["items"])
            appt = f'{s["appt"][0] or ""}~{s["appt"][1] or ""}' if s.get("appt") else ""
            rows_html.append(f'<tr><td class="n">{s["seq"]}</td><td>{e(s.get("eta") or "")}<br><small>{e(appt)}</small></td>'
                             f'<td><b>{e(s["name"])}</b><br>{e(s["phone"][:3] + "-" + s["phone"][3:-4] + "-" + s["phone"][-4:] if len(s["phone"]) >= 9 else s["phone"])}</td>'
                             f'<td>{e(s.get("dong") or "")}</td><td>{e(s["address"])}<br><small>{e(s.get("spot_note") or "")}</small></td><td>{items}</td>'
                             f'<td class="r">{amt:,}원{"<br><small>확인 대기 있음</small>" if pend else ""}</td><td class="chk"></td><td class="chk"></td></tr>')
            if s.get("unload_after"):
                u = s["unload_after"]
                rows_html.append(f'<tr class="u"><td class="n">⇩</td><td>{e(u["eta"])}</td><td colspan="7">{e(u["label"])} 하차 · 약 {u["volume_m3"]}m³ · {u["minutes"]}분</td></tr>')
        page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>배차표 {e(d['work_date'])} {e(d['crew'])}</title>
<link rel="stylesheet" href="/print.css"><script src="/print.js" defer></script></head><body>
<header><h1>대형폐기물 수거 배차표</h1><p>{e(d['work_date'])} · {e(d['crew'])} · 갱신본 {d['assign_ver']} · 수수료표 {e(d['fee_version'])}</p>
<button type="button" id="print">인쇄</button></header>
<p class="warn">개인정보(이름·연락처·주소)가 들어 있습니다. 휴대폰 고장 대비 예비본으로만 쓰고, 업무가 끝나면 파쇄하세요. 결과는 휴대폰 앱에 기록합니다.</p>
<table><thead><tr><th>순서</th><th>도착·약속</th><th>신청인</th><th>행정동</th><th>배출 위치</th><th>품목</th><th>금액</th><th>연락</th><th>수거</th></tr></thead>
<tbody>{''.join(rows_html)}</tbody></table>
<footer>출력 {e(d['printed_at'])} · 출력 기록은 변경 이력에 남습니다.</footer></body></html>"""
        return self.send_bytes(page.encode(), "text/html; charset=utf-8")

    def print_requests_page(self, d: dict):
        """고른 접수 목록 인쇄(개인정보 포함 → 파쇄 안내). 건마다 출력 기록은 service.print_requests 가 남겼다."""
        e = _h.escape
        trs = []
        for i, r in enumerate(d["rows"], 1):
            ph = r["phone"]
            ph = f"{ph[:3]}-{ph[3:-4]}-{ph[-4:]}" if len(ph) >= 9 else ph
            items = "<br>".join(e(f'{x["name"]} {x["spec"]} {x["qty"]}{x["unit_label"]}') for x in r["items"])
            asg = f'{r["assigned"]["name"]} {r["assigned"]["work_date"]}' if r.get("assigned") else ""
            st = {"CANCELLED": "취소", "DONE": "수거 완료"}.get(r["status"], "")
            trs.append(f'<tr><td class="n">{i}</td><td>{e(r["receipt_no"])}<br><small>{e(st)}</small></td><td>{e(r["planned_date"] or "")}</td><td>{e(r["dong"] or "")}</td>'
                       f'<td><b>{e(r["name"])}</b><br>{e(ph)}</td><td>{e(r["address"])}<br><small>{e(r["spot_note"] or "")}</small></td><td>{items}</td>'
                       f'<td>{e(asg)}</td><td class="chk"></td></tr>')
        page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>접수 목록 {len(d['rows'])}건</title>
<link rel="stylesheet" href="/print.css"><script src="/print.js" defer></script></head><body>
<header><h1>대형폐기물 접수 목록</h1><p>{len(d['rows'])}건 · 출력 {e(d['printed_at'])}</p><button type="button" id="print">인쇄</button></header>
<p class="warn">개인정보(이름·연락처·주소)가 들어 있습니다. 업무가 끝나면 파쇄하세요. 출력 기록은 건마다 변경 이력에 남습니다.</p>
<table><thead><tr><th>번호</th><th>접수번호</th><th>예정일</th><th>행정동</th><th>신청인</th><th>배출 위치</th><th>품목</th><th>배정</th><th>확인</th></tr></thead>
<tbody>{''.join(trs)}</tbody></table>
<footer>출력 {e(d['printed_at'])}</footer></body></html>"""
        return self.send_bytes(page.encode(), "text/html; charset=utf-8")

    def put_print(self, html_: str) -> str:
        t = secrets.token_urlsafe(12)
        for k in [k for k, v in PRINTS.items() if v["exp"] < time.time()]:
            PRINTS.pop(k, None)
        PRINTS[t] = {"exp": time.time() + 120, "html": html_, "user": self.user["user_id"]}
        return t

    @staticmethod
    def _doc(title: str, body: str, cls: str = "") -> str:
        return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>{_h.escape(title)}</title>'
                f'<link rel="stylesheet" href="/doc.css"><script src="/print.js" defer></script></head><body class="{cls}">'
                f'<div class="tools"><button type="button" id="print">인쇄</button></div>{body}</body></html>')

    def receipt_html(self, rid: str) -> str:
        """접수증(신청인 보관용): 접수번호·부과번호·품목·금액(계산→감면→부과)·배출일·배출 장소 안내·문의처."""
        e = _h.escape
        c = CON
        r = S.get_request(c, rid)
        st, ps, cfg = r["state"], r["payment"], S.settings(c)
        won = lambda v: "미확정" if v is None else f"{v:,}원"
        items = "".join(f'<tr><td>{e(l["name"])} {e(l["spec"])}</td><td class="r">{l["qty_confirmed"]}{e(l["unit_label"])}</td>'
                        f'<td class="r">{won(l["price"] * l["qty_confirmed"]) if l["price"] is not None else "금액 확인 대기"}</td></tr>' for l in st["lines"] if l["qty_confirmed"])
        appt = f' {r["appt_start"]}~{r["appt_end"] or ""}' if r["appt_start"] else ""
        red = (f'<tr><th>감면</th><td>{e(ps["reduction_label"] or "")} {ps["reduction_rate"]}% · −{won(ps["reduction"])}</td></tr>') if ps["reduction_rate"] else ""
        body = f"""<section class="receipt"><h1>대형폐기물 배출 접수증 <small>(신청인 보관용)</small></h1>
<table class="kv"><tr><th>접수번호</th><td class="big">{e(r["receipt_no"])}</td></tr><tr><th>부과번호</th><td>{e(r.get("levy_no") or "")}</td></tr>
<tr><th>접수일</th><td>{e(S._kst_date(r["received_at"]) or "")}</td></tr><tr><th>배출일(수거 예정일)</th><td class="big">{e(r["planned_date"] or "미정 (정해지면 안내)")}{e(appt)}</td></tr>
<tr><th>행정동</th><td>{e(r["dong"] or "")}</td></tr><tr><th>배출 위치</th><td>{e(r["spot_note"] or "")}</td></tr></table>
<h2>품목</h2><table><thead><tr><th>품목·규격</th><th>수량</th><th>금액</th></tr></thead><tbody>{items}</tbody></table>
<table class="kv"><tr><th>계산 금액</th><td>{won(ps["calculated"])}</td></tr>{red}<tr><th>부과 금액</th><td class="big">{won(ps["levied"])}</td></tr><tr><th>납부 안내</th><td>{e(cfg["payment_guide"])}</td></tr></table>
<h2>배출 장소 안내</h2><p>{e(cfg["disposal_guide"])}</p><h2>문의처</h2><p>{e(cfg["contact_info"])}</p>
<p class="foot">출력 {dt_now()} · 금액 확인 대기 품목이 있으면 확정 뒤 안내합니다. 이 접수증은 납부 영수증이 아닙니다.</p></section>"""
        with S.tx(c):
            S.audit(c, "request", rid, "PRINT_RECEIPT", None)
            PV.record_access(c, "PRINT", rid, 1, "접수증")
        return self._doc(f"접수증 {r['receipt_no']}", body)

    def labels_html(self, ids) -> str:
        """배출 표지(물건 부착용, A4 한 장에 6개): 접수번호·동·배출일·품목·수량, 큰 글씨, 접수번호만 담은 QR."""
        from pc_tool import qr_png_data_uri
        e = _h.escape
        c = CON
        ids = S._ids(c, ids)
        labels = []
        for rid in ids:
            r = S.one(c, "SELECT * FROM request WHERE id=?", (rid,))
            if r["status"] == "CANCELLED":
                continue
            qr = qr_png_data_uri(r["receipt_no"], "M")[0]
            st = S.derive(c, r)
            revisit = st["has_field_records"] and st["remaining_total"] > 0  # 재방문이면 남은 수량만, 아니면 확인 수량만큼
            units = [(l, i + 1) for l in st["lines"] for i in range(min(l["remaining"] if revisit else l["qty_confirmed"], 20))]
            total = len(units)
            for n, (l, _) in enumerate(units, 1):
                labels.append(f'<div class="label"><div class="lh"><b class="rno">{e(r["receipt_no"])}</b><span>{n}/{total}</span></div>'
                              f'<div class="lday">{e((r["planned_date"] or "배출일 미정")[5:].replace("-", "월 ") + ("일" if r["planned_date"] else ""))}</div>'
                              f'<div class="ldong">{e(r["dong"] or "")}</div><div class="litem">{e(l["name"])} <small>{e(l["spec"])}</small></div>'
                              f'<img class="lqr" src="{qr}" alt="접수번호 QR"><div class="lnote">대형폐기물 배출 표지 · 물건에 붙여 주세요</div></div>')
        if not labels:
            raise S.UserError("표지를 뽑을 접수가 없습니다(취소된 접수는 제외).")
        with S.tx(c):
            for rid in ids:
                S.audit(c, "request", rid, "PRINT_LABEL", None)
            PV.record_access(c, "PRINT", ids[0] if len(ids) == 1 else "", len(ids), f"배출 표지 {len(labels)}장")
        pages = "".join(f'<div class="sheet">{"".join(labels[i:i + 6])}</div>' for i in range(0, len(labels), 6))
        return self._doc("배출 표지", pages, "labels")

    def monthly_html(self, start: str, end: str, basis: str) -> str:
        """월보(통계 인쇄): 개인정보 없음. 제목은 설정에서 바꿈."""
        e = _h.escape
        c = CON
        d = S.stats(c, start, end, basis)
        title = S.settings(c)["monthly_report_title"]
        T, bs = d["totals"], d["totals"]["by_status"]
        won = lambda v: f"{v:,}"
        def tbl(head, rows_):
            return "<table><thead><tr>" + "".join(f"<th>{e(h)}</th>" for h in head) + "</tr></thead><tbody>" + "".join(
                "<tr>" + "".join(f'<td class="{"r" if isinstance(v, (int, float)) else ""}">{e(won(v) if isinstance(v, int) else str(v))}</td>' for v in r_) + "</tr>" for r_ in rows_) + "</tbody></table>"
        body = f"""<section class="report"><h1>{e(title)}</h1><p>기간 {e(start)} ~ {e(end)} · 집계 기준 {e(d["basis_label"])} · 출력 {dt_now()}</p>
<h2>요약</h2>{tbl(["건수", "수거 완료", "재방문·보류", "취소", "계산 금액", "감면액", "부과 금액", "납부 기록", "하차(m³)"],
                 [[T["requests"], bs.get("DONE", 0), bs.get("PARTIAL", 0) + bs.get("NOT_COLLECTED", 0) + bs.get("ON_HOLD", 0), bs.get("CANCELLED", 0), T["calculated"], T["reduction"], T["levied"], T["paid"], T["unload_m3"]]])}
<h2>월별 추이</h2>{tbl(["월", "건수", "완료", "재방문·보류", "취소", "부과 금액", "납부 기록"], [[m["month"], m["count"], m["done"], m["revisit"], m["cancelled"], m["levied"], m["paid"]] for m in d["months"]])}
<h2>행정동별</h2>{tbl(["행정동", "건수", "완료", "재방문·보류", "취소", "부과 금액"], [[x["dong"] or "(동 미지정)", x["planned"], x["done"], x["revisit"], x["cancelled"], x["levied"]] for x in d["dongs"]])}
<h2>품목 분류별</h2>{tbl(["분류", "확인 수량", "수거 수량", "계산 금액"], [[x["category"], x["requested"], x["collected"], x["amount"]] for x in d["categories"]])}
<h2>세입(납부 방법별, 납부일 기준)</h2>{tbl(["납부 방법", "납부 건수", "납부액", "환불액", "순수입"], [[x["method"], x["pay_n"], x["pay"], x["refund"], x["net"]] for x in d["revenue"]["methods"]])}
<h2>감면</h2>{tbl(["감면 사유", "감면율(%)", "건수", "감면액"], [[x["label"], x["rate"], x["count"], x["amount"]] for x in d["reductions"]])}
<h2>접수 경로별</h2>{tbl(["경로", "건수", "완료", "취소"], [[x["channel"], x["planned"], x["done"], x["cancelled"]] for x in d["channels"]])}
<h2>품목별 (전체)</h2>{tbl(["품목", "규격", "확인 수량", "수거 수량"], [[x["name"], x["spec"], f'{x["requested"]}{x["unit_label"]}', f'{x["collected"]}{x["unit_label"]}'] for x in d["items"]])}
<p class="foot">이름·연락처·주소는 담지 않았습니다. 금액 단위: 원.</p></section>"""
        with S.tx(c):
            S.audit(c, "report", "monthly", "PRINT", {"start": start, "end": end, "basis": basis})
        return self._doc(title, body)

    def bundle_page(self, bundle_short: str):
        from pc_tool import display_html, qr_png_data_uri
        with read_con() as rc:
            r = S.rows(rc, "SELECT t.*, c.name AS crew FROM transfer_bundle t LEFT JOIN assignment_batch b ON b.id=t.batch_id LEFT JOIN crew c ON c.id=b.crew_id WHERE t.id LIKE ?",
                       (bundle_short + "%",))
            if len(r) != 1:
                return self.send_json({"error": "없음"}, 404)
            b = r[0]
            n = S.one(rc, "SELECT COUNT(*) AS n FROM assignment_stop WHERE batch_id=?", (b["batch_id"],))["n"] if b["batch_id"] else ""
        if not b["frames_json"]:
            return self.send_json({"error": "수신이 확인됐거나 보관 기간이 지나 정리된 전송 자료입니다. 필요하면 목록 전송에서 다시 만드세요."}, 404)
        frames = json.loads(b["frames_json"])
        imgs = [qr_png_data_uri(f, "M")[0] for f in frames]
        self.raw_page(display_html(imgs, b["id"][:8], b["work_date"], b["assign_ver"], n, 3, b["crew"] or "").encode())


def dt_now() -> str:
    return dt.datetime.now(S.KST).strftime("%Y-%m-%d %H:%M")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows의 SO_REUSEADDR는 듣고 있는 포트에도 다른 서버를 바인딩할 수 있다.
    # CLI·서비스·실행 앱 모두 같은 주소에서 서로 다른 DB를 제공하지 않게 한다.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # HTTPServer의 getfqdn()은 로컬 주소도 외부 이름 조회가 끝날 때까지 멈출 수 있다.
        # 이 앱은 바인딩한 주소만 필요하므로 HTTP 메타데이터도 숫자 주소로 설정한다.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def verify_request(self, request, client_address):  # LAN 모드: 허용 대역 밖 PC는 연결 자체를 끊는다
        return not LAN or ip_allowed(client_address[0], LAN["nets"])


class DesktopServer(Server):
    connection_timeout = 10  # 로컬 브라우저의 idle·미완성 헤더/본문이 정상 종료를 무한히 막지 않게 한다.

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(self.connection_timeout)
        return request, address


def cleanup_sessions() -> int:
    """만료된 세션(무조작·절대 만료)과 지난 인쇄 자료를 메모리에서 지운다."""
    now_ = time.time()
    gone = [k for k, v in list(SESSIONS.items()) if v["exp"] < now_ or now_ - v.get("created", 0) > ABS_S]  # 복사본으로(로그인과 동시에 돌아도 깨지지 않게)
    for k in gone:
        SESSIONS.pop(k, None)
    for k in [k for k, v in list(PRINTS.items()) if v["exp"] < now_]:
        PRINTS.pop(k, None)
    return len(gone)


DAILY_AT = os.environ.get("DG_DAILY_AT", "02:30")  # 하루 예약 작업 시각(한국시간). 그 시각 뒤 처음 도는 때(늦게 켜졌으면 켜자마자) 한 번


def scheduler(stop: threading.Event, every_s: int = 30):
    """서버가 도는 동안: 정한 시각 자동 백업(하루 1회, 늦게 켜졌으면 켜자마자), 하루 1회 예약 작업(개인정보 자동 파기·사진 정리·
    환불 전체 점검·접속기록/백업 보관 정리·무결성 검사), 세션 정리·로그인 실패 이력 모으기(매번)."""
    while not stop.wait(every_s):
        try:
            S.ACTOR.set("system")
            PV.CLIENT.set("local")
            STATE["scheduler_tick"] = S.now()
            cleanup_sessions()
            with LOCK:
                flush_login_fails(CON)
                is_due = BK.due(CON)
                last = (S.get_setting(CON, "last_daily_tasks") or {}).get("at")
            if is_due:  # 잠금은 DB를 쓰는 짧은 동안만(파일 봉인·복사는 잠금 밖) → 백업 중에도 화면 요청이 기다리지 않음
                r = BK.make_auto(CON, "SCHEDULE", lock=LOCK)
                LOG.info("자동 백업 %s", "성공" if r["ok"] else "실패: " + scrub(r.get("error", "")))
            now_kst = time.strftime("%H:%M", time.gmtime(time.time() + 9 * 3600))
            if (S._kst_date(last) or "") < S.today_kst() and now_kst >= DAILY_AT:
                res = OPS.daily_tasks(CON, LOCK, lambda: store.connect(readonly=True))
                LOG.info("하루 예약 작업 %s", json.dumps({k: v for k, v in res.items() if not isinstance(v, dict)}, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            LOG.error("예약 작업 오류\n%s", scrub(traceback.format_exc()))


def serve(port: int = 8765, lan: dict | None = None, host: str | None = None,
          on_ready: Callable[[Server], None] | None = None):
    global CON, PORT, LAN
    PORT = port
    LAN = lan
    CON = store.connect()
    S.ensure_pc_keys(CON)
    S.migrate_legacy_login(CON)
    STATE["started_at"] = S.now()
    OPS.JOBS.start()
    logf = setup_logging()
    bind = (host or "0.0.0.0") if lan else "127.0.0.1"
    srv = (DesktopServer if on_ready else Server)((bind, port), Handler)
    if lan:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(lan["cert"], lan["key"])
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
        print("=" * 72)
        print("※ LAN 모드: 이 PC의 업무 화면을 같은 망의 다른 PC에 엽니다.")
        print("  정보화부서 승인(방화벽·망 구간·인증서·접속 PC 제한)을 받은 경우에만 쓰세요.")
        print(f"  주소: https://{lan['hostname']}:{port}  · 허용 대역: {', '.join(map(str, lan['nets']))}")
        print("  DB는 이 PC 한 곳에만 있습니다(공유 폴더 금지). 동시 사용은 몇 명 정도까지를 가정합니다.")
        print("=" * 72)
    else:
        print(f"동구 대형폐기물 PC 업무 화면: http://127.0.0.1:{port}  (이 PC에서만 열립니다. 끝내려면 Ctrl+C)")
    LOG.info("서버 시작 port=%s lan=%s log=%s", port, bool(lan), logf.name)
    stop = threading.Event()
    scheduled = None
    if os.environ.get("DG_NO_SCHEDULER") != "1":
        scheduled = threading.Thread(target=scheduler, args=(stop,), daemon=True)
        scheduled.start()
    try:
        if on_ready:
            on_ready(srv)
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()
        if on_ready:
            # 실행 앱은 정상 종료를 보장한다. 요청 접수와 예약 작업을 멈춘 뒤 이미 수락한 작업을 마친다.
            if scheduled:
                scheduled.join()
            while True:
                with OPS.JOBS.lock:
                    pending = any(job["status"] in {"QUEUED", "RUNNING"} for job in OPS.JOBS.jobs.values())
                if not pending:
                    break
                time.sleep(.1)
        LOG.info("서버 종료")
