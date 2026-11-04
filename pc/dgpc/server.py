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

import hashlib
import hmac
import ipaddress
import json
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
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import contextlib
import queue

from . import area as A, backup as BK, finance as FN, ops as OPS, privacy as PV, routing, service as S, store

ROOT = Path(__file__).resolve().parent.parent.parent
UI = ROOT / "pc" / "ui"
WEB = ROOT / "web"
WEB_ALLOWED = {"fees.js", "fees-data.js", "map.js", "proto.js", "nav.js", "vendor/jsQR.js", "vendor/maplibre/maplibre-gl.mjs", "vendor/maplibre/maplibre-gl-worker.mjs",
               "vendor/maplibre/maplibre-gl.css"}
UI_FILES = {"/": "index.html", "/index.html": "index.html", "/pc.js": "pc.js", "/pc.css": "pc.css", "/print.css": "print.css", "/print.js": "print.js", "/doc.css": "doc.css",
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


def ip_blocked(ip: str) -> int:
    """LAN 모드에서 이 PC(IP)가 잠겨 있으면 남은 초."""
    if not LAN:
        return 0
    f = IP_FAILS.get(ip)
    return max(0, int(f["until"] - time.time()) + 1) if f and f["until"] > time.time() else 0


def ip_failed(ip: str):
    """LAN 모드: 한 PC에서 10분에 20번 넘게 틀리면(아이디를 바꿔 가며 시도) 그 PC를 10분 잠근다."""
    if not LAN:
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


# 역할별 허용 API (운영 관리자는 전부)
#  INTAKE(접수 담당자): 접수·배정·전송·결과·충돌·수납·마감 기록
#  FIELD(수거팀): 조회, 결과·사진 가져오기, 하차 기록, QR 실측
#  LEAD(팀장·검토자): 모든 조회(변경 이력 포함) + 마감 확인. 사용자·설정 관리와 업무 변경은 못 함
PERM = {
    "INTAKE": {"GET": {"bootstrap", "requests", "duplicates", "batches", "conflicts", "close", "unloads", "volumes", "fee-usage", "qrtests", "photo", "me",
                       "fee-table", "geocode", "stats", "stats.csv", "calendar", "dong-detect", "holidays", "dongs", "addr-search", "juso-search", "juso-coord", "revenue", "revenue.csv",
                       "refunds", "recon-template", "recon-history", "field-intakes", "pilot", "pilot.csv", "load-check",
                       "jobs", "imports", "pending", "readiness", "assign", "ops-status"},
               "POST": {"requests", "route", "assign", "bundles", "import", "conflicts", "close", "payments", "unloads", "qrtest", "password",
                        "fee-revisions", "geocode", "links", "refunds", "recon", "stats", "pilot", "imports"}},
    "FIELD": {"GET": {"bootstrap", "requests", "batches", "close", "unloads", "fee-usage", "qrtests", "photo", "me", "fee-table", "jobs", "imports"},
              "POST": {"import", "unloads", "qrtest", "password", "imports"}},
}


LEAD_GET_DENY = {"backups", "photo-purge", "registration-qr", "juso-search", "juso-coord", "diag"}
ADMIN_ONLY = {("GET", "diag"), ("POST", "refunds/sync")}
LEAD_POST = {"password", "close/approve", "access/check", "stats/print"}
APPROVE_RE = re.compile(r"^refunds/[^/]+/(approve|reject)$")


def allowed(role: str, method: str, p: str) -> bool:
    if role == "ADMIN" or p in ("prefs", "me"):
        return True
    if (method, p) in ADMIN_ONLY:
        return False
    head = p.split("/")[0]
    if role == "LEAD":
        return (method == "GET" and head not in LEAD_GET_DENY) or (method == "POST" and (p in LEAD_POST or bool(APPROVE_RE.match(p))))
    if p == "close/approve" or APPROVE_RE.match(p):  # 마감 확인·환불 결재는 팀장(검토자)·관리자만
        return False
    if role == "FIELD" and method == "POST" and (head == "requests" or p.startswith("import/solo")):
        return False  # 수거팀 계정은 접수를 만들 수 없다(단독 휴대폰 자료 옮기기도 접수를 만든다)
    if role == "INTAKE" and method == "POST" and p == "fee-revisions":  # 개정안 작성은 관리자, 승인은 다른 담당자도 가능
        return False
    return head in PERM.get(role, {}).get(method, set())


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
        return self.rfile.read(n) if n else b""

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
                "must_change": bool(u.get("must_change")), "prefs": S.user_prefs(self.c, u["user_id"])}

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
            if method == "GET" and path.startswith("/bundle/"):
                if not self.session():
                    return self.send_json({"error": "로그인이 필요합니다."}, 401)
                return self.bundle_page(path[8:])
            if method == "GET" and path.startswith("/print/batch/"):
                sess = SESSIONS.get(self.session() or "")
                if not sess:
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
                if not self.session():
                    return self.send_json({"error": "로그인이 필요합니다."}, 401)
                return self.qrtest_page(path[8:])
            return self.send_json({"error": "없음"}, 404)
        except S.StaleError as e:  # 다른 곳에서 먼저 바뀜(낙관적 동시성) → 409 + 지금 버전
            return self.send_json({"error": str(e), "stale": True, **e.info}, 409)
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
        sess = SESSIONS[tok]
        if not allowed(sess["role"], method, p):
            return self.send_json({"error": "이 작업은 권한이 없습니다."}, 403)
        if sess.get("must_change") and p not in ("me", "password", "bootstrap", "prefs"):
            return self.send_json({"error": "비밀번호를 먼저 바꿔 주세요.", "must_change": True}, 403)
        self.user = sess
        S.ACTOR.set(sess["username"])
        if method == "GET":
            # 조회는 DB 잠금 없이 조회 전용 연결(WAL)로. 접속기록·이력은 잠금 안에서 쓰기 연결로 넘어간다.
            try:
                with read_con() as rc:
                    self.c = rc
                    return self.api_authed(method, p, qs)
            except Exception as e:  # noqa: BLE001 — 조회 중 쓰기가 필요했던 경우(드묾): 잠금 잡고 쓰기 연결로 다시
                if not _is_ro_error(e):
                    raise
            with LOCK:
                self.c = CON
                return self.api_authed(method, p, qs)
        if self.api_unlocked(p, qs) is not False:  # 처리함(응답 보냄)
            return None
        with LOCK:
            self.c = CON
            return self.api_authed(method, p, qs)

    def login(self):
        """로그인: scrypt 확인은 DB 잠금 밖(조회 전용 연결)에서, 마지막 로그인 시각·이력만 잠금 안에서."""
        d = self.json_body()
        ip = self.client_address[0] if LAN else "local"
        wait = login_blocked(d.get("username") or "") or ip_blocked(ip)
        if wait:
            raise S.UserError(f"여러 번 틀렸습니다. {wait}초 뒤 다시 시도하세요.")
        with read_con() as rc:
            good = S.verify_login(rc, d.get("username") or "", d.get("password") or "")
            un = (d.get("username") or "").strip()[:30]
            known = bool(un and S.one(rc, "SELECT 1 AS x FROM app_user WHERE username=?", (un,)))
        with LOCK:
            if good:
                S.ACTOR.set(good["username"])
                with S.tx(CON):
                    CON.execute("UPDATE app_user SET last_login_at=? WHERE id=?", (S.now(), good["id"]))
                    S.audit(CON, "user", good["id"], "LOGIN", None)
            else:
                # 아이디 칸에 비밀번호를 잘못 넣는 일이 있어, 있는 아이디일 때만 그대로 적고 아니면 가린다. 분마다 모아 한 줄로 남긴다.
                note_login_fail(un if known else "(없는 아이디)")
            flush_login_fails(CON)
        if not good:
            login_failed(d.get("username") or "")
            ip_failed(ip)
            raise S.UserError("아이디 또는 비밀번호가 맞지 않습니다.")
        login_succeeded(d.get("username") or "")
        return self.login_ok(good)

    def idem(self, endpoint: str, fn):
        """client_req_id가 있으면 같은 요청을 한 번만 처리(24시간 안 다시 오면 그때 응답). 잠금 안에서 부른다."""
        d = self.json_body()
        key = d.get("client_req_id") if isinstance(d, dict) else None
        return S.idempotent(self.c, self.user["username"], endpoint, key, fn)

    def job(self, kind: str, **kw) -> dict:
        jid = OPS.JOBS.submit(kind, OPS.job_fn(kind, CON, LOCK, lambda: store.connect(readonly=True), **kw), self.user["username"],
                              self.client_address[0] if LAN else "local")
        return {"job_id": jid, "status": "QUEUED", "kind": kind}

    def api_unlocked(self, p, qs):
        """DB 잠금을 오래 잡지 않을 POST: 파일 검증·복호·외부 호출·노선 계산은 잠금 밖, 반영만 잠금 안. 해당 없으면 False."""
        if p in ("import/result", "import/photos"):
            body = self.body()
            with read_con() as rc:
                if self.crew_scope() is not None and S.device_crew(rc, body) != self.crew_scope():
                    raise S.UserError("내 팀에 연결된 휴대폰의 파일만 가져올 수 있습니다.")
                prep = S.prepare_result(rc, body) if p == "import/result" else S.prepare_photos(rc, body)
            with LOCK:
                if p == "import/result":
                    return self.send_json(S.import_result(CON, body, "QR" if qs.get("channel") == "QR" else "MAIL", prepared=prep))
                return self.send_json(S.import_photos(CON, body, prepared=prep))
        if p == "import/solo":
            import base64 as _b64
            pw = _b64.b64decode(self.headers.get("X-DG-Pass") or "").decode("utf-8", "replace")
            body = self.body()
            opened = S.prepare_solo(body, pw)  # PBKDF2 60만 회: 잠금 밖
            if qs.get("preview") == "1":
                with read_con() as rc:
                    return self.send_json(S.import_solo(rc, body, pw, qs.get("crew") or None, preview=True, opened=opened))
            with LOCK:
                return self.send_json(S.import_solo(CON, body, pw, qs.get("crew") or None, opened=opened))
        if p == "requests/import" and qs.get("preview") == "1":
            with read_con() as rc:
                return self.send_json(S.import_requests(rc, self.body(), preview=True))
        if p == "recon" and qs.get("preview") == "1":
            with read_con() as rc:
                return self.send_json(FN.import_recon(rc, self.body(), preview=True))
        if p == "addr-import":
            cols = None
            if qs.get("columns"):
                from dgpc import geo
                cols = geo.parse_columns_arg(qs["columns"])
            body = self.body()
            parsed = S.parse_addr_points(body, qs.get("region", ""), cols)  # 큰 파일 읽기: 잠금 밖
            if qs.get("preview") == "1":
                with read_con() as rc:
                    return self.send_json(S.import_addr_points(rc, body, qs.get("region", ""), cols, preview=True, parsed=parsed))
            with LOCK:
                return self.send_json(S.import_addr_points(CON, body, qs.get("region", ""), cols, parsed=parsed))
        if p == "external/test":  # 외부 서비스 시험: 잠금 밖
            with read_con() as rc:
                return self.send_json(S.external_test(rc, self.json_body().get("service", "")))
        if p == "route":
            out = self.route_plan(self.json_body())
            with read_con() as rc:
                PV.record_access(rc, "LIST", "", len(out.get("requests") or {}), "노선 계산(이름·주소 포함 결과)")
            return self.send_json(out)
        # 오래 걸리는 관리 작업 → 백그라운드 작업 번호
        if p == "backup":
            pw = self.json_body().get("passphrase", "")
            if len(pw or "") < 12:
                raise S.UserError("백업 비밀번호는 12자 이상으로 정해 주세요.")
            return self.send_json(self.job("backup", passphrase=pw))
        if p == "backup/auto":
            return self.send_json(self.job("backup_auto"))
        if p == "backup/retention":
            return self.send_json(self.job("backup_retention"))
        if p == "privacy/purge":
            return self.send_json(self.job("pii_purge"))
        if p == "photo-purge":
            return self.send_json(self.job("photo_purge"))
        if p == "refunds/sync":
            return self.send_json(self.job("refund_sync"))
        if p == "integrity/full":
            return self.send_json(self.job("verify_full"))
        return False

    def login_ok(self, u):
        t = secrets.token_urlsafe(32)
        SESSIONS[t] = {"exp": time.time() + IDLE_S, "created": time.time(), "user_id": u["id"], "username": u["username"], "role": u["role"],
                       "display_name": u["display_name"], "must_change": u.get("must_change", False), "crew_id": u.get("crew_id")}
        return self.send_json({"ok": True}, extra={"Set-Cookie": f"dg_s={t}" + self.cookie_flags()})

    def api_authed(self, method, p, qs):
        c = self.c
        parts = p.split("/")
        if method == "GET":
            if p == "bootstrap":
                return self.send_json({"settings": S.settings(c), "crews": S.list_crews(c), "devices": S.list_devices(c),
                                       "today": S.today_kst(), "me": self.me(), "fee": S.fee_table_for_phone(c),
                                       "dongs": A.list_dongs(c), "channels": S.CHANNELS, "cancel_codes": S.CANCEL_CODES, "postpone_codes": S.POSTPONE_CODES,
                                       "contact_kinds": S.CONTACT_KINDS, "session_idle_s": IDLE_S, "roles": S.ROLES, "audit_entities": S.AUDIT_ENTITIES,
                                       "addr_count": S.addr_stats(c)["count"], "version": __import__("dgpc").__version__,
                                       "quick_filters": list(S.QUICK), "reduction_round_units": list(S.ROUND_UNITS), "api": 13})
            if p == "calendar":
                start = qs.get("start") or S.today_kst()
                dong = qs.get("dong") or None
                ex = qs.get("exclude") or None
                out = {"days": A.calendar(c, start, min(31, int(qs.get("days") or 14)), dong, ex),
                       "earliest": A.earliest_available(c, start, dong, ex)}
                if qs.get("date"):
                    out["warnings"] = A.capacity_warnings(c, qs["date"], dong, ex)
                return self.send_json(out)
            if p == "dong-detect":
                return self.send_json({**A.detect_dong(c, qs.get("address", "")), "warnings": S.address_warnings(c, qs.get("address", ""))})
            if p == "dongs":
                return self.send_json(A.list_dongs(c))
            if p == "holidays":
                return self.send_json(A.list_holidays(c, qs.get("start", ""), qs.get("end", "")))
            if p == "requests/import-template":
                return self.send_bytes(S.import_template(), "text/csv; charset=utf-8", extra={"Content-Disposition": 'attachment; filename="donggu-intake-template.csv"'})
            if p == "audit.csv":
                return self.send_bytes(S.audit_csv(c, qs), "text/csv; charset=utf-8", extra={"Content-Disposition": 'attachment; filename="donggu-audit.csv"'})
            if p == "stats":
                return self.send_json(S.stats(c, qs.get("start", ""), qs.get("end", ""), qs.get("basis") or "planned"))
            if p == "pilot":
                ps = S.pilot_settings(c)
                st_, en_ = qs.get("start") or ps["start"], qs.get("end") or ps["end"]
                return self.send_json({"settings": ps, "base_keys": S.PILOT_BASE,
                                       "report": S.pilot_report(c, st_, en_, qs.get("crew")) if st_ and en_ and st_ <= en_ else None})
            if p == "pilot.csv":
                body = S.pilot_csv(c, qs.get("start") or None, qs.get("end") or None, qs.get("crew"))
                return self.send_bytes(body, "text/csv; charset=utf-8", extra={"Content-Disposition": 'attachment; filename="donggu-pilot.csv"'})
            if p == "stats.csv":
                kind = qs.get("kind") if qs.get("kind") in ("days", "dongs", "items", "categories", "months", "reductions", "channels") else "requests"
                body = S.stats_csv(c, qs.get("start", ""), qs.get("end", ""), kind, qs.get("basis") or "planned")
                fn = f'donggu-{kind}-{qs.get("start")}_{qs.get("end")}.csv'
                return self.send_bytes(body, "text/csv; charset=utf-8", extra={"Content-Disposition": f'attachment; filename="{fn}"'})
            if p == "geocode":
                return self.send_json(S.geocode(c, qs.get("address", "")) or {"none": True})
            if p == "addr-search":
                return self.send_json(S.addr_search(c, qs.get("q", "")))
            if p == "juso-search":  # 행안부 주소 검색(서버가 대신 물음). 검색어는 로그·접속기록에 남기지 않는다
                return self.send_json(S.juso_search(c, qs.get("q", ""), qs.get("name", "")))
            if p == "juso-coord":
                return self.send_json(S.juso_coord(c, {k: qs.get(k, "") for k in ("admCd", "rnMgtSn", "udrtYn", "buldMnnm", "buldSlno")}))
            if p == "privacy":
                return self.send_json({"preview": PV.purge_preview(c), "log": PV.purge_log(c), "retention_years": PV.retention_years(c),
                                       "access_retention_years": PV.access_retention_years(c), "backup": BK.status(c)})
            if p == "access":
                return self.send_json(PV.access_page(c, qs))
            if p == "access.csv":
                return self.send_bytes(PV.access_csv(c, qs), "text/csv; charset=utf-8", extra={"Content-Disposition": 'attachment; filename="donggu-access.csv"'})
            if p == "access/status":
                return self.send_json({**PV.check_status(c), "checks": PV.list_checks(c)})
            if p == "integrity":  # 빠른 검사(검사점부터 새 행만). 전체 검사는 POST integrity/full (백그라운드 작업)
                res = PV.verify_all(c, full=False)
                S.audit(c, "access", "verify", "VERIFY", {"ok": res["ok"], "incremental": True})
                return self.send_json(res)
            if parts[0] == "jobs" and len(parts) == 2:
                j = OPS.JOBS.get(parts[1])
                if not j or (j["actor"] != self.user["username"] and self.user["role"] != "ADMIN"):
                    return self.send_json({"error": "작업을 찾을 수 없습니다."}, 404)
                return self.send_json(j)
            if p == "imports/recent":
                return self.send_json(OPS.recent_imports(c, int(qs.get("limit") or 30), self.crew_scope()))
            if p == "pending":
                return self.send_json(OPS.pending(c))
            if p == "readiness":
                return self.send_json(OPS.readiness(c))
            if p == "ops-status":
                return self.send_json(OPS.ops_status(c))
            if p == "diag":
                return self.send_json({**OPS.diag(c), "scheduler_tick": STATE["scheduler_tick"], "started_at": STATE["started_at"],
                                       "sessions": len(SESSIONS), "jobs_queued": OPS.JOBS.q.qsize()})
            if p == "assign/current":
                return self.send_json(S.current_assignment(c, qs.get("date") or S.today_kst()))
            if parts[0] == "requests" and len(parts) == 3 and parts[2] == "notice":
                self.require_crew_request(parts[1])
                return self.send_bytes(S.notice_text(c, parts[1]).encode("utf-8"), "text/plain; charset=utf-8")
            if p == "backup-status":
                return self.send_json(BK.status(c))
            if p == "revenue":
                return self.send_json(FN.revenue(c, qs.get("start", ""), qs.get("end", ""), qs.get("unit", "day")))
            if p == "revenue.csv":
                unit, kind = qs.get("unit", "day"), qs.get("kind", "method")
                return self.send_bytes(FN.revenue_csv(c, qs.get("start", ""), qs.get("end", ""), unit, kind), "text/csv; charset=utf-8",
                                       extra={"Content-Disposition": f'attachment; filename="donggu-revenue-{kind}.csv"'})
            if p == "recon-template":
                return self.send_bytes(FN.recon_template(), "text/csv; charset=utf-8", extra={"Content-Disposition": 'attachment; filename="donggu-recon-template.csv"'})
            if p == "recon-history":
                return self.send_json(FN.recon_history(c))
            if p == "refunds":
                return self.send_json(FN.list_refunds(c, qs.get("status", "")))
            if p == "addr-stats":
                return self.send_json(S.addr_stats(c))
            if p == "fee-table":
                return self.send_json({"state": S.fee_state(c), "review_rows": S.review_rows(), "revisions": S.list_fee_revisions(c),
                                       "source": {"label": S.FEE_TABLE["label"], "checked_on": S.FEE_TABLE["source_checked_on"], "rows": len(S.FEE_ROWS)}})
            if p == "me":
                return self.send_json(self.me())
            if p == "users":
                return self.send_json(S.list_users(c))
            if p == "backups":
                return self.send_json({"dir": str(S.backup_dir(c)), "files": S.list_backups(c)})
            if p == "photo-purge":
                cand = S.purge_candidates(c)
                return self.send_json({"count": len(cand), "bytes": sum(x["bytes"] or 0 for x in cand), "days": S.settings(c)["photo_retention_days"]})
            if p == "audit":
                return self.send_json(S.audit_page(c, qs))
            if p == "requests":
                # 수거팀 계정: 자기 팀에 배정된 접수만
                scope = S.crew_request_ids(c, self.crew_scope()) if self.crew_scope() is not None else None
                res = S.search_requests(c, qs, scope)
                PV.record_access(c, "LIST", "", len(res["rows"]), f"전체 {res['total']}건 · " + PV.filter_summary(qs))  # 건수·조건만 (내용 없음)
                return self.send_json(res)
            if parts[0] == "requests" and len(parts) == 2:
                self.require_crew_request(parts[1])
                out = S.get_request(c, parts[1])
                PV.record_access(c, "VIEW", parts[1], 1, "접수 상세")
                return self.send_json(out)
            if p == "duplicates":
                out = S.find_duplicates(c, qs.get("phone", ""), qs.get("address", ""), qs.get("exclude") or None)
                if out:
                    PV.record_access(c, "SEARCH", "", len(out), "중복 후보 조회")
                return self.send_json(out)
            if p == "batches":
                out = S.list_batches(c, qs["date"])
                if self.crew_scope() is not None:
                    out = [b for b in out if b["crew_id"] == self.crew_scope()]
                if out:  # 배정 목록(이름·주소 포함): 건수만 접속기록
                    PV.record_access(c, "LIST", "", sum(b.get("count") or len(b.get("stops") or []) for b in out), f"배정 목록 {qs['date']}")
                return self.send_json(out)
            if p == "conflicts":
                out = S.list_conflicts(c)
                if out:  # 현장 접수·정정 요청 원문(개인정보)이 들어 있다
                    PV.record_access(c, "LIST", "", len(out), "충돌함·현장 요청")
                return self.send_json(out)
            if p == "field-intakes":
                out = S.field_intakes(c)
                if out:
                    PV.record_access(c, "SEARCH", "", len(out), "현장 접수 확인 목록")
                return self.send_json(out)
            if p == "close":
                out = S.day_summary(c, qs["date"], self.crew_scope())
                PV.record_access(c, "LIST", "", len(out.get("no_result") or []) + len(out.get("revisit") or []), f"마감 점검 {qs['date']}")
                return self.send_json(out)
            if p == "registration-qr":
                from pc_tool import qr_png_data_uri
                return self.send_json({"image": qr_png_data_uri(S.registration_text(c), "M")[0]})
            if p == "unloads":
                return self.send_json([u for u in S.list_unloads(c, qs["date"]) if self.crew_scope() is None or u["crew_id"] == self.crew_scope()])
            if p == "volumes":
                ov = S.volume_overrides(c)
                ids = list(dict.fromkeys(S.fee_usage(c, 20) + list(ov)))
                return self.send_json([{"fee_rate_id": i, "name": S.FEE_ROWS[i]["name"], "spec": S.FEE_ROWS[i]["spec"], "unit_label": S.FEE_ROWS[i]["unit_label"],
                                        "estimate": routing.volume_of({"name": S.FEE_ROWS[i]["name"], "spec": S.FEE_ROWS[i]["spec"], "qty": 1}),
                                        "kg": routing.weight_of({"name": S.FEE_ROWS[i]["name"], "spec": S.FEE_ROWS[i]["spec"], "qty": 1}),
                                        "tags": [routing.TAG_LABEL[t] for t in routing.tags_of([{"name": S.FEE_ROWS[i]["name"], "spec": S.FEE_ROWS[i]["spec"], "qty": 1}])],
                                        "override": ov.get(i)} for i in ids if i in S.FEE_ROWS])
            if p == "load-check":
                return self.send_json(S.load_check(c, qs.get("start") or "", qs.get("end") or ""))
            if p == "fee-usage":
                return self.send_json(S.fee_usage(c))
            if p == "qrtests":
                return self.send_json(S.list_qr_tests(c))
            if parts[0] == "photo" and len(parts) == 2:
                self.require_crew_request(S.photo_request(c, parts[1]) or "")
                b = S.photo_bytes(c, parts[1])
                if b:
                    PV.record_access(c, "PHOTO", S.photo_request(c, parts[1]) or "", 1, "현장 사진")
                return self.send_bytes(b, "image/jpeg") if b else self.send_json({"error": "없음"}, 404)
        if method == "POST":
            if p == "requests":
                d = self.json_body()
                return self.send_json(self.idem(p, lambda: S.create_request(c, d)))
            if p == "requests/bulk":
                return self.send_json(self.bulk(self.json_body()))
            if p == "requests/export":
                d = self.json_body()
                pii = bool(d.get("include_pii"))
                if pii and self.user["role"] not in ("ADMIN", "INTAKE"):
                    return self.send_json({"error": "개인정보가 든 목록은 접수 담당자·관리자만 내보낼 수 있습니다."}, 403)
                return self.send_bytes(S.export_requests(c, d.get("ids"), pii), "text/csv; charset=utf-8",
                                       extra={"Content-Disposition": 'attachment; filename="donggu-selected.csv"'})
            if p == "requests/print":
                data = S.print_requests(c, self.json_body().get("ids"))
                t = secrets.token_urlsafe(12)
                for k in [k for k, v in PRINTS.items() if v["exp"] < time.time()]:
                    PRINTS.pop(k, None)
                PRINTS[t] = {"exp": time.time() + 120, "data": data, "user": self.user["user_id"]}
                return self.send_json({"token": t})
            if parts[0] == "requests" and len(parts) == 3 and parts[2] == "reduction":
                return self.send_json(FN.set_reduction(c, parts[1], self.json_body()))
            if parts[0] == "requests" and len(parts) == 3 and parts[2] == "receipt-print":
                return self.send_json({"token": self.put_print(self.receipt_html(parts[1]))})
            if p == "requests/labels":
                return self.send_json({"token": self.put_print(self.labels_html(self.json_body().get("ids")))})
            if p == "stats/print":
                d = self.json_body()
                return self.send_json({"token": self.put_print(self.monthly_html(d.get("start", ""), d.get("end", ""), d.get("basis") or "planned"))})
            if p == "access/check":
                return self.send_json(PV.record_check(c, self.json_body().get("note", "")))
            if p == "recon":
                return self.send_json(FN.import_recon(c, self.body(), preview=qs.get("preview") == "1"))
            if p == "refunds/request":
                return self.send_json(self.idem(p, lambda: {"id": FN.request_refund(c, self.json_body())}))
            if parts[0] == "refunds" and len(parts) == 3 and parts[2] in ("approve", "reject"):
                FN.approve_refund(c, parts[1], parts[2] == "approve", self.json_body().get("note", ""))
                return self.send_json({"ok": True})
            if parts[0] == "refunds" and len(parts) == 3 and parts[2] == "pay":
                return self.send_json(FN.pay_refund(c, parts[1], self.json_body()))
            if parts[0] == "refunds" and len(parts) == 3 and parts[2] == "void":
                FN.cancel_refund(c, parts[1], self.json_body().get("reason", ""))
                return self.send_json({"ok": True})
            if p == "backup-key":
                pem, info = BK.generate_key(c, self.json_body().get("password", ""))
                return self.send_json({"pem": pem.decode(), **info})
            if p == "backups/verify-auto":
                d = self.json_body()
                return self.send_json(BK.verify_auto(c, d.get("name", ""), (d.get("key") or "").encode(), d.get("password", "")))
            if p == "requests/import":
                return self.send_json(S.import_requests(c, self.body(), preview=qs.get("preview") == "1", skip_duplicates=qs.get("skip_dup") == "1"))
            if parts[0] == "requests" and len(parts) == 2:
                d = self.json_body()
                return self.send_json(S.update_request(c, parts[1], d, d.get("reason", "")))
            if parts[0] == "requests" and len(parts) == 3 and parts[2] == "field-check":
                return self.send_json(S.check_field_intake(c, parts[1]))
            if parts[0] == "requests" and len(parts) == 3 and parts[2] == "cancel":
                d = self.json_body()
                return self.send_json(S.cancel_request(c, parts[1], d.get("reason", ""), d.get("code") or None, d.get("req_ver")))
            if parts[0] == "requests" and len(parts) == 3 and parts[2] == "contacts":
                return self.send_json(self.idem(p, lambda: {"id": S.add_contact(c, parts[1], self.json_body())}))
            if parts[0] == "requests" and len(parts) == 3 and parts[2] == "location":
                d = self.json_body()
                S.set_location(c, parts[1], d["lat"], d["lon"])
                return self.send_json({"ok": True})
            if p == "assign":
                d = self.json_body()
                return self.send_json(S.confirm_plan(c, d["date"], d["plan"], d.get("adjust_reason", ""), bool(d.get("adjusted"))))
            if p == "assign/edit":  # 확정 배정 부분 수정(넣기·빼기·순서·팀 옮기기)
                d = self.json_body()
                return self.send_json(S.edit_assignment(c, d.get("date", ""), d.get("op", ""), d))
            if parts[0] == "imports" and len(parts) == 3 and parts[2] == "confirm":
                return self.send_json(OPS.confirm_import(c, parts[1]))
            if p == "conflicts/bulk":
                d = self.json_body()
                return self.send_json(S.resolve_conflicts_bulk(c, d.get("action", ""), d.get("reason", ""), d.get("request_id") or "", d.get("import_id") or ""))
            if p == "bundles":
                b = S.make_bundle(c, self.json_body()["batch_id"])
                return self.send_json({k: b[k] for k in ("bundle_id", "bytes", "count", "work_date", "assign_ver")} | {"frames": len(b["frames"])})
            if parts[0] == "bundles" and len(parts) == 3 and parts[2] == "receipt":
                S.confirm_receipt(c, parts[1], self.json_body().get("code", ""))
                return self.send_json({"ok": True})
            if parts[0] == "conflicts" and len(parts) == 2:
                d = self.json_body()
                S.resolve_conflict(c, parts[1], d.get("action", ""), d.get("reason", ""))
                return self.send_json({"ok": True})
            if p == "pilot":
                return self.send_json(S.save_pilot(c, self.json_body()))
            if p == "close":
                d = self.json_body()
                return self.send_json(self.idem(p, lambda: S.close_day(c, d.get("date", ""), d.get("note", ""), d.get("close_secs"))))
            if p == "close/approve":
                d = self.json_body()
                return self.send_json(S.approve_close(c, d.get("date", ""), d.get("note", "")))
            if p == "dongs":
                return self.send_json({"id": A.save_dong(c, self.json_body())})
            if parts[0] == "dongs" and len(parts) == 3 and parts[2] == "delete":
                A.delete_dong(c, parts[1])
                return self.send_json({"ok": True})
            if p == "holidays":
                d = self.json_body()
                A.add_holiday(c, d.get("day", ""), d.get("label", ""))
                return self.send_json({"ok": True})
            if parts[0] == "holidays" and len(parts) == 3 and parts[2] == "delete":
                A.delete_holiday(c, parts[1])
                return self.send_json({"ok": True})
            if p == "crews":
                return self.send_json({"id": S.save_crew(c, self.json_body())})
            if p == "devices/register":
                d = self.json_body()
                return self.send_json(S.register_device(c, d.get("code", ""), d.get("label", "")))
            if parts[0] == "devices" and len(parts) == 3 and parts[2] == "confirm":
                S.confirm_device(c, parts[1])
                return self.send_json({"ok": True})
            if parts[0] == "devices" and len(parts) == 3 and parts[2] == "status":
                S.set_device_status(c, parts[1], self.json_body().get("status", ""))
                return self.send_json({"ok": True})
            if parts[0] == "requests" and len(parts) == 3 and parts[2] == "link":
                d = self.json_body()
                return self.send_json({"id": S.link_similar(c, parts[1], d.get("line_id", ""), d.get("fee_rate_id", ""), d.get("reason", ""), d.get("confirmed_by", ""))})
            if parts[0] == "links" and len(parts) == 3 and parts[2] == "void":
                S.unlink_similar(c, parts[1], self.json_body().get("reason", ""))
                return self.send_json({"ok": True})
            if parts[0] == "requests" and len(parts) == 3 and parts[2] == "payments":
                return self.send_json(self.idem(p, lambda: S.add_payment(c, parts[1], self.json_body())))
            if parts[0] == "payments" and len(parts) == 3 and parts[2] == "void":
                return self.send_json(S.void_payment(c, parts[1], self.json_body().get("reason", "")))
            if p == "unloads":
                d = self.json_body()
                if self.crew_scope() is not None and d.get("crew_id") != self.crew_scope():
                    raise S.UserError("내 팀의 하차만 기록할 수 있습니다.")
                return self.send_json(self.idem(p, lambda: {"id": S.add_unload(c, d)}))
            if p == "volumes":
                d = self.json_body()
                S.save_volume_override(c, d.get("fee_rate_id", ""), d.get("m3"))
                return self.send_json({"ok": True})
            if p == "qrtest":
                d = self.json_body()
                return self.send_json(S.make_qr_test(c, d.get("device_id", ""), int(d.get("count", 20)), d.get("addr", "mixed"),
                                                     int(d.get("chunk", 700)), d.get("ec", "M"), int(d.get("fps", 3))))
            if parts[0] == "qrtest" and len(parts) == 3 and parts[2] == "result":
                d = self.json_body()
                return self.send_json(S.record_qr_test(c, parts[1], d.get("elapsed_s") or 0, d.get("reads") or 0, d.get("check", ""), d.get("note", "")))
            if p == "users":
                d = self.json_body()
                return self.send_json({"id": S.create_user(c, d.get("username", ""), d.get("display_name", ""), d.get("role", ""),
                                                           d.get("password", ""), must_change=True, crew_id=d.get("crew_id") or None)})
            if parts[0] == "users" and len(parts) == 3 and parts[2] == "crew":
                S.set_user_crew(c, parts[1], self.json_body().get("crew_id") or None)
                for v in SESSIONS.values():
                    if v["user_id"] == parts[1]:
                        v["crew_id"] = self.json_body().get("crew_id") or None
                return self.send_json({"ok": True})
            if parts[0] == "users" and len(parts) == 3 and parts[2] == "active":
                S.set_user_active(c, parts[1], bool(self.json_body().get("active")), self.user["user_id"])
                if not self.json_body_cache.get("active"):
                    for k in [k for k, v in SESSIONS.items() if v["user_id"] == parts[1]]:
                        SESSIONS.pop(k, None)
                return self.send_json({"ok": True})
            if parts[0] == "users" and len(parts) == 3 and parts[2] == "password":
                S.set_password(c, parts[1], self.json_body().get("password", ""), must_change=True)
                for k in [k for k, v in SESSIONS.items() if v["user_id"] == parts[1]]:
                    SESSIONS.pop(k, None)
                return self.send_json({"ok": True})
            if p == "geocode/missing":
                return self.send_json(S.geocode_missing(c, self.json_body().get("date", "")))
            if p == "fee-revisions":
                return self.send_json({"id": S.save_fee_draft(c, self.json_body())})
            if parts[0] == "fee-revisions" and len(parts) == 3 and parts[2] in ("approve", "reject"):
                S.approve_fee_revision(c, parts[1], parts[2] == "approve", self.json_body().get("reason", ""))
                return self.send_json({"ok": True})
            if p == "backups/verify":
                d = self.json_body()
                return self.send_json(S.verify_backup(c, d.get("name", ""), d.get("passphrase", "")))
            if p == "prefs":  # 본인 화면 설정(테마·글자 크기)
                return self.send_json(S.save_prefs(c, self.user["user_id"], self.json_body()))
            if p == "password":  # 본인 비밀번호 변경
                d = self.json_body()
                S.set_password(c, self.user["user_id"], d.get("password", ""), old=d.get("old", ""))
                self.user["must_change"] = False
                return self.send_json({"ok": True})
            if p == "settings":
                S.save_settings(c, self.json_body())
                return self.send_json(S.settings(c))
        return self.send_json({"error": "없음"}, 404)

    def route_plan(self, d):
        """노선 계산: 자료 모으기는 잠금 안(짧게), 계산(OR-Tools·카카오 길찾기)은 잠금 밖, 이동시간 캐시·이력 저장만 다시 잠금 안."""
        date = d["date"]
        ids = set(d.get("request_ids") or [])
        with LOCK:
            c = CON
            crews = [x for x in S.list_crews(c) if x["id"] in set(d.get("crew_ids") or []) and x["active"]]
            reqs = []
            for r in S.list_requests(c, date=date):
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
        pref = {r["id"]: dongs[r["dong"]]["crew_id"] for r in reqs if r.get("dong") in dongs and dongs[r["dong"]]["crew_id"]} \
            if st["capacity"]["prefer_dong_crew"] else {}
        cache = BufferedTravelCache() if method == "KAKAO" else None
        try:
            res = routing.plan(reqs, crews, st["depot"], osrm_url=st["osrm_url"], service_base=st["service_min_base"],
                               travel_method=method, kakao_key=kakao_key, travel_cache=cache,
                               service_per_item=st["service_min_per_item"], time_limit_s=int(d.get("time_limit") or min(20, max(5, len(reqs) // 10))),
                               volume_overrides=ov, unload_site=st["unload_site"], unload_min=st["unload_min"],
                               max_unloads=st["max_unloads"], end_site=st["end_site"], crew_pref=pref,
                               unload_hours=st["unload_hours"], lunch=st["lunch"], base_first=st["base_first"])
        finally:
            if cache is not None:
                cache.close()
        res["prefer_dong_crew"] = bool(st["capacity"]["prefer_dong_crew"])
        if method == "KAKAO":  # 좌표만 보냈다는 기록(건수만) + 새로 받은 이동시간 저장
            mi = res.get("matrix_info") or {}
            with LOCK:
                routing.TravelCache(CON).put_many(cache.pending)
                with S.tx(CON):
                    S.audit(CON, "settings", "kakao", "ROUTE_MATRIX", {"calls": mi.get("api_calls", 0), "cache": mi.get("cache_hits", 0), "estimated": mi.get("fallback_pairs", 0)})
        res["requests"] = {r["id"]: {"receipt_no": r["receipt_no"], "name": r["name"], "address": r["address"], "lat": r["lat"],
                                     "lon": r["lon"], "must_today": r["must_today"], "appt": [r["appt_start"], r["appt_end"]],
                                     "status": r["status"], "dong": r["dong"], "pref_crew_id": pref.get(r["id"]),
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
        import html as _h
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
        import html as _h
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
        import html as _h
        return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>{_h.escape(title)}</title>'
                f'<link rel="stylesheet" href="/doc.css"><script src="/print.js" defer></script></head><body class="{cls}">'
                f'<div class="tools"><button type="button" id="print">인쇄</button></div>{body}</body></html>')

    def receipt_html(self, rid: str) -> str:
        """접수증(신청인 보관용): 접수번호·부과번호·품목·금액(계산→감면→부과)·배출일·배출 장소 안내·문의처."""
        import html as _h
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
        import html as _h
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
        import html as _h
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


class BufferedTravelCache(routing.TravelCache):
    """노선 계산 중(잠금 밖) 쓰는 이동시간 캐시: 읽기는 조회 전용 연결, 새 값은 모아 두었다가 계산 뒤 잠금 안에서 저장."""

    def __init__(self):
        super().__init__(store.connect(readonly=True))
        self.pending: dict = {}

    def get_many(self, pairs):
        got = super().get_many(pairs)
        return {**got, **{k: v for k, v in self.pending.items() if k in set(pairs)}}

    def put_many(self, vals):
        self.pending.update(vals or {})

    def close(self):
        try:
            self.con.close()
        except Exception:  # noqa: BLE001
            pass


def dt_now() -> str:
    import datetime as _dt
    return _dt.datetime.now(S.KST).strftime("%Y-%m-%d %H:%M")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def verify_request(self, request, client_address):  # LAN 모드: 허용 대역 밖 PC는 연결 자체를 끊는다
        return not LAN or ip_allowed(client_address[0], LAN["nets"])


def cleanup_sessions() -> int:
    """만료된 세션(무조작·절대 만료)과 지난 인쇄 자료를 메모리에서 지운다."""
    now_ = time.time()
    gone = [k for k, v in SESSIONS.items() if v["exp"] < now_ or now_ - v.get("created", 0) > ABS_S]
    for k in gone:
        SESSIONS.pop(k, None)
    for k in [k for k, v in PRINTS.items() if v["exp"] < now_]:
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
                last = (S.get_setting(CON, "last_daily_tasks", None) or {}).get("at")
            if is_due:  # 잠금은 DB를 쓰는 짧은 동안만(파일 봉인·복사는 잠금 밖) → 백업 중에도 화면 요청이 기다리지 않음
                r = BK.make_auto(CON, "SCHEDULE", lock=LOCK)
                LOG.info("자동 백업 %s", "성공" if r["ok"] else "실패: " + scrub(r.get("error", "")))
            now_kst = time.strftime("%H:%M", time.gmtime(time.time() + 9 * 3600))
            if (S._kst_date(last) or "") < S.today_kst() and now_kst >= DAILY_AT:
                res = OPS.daily_tasks(CON, LOCK, lambda: store.connect(readonly=True))
                LOG.info("하루 예약 작업 %s", json.dumps({k: v for k, v in res.items() if not isinstance(v, dict)}, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            LOG.error("예약 작업 오류\n%s", scrub(traceback.format_exc()))


def serve(port: int = 8765, lan: dict | None = None, host: str | None = None):
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
    srv = Server((bind, port), Handler)
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
    if os.environ.get("DG_NO_SCHEDULER") != "1":
        threading.Thread(target=scheduler, args=(stop,), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        LOG.info("서버 종료")
