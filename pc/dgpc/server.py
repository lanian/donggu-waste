"""PC 업무 화면 서버. 이 PC 안(127.0.0.1)에서만 열리며 휴대폰이나 외부에서 접속하는 서버가 아니다.

보호
- 127.0.0.1에만 바인딩, Host 헤더가 127.0.0.1/localhost가 아니면 거부(DNS 리바인딩 방지)
- 업무 비밀번호 로그인(scrypt), 세션 쿠키(HttpOnly, SameSite=Strict), 30분 무조작 시 만료
- 모든 API 요청에 사용자 지정 헤더(X-DG-CSRF) 필수 → 다른 웹사이트가 이 PC의 API를 부를 수 없음
- 응답에 CSP·nosniff·no-referrer·no-store
- 외부로 나가는 요청은 없다(화면의 바탕 지도 조각만 브라우저가 지도 서버에서 받음, 개인정보 미포함)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import traceback
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import area as A, routing, service as S, store

ROOT = Path(__file__).resolve().parent.parent.parent
UI = ROOT / "pc" / "ui"
WEB = ROOT / "web"
WEB_ALLOWED = {"fees.js", "fees-data.js", "map.js", "proto.js", "vendor/jsQR.js", "vendor/maplibre/maplibre-gl.mjs", "vendor/maplibre/maplibre-gl-worker.mjs",
               "vendor/maplibre/maplibre-gl.css"}
UI_FILES = {"/": "index.html", "/index.html": "index.html", "/pc.js": "pc.js", "/pc.css": "pc.css", "/print.css": "print.css", "/print.js": "print.js"}
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob: https://tiles.openfreemap.org; "
       "connect-src 'self' https://tiles.openfreemap.org; worker-src 'self' blob:; media-src 'self' blob: mediastream:; object-src 'none'; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")
# 세션 무조작 만료(30분). 자동 시험에서만 DG_SESSION_IDLE_S 로 줄일 수 있다(60초 이상).
IDLE_S = max(60, int(os.environ.get("DG_SESSION_IDLE_S") or 30 * 60))
MAX_BODY = 40 * 1024 * 1024

LOCK = threading.Lock()
CON = None
SESSIONS: dict[str, dict] = {}
FAILS: dict[str, dict] = {}  # 아이디별 로그인 실패 {"n": 횟수, "until": 잠금 끝 시각, "at": 마지막 실패}
PRINTS: dict[str, dict] = {}  # 고른 접수 인쇄: 잠깐 쓰는 표(주소에 접수 ID를 길게 싣지 않게)
PORT = 8765


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
                       "fee-table", "geocode", "stats", "stats.csv", "calendar", "dong-detect", "holidays", "dongs"},
               "POST": {"requests", "route", "assign", "bundles", "import", "conflicts", "close", "payments", "unloads", "qrtest", "password",
                        "fee-revisions", "geocode", "links"}},
    "FIELD": {"GET": {"bootstrap", "requests", "batches", "close", "unloads", "fee-usage", "qrtests", "photo", "me", "fee-table"},
              "POST": {"import", "unloads", "qrtest", "password"}},
}


LEAD_GET_DENY = {"backups", "photo-purge", "registration-qr"}
LEAD_POST = {"password", "close/approve"}


def allowed(role: str, method: str, p: str) -> bool:
    if role == "ADMIN":
        return True
    head = p.split("/")[0]
    if role == "LEAD":
        return (method == "GET" and head not in LEAD_GET_DENY) or (method == "POST" and p in LEAD_POST)
    if p == "close/approve":  # 마감 확인은 팀장(검토자)·관리자만
        return False
    if role == "FIELD" and method == "POST" and head == "requests":
        return False
    if role == "INTAKE" and method == "POST" and p == "fee-revisions":  # 개정안 작성은 관리자, 승인은 다른 담당자도 가능
        return False
    return head in PERM.get(role, {}).get(method, set())


class Handler(BaseHTTPRequestHandler):
    server_version = "dgpc"
    sys_version = ""

    def log_message(self, fmt, *args):  # 요청 주소에 개인정보가 들어갈 수 있어 접근 로그를 남기지 않는다
        pass

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
        return host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def session(self) -> str | None:
        m = re.search(r"(?:^|;\s*)dg_s=([A-Za-z0-9_-]+)", self.headers.get("Cookie") or "")
        if not m:
            return None
        t = m.group(1)
        sess = SESSIONS.get(t)
        if not sess or sess["exp"] < time.time():
            SESSIONS.pop(t, None)
            return None
        sess["exp"] = time.time() + IDLE_S
        return t

    def body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
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
        if cs is not None and not (cs and S.request_in_crew(CON, rid, cs)):
            raise S.UserError("내 팀에 배정된 접수만 볼 수 있습니다.")

    def me(self) -> dict:
        u = self.user
        return {"username": u["username"], "display_name": u["display_name"], "role": u["role"], "role_label": S.ROLES[u["role"]], "crew_id": u.get("crew_id"),
                "must_change": bool(u.get("must_change"))}

    # ---------------------------------------------------------- 진입점
    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        self.json_body_cache = None
        self.user = None
        S.ACTOR.set("system")
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
            if method == "GET" and path.startswith("/qrtest/"):
                if not self.session():
                    return self.send_json({"error": "로그인이 필요합니다."}, 401)
                return self.qrtest_page(path[8:])
            return self.send_json({"error": "없음"}, 404)
        except S.UserError as e:
            return self.send_json({"error": str(e)}, 400)
        except Exception:  # 오류 내용에 개인정보가 섞일 수 있어 화면에는 일반 문구만
            traceback.print_exc()
            return self.send_json({"error": "처리 중 오류가 발생했습니다."}, 500)

    # ---------------------------------------------------------- API
    def api(self, method, p, qs):
        if p == "state" and method == "GET":
            with LOCK:
                need = not S.users_exist(CON)
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
            d = self.json_body()
            wait = login_blocked(d.get("username") or "")
            if wait:
                raise S.UserError(f"이 아이디로 여러 번 틀렸습니다. {wait}초 뒤 다시 시도하세요.")
            with LOCK:
                good = S.check_login(CON, d.get("username") or "", d.get("password") or "")
                if good:
                    S.ACTOR.set(good["username"])
                    with S.tx(CON):
                        S.audit(CON, "user", good["id"], "LOGIN", None)
                else:
                    S.ACTOR.set((d.get("username") or "?")[:30])
                    with S.tx(CON):
                        S.audit(CON, "user", "-", "LOGIN_FAIL", None)
            if not good:
                login_failed(d.get("username") or "")
                raise S.UserError("아이디 또는 비밀번호가 맞지 않습니다.")
            login_succeeded(d.get("username") or "")
            return self.login_ok(good)
        tok = self.session()
        if not tok:
            return self.send_json({"error": "로그인이 필요합니다.", "login": True}, 401)
        if p == "logout" and method == "POST":
            SESSIONS.pop(tok, None)
            return self.send_json({"ok": True}, extra={"Set-Cookie": "dg_s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})
        sess = SESSIONS[tok]
        if not allowed(sess["role"], method, p):
            return self.send_json({"error": "이 작업은 권한이 없습니다."}, 403)
        if sess.get("must_change") and p not in ("me", "password", "bootstrap"):
            return self.send_json({"error": "비밀번호를 먼저 바꿔 주세요.", "must_change": True}, 403)
        self.user = sess
        with LOCK:
            S.ACTOR.set(sess["username"])
            return self.api_authed(method, p, qs)

    def login_ok(self, u):
        t = secrets.token_urlsafe(32)
        SESSIONS[t] = {"exp": time.time() + IDLE_S, "user_id": u["id"], "username": u["username"], "role": u["role"],
                       "display_name": u["display_name"], "must_change": u.get("must_change", False), "crew_id": u.get("crew_id")}
        return self.send_json({"ok": True}, extra={"Set-Cookie": f"dg_s={t}; Path=/; HttpOnly; SameSite=Strict"})

    def api_authed(self, method, p, qs):
        c = CON
        parts = p.split("/")
        if method == "GET":
            if p == "bootstrap":
                return self.send_json({"settings": S.settings(c), "crews": S.list_crews(c), "devices": S.list_devices(c),
                                       "today": S.today_kst(), "me": self.me(), "fee": S.fee_table_for_phone(c),
                                       "dongs": A.list_dongs(c), "channels": S.CHANNELS, "cancel_codes": S.CANCEL_CODES, "postpone_codes": S.POSTPONE_CODES,
                                       "contact_kinds": S.CONTACT_KINDS, "session_idle_s": IDLE_S, "roles": S.ROLES, "audit_entities": S.AUDIT_ENTITIES})
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
                return self.send_json(A.detect_dong(c, qs.get("address", "")))
            if p == "dongs":
                return self.send_json(A.list_dongs(c))
            if p == "holidays":
                return self.send_json(A.list_holidays(c, qs.get("start", ""), qs.get("end", "")))
            if p == "requests/import-template":
                return self.send_bytes(S.import_template(), "text/csv; charset=utf-8", extra={"Content-Disposition": 'attachment; filename="donggu-intake-template.csv"'})
            if p == "audit.csv":
                return self.send_bytes(S.audit_csv(c, qs), "text/csv; charset=utf-8", extra={"Content-Disposition": 'attachment; filename="donggu-audit.csv"'})
            if p == "stats":
                return self.send_json(S.stats(c, qs.get("start", ""), qs.get("end", "")))
            if p == "stats.csv":
                kind = "days" if qs.get("kind") == "days" else "requests"
                body = S.stats_csv(c, qs.get("start", ""), qs.get("end", ""), kind)
                fn = f'donggu-{"daily" if kind == "days" else "requests"}-{qs.get("start")}_{qs.get("end")}.csv'
                return self.send_bytes(body, "text/csv; charset=utf-8", extra={"Content-Disposition": f'attachment; filename="{fn}"'})
            if p == "geocode":
                return self.send_json(S.geocode(c, qs.get("address", "")) or {"none": True})
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
                return self.send_json(S.search_requests(c, qs, scope))
            if parts[0] == "requests" and len(parts) == 2:
                self.require_crew_request(parts[1])
                return self.send_json(S.get_request(c, parts[1]))
            if p == "duplicates":
                return self.send_json(S.find_duplicates(c, qs.get("phone", ""), qs.get("address", ""), qs.get("exclude") or None))
            if p == "batches":
                out = S.list_batches(c, qs["date"])
                if self.crew_scope() is not None:
                    out = [b for b in out if b["crew_id"] == self.crew_scope()]
                return self.send_json(out)
            if p == "conflicts":
                return self.send_json(S.list_conflicts(c))
            if p == "close":
                return self.send_json(S.day_summary(c, qs["date"], self.crew_scope()))
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
                                        "override": ov.get(i)} for i in ids if i in S.FEE_ROWS])
            if p == "fee-usage":
                return self.send_json(S.fee_usage(c))
            if p == "qrtests":
                return self.send_json(S.list_qr_tests(c))
            if parts[0] == "photo" and len(parts) == 2:
                self.require_crew_request(S.photo_request(c, parts[1]) or "")
                b = S.photo_bytes(c, parts[1])
                return self.send_bytes(b, "image/jpeg") if b else self.send_json({"error": "없음"}, 404)
        if method == "POST":
            if p == "requests":
                d = self.json_body()
                return self.send_json(S.create_request(c, d))
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
            if p == "requests/import":
                return self.send_json(S.import_requests(c, self.body(), preview=qs.get("preview") == "1", skip_duplicates=qs.get("skip_dup") == "1"))
            if parts[0] == "requests" and len(parts) == 2:
                d = self.json_body()
                return self.send_json(S.update_request(c, parts[1], d, d.get("reason", "")))
            if parts[0] == "requests" and len(parts) == 3 and parts[2] == "cancel":
                d = self.json_body()
                return self.send_json(S.cancel_request(c, parts[1], d.get("reason", ""), d.get("code") or None))
            if parts[0] == "requests" and len(parts) == 3 and parts[2] == "contacts":
                return self.send_json({"id": S.add_contact(c, parts[1], self.json_body())})
            if parts[0] == "requests" and len(parts) == 3 and parts[2] == "location":
                d = self.json_body()
                S.set_location(c, parts[1], d["lat"], d["lon"])
                return self.send_json({"ok": True})
            if p == "route":
                return self.send_json(self.route_plan(self.json_body()))
            if p == "assign":
                d = self.json_body()
                return self.send_json(S.confirm_plan(c, d["date"], d["plan"], d.get("adjust_reason", ""), bool(d.get("adjusted"))))
            if p == "bundles":
                b = S.make_bundle(c, self.json_body()["batch_id"])
                return self.send_json({k: b[k] for k in ("bundle_id", "bytes", "count", "work_date", "assign_ver")} | {"frames": len(b["frames"])})
            if parts[0] == "bundles" and len(parts) == 3 and parts[2] == "receipt":
                S.confirm_receipt(c, parts[1], self.json_body().get("code", ""))
                return self.send_json({"ok": True})
            if p in ("import/result", "import/photos") and self.crew_scope() is not None:
                body = self.body()
                if S.device_crew(c, body) != self.crew_scope():
                    raise S.UserError("내 팀에 연결된 휴대폰의 파일만 가져올 수 있습니다.")
                self.body = lambda b=body: b  # 이미 읽은 본문을 다시 쓰도록
            if p == "import/result":
                return self.send_json(S.import_result(c, self.body(), "QR" if qs.get("channel") == "QR" else "MAIL"))
            if p == "import/solo":
                import base64 as _b64
                pw = _b64.b64decode(self.headers.get("X-DG-Pass") or "").decode("utf-8", "replace")
                return self.send_json(S.import_solo(c, self.body(), pw, qs.get("crew") or None, preview=qs.get("preview") == "1"))
            if p == "import/photos":
                return self.send_json(S.import_photos(c, self.body()))
            if parts[0] == "conflicts" and len(parts) == 2:
                d = self.json_body()
                S.resolve_conflict(c, parts[1], d.get("action", ""), d.get("reason", ""))
                return self.send_json({"ok": True})
            if p == "close":
                d = self.json_body()
                return self.send_json(S.close_day(c, d.get("date", ""), d.get("note", "")))
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
                return self.send_json(S.add_payment(c, parts[1], self.json_body()))
            if parts[0] == "payments" and len(parts) == 3 and parts[2] == "void":
                return self.send_json(S.void_payment(c, parts[1], self.json_body().get("reason", "")))
            if p == "unloads":
                d = self.json_body()
                if self.crew_scope() is not None and d.get("crew_id") != self.crew_scope():
                    raise S.UserError("내 팀의 하차만 기록할 수 있습니다.")
                return self.send_json({"id": S.add_unload(c, d)})
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
            if p == "addr-import":
                cols = None
                if qs.get("columns"):
                    from dgpc import geo
                    cols = geo.parse_columns_arg(qs["columns"])
                return self.send_json(S.import_addr_points(c, self.body(), qs.get("region", ""), cols, preview=qs.get("preview") == "1"))
            if p == "fee-revisions":
                return self.send_json({"id": S.save_fee_draft(c, self.json_body())})
            if parts[0] == "fee-revisions" and len(parts) == 3 and parts[2] in ("approve", "reject"):
                S.approve_fee_revision(c, parts[1], parts[2] == "approve", self.json_body().get("reason", ""))
                return self.send_json({"ok": True})
            if p == "backup":
                r = S.make_backup(c, self.json_body().get("passphrase", ""))
                return self.send_json({k: r[k] for k in ("name", "bytes", "sha256")})
            if p == "backups/verify":
                d = self.json_body()
                return self.send_json(S.verify_backup(c, d.get("name", ""), d.get("passphrase", "")))
            if p == "photo-purge":
                return self.send_json(S.purge_photos(c))
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
        c = CON
        date = d["date"]
        ids = set(d.get("request_ids") or [])
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
        pref = {r["id"]: dongs[r["dong"]]["crew_id"] for r in reqs if r.get("dong") in dongs and dongs[r["dong"]]["crew_id"]} \
            if st["capacity"]["prefer_dong_crew"] else {}
        res = routing.plan(reqs, crews, st["depot"], osrm_url=st["osrm_url"], service_base=st["service_min_base"],
                           service_per_item=st["service_min_per_item"], time_limit_s=int(d.get("time_limit", 5)),
                           volume_overrides=S.volume_overrides(c), unload_site=st["unload_site"], unload_min=st["unload_min"],
                           max_unloads=st["max_unloads"], end_site=st["end_site"], crew_pref=pref)
        res["prefer_dong_crew"] = bool(st["capacity"]["prefer_dong_crew"])
        res["requests"] = {r["id"]: {"receipt_no": r["receipt_no"], "name": r["name"], "address": r["address"], "lat": r["lat"],
                                     "lon": r["lon"], "must_today": r["must_today"], "appt": [r["appt_start"], r["appt_end"]],
                                     "status": r["status"], "dong": r["dong"], "pref_crew_id": pref.get(r["id"])} for r in reqs}
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
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(page)

    def qrtest_page(self, short: str):
        from pc_tool import display_html, qr_png_data_uri
        with LOCK:
            r = S.rows(CON, "SELECT * FROM qr_test WHERE id LIKE ?", (short + "%",))
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

    def bundle_page(self, bundle_short: str):
        from pc_tool import display_html, qr_png_data_uri
        r = S.rows(CON, "SELECT t.*, c.name AS crew FROM transfer_bundle t LEFT JOIN assignment_batch b ON b.id=t.batch_id LEFT JOIN crew c ON c.id=b.crew_id WHERE t.id LIKE ?",
                   (bundle_short + "%",))
        if len(r) != 1:
            return self.send_json({"error": "없음"}, 404)
        b = r[0]
        frames = json.loads(b["frames_json"])
        imgs = [qr_png_data_uri(f, "M")[0] for f in frames]
        n = S.one(CON, "SELECT COUNT(*) AS n FROM assignment_stop WHERE batch_id=?", (b["batch_id"],))["n"] if b["batch_id"] else ""
        page = display_html(imgs, b["id"][:8], b["work_date"], b["assign_ver"], n, 3, b["crew"] or "").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(page)


def serve(port: int = 8765):
    global CON, PORT
    PORT = port
    CON = store.connect()
    S.ensure_pc_keys(CON)
    S.migrate_legacy_login(CON)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"동구 대형폐기물 PC 업무 화면: http://127.0.0.1:{port}  (이 PC에서만 열립니다. 끝내려면 Ctrl+C)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
