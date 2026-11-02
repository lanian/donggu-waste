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

from . import routing, service as S, store

ROOT = Path(__file__).resolve().parent.parent.parent
UI = ROOT / "pc" / "ui"
WEB = ROOT / "web"
WEB_ALLOWED = {"fees.js", "fees-data.js", "map.js", "vendor/maplibre/maplibre-gl.mjs", "vendor/maplibre/maplibre-gl-worker.mjs",
               "vendor/maplibre/maplibre-gl.css"}
UI_FILES = {"/": "index.html", "/index.html": "index.html", "/pc.js": "pc.js", "/pc.css": "pc.css"}
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob: https://tiles.openfreemap.org; "
       "connect-src 'self' https://tiles.openfreemap.org; worker-src 'self' blob:; object-src 'none'; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")
IDLE_S = 30 * 60
MAX_BODY = 40 * 1024 * 1024

LOCK = threading.Lock()
CON = None
SESSIONS: dict[str, float] = {}
FAILS = {"n": 0, "until": 0.0}
PORT = 8765


def _hash(pw: str, salt: bytes) -> str:
    return hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1).hex()


def password_set() -> bool:
    return bool(S.get_setting(CON, "login"))


def set_password(pw: str):
    if len(pw) < 10:
        raise S.UserError("업무 비밀번호는 10자 이상으로 정해 주세요.")
    salt = secrets.token_bytes(16)
    with S.tx(CON):
        S.set_setting(CON, "login", {"salt": salt.hex(), "hash": _hash(pw, salt)})


def check_password(pw: str) -> bool:
    rec = S.get_setting(CON, "login")
    return bool(rec) and hmac.compare_digest(_hash(pw, bytes.fromhex(rec["salt"])), rec["hash"])


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
        exp = SESSIONS.get(t)
        if not exp or exp < time.time():
            SESSIONS.pop(t, None)
            return None
        SESSIONS[t] = time.time() + IDLE_S
        return t

    def body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise S.UserError("파일이 너무 큽니다.")
        return self.rfile.read(n) if n else b""

    def json_body(self) -> dict:
        b = self.body()
        return json.loads(b.decode() or "{}") if b else {}

    # ---------------------------------------------------------- 진입점
    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
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
            return self.send_json({"setup_needed": not password_set(), "logged_in": bool(self.session())})
        if p == "setup" and method == "POST":
            with LOCK:
                if password_set():
                    raise S.UserError("이미 설정되어 있습니다.")
                set_password(self.json_body().get("password") or "")
            return self.login_ok()
        if p == "login" and method == "POST":
            if time.time() < FAILS["until"]:
                raise S.UserError(f"여러 번 틀렸습니다. {int(FAILS['until'] - time.time())}초 뒤 다시 시도하세요.")
            with LOCK:
                good = check_password(self.json_body().get("password") or "")
            if not good:
                FAILS["n"] += 1
                if FAILS["n"] >= 5:
                    FAILS["until"] = time.time() + min(30 * 2 ** (FAILS["n"] - 5), 1800)
                raise S.UserError("비밀번호가 맞지 않습니다.")
            FAILS.update(n=0, until=0)
            return self.login_ok()
        tok = self.session()
        if not tok:
            return self.send_json({"error": "로그인이 필요합니다.", "login": True}, 401)
        if p == "logout" and method == "POST":
            SESSIONS.pop(tok, None)
            return self.send_json({"ok": True}, extra={"Set-Cookie": "dg_s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})
        with LOCK:
            return self.api_authed(method, p, qs)

    def login_ok(self):
        t = secrets.token_urlsafe(32)
        SESSIONS[t] = time.time() + IDLE_S
        return self.send_json({"ok": True}, extra={"Set-Cookie": f"dg_s={t}; Path=/; HttpOnly; SameSite=Strict"})

    def api_authed(self, method, p, qs):
        c = CON
        parts = p.split("/")
        if method == "GET":
            if p == "bootstrap":
                return self.send_json({"settings": S.settings(c), "crews": S.list_crews(c), "devices": S.list_devices(c),
                                       "today": S.today_kst()})
            if p == "requests":
                return self.send_json(S.list_requests(c, qs.get("q", ""), qs.get("date", ""), qs.get("status", "")))
            if parts[0] == "requests" and len(parts) == 2:
                return self.send_json(S.get_request(c, parts[1]))
            if p == "duplicates":
                return self.send_json(S.find_duplicates(c, qs.get("phone", ""), qs.get("address", ""), qs.get("exclude") or None))
            if p == "batches":
                return self.send_json(S.list_batches(c, qs["date"]))
            if p == "conflicts":
                return self.send_json(S.list_conflicts(c))
            if p == "close":
                return self.send_json(S.day_summary(c, qs["date"]))
            if p == "registration-qr":
                from pc_tool import qr_png_data_uri
                return self.send_json({"image": qr_png_data_uri(S.registration_text(c), "M")[0]})
            if p == "unloads":
                return self.send_json(S.list_unloads(c, qs["date"]))
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
                b = S.photo_bytes(c, parts[1])
                return self.send_bytes(b, "image/jpeg") if b else self.send_json({"error": "없음"}, 404)
        if method == "POST":
            if p == "requests":
                d = self.json_body()
                return self.send_json(S.create_request(c, d))
            if parts[0] == "requests" and len(parts) == 2:
                d = self.json_body()
                return self.send_json(S.update_request(c, parts[1], d, d.get("reason", "")))
            if parts[0] == "requests" and len(parts) == 3 and parts[2] == "cancel":
                return self.send_json(S.cancel_request(c, parts[1], self.json_body().get("reason", "")))
            if parts[0] == "requests" and len(parts) == 3 and parts[2] == "location":
                d = self.json_body()
                S.set_location(c, parts[1], d["lat"], d["lon"])
                return self.send_json({"ok": True})
            if p == "route":
                return self.send_json(self.route_plan(self.json_body()))
            if p == "assign":
                d = self.json_body()
                return self.send_json(S.confirm_plan(c, d["date"], d["plan"]))
            if p == "bundles":
                b = S.make_bundle(c, self.json_body()["batch_id"])
                return self.send_json({k: b[k] for k in ("bundle_id", "bytes", "count", "work_date", "assign_ver")} | {"frames": len(b["frames"])})
            if parts[0] == "bundles" and len(parts) == 3 and parts[2] == "receipt":
                S.confirm_receipt(c, parts[1], self.json_body().get("code", ""))
                return self.send_json({"ok": True})
            if p == "import/result":
                return self.send_json(S.import_result(c, self.body(), "MAIL"))
            if p == "import/photos":
                return self.send_json(S.import_photos(c, self.body()))
            if parts[0] == "conflicts" and len(parts) == 2:
                d = self.json_body()
                S.resolve_conflict(c, parts[1], d.get("action", ""), d.get("reason", ""))
                return self.send_json({"ok": True})
            if p == "close":
                d = self.json_body()
                return self.send_json(S.close_day(c, d["date"], d.get("note", "")))
            if p == "crews":
                return self.send_json({"id": S.save_crew(c, self.json_body())})
            if p == "devices/register":
                d = self.json_body()
                return self.send_json(S.register_device(c, d.get("code", ""), d.get("label", "")))
            if parts[0] == "devices" and len(parts) == 3 and parts[2] == "status":
                S.set_device_status(c, parts[1], self.json_body().get("status", ""))
                return self.send_json({"ok": True})
            if parts[0] == "requests" and len(parts) == 3 and parts[2] == "payments":
                return self.send_json(S.add_payment(c, parts[1], self.json_body()))
            if parts[0] == "payments" and len(parts) == 3 and parts[2] == "void":
                return self.send_json(S.void_payment(c, parts[1], self.json_body().get("reason", "")))
            if p == "unloads":
                return self.send_json({"id": S.add_unload(c, self.json_body())})
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
        res = routing.plan(reqs, crews, st["depot"], osrm_url=st["osrm_url"], service_base=st["service_min_base"],
                           service_per_item=st["service_min_per_item"], time_limit_s=int(d.get("time_limit", 5)),
                           volume_overrides=S.volume_overrides(c))
        res["requests"] = {r["id"]: {"receipt_no": r["receipt_no"], "name": r["name"], "address": r["address"], "lat": r["lat"],
                                     "lon": r["lon"], "must_today": r["must_today"], "appt": [r["appt_start"], r["appt_end"]],
                                     "status": r["status"]} for r in reqs}
        return res

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

    def bundle_page(self, bundle_short: str):
        from pc_tool import display_html, qr_png_data_uri
        r = S.rows(CON, "SELECT t.*, c.name AS crew FROM transfer_bundle t LEFT JOIN assignment_batch b ON b.id=t.batch_id LEFT JOIN crew c ON c.id=b.crew_id WHERE t.id LIKE ?",
                   (bundle_short + "%",))
        if len(r) != 1:
            return self.send_json({"error": "없음"}, 404)
        b = r[0]
        frames = json.loads(b["frames_json"])
        imgs = [qr_png_data_uri(f, "M")[0] for f in frames]
        page = display_html(imgs, b["id"][:8], b["work_date"], b["assign_ver"], "", 3, b["crew"] or "").encode()
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
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"동구 대형폐기물 PC 업무 화면: http://127.0.0.1:{port}  (이 PC에서만 열립니다. 끝내려면 Ctrl+C)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
