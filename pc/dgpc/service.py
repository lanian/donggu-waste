"""PC 업무 로직: 접수·이력·중복·팀·기기·배정·목록 묶음·결과 병합·충돌·사진 회수·마감.

원칙
- 원본(접수)은 PC에만 있다. 수정은 이전 내용을 버전으로 보존하고 사유를 남긴다. 취소·정정도 지우지 않는다.
- 현장 결과는 이벤트로만 들어오며 같은 이벤트는 한 번만 반영된다(event_id 고유).
- 오래된 배정의 결과, 취소된 건의 결과는 자동 반영하지 않고 충돌함에 넣는다. 기기 시각으로 덮어쓰지 않는다.
- 한 접수는 활성 배정이 하나뿐이다(두 팀이 오프라인으로 같은 건을 처리하지 않게).
"""
from __future__ import annotations

import base64
import contextlib
import contextvars
import datetime as dt
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import serialization

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dgproto as P  # noqa: E402

KST = dt.timezone(dt.timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent.parent
FEE_TABLE = json.loads((ROOT / "data" / "fee_table.json").read_text(encoding="utf8"))
FEE_ROWS = {r["id"]: r for r in FEE_TABLE["rows"]}
TYPE_PHOTOS = 4
FINAL = {"DONE", "PARTIAL", "NOT_COLLECTED", "ON_HOLD"}
CHANNELS = ("전화", "방문", "온라인", "단체(공동주택)", "현장", "기타")  # 접수 경로 (현장 = 수거 중 휴대폰으로 받은 접수)
# 사유 코드 (코드 + 직접 입력 글). 기타(OTHER)는 글이 꼭 있어야 한다.
CANCEL_CODES = {"OUT_OF_AREA": "관할 밖", "RESIDENT": "주민 취소", "DUPLICATE": "중복 접수", "ITEM_REFUSED": "품목 불가", "OTHER": "기타"}
POSTPONE_CODES = {"RESIDENT": "주민 요청", "OVER_CAPACITY": "수거 한도 초과", "NO_ROUTE": "배정 불가(노선·시간)", "REVISIT": "재방문(미수거·보류)",
                  "HOLIDAY": "휴무일", "OTHER": "기타"}
CONTACT_KINDS = ("문의", "독촉", "변경요청", "불만")


class UserError(Exception):
    """화면에 그대로 보여 줄 수 있는 업무 오류."""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_kst() -> str:
    return dt.datetime.now(KST).date().isoformat()


@contextlib.contextmanager
def tx(con):
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def rows(con, sql, args=()):
    cur = con.execute(sql, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def one(con, sql, args=()):
    r = rows(con, sql, args)
    return r[0] if r else None


ACTOR: contextvars.ContextVar[str] = contextvars.ContextVar("actor", default="system")


def chain_hash(prev: str, fields: list) -> str:
    """해시 사슬: sha256(이전 해시 + 이 행 내용(JSON)). 한 행을 고치거나 지우면 그 뒤 사슬이 모두 어긋난다."""
    body = json.dumps(fields, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256((prev + body).encode("utf-8")).hexdigest()


def _audit_fields(r: dict) -> list:
    return [r["id"], r["entity"], r["entity_id"], r["action"], r["detail"], r["at"], r["actor"]]


def audit(con, entity, entity_id, action, detail=None):
    prev = con.execute("SELECT hash FROM audit_log WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    prev = prev[0] if prev else (get_setting(con, "audit_chain_anchor", "") or "")
    d = json.dumps(detail, ensure_ascii=False) if detail is not None else None
    at, actor = now(), ACTOR.get()
    cur = con.execute("INSERT INTO audit_log (entity, entity_id, action, detail, at, actor, prev_hash, legacy) VALUES (?,?,?,?,?,?,?,0)",
                      (entity, entity_id, action, d, at, actor, prev))
    rid = cur.lastrowid
    con.execute("UPDATE audit_log SET hash=? WHERE id=?",
                (chain_hash(prev, _audit_fields({"id": rid, "entity": entity, "entity_id": entity_id, "action": action, "detail": d, "at": at, "actor": actor})), rid))


# ------------------------------------------------------------------ PC 키 (암호화 DB 안에 보관)
def ensure_pc_keys(con) -> bool:
    if one(con, "SELECT 1 AS x FROM pc_key WHERE name='sign'"):
        return False
    with tx(con):
        for name in ("sign", "kex"):
            pem = P.gen_key().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
            con.execute("INSERT INTO pc_key VALUES (?,?,?)", (name, pem, now()))
    return True


def pc_key(con, name):
    r = one(con, "SELECT pem FROM pc_key WHERE name=?", (name,))
    if not r:
        raise UserError("PC 키가 없습니다. 초기화를 먼저 하세요.")
    return serialization.load_pem_private_key(r["pem"], None)


def registration_text(con) -> str:
    raw = P.MAGIC + bytes([P.VERSION, 0x09]) + P.pub_raw(pc_key(con, "sign")) + P.pub_raw(pc_key(con, "kex"))
    return P.b45encode(raw)


# ------------------------------------------------------------------ 기기·팀
def register_device(con, code: str, label: str = "") -> dict:
    try:
        dev, kex, sig = P.parse_device_code(code)
    except P.ProtocolError as e:
        raise UserError(f"기기 코드가 올바르지 않습니다 ({e.code}).")
    dev_id = str(uuid.UUID(bytes=dev))
    with tx(con):
        con.execute("INSERT OR REPLACE INTO device (id, kex_pub, sign_pub, status, registered_at, label) VALUES (?,?,?,'ACTIVE',?,?)",
                    (dev_id, kex, sig, now(), label))
        audit(con, "device", dev_id, "REGISTER", {"label": label})
    return {"device_id": dev_id, "fingerprint": P.fingerprint(P.pub_raw(pc_key(con, "sign")), kex, sig)}


def set_device_status(con, dev_id: str, status: str):
    if status not in ("ACTIVE", "LOST", "RETIRED"):
        raise UserError("기기 상태 값이 올바르지 않습니다.")
    with tx(con):
        con.execute("UPDATE device SET status=? WHERE id=?", (status, dev_id))
        audit(con, "device", dev_id, "STATUS", {"status": status})


def list_devices(con):
    return rows(con, "SELECT id, status, registered_at, label FROM device ORDER BY registered_at")


def save_crew(con, data: dict) -> str:
    cid = data.get("id") or str(uuid.uuid4())
    name = (data.get("name") or "").strip()
    if not name:
        raise UserError("팀 이름을 적어 주세요.")
    dev = data.get("device_id") or None
    if dev and not one(con, "SELECT 1 AS x FROM device WHERE id=?", (dev,)):
        raise UserError("등록되지 않은 기기입니다.")
    for k in ("shift_start", "shift_end"):
        if not re.fullmatch(r"\d\d:\d\d", data.get(k) or "09:00"):
            raise UserError("근무 시간은 HH:MM 형식으로 적어 주세요.")
    with tx(con):
        con.execute("""INSERT INTO crew (id, name, device_id, capacity_m3, shift_start, shift_end, active) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name, device_id=excluded.device_id, capacity_m3=excluded.capacity_m3,
            shift_start=excluded.shift_start, shift_end=excluded.shift_end, active=excluded.active""",
                    (cid, name, dev, float(data.get("capacity_m3") or 8), data.get("shift_start") or "09:00",
                     data.get("shift_end") or "17:00", 1 if data.get("active", True) else 0))
        audit(con, "crew", cid, "SAVE", {"name": name, "device_id": dev})
    return cid


def list_crews(con):
    return rows(con, "SELECT * FROM crew ORDER BY name")


# ------------------------------------------------------------------ 설정
def get_setting(con, key, default=None):
    r = one(con, "SELECT value FROM meta WHERE key=?", (key,))
    return json.loads(r["value"]) if r else default


def set_setting(con, key, value):
    con.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)))


def settings(con) -> dict:
    return {
        "depot": get_setting(con, "depot", {"lat": 35.1462, "lon": 126.9233, "label": "차고지(기본값: 동구청 부근)"}),
        "osrm_url": get_setting(con, "osrm_url", ""),
        "external": external_settings(con),
        "service_min_base": get_setting(con, "service_min_base", 5),
        "service_min_per_item": get_setting(con, "service_min_per_item", 3),
        "photo_retention_days": get_setting(con, "photo_retention_days", 90),
        "unload_site": get_setting(con, "unload_site", None),
        "end_site": get_setting(con, "end_site", None),
        "jurisdiction": get_setting(con, "jurisdiction", "동구"),
        "unload_min": get_setting(con, "unload_min", 20),
        "max_unloads": get_setting(con, "max_unloads", 2),
        "backup_dir": get_setting(con, "backup_dir", ""),
        "capacity": _area().capacity_settings(con),
        "intake_notices": get_setting(con, "intake_notices", []),
        "contact_info": get_setting(con, "contact_info", "동구청 청소 담당 부서 (전화번호는 설정에서 입력)"),
        "disposal_guide": get_setting(con, "disposal_guide", "수거일 아침 7시까지 집 앞(공동주택은 관리사무소가 정한 곳)에 내놓고, 이 표지를 물건에 붙여 주세요."),
        "monthly_report_title": get_setting(con, "monthly_report_title", "대형폐기물 수거 월보"),
        "reductions": _fin().reductions(con), "reduction_proofs": list(_fin().PROOF_METHODS),
        "pii_retention_years": get_setting(con, "pii_retention_years", 3), "access_log_retention_years": max(2, get_setting(con, "access_log_retention_years", 2)),
        "pay_methods": list(PAY_METHODS), "refund_methods": list(REFUND_PAY_METHODS),
        **_backup_conf(con),
        "fee_version": fee_state(con)["version_id"], "fee_label": FEE_TABLE["label"],
        "fee_checked_on": FEE_TABLE["source_checked_on"], "fee_effective_from": fee_state(con)["effective_from"],
    }


def _area():
    from dgpc import area
    return area


def _backup_conf(con):
    from dgpc import backup
    return backup.conf(con)


def save_settings(con, data: dict):
    with tx(con):
        if "capacity" in data:
            _area().save_capacity(con, data["capacity"] or {})
        if "intake_notices" in data:
            notes = []
            for n in data["intake_notices"] or []:
                kws = n.get("keywords") or []
                if isinstance(kws, str):
                    kws = re.split(r"[,\n]+", kws)
                kws = [k.strip()[:30] for k in kws if k and k.strip()][:30]
                text = (n.get("text") or "").strip()[:300]
                if not text:
                    continue
                if not kws:
                    raise UserError("접수 시 안내마다 품목 낱말을 하나 이상 적어 주세요.")
                notes.append({"keywords": kws, "text": text})
            set_setting(con, "intake_notices", notes[:30])
        audit(con, "settings", "pc", "SAVE", {"keys": ",".join(sorted(data))[:120]})
        for k, lim in (("contact_info", 200), ("disposal_guide", 400), ("monthly_report_title", 60)):
            if k in data:
                v = (data[k] or "").strip()[:lim]
                if k == "monthly_report_title" and not v:
                    raise UserError("월보 제목을 적어 주세요.")
                set_setting(con, k, v)
        if "reductions" in data:
            _fin().save_reductions(con, data["reductions"])
        from dgpc import backup as _bk, privacy as _pv
        _pv.save_settings(con, data)
        _bk.save_settings(con, data)
        if "depot" in data:
            d = data["depot"]
            set_setting(con, "depot", {"lat": float(d["lat"]), "lon": float(d["lon"]), "label": str(d.get("label", "차고지"))})
        if "external" in data:
            _save_external(con, data["external"] or {})
        if "osrm_url" in data:
            u = (data["osrm_url"] or "").strip()
            if u and not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?/?$", u):
                raise UserError("OSRM 주소는 이 PC 안(http://127.0.0.1:포트)만 쓸 수 있습니다.")
            set_setting(con, "osrm_url", u)
        for k in ("service_min_base", "service_min_per_item"):
            if k in data:
                set_setting(con, k, max(0, int(data[k])))
        for key, default_label, what in (("unload_site", "반입지", "반입지"), ("end_site", "종료지", "종료지")):
            if key not in data:
                continue
            u = data[key] or {}
            if u.get("lat") in (None, "") or u.get("lon") in (None, ""):
                set_setting(con, key, None)
            else:
                lat, lon = float(u["lat"]), float(u["lon"])
                if not (33 <= lat <= 39 and 124 <= lon <= 132):
                    raise UserError(f"{what} 좌표가 국내 범위를 벗어났습니다.")
                set_setting(con, key, {"lat": lat, "lon": lon, "label": str(u.get("label") or default_label)[:40]})
        for k, lo, hi in (("unload_min", 0, 180), ("max_unloads", 0, 5)):
            if k in data:
                set_setting(con, k, min(hi, max(lo, int(data[k]))))
        if "jurisdiction" in data:
            set_setting(con, "jurisdiction", (data["jurisdiction"] or "").strip()[:20])
        if "photo_retention_days" in data:
            n = int(data["photo_retention_days"])
            if not 7 <= n <= 3650:
                raise UserError("사진 보관 기간은 7일~3650일 사이로 정해 주세요.")
            set_setting(con, "photo_retention_days", n)
        if "backup_dir" in data:
            b = (data["backup_dir"] or "").strip()
            if b and not Path(b).is_absolute():
                raise UserError("백업 폴더는 이 PC의 전체 경로로 적어 주세요 (예: D:\\동구백업).")
            set_setting(con, "backup_dir", b)


# ------------------------------------------------------------------ 외부 서비스 설정 (v0.11): 행안부 주소 API 승인키·카카오모빌리티 키
# 키는 암호화 DB(meta)에만 두고, 화면(브라우저)에는 저장 뒤 가린 값만 보낸다.
EXTERNAL_KEYS = {"juso_search_key": "주소 검색 승인키", "juso_coord_key": "좌표 승인키", "kakao_rest_key": "카카오모빌리티 REST 키"}
TRAVEL_METHODS = ("ESTIMATE", "OSRM", "KAKAO")


def secret(con, name: str) -> str:
    return get_setting(con, "ext_" + name, "") or ""


def travel_method(con) -> str:
    m = get_setting(con, "travel_method", None)
    if m in TRAVEL_METHODS:
        return m
    return "OSRM" if get_setting(con, "osrm_url", "") else "ESTIMATE"  # 이전 설정 그대로(OSRM 주소가 있으면 OSRM)


def external_settings(con) -> dict:
    from dgpc import external as X
    return {"keys": {k: {"set": bool(secret(con, k)), "mask": X.mask(secret(con, k)), "label": lab} for k, lab in EXTERNAL_KEYS.items()},
            "travel_method": travel_method(con)}


def _save_external(con, d: dict):
    from dgpc import external as X
    for k, lab in EXTERNAL_KEYS.items():
        if k not in (d.get("keys") or {}):
            continue
        v = ((d.get("keys") or {})[k] or "").strip()
        if v and not X.KEY_RE.match(v):
            raise UserError(f"{lab} 형식이 올바르지 않습니다 (영문·숫자 8자 이상).")
        set_setting(con, "ext_" + k, v)
        audit(con, "settings", "external", "KEY_SET" if v else "KEY_CLEAR", {"key": k})  # 키 값은 남기지 않는다
    if "travel_method" in d:
        m = d["travel_method"]
        if m not in TRAVEL_METHODS:
            raise UserError("이동시간 계산 방식을 골라 주세요.")
        if m == "KAKAO" and not secret(con, "kakao_rest_key"):
            raise UserError("카카오모빌리티 REST 키를 먼저 저장하세요.")
        set_setting(con, "travel_method", m)


def _known_names(con) -> list[str]:
    return [r["name"] for r in rows(con, "SELECT DISTINCT name FROM request WHERE pii_purged_at IS NULL AND name IS NOT NULL AND length(name) >= 2")]


def _dong_for_juso(con, it: dict) -> str | None:
    area = _area()
    for n in (it.get("hemdNm"), it.get("emdNm")):
        if n and area.get_dong(con, n):
            return n
    addr = f'{it.get("road_part1") or it.get("road") or ""} {it.get("emdNm") or ""} {it.get("hemdNm") or ""}'
    return area.detect_dong(con, addr).get("dong")


def juso_search(con, q: str, name: str = "") -> dict:
    """PC가 대신 묻는 행정안전부 도로명주소 검색. 보내는 것은 검색어와 승인키뿐.
    검색어가 전화번호·신청인 이름·저장된 주민 이름을 담고 있으면 보내지 않는다. 접속기록에는 검색어 없이 건수만 남긴다."""
    from dgpc import external as X, privacy as _pv
    key = secret(con, "juso_search_key")
    if not key:
        return {"configured": False, "items": []}
    kw = X.clean_keyword(q)
    if len(kw) < 2:
        return {"configured": True, "items": []}
    why = X.keyword_guard(kw, name, _known_names(con))
    if why:
        return {"configured": True, "items": [], "blocked": why}
    try:
        res = X.juso_search(key, kw)
    except X.ExternalError as e:
        _pv.record_access(con, "SEARCH", "", 0, "행안부 주소 검색 실패 (검색어 기록 안 함)")
        return {"configured": True, "items": [], "error": str(e), "code": e.code}
    items = []
    for it in res["items"]:
        addr = it["road_part1"] + (f' ({", ".join(x for x in (it["emdNm"], it["bdNm"]) if x)})' if it["emdNm"] or it["bdNm"] else "")
        items.append({"kind": "행안부", "address": addr, "label": addr + (f' · 지번 {it["jibun"]}' if it["jibun"] else ""), "zip": it["zip"],
                      "dong": _dong_for_juso(con, it), "emdNm": it["emdNm"], "hemdNm": it["hemdNm"], "bname": it["bdNm"], "ref": it["ref"],
                      "lat": None, "lon": None})
    _pv.record_access(con, "SEARCH", "", len(items), "행안부 주소 검색 (검색어 기록 안 함)")
    return {"configured": True, "items": items, "total": res["total"], "coord": bool(secret(con, "juso_coord_key"))}


def juso_coord(con, ref: dict) -> dict:
    """고른 행안부 검색 결과의 출입구 좌표(WGS84). 보내는 것은 좌표 승인키와 건물 식별 번호뿐."""
    from dgpc import external as X
    key = secret(con, "juso_coord_key")
    if not key:
        return {"none": True, "reason": "좌표 승인키가 없어 위치는 지도에서 지정하세요."}
    try:
        lat, lon = X.juso_coord(key, ref)
    except X.ExternalError as e:
        return {"none": True, "reason": str(e), "code": e.code}
    return {"lat": lat, "lon": lon}


def external_test(con, service: str) -> dict:
    """설정 화면 [연결 시험]: 개인정보 없는 고정 값으로 한 번 묻는다."""
    from dgpc import external as X
    try:
        if service == "juso":
            key = secret(con, "juso_search_key")
            if not key:
                raise UserError("주소 검색 승인키를 먼저 저장하세요.")
            r = X.juso_search(key, "동구청", per=1)
            msg = f"주소 검색 정상 (시험 검색 '동구청' {r['total']}건)"
            if secret(con, "juso_coord_key") and r["items"]:
                X.juso_coord(secret(con, "juso_coord_key"), r["items"][0]["ref"])
                msg += " · 좌표 정상"
            return {"ok": True, "message": msg}
        if service == "kakao":
            key = secret(con, "kakao_rest_key")
            if not key:
                raise UserError("카카오모빌리티 REST 키를 먼저 저장하세요.")
            d = get_setting(con, "depot", {"lat": 35.1462, "lon": 126.9233})
            o = (float(d["lat"]), float(d["lon"]))
            sec = X.kakao_direction(key, o, (o[0] + 0.005, o[1] + 0.005))
            return {"ok": sec is not None, "message": f"카카오모빌리티 정상 (차고지 부근 시험 경로 {sec}초)" if sec is not None else "응답은 왔으나 경로가 없습니다."}
    except X.ExternalError as e:
        return {"ok": False, "message": str(e), "code": e.code}
    raise UserError("알 수 없는 서비스입니다.")


# ------------------------------------------------------------------ 접수
def norm_phone(p: str) -> str:
    return re.sub(r"\D", "", p or "")


def norm_addr(a: str) -> str:
    return re.sub(r"\s+", "", a or "")


def build_items(items: list[dict], con=None) -> list[dict]:
    """화면에서 받은 품목을 공식 수수료표 기준으로 다시 계산한다(화면 값의 단가를 믿지 않음)."""
    rows_now = fee_rows(con) if con is not None else FEE_ROWS
    ver = fee_state(con)["version_id"] if con is not None else FEE_TABLE["version_id"]
    out = []
    for it in items:
        qty = int(it.get("qty") or 0)
        if qty <= 0:
            raise UserError("품목 수량은 1 이상이어야 합니다.")
        rid = it.get("fee_rate_id")
        line_id = it.get("line_id") or str(uuid.uuid4())
        if rid:
            r = rows_now.get(rid)
            if not r:
                raise UserError("수수료표에 없는 품목입니다. 다시 골라 주세요.")
            confirmed = r["status"] == "CONFIRMED"
            out.append({"line_id": line_id, "fee_rate_id": rid, "fee_version_id": ver, "name": r["name"],
                        "spec": r["spec"], "unit": r["unit"], "unit_label": r["unit_label"], "qty": qty, "unit_price": r["price"],
                        "amount": r["price"] * qty if confirmed else None, "price_status": "CONFIRMED" if confirmed else "PENDING"})
        else:
            name = (it.get("name") or "").strip()
            if not name:
                raise UserError("품목 이름을 적어 주세요.")
            out.append({"line_id": line_id, "fee_rate_id": None, "fee_version_id": None, "name": name, "spec": it.get("spec") or "",
                        "unit": it.get("unit") or "EA", "unit_label": it.get("unit_label") or "개", "qty": qty,
                        "unit_price": None, "amount": None, "price_status": "PENDING"})
    return out


def validate(data: dict) -> dict:
    d = {k: (data.get(k) or "").strip() if isinstance(data.get(k), str) or data.get(k) is None else data.get(k)
         for k in ("name", "phone", "address", "spot_note", "memo", "planned_date", "appt_start", "appt_end")}
    d["phone"] = norm_phone(d["phone"])
    if not d["name"]:
        raise UserError("이름을 적어 주세요.")
    if not re.fullmatch(r"0\d{8,10}", d["phone"]):
        raise UserError("연락처를 확인해 주세요 (0으로 시작하는 9~11자리).")
    if not d["address"]:
        raise UserError("배출 주소를 적어 주세요.")
    if d["planned_date"] and not re.fullmatch(r"\d{4}-\d\d-\d\d", d["planned_date"]):
        raise UserError("수거 예정일 형식이 올바르지 않습니다.")
    for k in ("appt_start", "appt_end"):
        if d[k] and not re.fullmatch(r"\d\d:\d\d", d[k]):
            raise UserError("방문 약속 시간은 HH:MM 형식입니다.")
    for k in ("must_today", "urgent"):
        d[k] = 1 if data.get(k) else 0
    d["priority"] = int(data.get("priority") or 0)
    for k in ("lat", "lon"):
        v = data.get(k)
        d[k] = float(v) if v not in (None, "") else None
    d["channel"] = (data.get("channel") or "전화").strip()
    if d["channel"] not in CHANNELS:
        raise UserError("접수 경로를 골라 주세요 (" + ", ".join(CHANNELS) + ").")
    if not data.get("items"):
        raise UserError("버릴 물건을 하나 이상 추가해 주세요.")
    return d


def _resolve_dong(con, data: dict, address: str) -> tuple[str | None, list[str]]:
    """접수의 행정동: 화면에서 고른 값(빈 값 = 미지정)이 있으면 그것, 없으면 주소로 자동 판별. 경고 문구도 함께."""
    area = _area()
    if "dong" in data:
        v = (data.get("dong") or "").strip()
        if v and not area.get_dong(con, v):
            raise UserError(f"등록되지 않은 행정동입니다({v}). 설정 → 행정동 관리에서 확인하세요.")
        dong = v or None
    else:
        dong = area.detect_dong(con, address)["dong"]
    warn = []
    if not dong and area.list_dongs(con, active_only=True):
        warn.append("행정동을 정하지 못했습니다. 주소를 확인하고 동을 골라 주세요.")
    return dong, warn


def find_duplicates(con, phone: str, address: str, exclude_id: str | None = None) -> list[dict]:
    p, a = norm_phone(phone), norm_addr(address)
    out = []
    for r in rows(con, "SELECT id, receipt_no, name, phone, address, planned_date, status FROM request WHERE status NOT IN ('DONE','CANCELLED')"):
        if r["id"] != exclude_id and (r["phone"] == p or norm_addr(r["address"]) == a):
            out.append(r)
    return out


def _insert_request(con, d: dict, items: list[dict], data: dict, dong: str | None, rid: str | None = None) -> tuple[str, str]:
    """접수 한 건 넣기(트랜잭션은 호출하는 쪽). 접수번호는 오늘 날짜의 다음 번호. rid: 휴대폰 현장 접수처럼 ID가 정해진 경우."""
    rid, ts = rid or str(uuid.uuid4()), now()
    day = today_kst().replace("-", "")
    n = con.execute("SELECT COUNT(*) FROM request WHERE receipt_no LIKE ?", (f"DG-{day}-%",)).fetchone()[0] + 1
    receipt = f"DG-{day}-{n:04d}"
    from dgpc import store as _store
    dcode = (one(con, "SELECT code FROM dong WHERE name=?", (dong,)) or {}).get("code") if dong else None
    levy = _store.next_levy_no(con, today_kst()[:4], dcode)
    red = _fin().apply_reduction_fields(con, data) or {}
    con.execute("""INSERT INTO request (id, receipt_no, req_ver, name, phone, address, spot_note, memo, lat, lon, geo_source,
        planned_date, appt_start, appt_end, must_today, urgent, priority, status, received_at, updated_at, dong, channel, levy_no,
        reduction_code, reduction_rate, reduction_label, reduction_proof, reduction_checker, reduction_at)
        VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'RECEIVED',?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, receipt, d["name"], d["phone"], d["address"], d["spot_note"], d["memo"], d["lat"], d["lon"],
                 (data.get("geo_source") if data.get("geo_source") in ("MANUAL", "ADDR") else "MANUAL") if d["lat"] is not None else None, d["planned_date"] or None, d["appt_start"] or None,
                 d["appt_end"] or None, d["must_today"], d["urgent"], d["priority"], ts, ts, dong, d["channel"], levy,
                 red.get("reduction_code"), red.get("reduction_rate"), red.get("reduction_label"), red.get("reduction_proof"), red.get("reduction_checker"), red.get("reduction_at")))
    for it in items:
        _insert_item(con, rid, it)
    return rid, receipt


def create_request(con, data: dict) -> dict:
    d = validate(data)
    items = build_items(data["items"], con)
    dong, dwarn = _resolve_dong(con, data, d["address"])
    red = _fin().apply_reduction_fields(con, data)
    with tx(con):
        rid, receipt = _insert_request(con, d, items, data, dong)
        audit(con, "request", rid, "CREATE", {"receipt_no": receipt, "channel": d["channel"], **({"dong": dong} if dong else {}),
                                              **({"reduction": red["reduction_code"], "rate": red["reduction_rate"], "proof": red["reduction_proof"]} if red else {})})
    warn = jurisdiction_warnings(con, d["address"]) + dwarn + _area().capacity_warnings(con, d["planned_date"] or None, dong, rid)
    return {"id": rid, "receipt_no": receipt, "dong": dong, "warnings": warn, "levy_no": one(con, "SELECT levy_no FROM request WHERE id=?", (rid,))["levy_no"]}


def _insert_item(con, rid, it):
    con.execute("""INSERT INTO request_item (line_id, request_id, fee_rate_id, fee_version_id, name, spec, unit, unit_label, qty,
        unit_price, amount, price_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (it["line_id"], rid, it["fee_rate_id"], it["fee_version_id"], it["name"], it["spec"], it["unit"], it["unit_label"],
                 it["qty"], it["unit_price"], it["amount"], it["price_status"]))


def _snapshot(con, rid) -> dict:
    r = one(con, "SELECT * FROM request WHERE id=?", (rid,))
    r["items"] = rows(con, "SELECT * FROM request_item WHERE request_id=? AND removed=0", (rid,))
    return r


def locked_lines(con, rid) -> set[str]:
    """현장 결과가 이미 들어온 품목 행은 원접수에서 바꾸거나 지울 수 없다."""
    out = set()
    for e in rows(con, "SELECT body_json FROM result_event WHERE request_id=? AND kind='ITEM_RESULT' AND apply_status!='REJECTED'", (rid,)):
        out.add(json.loads(e["body_json"]).get("line_id"))
    return out


# ------------------------------------------------------------------ 마감 통제: 마감한 날의 접수·수납을 고치면 사유 필수 + "마감 후 수정" 표시
def close_info(con, work_date: str | None) -> dict | None:
    """그날의 가장 최근 마감 회차(없으면 None)."""
    if not work_date:
        return None
    return one(con, "SELECT * FROM day_close_log WHERE work_date=? ORDER BY seq DESC LIMIT 1", (work_date,))


def _after_close(con, rid: str) -> dict:
    """그 접수의 예정일이 이미 마감된 날이면 이력에 붙일 표시 {'after_close': 날짜}."""
    r = one(con, "SELECT planned_date FROM request WHERE id=?", (rid,))
    return {"after_close": r["planned_date"]} if r and close_info(con, r["planned_date"]) else {}


def update_request(con, rid: str, data: dict, reason: str) -> dict:
    cur = one(con, "SELECT * FROM request WHERE id=?", (rid,))
    if not cur:
        raise UserError("접수를 찾을 수 없습니다.")
    if cur["status"] == "CANCELLED":
        raise UserError("취소된 접수는 수정할 수 없습니다.")
    if not (reason or "").strip():
        raise UserError("수정 사유를 적어 주세요." + (" (마감한 날의 접수입니다)" if close_info(con, cur["planned_date"]) else ""))
    d = validate({"channel": cur["channel"], **data})
    items = build_items(data["items"], con)
    dong, dwarn = _resolve_dong(con, data, d["address"]) if "dong" in data or d["address"] != cur["address"] else (cur["dong"], [])
    locked = locked_lines(con, rid)
    old_items = {i["line_id"]: i for i in rows(con, "SELECT * FROM request_item WHERE request_id=? AND removed=0", (rid,))}
    new_ids = {i["line_id"] for i in items}
    for lid in locked:
        if lid in old_items and (lid not in new_ids or next(i for i in items if i["line_id"] == lid)["qty"] != old_items[lid]["qty"]):
            raise UserError(f"현장 결과가 있는 품목({old_items[lid]['name']})은 바꾸거나 지울 수 없습니다.")
    flag = _after_close(con, rid)
    with tx(con):
        con.execute("INSERT INTO request_version VALUES (?,?,?,?,?)",
                    (rid, cur["req_ver"], json.dumps(_snapshot(con, rid), ensure_ascii=False), reason.strip(), now()))
        con.execute("""UPDATE request SET req_ver=req_ver+1, name=?, phone=?, address=?, spot_note=?, memo=?, lat=?, lon=?,
            geo_source=COALESCE(?, geo_source), planned_date=?, appt_start=?, appt_end=?, must_today=?, urgent=?, priority=?, updated_at=?,
            dong=?, channel=? WHERE id=?""",
                    (d["name"], d["phone"], d["address"], d["spot_note"], d["memo"], d["lat"], d["lon"],
                     (data.get("geo_source") if data.get("geo_source") in ("MANUAL", "ADDR") else "MANUAL") if d["lat"] is not None and (d["lat"], d["lon"]) != (cur["lat"], cur["lon"]) else None,
                     d["planned_date"] or None, d["appt_start"] or None, d["appt_end"] or None, d["must_today"], d["urgent"],
                     d["priority"], now(), dong, d["channel"], rid))
        for lid in old_items:
            if lid not in new_ids:
                con.execute("UPDATE request_item SET removed=1 WHERE line_id=?", (lid,))
        for it in items:
            if it["line_id"] in old_items:
                con.execute("UPDATE request_item SET qty=?, amount=?, unit_price=?, price_status=? WHERE line_id=?",
                            (it["qty"], it["amount"], it["unit_price"], it["price_status"], it["line_id"]))
            else:
                _insert_item(con, rid, it)
        audit(con, "request", rid, "UPDATE", {"reason": reason.strip(), "from_ver": cur["req_ver"], **flag})
    warn = jurisdiction_warnings(con, d["address"]) + dwarn
    if (d["planned_date"] or None) != cur["planned_date"] or dong != cur["dong"]:
        warn += _area().capacity_warnings(con, d["planned_date"] or None, dong, rid)
    return {"id": rid, "resend_needed": bool(one(con, "SELECT 1 AS x FROM active_assignment WHERE request_id=?", (rid,))),
            "warnings": warn, "after_close": bool(flag)}


def _reason(codes: dict, code: str | None, text: str | None, what: str) -> tuple[str, str, str]:
    """(코드, 직접 입력 글, 화면·이력용 글). 코드가 없으면 글만으로(이전 방식) 기타 처리."""
    text = (text or "").strip()
    if not code:
        if not text:
            raise UserError(f"{what} 사유를 적어 주세요.")
        code = "OTHER"
    if code not in codes:
        raise UserError(f"{what} 사유 코드가 올바르지 않습니다.")
    if code == "OTHER" and not text:
        raise UserError(f"{what} 사유를 직접 적어 주세요 (기타).")
    full = codes[code] + (f": {text}" if text else "")
    return code, text[:200], full[:220]


def _cancel(con, rid: str, code: str, text: str, full: str) -> dict:
    """취소 한 건(트랜잭션은 호출하는 쪽). 납부 기록이 남아 있으면 환불 필요로 표시된다."""
    cur = one(con, "SELECT status, planned_date FROM request WHERE id=?", (rid,))
    if not cur:
        raise UserError("접수를 찾을 수 없습니다.")
    if cur["status"] == "CANCELLED":
        raise UserError("이미 취소된 접수입니다.")
    act = one(con, "SELECT a.batch_id, b.crew_id FROM active_assignment a JOIN assignment_batch b ON b.id=a.batch_id WHERE a.request_id=?", (rid,))
    sent = act and one(con, "SELECT 1 AS x FROM transfer_bundle WHERE batch_id=?", (act["batch_id"],))
    flag = _after_close(con, rid)
    paid = payment_status(con, rid)["paid"]
    con.execute("UPDATE request SET status='CANCELLED', cancel_code=?, cancel_reason=?, cancelled_at=?, updated_at=? WHERE id=?",
                (code, text or None, now(), now(), rid))
    con.execute("DELETE FROM active_assignment WHERE request_id=?", (rid,))
    audit(con, "request", rid, "CANCEL", {"code": code, "reason": full, "was": cur["status"], **({"refund_needed": paid} if paid > 0 else {}), **flag})
    return {"field_notice_needed": bool(sent), "refund_needed": paid > 0, "paid": paid}


def cancel_request(con, rid: str, reason: str = "", code: str | None = None) -> dict:
    """접수 취소(사유 코드 + 직접 입력). 이미 납부한 금액이 있으면 환불 필요로 알려 준다(마감 수납 확인·'환불 필요' 목록)."""
    code, text, full = _reason(CANCEL_CODES, code, reason, "취소")
    with tx(con):
        res = _cancel(con, rid, code, text, full)
    _fin().sync_refunds(con)
    return res


def set_location(con, rid: str, lat: float, lon: float, source: str = "MANUAL"):
    if not (33 <= float(lat) <= 39 and 124 <= float(lon) <= 132):
        raise UserError("좌표가 국내 범위를 벗어났습니다.")
    with tx(con):
        con.execute("UPDATE request SET lat=?, lon=?, geo_source=?, updated_at=? WHERE id=?", (float(lat), float(lon), source, now(), rid))
        audit(con, "request", rid, "LOCATION", {"source": source})


_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"


def chosung_of(s: str) -> str:
    return "".join(_CHO[(ord(ch) - 0xAC00) // 588] if 0 <= ord(ch) - 0xAC00 < 11172 else ch for ch in (s or ""))


def is_chosung(s: str) -> bool:
    return any("ㄱ" <= ch <= "ㅎ" for ch in s or "") and all("ㄱ" <= ch <= "ㅎ" or ch.isascii() for ch in s)


SORTABLE = {"receipt_no": "receipt_no", "planned_date": "COALESCE(planned_date,'9999')", "received_at": "received_at", "name": "name",
            "dong": "COALESCE(dong,'')", "status": "status", "channel": "channel", "address": "address"}
QUICK = ("ALL", "TODAY", "NOLOC", "FEE", "UNASSIGNED", "REFUND", "FIELD")


def _enrich(con, r: dict) -> dict:
    r["items"] = rows(con, "SELECT * FROM request_item WHERE request_id=? AND removed=0", (r["id"],))
    r["state"] = derive(con, r)
    r["warnings"] = jurisdiction_warnings(con, r["address"])
    r["assigned_to"] = (one(con, """SELECT c.name, b.work_date FROM active_assignment a JOIN assignment_batch b ON b.id=a.batch_id
        JOIN crew c ON c.id=b.crew_id WHERE a.request_id=?""", (r["id"],)) or None)
    return r


def _request_sql(f: dict) -> tuple[str, list]:
    """조회조건 → SQL (검색어·날짜 범위·상태·팀·동·경로). 초성·수납 상태·빠른 조회는 계산 뒤 거른다."""
    sql, args = "SELECT * FROM request WHERE 1=1", []
    q = " ".join(t for t in (f.get("q") or "").split() if not is_chosung(t))
    if f.get("date"):
        sql += " AND planned_date=?"; args.append(f["date"])
    col = "received_at" if f.get("date_field") == "received" else "planned_date"
    for k, op in (("date_from", ">="), ("date_to", "<=")):
        v = f.get(k)
        if v:
            if not re.fullmatch(r"\d{4}-\d\d-\d\d", v):
                raise UserError("조회 기간을 날짜로 골라 주세요.")
            if col == "received_at":  # 접수일은 한국시간 날짜 기준 → UTC 시각 범위
                d0 = dt.datetime.fromisoformat(v).replace(tzinfo=KST).astimezone(dt.timezone.utc)
                if op == "<=":
                    d0 += dt.timedelta(days=1)
                sql += f" AND received_at {'>=' if op == '>=' else '<'} ?"; args.append(d0.strftime("%Y-%m-%dT%H:%M:%SZ"))
            else:
                sql += f" AND planned_date {op} ?"; args.append(v)
    status = f.get("status") or ""
    if status == "OPEN":
        sql += " AND status NOT IN ('DONE','CANCELLED')"
    elif status:
        sql += " AND status=?"; args.append(status)
    if f.get("crew"):
        sql += """ AND id IN (SELECT a.request_id FROM active_assignment a JOIN assignment_batch b ON b.id=a.batch_id WHERE b.crew_id=?)"""
        args.append(f["crew"])
    if f.get("dong"):
        if f["dong"] == "-":
            sql += " AND dong IS NULL"
        else:
            sql += " AND dong=?"; args.append(f["dong"])
    if f.get("channel"):
        sql += " AND channel=?"; args.append(f["channel"])
    if q:
        qp = norm_phone(q)
        sql += " AND (name LIKE ? OR address LIKE ? OR receipt_no LIKE ?" + (" OR phone LIKE ?" if qp else "") + ")"
        args += [f"%{q}%"] * 3 + ([f"%{qp}%"] if qp else [])
    return sql, args


def _quick_ok(con, r: dict, key: str, today: str) -> bool:
    if key == "TODAY":
        return r["planned_date"] == today
    if key == "NOLOC":
        return r["lat"] is None
    if key == "UNASSIGNED":
        return r["status"] not in ("DONE", "CANCELLED") and not one(con, "SELECT 1 AS x FROM active_assignment WHERE request_id=?", (r["id"],))
    if key == "FEE":
        return derive(con, r)["fee"]["final"] is None
    if key == "REFUND":
        return r["status"] == "CANCELLED" and payment_status(con, r["id"])["status"] == "REFUND_NEEDED"
    if key == "FIELD":  # 휴대폰 현장 접수 중 사무실 확인 전
        return bool(r.get("origin_device")) and not r.get("field_checked_at")
    return True


def search_requests(con, f: dict | None = None, scope_ids: set[str] | None = None, with_counts: bool = True) -> dict:
    """접수 목록 조회(서버 쪽 쪽 나눔). f: q, date, date_field(planned|received), date_from, date_to, status, crew, dong, channel,
    pay(수납 상태), quick, sort, dir, page(1부터), size. 돌려줌: rows(이 쪽), total(조건에 맞는 전체), counts(빠른 조회별 건수)."""
    f = dict(f or {})
    sql, args = _request_sql(f)
    key = SORTABLE.get(f.get("sort") or "", None)
    desc = (f.get("dir") or "").lower() == "desc"
    order = f" ORDER BY {key} {'DESC' if desc else 'ASC'}, received_at DESC" if key else " ORDER BY COALESCE(planned_date,'9999'), received_at DESC"
    out = rows(con, sql + order, args)
    cho = [t for t in (f.get("q") or "").split() if is_chosung(t)]
    if cho:  # 초성 검색: 이름·주소의 초성 (예: ㄱㅅㅎ → 가상하나)
        out = [r for r in out if all(t in chosung_of(re.sub(r"\s+", "", (r["name"] or "") + "|" + (r["address"] or ""))) for t in cho)]
    if scope_ids is not None:
        out = [r for r in out if r["id"] in scope_ids]
    if f.get("pay"):
        out = [r for r in out if payment_status(con, r["id"])["status"] == f["pay"]]
    today = today_kst()
    counts = {k: sum(1 for r in out if _quick_ok(con, r, k, today)) for k in QUICK if k != "ALL"} if with_counts else {}
    counts["ALL"] = len(out)
    quick = f.get("quick") or "ALL"
    if quick not in QUICK:
        raise UserError("빠른 조회 값이 올바르지 않습니다.")
    if quick != "ALL":
        out = [r for r in out if _quick_ok(con, r, quick, today)]
    total = len(out)
    size = max(1, min(500, int(f.get("size") or 50)))
    pages = max(1, -(-total // size))
    page = max(1, min(pages, int(f.get("page") or 1)))
    part = [_enrich(con, r) for r in out[(page - 1) * size: page * size]]
    return {"rows": part, "total": total, "page": page, "size": size, "pages": pages, "counts": counts}


def list_requests(con, q: str = "", date: str = "", status: str = "") -> list[dict]:
    """조건에 맞는 접수(최대 500건, 노선 계산 등 내부용). 화면 목록은 search_requests(쪽 나눔·전체 건수)를 쓴다."""
    return search_requests(con, {"q": q, "date": date, "status": status, "size": 500}, with_counts=False)["rows"]


def get_request(con, rid: str) -> dict:
    r = one(con, "SELECT * FROM request WHERE id=?", (rid,))
    if not r:
        raise UserError("접수를 찾을 수 없습니다.")
    r["items"] = rows(con, "SELECT * FROM request_item WHERE request_id=? AND removed=0", (rid,))
    r["versions"] = rows(con, "SELECT req_ver, reason, at FROM request_version WHERE request_id=? ORDER BY req_ver DESC", (rid,))
    r["locked_lines"] = sorted(locked_lines(con, rid))
    r["state"] = derive(con, r)
    r["events"] = rows(con, """SELECT event_id, kind, body_json, apply_status, conflict_reason, device_ts FROM result_event
        WHERE request_id=? ORDER BY device_ts""", (rid,))
    r["photos"] = rows(con, "SELECT photo_id, phase, recovery_status, purged_at, line_id, taken_at, acc, (lat IS NOT NULL) AS has_gps FROM photo WHERE request_id=?", (rid,))
    r["refunds"] = rows(con, "SELECT * FROM refund_case WHERE request_id=? ORDER BY created_at DESC", (rid,))
    r["payment"] = payment_status(con, rid)
    r["contacts"] = list_contacts(con, rid)
    r["cancel_label"] = (CANCEL_CODES.get(r["cancel_code"] or "", "") + (": " if r["cancel_code"] and r["cancel_reason"] else "") + (r["cancel_reason"] or "")) if r["status"] == "CANCELLED" else None
    ci = close_info(con, r["planned_date"])
    r["closed"] = {"work_date": ci["work_date"], "seq": ci["seq"], "status": ci["status"]} if ci else None
    return r


# ------------------------------------------------------------------ 현장 결과로 현재 상태 계산
def _events_for(con, rid):
    return rows(con, """SELECT e.kind, e.body_json, e.device_ts FROM result_event e JOIN result_import i ON i.id=e.import_id
        WHERE e.request_id=? AND e.apply_status='APPLIED' ORDER BY i.imported_at, e.device_seq""", (rid,))


def derive(con, req: dict) -> dict:
    """휴대폰 앱(fees.js deriveStop)과 같은 규칙. 행별 잔량, 상쇄 없음, 금액은 현장 확인 수량 기준."""
    items = req.get("items") or rows(con, "SELECT * FROM request_item WHERE request_id=? AND removed=0", (req["id"],))
    lines = [{"line_id": i["line_id"], "kind": "RECEIVED", "name": i["name"], "spec": i["spec"], "unit": i["unit"],
              "unit_label": i["unit_label"], "price": i["unit_price"] if i["price_status"] == "CONFIRMED" else None,
              "qty_received": i["qty"], "amount_received": i["amount"], "qty_confirmed": i["qty"], "qty_collected": 0, "reason": "",
              "fee_rate_id": i["fee_rate_id"]} for i in items]
    by = {l["line_id"]: l for l in lines}
    contact, outcome, loc, n = "NOT_CONTACTED", None, None, 0
    memos = []
    # 휴대폰 v0.10 기록: 도착·스티커 확인·추가 금액 고지·문자/길안내(횟수만)·작업자·부재 증거
    fx = {"arrive": None, "stickers": {}, "fee_notices": [], "sms": {}, "nav": 0, "nav_apps": {}, "workers": [], "no_photo_reason": None, "other_kinds": {}}
    known = {"CONTACT", "FIELD_ITEM_ADD", "ITEM_RESULT", "ATTEMPT", "LOCATION_FIX", "FIELD_MEMO", "PHOTO_META", "REVISIT_APPT", "DIAL_INTENT", "UNLOAD",
             "ARRIVE", "STICKER_CHECK", "FEE_NOTICE", "SMS_INTENT", "NAV_INTENT", "FIELD_INTAKE", "CORRECTION", "CANCEL_REQUEST"}
    for e in _events_for(con, req["id"]):
        try:
            b = json.loads(e["body_json"]) or {}
        except ValueError:
            b = {}
        if not isinstance(b, dict):
            b = {}
        k = e["kind"]
        w = b.get("worker")
        if isinstance(w, str) and w.strip() and w.strip()[:10] not in fx["workers"]:
            fx["workers"].append(w.strip()[:10])
        if k == "ARRIVE":
            fx["arrive"] = {"at": e["device_ts"], "acc": b.get("acc"), "has_gps": b.get("lat") is not None, "worker": w}
        elif k == "STICKER_CHECK" and b.get("line_id"):
            fx["stickers"][b["line_id"]] = {"result": b.get("result"), "number": (b.get("number") or "")[:30], "decision": b.get("decision"), "at": e["device_ts"]}
        elif k == "FEE_NOTICE":
            fx["fee_notices"].append({"at": e["device_ts"], "received": b.get("received"), "total": b.get("total"), "extra": b.get("extra"),
                                      "pending": b.get("pending"), "response": b.get("response"), "lines": len(b.get("lines") or []), "worker": w})
        elif k == "SMS_INTENT":
            t = b.get("template") or "OTHER"
            fx["sms"][t] = fx["sms"].get(t, 0) + 1
        elif k == "NAV_INTENT":
            fx["nav"] += 1
            a_ = str(b.get("app") or "기타")[:20]
            fx["nav_apps"][a_] = fx["nav_apps"].get(a_, 0) + 1
        elif k == "ATTEMPT" and b.get("no_photo_reason"):
            fx["no_photo_reason"] = b.get("no_photo_reason")
        elif k not in known:
            fx["other_kinds"][k] = fx["other_kinds"].get(k, 0) + 1
        if k == "CONTACT":
            contact = b.get("result", contact)
        elif k == "FIELD_ITEM_ADD" and b.get("line_id") not in by:
            l = {"line_id": b["line_id"], "kind": "FIELD", "name": b.get("name"), "spec": b.get("spec", ""), "unit": b.get("unit"),
                 "unit_label": b.get("unit_label"), "price": b.get("unit_price") if b.get("price_status") == "CONFIRMED" else None,
                 "qty_received": 0, "amount_received": 0, "qty_confirmed": int(b.get("qty") or 0), "qty_collected": 0, "reason": "",
                 "fee_rate_id": b.get("fee_rate_id")}
            lines.append(l); by[l["line_id"]] = l; n += 1
        elif k == "ITEM_RESULT" and b.get("line_id") in by:
            by[b["line_id"]].update(qty_confirmed=int(b.get("qty_confirmed", 0)), qty_collected=int(b.get("qty_collected", 0)), reason=b.get("reason") or "")
            n += 1
        elif k == "ATTEMPT":
            outcome = b.get("outcome"); n += 1
        elif k == "LOCATION_FIX":
            loc = {"lat": b.get("lat"), "lon": b.get("lon"), "source": b.get("source")}
        elif k == "FIELD_MEMO":
            memos.append((b.get("text") or "")[:300])
    # 표에 없던 품목을 담당자가 유사 품목 단가로 확정한 기록(근거·확인자)을 덧씌운다. 원래 행은 그대로 둔다.
    for lk in rows(con, "SELECT * FROM item_link WHERE request_id=? AND void_reason IS NULL ORDER BY at", (req["id"],)):
        l = by.get(lk["line_id"])
        if l and l["price"] is None:
            row = FEE_ROWS.get(lk["fee_rate_id"]) or {}
            l["price"] = lk["unit_price"]
            l["link"] = {"id": lk["id"], "fee_rate_id": lk["fee_rate_id"], "name": row.get("name"), "spec": row.get("spec"), "reason": lk["reason"],
                         "confirmed_by": lk["confirmed_by"], "at": lk["at"], "fee_version_id": lk["fee_version_id"]}
    for l in lines:
        l["remaining"] = max(0, l["qty_confirmed"] - l["qty_collected"])
    received = sum(l["amount_received"] or 0 for l in lines if l["kind"] == "RECEIVED")
    pending = [l for l in lines if l["price"] is None and l["qty_confirmed"] > 0]
    subtotal = sum(l["price"] * l["qty_confirmed"] for l in lines if l["price"] is not None)
    qty_by_unit: dict[str, int] = {}
    for l in lines:
        qty_by_unit[l["unit_label"]] = qty_by_unit.get(l["unit_label"], 0) + l["qty_confirmed"]
    return {"lines": lines, "contact": contact, "outcome": outcome, "field_loc": loc, "has_field_records": n > 0, "memos": memos, "field": fx,
            "fee": {"received": received, "confirmed_subtotal": subtotal, "diff": subtotal - received,
                    "field_added": sum(l["price"] * l["qty_confirmed"] for l in lines if l["kind"] == "FIELD" and l["price"] is not None),
                    "pending_count": len(pending), "final": None if pending else subtotal, "qty_by_unit": qty_by_unit},
            "remaining_total": sum(l["remaining"] for l in lines)}


def _refresh_status(con, rid):
    """현장 결과에 따라 업무 상태를 갱신한다. 최종 결과가 나오면 활성 배정을 풀어 재배정할 수 있게 한다."""
    r = one(con, "SELECT * FROM request WHERE id=?", (rid,))
    if not r or r["status"] == "CANCELLED":
        return
    st = derive(con, r)
    new = r["status"]
    if st["outcome"] in FINAL:
        new = st["outcome"]
    elif st["has_field_records"]:
        new = "IN_PROGRESS"
    if st["field_loc"] and st["field_loc"]["lat"] is not None:
        con.execute("UPDATE request SET lat=?, lon=?, geo_source=? WHERE id=?",
                    (st["field_loc"]["lat"], st["field_loc"]["lon"], "ADDR" if st["field_loc"].get("source") == "ADDR" else "FIELD", rid))
    if new != r["status"]:
        con.execute("UPDATE request SET status=?, updated_at=?, completed_at=CASE WHEN ? IN ('DONE','PARTIAL','NOT_COLLECTED','ON_HOLD') THEN ? ELSE completed_at END WHERE id=?",
                    (new, now(), new, now(), rid))
        audit(con, "request", rid, "STATUS", {"from": r["status"], "to": new})
    if new in FINAL:
        con.execute("DELETE FROM active_assignment WHERE request_id=?", (rid,))


# ------------------------------------------------------------------ 배정
def confirm_plan(con, work_date: str, plan: dict[str, list[dict]], adjust_reason: str = "", adjusted: bool = False) -> list[dict]:
    """plan = {crew_id: [{request_id, eta}, ...]} 순서대로. 팀별로 새 배정 버전을 만든다."""
    crews = {c["id"]: c for c in list_crews(con)}
    for cid in plan:
        if cid not in crews:
            raise UserError("없는 팀이 포함되어 있습니다.")
    if adjusted and not (adjust_reason or "").strip():
        raise UserError("추천 순서·팀을 바꿨습니다. 조정 사유를 적어 주세요 (예: 골목 진입 불가, 주민 요청 시간).")
    seen = set()
    for cid, stops in plan.items():
        for s in stops:
            if s["request_id"] in seen:
                raise UserError("같은 접수가 두 팀에 들어 있습니다.")
            seen.add(s["request_id"])
    replaced = {r["id"] for r in rows(con, "SELECT id FROM assignment_batch WHERE work_date=? AND status='ACTIVE' AND crew_id IN (%s)"
                                       % ",".join("?" * len(plan)), [work_date, *plan]) } if plan else set()
    for rid in seen:
        req = one(con, "SELECT status FROM request WHERE id=?", (rid,))
        if not req:
            raise UserError("없는 접수가 포함되어 있습니다.")
        if req["status"] in ("CANCELLED", "DONE"):
            raise UserError("취소·완료된 접수는 배정할 수 없습니다.")
        act = one(con, "SELECT batch_id FROM active_assignment WHERE request_id=?", (rid,))
        if act and act["batch_id"] not in replaced:
            other = one(con, "SELECT b.work_date, c.name FROM assignment_batch b JOIN crew c ON c.id=b.crew_id WHERE b.id=?", (act["batch_id"],))
            raise UserError(f"이미 다른 배정({other['work_date']} {other['name']})에 들어 있는 접수가 있습니다. 먼저 그 배정에서 빼 주세요.")
    out = []
    with tx(con):
        for cid, stops in plan.items():
            prev = one(con, "SELECT MAX(assign_ver) AS v FROM assignment_batch WHERE crew_id=? AND work_date=?", (cid, work_date))["v"] or 0
            con.execute("UPDATE assignment_batch SET status='SUPERSEDED' WHERE crew_id=? AND work_date=? AND status='ACTIVE'", (cid, work_date))
            bid = str(uuid.uuid4())
            clean = []
            for x in stops:
                u = x.get("unload_after")
                item = {"request_id": x["request_id"], "eta": x.get("eta") or ""}
                if isinstance(u, dict) and u.get("label"):
                    item["unload_after"] = {"eta": str(u.get("eta") or "")[:5], "label": str(u["label"])[:40],
                                            "minutes": int(u.get("minutes") or 0), "volume_m3": float(u.get("volume_m3") or 0)}
                clean.append(item)
            con.execute("INSERT INTO assignment_batch VALUES (?,?,?,?,'ACTIVE',?,?)",
                        (bid, cid, work_date, prev + 1, json.dumps(clean, ensure_ascii=False), now()))
            for i, s in enumerate(stops, 1):
                con.execute("INSERT INTO assignment_stop VALUES (?,?,?,?)", (bid, s["request_id"], i, s.get("eta")))
                con.execute("INSERT INTO active_assignment VALUES (?,?) ON CONFLICT(request_id) DO UPDATE SET batch_id=excluded.batch_id",
                            (s["request_id"], bid))
                con.execute("UPDATE request SET status=CASE WHEN status IN ('RECEIVED','PARTIAL','NOT_COLLECTED','ON_HOLD') THEN 'ASSIGNED' ELSE status END, planned_date=?, updated_at=? WHERE id=?",
                            (work_date, now(), s["request_id"]))
            out.append({"batch_id": bid, "crew_id": cid, "assign_ver": prev + 1, "count": len(stops)})
            audit(con, "batch", bid, "CONFIRM", {"crew": crews[cid]["name"], "date": work_date, "ver": prev + 1, "n": len(stops),
                                                 **({"adjusted": True, "reason": adjust_reason.strip()[:200]} if adjusted else {})})
        # 이전 버전에만 있고 새 버전에서 빠진 접수는 활성 배정을 풀고 접수 상태로 되돌린다
        for rid, in con.execute("SELECT request_id FROM active_assignment WHERE batch_id IN (%s)" % ",".join("?" * len(replaced)), list(replaced)).fetchall() if replaced else []:
            con.execute("DELETE FROM active_assignment WHERE request_id=?", (rid,))
            con.execute("UPDATE request SET status='RECEIVED', updated_at=? WHERE id=? AND status='ASSIGNED'", (now(), rid))
    return out


def list_batches(con, work_date: str) -> list[dict]:
    out = rows(con, """SELECT b.*, c.name AS crew_name, c.device_id FROM assignment_batch b JOIN crew c ON c.id=b.crew_id
        WHERE b.work_date=? ORDER BY c.name, b.assign_ver DESC""", (work_date,))
    for b in out:
        b["stops"] = rows(con, """SELECT s.seq, s.eta, r.id AS request_id, r.receipt_no, r.name, r.address, r.status, r.updated_at, r.lat, r.lon
            FROM assignment_stop s JOIN request r ON r.id=s.request_id WHERE s.batch_id=? ORDER BY s.seq""", (b["id"],))
        b["bundles"] = rows(con, "SELECT id, chunk_count, displayed_at, receipt_confirmed_at FROM transfer_bundle WHERE batch_id=? ORDER BY displayed_at DESC", (b["id"],))
        last = b["bundles"][0]["displayed_at"] if b["bundles"] else None
        b["resend_needed"] = bool(last and any(s["updated_at"] > last for s in b["stops"]))
        b.pop("route_json", None)
    return out


# ------------------------------------------------------------------ 목록 묶음 (QR)
def build_daylist(con, batch_id: str) -> tuple[dict, dict]:
    b = one(con, "SELECT b.*, c.name AS crew_name, c.device_id FROM assignment_batch b JOIN crew c ON c.id=b.crew_id WHERE b.id=?", (batch_id,))
    if not b:
        raise UserError("배정을 찾을 수 없습니다.")
    if b["status"] != "ACTIVE":
        raise UserError("새 배정 버전이 있습니다. 최신 배정으로 전송하세요.")
    stops = []
    # 배정 확정 때 저장한 반입지 경유(하차) 위치: 그 지점을 마친 뒤 반입지에 들른다
    unload_by = {x["request_id"]: x["unload_after"] for x in json.loads(b["route_json"] or "[]") if isinstance(x, dict) and x.get("unload_after")}
    us = get_setting(con, "unload_site", None)  # 반입지 좌표를 함께 보낸다(휴대폰 길안내용, 설정에 있을 때만)
    if us and us.get("lat") is not None:
        for u_ in unload_by.values():
            u_.setdefault("lat", us["lat"]); u_.setdefault("lon", us["lon"])
    for s in rows(con, "SELECT request_id, seq, eta FROM assignment_stop WHERE batch_id=? ORDER BY seq", (batch_id,)):
        r = one(con, "SELECT * FROM request WHERE id=?", (s["request_id"],))
        if r["status"] == "CANCELLED":
            if unload_by.get(r["id"]) and stops:  # 취소된 지점 뒤의 하차는 앞 지점 뒤로 옮긴다
                stops[-1]["unload_after"] = unload_by[r["id"]]
            continue
        st = derive(con, r)
        revisit = st["has_field_records"]
        items = []
        for l in st["lines"]:
            if revisit and l["remaining"] == 0:
                continue
            items.append({"line_id": l["line_id"], "line_kind": l["kind"], "fee_rate_id": l["fee_rate_id"], "name": l["name"], "spec": l["spec"],
                          "unit": l["unit"], "unit_label": l["unit_label"], "qty": l["qty_confirmed"],
                          "prior_collected": l["qty_collected"] if revisit else 0,
                          "unit_price": l["price"] if l["price"] is not None else None,
                          "amount": l["amount_received"] if l["kind"] == "RECEIVED" else 0,
                          "price_status": "CONFIRMED" if l["price"] is not None else "PENDING"})
        if unload_by.get(r["id"]):
            extra_u = {"unload_after": unload_by[r["id"]]}
        else:
            extra_u = {}
        # "dong": 행정동 이름(문자열). 휴대폰 목록 형식(dgproto)이 아직 모르면 압축할 때 빠진다.
        # "reduction": 감면율(%)·사유. 휴대폰 목록 압축 형식에는 감면율만 "rd"로 싣는다(사유 이름은 복지 정보라 휴대폰에 보내지 않음).
        # "levy_no": 부과번호 → "lv"
        red_ = {"reduction": {"rate": int(r["reduction_rate"] or 0), "label": r["reduction_label"] or ""}} if r.get("reduction_code") else {}
        stops.append({**extra_u, **red_, "seq": s["seq"], "request_id": r["id"], "receipt_no": r["receipt_no"], "req_ver": r["req_ver"], "dong": r["dong"] or "", "levy_no": r.get("levy_no") or "",
                      "name": r["name"], "phone": r["phone"], "address": r["address"], "spot_note": r["spot_note"], "memo": r["memo"],
                      "lat": r["lat"], "lon": r["lon"], "eta": s["eta"] or (r["appt_start"] or ""),
                      "appt": [r["appt_start"], r["appt_end"]] if r["appt_start"] else None,
                      "revisit": revisit, "assign_ver": b["assign_ver"], "items": items})
    payload = {"schema": "dg.daylist/1", "work_date": b["work_date"], "crew": b["crew_name"], "assign_ver": b["assign_ver"],
               "fee_table": fee_table_for_phone(con), "stops": stops}
    return payload, b


def make_bundle(con, batch_id: str | None = None, payload: dict | None = None, device_id: str | None = None,
                chunk: int = 700) -> dict:
    if batch_id:
        payload, b = build_daylist(con, batch_id)
        device_id = b["device_id"]
        if not device_id:
            raise UserError("이 팀에 연결된 휴대폰이 없습니다. 설정에서 기기를 연결하세요.")
    if payload.get("schema") == "dg.daylist/1":
        payload = P.compact_daylist(payload, FEE_ROWS)  # QR 장수를 줄이는 압축 형식
    dev = one(con, "SELECT * FROM device WHERE id=?", (device_id,))
    if not dev or dev["status"] != "ACTIVE":
        raise UserError("휴대폰이 등록되어 있지 않거나 사용 중지 상태입니다.")
    bundle_id = uuid.uuid4().bytes
    sign = pc_key(con, "sign")
    head = P.Header(P.TYPE_DAYLIST, bundle_id, uuid.UUID(device_id).bytes, payload["assign_ver"], 1, P.key_id(P.pub_raw(sign)))
    env, code = P.seal(payload, head, P.pub_from_raw(dev["kex_pub"]), sign)
    frames = P.split_frames(env, bundle_id, chunk)
    bid = str(uuid.UUID(bytes=bundle_id))
    with tx(con):
        con.execute("INSERT INTO transfer_bundle VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                    (bid, device_id, batch_id, payload["work_date"], payload["assign_ver"], len(frames),
                     hashlib.sha256(env).hexdigest(), code, json.dumps(frames), now()))
        audit(con, "bundle", bid, "DISPLAY", {"batch": batch_id, "frames": len(frames)})
    return {"bundle_id": bid, "frames": frames, "bytes": len(env), "count": len(payload.get("s") or payload.get("stops") or []),
            "work_date": payload["work_date"], "assign_ver": payload["assign_ver"]}


def confirm_receipt(con, bundle: str, code: str) -> str:
    r = rows(con, "SELECT id, receipt_code FROM transfer_bundle WHERE id LIKE ?", (bundle + "%",))
    if len(r) != 1:
        raise UserError("전송 묶음을 찾을 수 없습니다.")
    if re.sub(r"\s", "", code or "").upper() != r[0]["receipt_code"]:
        raise UserError("확인 코드가 맞지 않습니다. 수신이 확인되지 않았습니다.")
    with tx(con):
        con.execute("UPDATE transfer_bundle SET receipt_confirmed_at=? WHERE id=?", (now(), r[0]["id"]))
        audit(con, "bundle", r[0]["id"], "RECEIPT", None)
    return r[0]["id"]


# ------------------------------------------------------------------ 결과 가져오기·충돌
def _open_from_device(con, data: bytes, expect_type: int):
    try:
        head = P.Header.unpack(data)
    except P.ProtocolError:
        raise UserError("휴대폰 결과 파일 형식이 아닙니다.")
    dev_id = str(uuid.UUID(bytes=head.device_id))
    dev = one(con, "SELECT * FROM device WHERE id=?", (dev_id,))
    if not dev:
        raise UserError("등록되지 않은 휴대폰의 파일입니다. 반영하지 않았습니다.")
    if dev["status"] != "ACTIVE":
        raise UserError(f"휴대폰 상태가 {dev['status']}입니다. 분실·중지 기기의 파일은 반영하지 않습니다.")
    try:
        head, payload, release = P.open_envelope(data, P.pub_from_raw(dev["sign_pub"]), pc_key(con, "kex"))
    except P.ProtocolError as e:
        raise UserError(f"파일 검증에 실패했습니다({e.code}). 아무것도 반영하지 않았습니다.")
    if head.btype != expect_type:
        raise UserError("파일 종류가 다릅니다 (결과 파일과 사진 파일을 구분해 넣어 주세요).")
    return dev_id, head, payload, release


KNOWN_KINDS = {"CONTACT", "FIELD_ITEM_ADD", "ITEM_RESULT", "ATTEMPT", "LOCATION_FIX", "FIELD_MEMO", "PHOTO_META", "REVISIT_APPT", "DIAL_INTENT", "UNLOAD",
               "ARRIVE", "STICKER_CHECK", "FEE_NOTICE", "SMS_INTENT", "NAV_INTENT", "FIELD_INTAKE", "CORRECTION", "CANCEL_REQUEST"}
# v0.12 휴대폰(PC 연결 방식)의 요청 기록: 원본(PC)을 바로 바꾸지 않고 사무실 확인을 거친다
REVIEW_KINDS = {"CORRECTION": "현장 정정 요청 — 사무실 확인 필요", "CANCEL_REQUEST": "현장 취소 요청 — 사무실 확인 필요"}
CORRECTABLE = ("name", "phone", "address", "spot_note", "planned_date")
# 취소된 접수라도 충돌함으로 보내지 않는 기록(연락·사진·도착·문자/길안내 앱 열기 — 결과를 바꾸지 않음)
CANCELLED_OK_KINDS = ("CONTACT", "DIAL_INTENT", "PHOTO_META", "ARRIVE", "SMS_INTENT", "NAV_INTENT")


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def import_result(con, data: bytes, channel: str = "MAIL") -> dict:
    sha = hashlib.sha256(data).hexdigest()
    prev = one(con, "SELECT summary_json, release_code FROM result_import WHERE package_sha256=?", (sha,))
    if prev:
        return {"duplicate_file": True, **json.loads(prev["summary_json"] or "{}"), "release_code": prev["release_code"]}
    dev_id, head, payload, release = _open_from_device(con, data, P.TYPE_RESULT)
    if payload.get("schema") != "dg.result/1":
        raise UserError("결과 파일 형식이 아닙니다.")
    crew = one(con, "SELECT id FROM crew WHERE device_id=?", (dev_id,))
    imp = str(uuid.uuid4())
    counts = {"applied": 0, "skipped_duplicate": 0, "conflict": 0, "photos_expected": 0}
    touched = set()
    with tx(con):
        con.execute("INSERT INTO result_import VALUES (?,?,?,?,?,NULL,?)", (imp, channel, dev_id, sha, now(), release))
        for ev in payload.get("events", []):
            if one(con, "SELECT 1 AS x FROM result_event WHERE event_id=?", (ev["event_id"],)):
                counts["skipped_duplicate"] += 1
                continue
            if ev["kind"] == "UNLOAD":  # 휴대폰에서 기록한 하차 (접수와 무관, 팀 단위)
                b = ev.get("body", {})
                ok_ = bool(crew) and (b.get("volume_m3") or b.get("weight_kg"))
                con.execute("""INSERT INTO result_event (event_id, import_id, device_id, device_seq, request_id, assign_ver, base_req_ver, kind,
                    body_json, apply_status, conflict_reason, device_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (ev["event_id"], imp, dev_id, ev["seq"], "", ev.get("assign_ver") or 0, None, "UNLOAD", json.dumps(b, ensure_ascii=False),
                             "APPLIED" if ok_ else "REJECTED", None if ok_ else "팀에 연결되지 않은 휴대폰이거나 부피·중량 없음", ev.get("device_ts")))
                if ok_:
                    con.execute("""INSERT OR IGNORE INTO unload_log (id, crew_id, work_date, at_time, place, volume_m3, weight_kg, note, recorded_at)
                        VALUES (?,?,?,?,?,?,?,?,?)""", (ev["event_id"], crew["id"], b.get("work_date") or payload.get("work_date") or today_kst(),
                                                         (b.get("at_time") or "")[:5], (b.get("place") or "반입지")[:60],
                                                         float(b["volume_m3"]) if b.get("volume_m3") else None, float(b["weight_kg"]) if b.get("weight_kg") else None,
                                                         ("휴대폰 기록 " + (b.get("note") or "")).strip()[:200], now()))
                    counts["unloads"] = counts.get("unloads", 0) + 1
                continue
            ev.setdefault("request_id", "")
            if not isinstance(ev.get("body"), dict):
                ev["body"] = {}
            if ev["kind"] == "FIELD_INTAKE" and ev["request_id"]:  # 휴대폰 현장 접수 → PC 원본에 접수를 만든다(사무실 확인 표시)
                reason, note = None, None
                if one(con, "SELECT 1 AS x FROM request WHERE id=?", (ev["request_id"],)):
                    note = "이미 PC에 있는 접수(다시 만들지 않음)"
                else:
                    try:
                        _field_request(con, ev["request_id"], ev["body"], dev_id)
                        counts["field_intakes"] = counts.get("field_intakes", 0) + 1
                        touched.add(ev["request_id"])
                    except UserError as e:
                        reason = f"현장 접수 확인 필요: {e}"
                        counts["review"] = counts.get("review", 0) + 1
                con.execute("""INSERT INTO result_event (event_id, import_id, device_id, device_seq, request_id, assign_ver, base_req_ver, kind,
                    body_json, apply_status, conflict_reason, device_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (ev["event_id"], imp, dev_id, ev["seq"], ev["request_id"], 0, None, "FIELD_INTAKE",
                             json.dumps(ev["body"] if reason else _strip_request_pii("FIELD_INTAKE", ev["body"]), ensure_ascii=False),
                             "CONFLICT" if reason else "APPLIED", reason or note, ev.get("device_ts")))
                continue
            if not ev["request_id"] and ev.get("kind") not in KNOWN_KINDS:
                # 앞으로 생길 팀 단위 기록(알 수 없는 종류): 접수와 무관하게 보관만 하고 병합을 막지 않는다
                con.execute("""INSERT INTO result_event (event_id, import_id, device_id, device_seq, request_id, assign_ver, base_req_ver, kind,
                    body_json, apply_status, conflict_reason, device_ts) VALUES (?,?,?,?,?,?,?,?,?,'APPLIED',NULL,?)""",
                            (ev["event_id"], imp, dev_id, ev["seq"], "", ev.get("assign_ver") or 0, None, str(ev.get("kind") or "?")[:40],
                             json.dumps(ev["body"], ensure_ascii=False), ev.get("device_ts")))
                counts["stored_other"] = counts.get("stored_other", 0) + 1
                continue
            req = one(con, "SELECT status, req_ver, origin_device FROM request WHERE id=?", (ev["request_id"],))
            reason = None
            # 이 휴대폰이 현장에서 접수한 건: PC 목록에 들어오기 전(갱신본 0)에 남긴 결과는 배정 기록 없이도 받는다
            own = bool(req) and req.get("origin_device") == dev_id
            if not req:
                reason = "PC에 없는 접수"
            elif req["status"] == "CANCELLED" and ev["kind"] not in CANCELLED_OK_KINDS:
                reason = "취소된 접수의 결과"
            elif ev["kind"] in REVIEW_KINDS:
                reason = REVIEW_KINDS[ev["kind"]]
            elif ev["kind"] in ("ITEM_RESULT", "FIELD_ITEM_ADD", "ATTEMPT"):
                act = one(con, """SELECT b.assign_ver, b.crew_id FROM active_assignment a JOIN assignment_batch b ON b.id=a.batch_id
                    WHERE a.request_id=?""", (ev["request_id"],))
                done_before = req and req["status"] in FINAL
                if act and crew and act["crew_id"] != crew["id"]:
                    reason = "다른 팀에 다시 배정된 접수"
                elif act and ev.get("assign_ver") is not None and ev["assign_ver"] < act["assign_ver"] and not (own and not ev["assign_ver"]):
                    reason = f"오래된 목록(갱신본 {ev['assign_ver']} < 현재 {act['assign_ver']})의 결과"
                elif not act and not done_before and not own and not one(con, """SELECT 1 AS x FROM assignment_stop s JOIN assignment_batch b ON b.id=s.batch_id
                        WHERE s.request_id=? AND b.crew_id=?""", (ev["request_id"], crew["id"] if crew else "")):
                    reason = "이 팀에 배정된 적 없는 접수"
            status = "CONFLICT" if reason else "APPLIED"
            note = reason or (f"접수 수정(버전 {ev.get('base_req_ver')}→{req['req_ver']}) 뒤에 기록된 결과" if req and ev.get("base_req_ver") and ev["base_req_ver"] != req["req_ver"] else None)
            if reason and ev["kind"] in REVIEW_KINDS:
                counts["review"] = counts.get("review", 0) + 1  # 정정·취소 요청: 충돌이 아니라 사무실 확인 대상
            else:
                counts["conflict" if reason else "applied"] += 1
            con.execute("""INSERT INTO result_event (event_id, import_id, device_id, device_seq, request_id, assign_ver, base_req_ver, kind,
                body_json, apply_status, conflict_reason, device_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (ev["event_id"], imp, dev_id, ev["seq"], ev["request_id"], ev.get("assign_ver") or 0, ev.get("base_req_ver"),
                         ev["kind"], json.dumps(ev.get("body", {}), ensure_ascii=False), status, note, ev.get("device_ts")))
            if ev["kind"] == "PHOTO_META" and not reason:
                b = ev.get("body", {})
                con.execute("INSERT OR IGNORE INTO photo (photo_id, request_id, phase, sha256, bytes, device_id, line_id, taken_at, lat, lon, acc) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (b.get("photo_id"), ev["request_id"], b.get("phase"), b.get("sha256"), b.get("bytes"), dev_id, b.get("line_id"),
                             (b.get("taken_at") or b.get("file_ts") or None), _num(b.get("lat")), _num(b.get("lon")), _num(b.get("acc"))))
                counts["photos_expected"] += 1
            if not reason:
                touched.add(ev["request_id"])
                if ev["kind"] == "REVISIT_APPT":
                    _apply_revisit(con, ev["request_id"], ev.get("body", {}))
        for rid in touched:
            _refresh_status(con, rid)
        con.execute("UPDATE result_import SET summary_json=? WHERE id=?", (json.dumps(counts), imp))
        audit(con, "import", imp, "RESULT", counts)
    return {**counts, "release_code": release, "import_id": imp}


def _apply_revisit(con, rid: str, b: dict):
    """현장에서 통화로 약속한 재방문 날짜·시간을 접수 일정에 반영한다(이전 일정은 버전으로 보존)."""
    r = one(con, "SELECT * FROM request WHERE id=?", (rid,))
    date = b.get("date") or ""
    if not r or r["status"] == "CANCELLED" or not re.fullmatch(r"\d{4}-\d\d-\d\d", date):
        return
    start = b.get("start") if re.fullmatch(r"\d\d:\d\d", b.get("start") or "") else None
    end = b.get("end") if re.fullmatch(r"\d\d:\d\d", b.get("end") or "") else None
    if (r["planned_date"], r["appt_start"], r["appt_end"]) == (date, start, end):
        return
    con.execute("INSERT INTO request_version VALUES (?,?,?,?,?)",
                (rid, r["req_ver"], json.dumps(_snapshot(con, rid), ensure_ascii=False), "현장 통화로 재방문 약속(휴대폰 기록)", now()))
    con.execute("UPDATE request SET req_ver=req_ver+1, planned_date=?, appt_start=?, appt_end=?, updated_at=? WHERE id=?", (date, start, end, now(), rid))
    audit(con, "request", rid, "REVISIT_APPT", {"date": date, "start": start, "end": end})


def fee_usage(con, limit: int = 10) -> list[str]:
    """접수에 많이 쓰인 품목(PC 화면의 자주 쓰는 품목)."""
    return [r["fee_rate_id"] for r in rows(con, """SELECT fee_rate_id, COUNT(*) AS n FROM request_item WHERE fee_rate_id IS NOT NULL AND removed=0
        GROUP BY fee_rate_id ORDER BY n DESC LIMIT ?""", (limit,))]


def list_conflicts(con, include_resolved=False):
    sql = """SELECT e.event_id, e.kind, e.body_json, e.conflict_reason, e.apply_status, e.resolved_reason, e.device_ts, e.assign_ver,
        r.receipt_no, r.name, r.status AS request_status, e.request_id FROM result_event e LEFT JOIN request r ON r.id=e.request_id
        WHERE e.conflict_reason IS NOT NULL AND e.kind NOT IN ('DIAL_INTENT')"""
    if not include_resolved:
        sql += " AND e.apply_status='CONFLICT'"
    out = rows(con, sql + " ORDER BY e.device_ts")
    for c in out:  # 정정 요청: 지금 PC 원본 값을 나란히 보여 준다
        if c["kind"] == "CORRECTION" and c["request_id"]:
            r = one(con, "SELECT name, phone, address, spot_note, planned_date FROM request WHERE id=?", (c["request_id"],))
            c["current"] = r or {}
    return out


def resolve_conflict(con, event_id: str, action: str, reason: str):
    e = one(con, "SELECT * FROM result_event WHERE event_id=?", (event_id,))
    if not e or e["apply_status"] != "CONFLICT":
        raise UserError("처리할 충돌 기록이 없습니다.")
    if action not in ("APPLY", "REJECT"):
        raise UserError("반영 또는 거부를 골라 주세요.")
    if not (reason or "").strip():
        raise UserError("판단 사유를 적어 주세요.")
    body = json.loads(e["body_json"] or "{}")
    cancelled = False
    with tx(con):
        con.execute("UPDATE result_event SET apply_status=?, resolved_reason=?, resolved_at=? WHERE event_id=?",
                    ("APPLIED" if action == "APPLY" else "REJECTED", reason.strip(), now(), event_id))
        if action == "APPLY":
            if e["kind"] == "FIELD_INTAKE" and not one(con, "SELECT 1 AS x FROM request WHERE id=?", (e["request_id"],)):
                _field_request(con, e["request_id"], body, e["device_id"])
            if e["kind"] == "CORRECTION":
                _apply_correction(con, e["request_id"], body, reason.strip())
            if e["kind"] == "CANCEL_REQUEST":
                code, text, full = _reason(CANCEL_CODES, body.get("code") or "OTHER", body.get("text") or "현장 요청", "취소")
                _cancel(con, e["request_id"], code, text, f"{full} (현장 요청 · 사무실 확인: {reason.strip()})"[:300])
                cancelled = True
            if e["kind"] == "REVISIT_APPT":
                _apply_revisit(con, e["request_id"], json.loads(e["body_json"]))
            if e["kind"] == "PHOTO_META":
                b = json.loads(e["body_json"])
                con.execute("INSERT OR IGNORE INTO photo (photo_id, request_id, phase, sha256, bytes, device_id) VALUES (?,?,?,?,?,?)",
                            (b.get("photo_id"), e["request_id"], b.get("phase"), b.get("sha256"), b.get("bytes"), e["device_id"]))
            _refresh_status(con, e["request_id"])
        if e["kind"] in ("FIELD_INTAKE", "CORRECTION"):  # 처리한 요청 기록에서는 개인정보를 지운다(원본은 접수·버전 이력에)
            con.execute("UPDATE result_event SET body_json=? WHERE event_id=?", (json.dumps(_strip_request_pii(e["kind"], body), ensure_ascii=False), event_id))
        audit(con, "event", event_id, "RESOLVE_" + action, {"reason": reason.strip(), "kind": e["kind"]})
    if cancelled:
        _fin().sync_refunds(con)


# ------------------------------------------------------------------ 휴대폰(PC 연결 방식)의 현장 접수·정정 요청·취소 요청
def _strip_request_pii(kind: str, b: dict) -> dict:
    """반영·거부한 요청 기록에 남길 내용(개인정보 없이). 실제 값은 접수 원본과 버전 이력에 있다."""
    keep = {k: b[k] for k in ("worker", "temp_no") if b.get(k)}
    if kind == "FIELD_INTAKE":
        return {**keep, "planned_date": b.get("planned_date"), "items": len(b.get("items") or []), "pii_purged": True}
    if kind == "CORRECTION":
        f = b.get("fields") or {}
        return {**keep, "fields": ",".join(k for k in CORRECTABLE if k in f), **({"date": f["planned_date"]} if f.get("planned_date") else {}),
                "reason": (b.get("reason") or "")[:100], "pii_purged": True}
    return b


def _field_request(con, rid: str, b: dict, dev_id: str) -> str:
    """휴대폰 현장 접수로 PC 원본 접수를 만든다(트랜잭션은 호출하는 쪽). 금액은 PC 수수료표로 다시 계산하고,
    PC 표에 없는 품목은 '확인 대기'로 둔다(0원·최저가로 두지 않음). 품목 행 ID는 휴대폰 것을 그대로 써서 현장 결과가 이어진다."""
    if not re.fullmatch(r"[0-9a-f-]{36}", rid or ""):
        raise UserError("접수 ID 형식이 올바르지 않습니다.")
    fr = fee_rows(con)
    items = []
    for it in (b.get("items") or [])[:50]:
        if not isinstance(it, dict):
            continue
        x = {"line_id": str(it.get("line_id") or "")[:40] or None, "qty": it.get("qty"), "name": str(it.get("name") or "")[:60],
             "spec": str(it.get("spec") or "")[:60], "unit": str(it.get("unit") or "EA")[:10], "unit_label": str(it.get("unit_label") or "개")[:10]}
        if it.get("fee_rate_id") in fr:
            x["fee_rate_id"] = it["fee_rate_id"]
        elif not x["name"]:
            continue
        items.append(x)
    data = {k: str(b.get(k) or "")[:300] for k in ("name", "phone", "address", "spot_note", "memo", "planned_date")}
    data.update(channel="현장", items=items, appt_start=b.get("eta") if re.fullmatch(r"\d\d:\d\d", str(b.get("eta") or "")) else None)
    if _num(b.get("lat")) is not None and _num(b.get("lon")) is not None:
        data.update(lat=_num(b["lat"]), lon=_num(b["lon"]), geo_source="ADDR")
    dong_s = str(b.get("dong") or "").strip()[:20]
    if dong_s and _area().get_dong(con, dong_s):
        data["dong"] = dong_s
    d = validate(data)
    built = build_items(items, con)
    for it in built:
        if one(con, "SELECT 1 AS x FROM request_item WHERE line_id=?", (it["line_id"],)):
            raise UserError("품목 행 ID가 이미 PC에 있습니다(같은 기록을 다시 보냈는지 확인).")
    dong, _ = _resolve_dong(con, data, d["address"])
    _, receipt = _insert_request(con, d, built, data, dong, rid=rid)
    con.execute("UPDATE request SET origin_device=? WHERE id=?", (dev_id, rid))
    audit(con, "request", rid, "CREATE", {"receipt_no": receipt, "channel": "현장", "origin": "FIELD", "device": (dev_id or "")[:8],
                                          **({"temp_no": str(b["temp_no"])[:30]} if b.get("temp_no") else {}), **({"dong": dong} if dong else {})})
    return receipt


def _apply_correction(con, rid: str, b: dict, office_reason: str):
    """현장 정정 요청(이름·연락처·주소·상세 위치·예정일)을 원본에 반영한다. 이전 내용은 버전으로 보존(트랜잭션은 호출하는 쪽)."""
    r = one(con, "SELECT * FROM request WHERE id=?", (rid,))
    if not r:
        raise UserError("접수를 찾을 수 없습니다.")
    if r["status"] == "CANCELLED":
        raise UserError("취소된 접수는 고칠 수 없습니다. [거부]로 정리하세요.")
    f = {k: str(v).strip() for k, v in (b.get("fields") or {}).items() if k in CORRECTABLE and v is not None}
    new = {k: (f[k] if k in f else (r[k] or "")) for k in CORRECTABLE}
    new["phone"] = norm_phone(new["phone"])
    if not new["name"]:
        raise UserError("이름이 비어 있습니다.")
    if not re.fullmatch(r"0\d{8,10}", new["phone"]):
        raise UserError("요청한 연락처 형식이 올바르지 않습니다.")
    if not new["address"]:
        raise UserError("주소가 비어 있습니다.")
    if new["planned_date"] and not re.fullmatch(r"\d{4}-\d\d-\d\d", new["planned_date"]):
        raise UserError("요청한 예정일 형식이 올바르지 않습니다.")
    changed = [k for k in CORRECTABLE if (new[k] or None) != (r[k] or None)]
    if not changed:
        raise UserError("PC 원본과 같은 내용입니다. [거부]로 정리하세요.")
    flag = _after_close(con, rid)
    con.execute("INSERT INTO request_version VALUES (?,?,?,?,?)",
                (rid, r["req_ver"], json.dumps(_snapshot(con, rid), ensure_ascii=False), f"현장 정정 요청 반영: {office_reason}"[:200], now()))
    dong, lat, lon, geo, status = r["dong"], r["lat"], r["lon"], r["geo_source"], r["status"]
    if "address" in changed:  # 주소가 바뀌면 동을 다시 정하고, 이전 주소의 위치는 쓰지 않는다(현장 위치 보정이 있으면 다시 반영됨)
        dong = _area().detect_dong(con, new["address"])["dong"] or r["dong"]
        lat = lon = geo = None
    if "planned_date" in changed and con.execute("DELETE FROM active_assignment WHERE request_id=?", (rid,)).rowcount and status == "ASSIGNED":
        status = "RECEIVED"
    con.execute("""UPDATE request SET req_ver=req_ver+1, name=?, phone=?, address=?, spot_note=?, planned_date=?, dong=?, lat=?, lon=?, geo_source=?,
        status=?, updated_at=? WHERE id=?""", (new["name"], new["phone"], new["address"], new["spot_note"], new["planned_date"] or None, dong, lat, lon, geo,
                                               status, now(), rid))
    audit(con, "request", rid, "UPDATE", {"reason": f"현장 정정 요청: {office_reason}"[:200], "from_ver": r["req_ver"], "fields": changed, "origin": "FIELD", **flag})


def field_intakes(con) -> list[dict]:
    """휴대폰에서 현장 접수했는데 아직 사무실이 확인하지 않은 접수."""
    return rows(con, """SELECT id, receipt_no, name, address, planned_date, status, received_at, dong FROM request
        WHERE origin_device IS NOT NULL AND field_checked_at IS NULL ORDER BY received_at""")


def check_field_intake(con, rid: str) -> dict:
    """현장 접수를 사무실이 확인했다고 표시(주소·품목·금액을 본 뒤). 내용 수정은 일반 접수 수정으로."""
    r = one(con, "SELECT origin_device, field_checked_at FROM request WHERE id=?", (rid,))
    if not r or not r["origin_device"]:
        raise UserError("휴대폰 현장 접수가 아닙니다.")
    if r["field_checked_at"]:
        raise UserError("이미 확인한 접수입니다.")
    with tx(con):
        con.execute("UPDATE request SET field_checked_at=?, field_checked_by=? WHERE id=?", (now(), ACTOR.get(), rid))
        audit(con, "request", rid, "FIELD_CHECK", {})
    return {"ok": True}


# ------------------------------------------------------------------ 사진 회수 (보안 매체로 옮긴 사진 묶음 파일)
def import_photos(con, data: bytes) -> dict:
    sha = hashlib.sha256(data).hexdigest()
    prev = one(con, "SELECT summary_json, release_code FROM photo_import WHERE package_sha256=?", (sha,))
    if prev:
        return {"duplicate_file": True, **json.loads(prev["summary_json"]), "release_code": prev["release_code"]}
    dev_id, head, payload, release = _open_from_device(con, data, TYPE_PHOTOS)
    if payload.get("schema") != "dg.photos/1":
        raise UserError("사진 파일 형식이 아닙니다.")
    counts = {"recovered": 0, "already": 0, "hash_mismatch": 0, "unexpected": 0}
    with tx(con):
        for p in payload.get("photos", []):
            raw = base64.b64decode(p["data"])
            if hashlib.sha256(raw).hexdigest() != p.get("sha256"):
                counts["hash_mismatch"] += 1
                continue
            cur = one(con, "SELECT sha256, recovery_status FROM photo WHERE photo_id=?", (p["photo_id"],))
            if cur and cur["recovery_status"] == "RECOVERED":
                counts["already"] += 1
                continue
            if cur and cur["sha256"] != p["sha256"]:
                counts["hash_mismatch"] += 1
                continue
            if not cur:
                counts["unexpected"] += 1  # 결과 파일보다 사진이 먼저 온 경우: 보관하되 표시
                con.execute("INSERT INTO photo (photo_id, request_id, phase, sha256, bytes, device_id) VALUES (?,?,?,?,?,?)",
                            (p["photo_id"], p.get("request_id"), p.get("phase"), p["sha256"], len(raw), dev_id))
            con.execute("UPDATE photo SET data=?, recovery_status='RECOVERED', recovered_at=? WHERE photo_id=?", (raw, now(), p["photo_id"]))
            counts["recovered"] += 1
        con.execute("INSERT INTO photo_import VALUES (?,?,?,?,?,?)", (str(uuid.uuid4()), sha, dev_id, now(), json.dumps(counts), release))
        audit(con, "photo_import", sha[:16], "IMPORT", counts)
    # 해시가 맞지 않은 사진이 있으면 휴대폰에서 지우지 않도록 확인 코드를 주지 않는다
    return {**counts, "release_code": release if counts["hash_mismatch"] == 0 else None}


def photo_bytes(con, photo_id: str) -> bytes | None:
    r = one(con, "SELECT data FROM photo WHERE photo_id=? AND recovery_status='RECOVERED' AND purged_at IS NULL", (photo_id,))
    return r["data"] if r else None


# ------------------------------------------------------------------ 일일 마감
CARRY_KIND = {"RECEIVED": "미배정", "ASSIGNED": "결과 없음", "IN_PROGRESS": "최종 결과 없음", "PARTIAL": "일부 수거 · 재방문 약속 없음",
              "NOT_COLLECTED": "미수거 · 재방문 약속 없음", "ON_HOLD": "보류 · 재방문 약속 없음"}


def carry_candidates(con, work_date: str) -> list[dict]:
    """마감 때 다음 업무일로 넘길 접수: 그날 예정인데 끝나지 않은 건(미배정, 결과 없음, 재방문 약속 없는 일부·미수거·보류).
    재방문 약속이 있는 건은 이미 그 날짜로 옮겨져 있어 여기에 나오지 않는다."""
    area = _area()
    out = []
    for r in rows(con, "SELECT id, receipt_no, name, status, dong FROM request WHERE planned_date=? AND status NOT IN ('DONE','CANCELLED') ORDER BY receipt_no", (work_date,)):
        out.append({"id": r["id"], "receipt_no": r["receipt_no"], "name": r["name"], "status": r["status"], "dong": r["dong"],
                    "kind": CARRY_KIND.get(r["status"], r["status"]), "to": area.next_working_day(con, work_date, r["dong"])})
    return out


def _carry(con, c: dict, work_date: str):
    """한 건을 다음 업무일로 옮긴다(이전 일정은 버전으로 보존, 활성 배정은 풀어 다시 배정할 수 있게)."""
    r = one(con, "SELECT * FROM request WHERE id=?", (c["id"],))
    con.execute("INSERT INTO request_version VALUES (?,?,?,?,?)",
                (r["id"], r["req_ver"], json.dumps(_snapshot(con, r["id"]), ensure_ascii=False), f"마감 이월 ({work_date} → {c['to']})", now()))
    released = con.execute("DELETE FROM active_assignment WHERE request_id=?", (r["id"],)).rowcount
    con.execute("UPDATE request SET req_ver=req_ver+1, planned_date=?, status=CASE WHEN status='ASSIGNED' THEN 'RECEIVED' ELSE status END, updated_at=? WHERE id=?",
                (c["to"], now(), r["id"]))
    audit(con, "request", r["id"], "CARRY", {"reason": "마감 이월", "from": work_date, "to": c["to"], "kind": c["kind"], **({"released": True} if released else {})})


def _post_close_edits(con, work_date: str) -> list[dict]:
    """마감한 뒤 그날 접수·수납을 고친 기록(변경 이력의 '마감 후 수정' 표시)."""
    out = []
    for a in rows(con, """SELECT id, entity, entity_id, action, detail, at, actor FROM audit_log
            WHERE entity IN ('request','payment') AND json_extract(detail, '$.after_close')=? ORDER BY id""", (work_date,)):
        d = json.loads(a["detail"] or "{}")
        rid = a["entity_id"] if a["entity"] == "request" else d.get("request")
        rq = one(con, "SELECT receipt_no FROM request WHERE id=?", (rid,)) if rid else None
        out.append({"at": a["at"], "actor": a["actor"], "entity": a["entity"], "action": a["action"], "request_id": rid,
                    "receipt_no": rq["receipt_no"] if rq else "", "reason": d.get("reason") or ""})
    return out


def day_summary(con, work_date: str, crew_id: str | None = None) -> dict:
    reqs = rows(con, "SELECT * FROM request WHERE planned_date=?", (work_date,))
    if crew_id:
        mine = crew_request_ids(con, crew_id)
        reqs = [r for r in reqs if r["id"] in mine]
    by_status: dict[str, int] = {}
    pending_fee, revisit, no_result = [], [], []
    assigned_ids = {r["request_id"] for r in rows(con, """SELECT s.request_id FROM assignment_stop s JOIN assignment_batch b ON b.id=s.batch_id
        WHERE b.work_date=? AND b.status='ACTIVE'""", (work_date,))}
    for r in reqs:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        st = derive(con, r)
        if st["fee"]["pending_count"] and r["status"] != "CANCELLED":
            pending_fee.append({"id": r["id"], "receipt_no": r["receipt_no"], "name": r["name"], "count": st["fee"]["pending_count"]})
        if r["status"] in ("PARTIAL", "NOT_COLLECTED", "ON_HOLD"):
            revisit.append({"id": r["id"], "receipt_no": r["receipt_no"], "name": r["name"], "status": r["status"], "remaining": st["remaining_total"],
                            "next_date": r["planned_date"] if r["planned_date"] != work_date else None, "appt": [r["appt_start"], r["appt_end"]] if r["appt_start"] else None})
        if r["id"] in assigned_ids and r["status"] == "ASSIGNED":
            no_result.append({"id": r["id"], "receipt_no": r["receipt_no"], "name": r["name"]})
    # 휴대폰 v0.10 기록 확인: 스티커 없음·불일치, 추가 금액 고지 거부, 미수거인데 사진도 사진 없는 사유도 없음
    sticker_issues, fee_refused, no_evidence = [], [], []
    for r in reqs:
        if r["status"] == "CANCELLED":
            continue
        fx = derive(con, r)["field"]
        bad = [v for v in fx["stickers"].values() if v["result"] in ("MISSING", "MISMATCH")]
        if bad:
            sticker_issues.append({"id": r["id"], "receipt_no": r["receipt_no"], "name": r["name"], "count": len(bad),
                                   "kinds": sorted({"없음" if v["result"] == "MISSING" else "번호 불일치" for v in bad}),
                                   "left": sum(1 for v in bad if v["decision"] == "LEAVE")})
        if any(n["response"] == "REFUSED" for n in fx["fee_notices"]):
            last = [n for n in fx["fee_notices"] if n["response"] == "REFUSED"][-1]
            fee_refused.append({"id": r["id"], "receipt_no": r["receipt_no"], "name": r["name"], "extra": last["extra"]})
        if r["status"] == "NOT_COLLECTED" and not fx["no_photo_reason"] and not one(con, "SELECT 1 AS x FROM photo WHERE request_id=?", (r["id"],)):
            no_evidence.append({"id": r["id"], "receipt_no": r["receipt_no"], "name": r["name"]})
    ids = {r["id"] for r in reqs}
    photos = [x for x in rows(con, """SELECT p.photo_id, p.request_id, r.receipt_no FROM photo p JOIN request r ON r.id=p.request_id
        WHERE r.planned_date=? AND p.recovery_status!='RECOVERED'""", (work_date,)) if x["request_id"] in ids]
    conflicts = [c for c in list_conflicts(con) if c["request_id"] in ids]
    unconfirmed = rows(con, """SELECT t.id, t.work_date, t.assign_ver FROM transfer_bundle t LEFT JOIN assignment_batch b ON b.id=t.batch_id
        WHERE t.work_date=? AND t.receipt_confirmed_at IS NULL""" + (" AND b.crew_id=?" if crew_id else ""), (work_date, crew_id) if crew_id else (work_date,))
    unpaid = []
    for r in reqs:
        if r["status"] in ("DONE", "PARTIAL", "NOT_COLLECTED", "ON_HOLD", "CANCELLED"):
            ps = payment_status(con, r["id"])
            if ps["status"] in ("UNPAID", "UNDERPAID", "OVERPAID_REVIEW", "AMOUNT_PENDING", "PAID_AMOUNT_PENDING", "REFUND_NEEDED", "OVERREFUNDED_REVIEW"):
                unpaid.append({"id": r["id"], "receipt_no": r["receipt_no"], "name": r["name"], "status": ps["status"], "calculated": ps["calculated"],
                               "levied": ps["levied"], "paid": ps["paid"]})
    if not crew_id:  # 예정일이 없거나 다른 날인 접수라도 이날 취소했고 환불할 돈이 남았으면 함께 본다
        d0 = dt.datetime.fromisoformat(work_date).replace(tzinfo=KST).astimezone(dt.timezone.utc)
        rng = (d0.strftime("%Y-%m-%dT%H:%M:%SZ"), (d0 + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        for r in rows(con, "SELECT * FROM request WHERE status='CANCELLED' AND cancelled_at>=? AND cancelled_at<? AND COALESCE(planned_date,'')!=?", (*rng, work_date)):
            ps = payment_status(con, r["id"])
            if ps["status"] == "REFUND_NEEDED":
                unpaid.append({"id": r["id"], "receipt_no": r["receipt_no"], "name": r["name"], "status": ps["status"], "calculated": ps["calculated"],
                               "levied": ps["levied"], "paid": ps["paid"]})
    unloads = [u for u in list_unloads(con, work_date) if not crew_id or u["crew_id"] == crew_id]
    # 동별 현황 (오늘 건수·완료·미처리·한도 대비)
    area = _area()
    dongs = []
    by_dong: dict[str, list] = {}
    for r in reqs:
        by_dong.setdefault(r["dong"] or "", []).append(r)
    for d in area.list_dongs(con):
        lst = by_dong.pop(d["name"], [])
        if not lst and not d["active"]:
            continue
        live = [r for r in lst if r["status"] != "CANCELLED"]
        dongs.append({"dong": d["name"], "total": len(live), "done": sum(r["status"] == "DONE" for r in live),
                      "open": sum(r["status"] not in ("DONE",) for r in live), "limit": d["daily_limit"], "limit_m3": d["daily_limit_m3"],
                      "m3": round(sum(area.request_volume(con, r["id"]) for r in live), 2) if d["daily_limit_m3"] else None,
                      "crew": d["crew_name"], "working": area.working_reason(con, work_date, d) is None})
    for name, lst in by_dong.items():  # 동 미지정·지운 동
        live = [r for r in lst if r["status"] != "CANCELLED"]
        dongs.append({"dong": name or None, "total": len(live), "done": sum(r["status"] == "DONE" for r in live), "open": sum(r["status"] != "DONE" for r in live),
                      "limit": None, "limit_m3": None, "m3": None, "crew": None, "working": True})
    closes = rows(con, "SELECT id, seq, closed_at, closed_by, note, carried_json, status, approved_by, approved_at, approve_note FROM day_close_log WHERE work_date=? ORDER BY seq", (work_date,))
    for c in closes:
        c["carried"] = len(json.loads(c.pop("carried_json") or "[]"))
    cap = area.capacity_settings(con)
    return {"work_date": work_date, "total": len(reqs), "by_status": by_status, "no_result": no_result, "pending_fee": pending_fee,
            "revisit": revisit, "photos_missing": len(photos), "conflicts": len(conflicts), "unconfirmed_bundles": len(unconfirmed),
            "field_intakes": len(field_intakes(con)),
            "payments": unpaid, "unloads": unloads, "sticker_issues": sticker_issues, "fee_refused": fee_refused, "no_evidence": no_evidence,
            "unload_total": {"volume_m3": round(sum(u["volume_m3"] or 0 for u in unloads), 2), "weight_kg": round(sum(u["weight_kg"] or 0 for u in unloads), 1)},
            "carry": [] if crew_id else carry_candidates(con, work_date), "dongs": dongs,
            "capacity": {"limit": cap["daily_count"], "booked": sum(1 for r in reqs if r["status"] != "CANCELLED")},
            "working": area.working_reason(con, work_date),
            "post_close_edits": [] if crew_id else _post_close_edits(con, work_date),
            "closes": closes, "closed": closes[-1] if closes else None}


def close_day(con, work_date: str, note: str = "") -> dict:
    """마감 기록(담당자). 끝나지 않은 건은 다음 업무일로 예정일을 옮기고(마감 이월), 기록은 회차로 쌓는다(덮어쓰지 않음).
    팀장(검토자)이 approve_close 로 확인한다."""
    if not re.fullmatch(r"\d{4}-\d\d-\d\d", work_date or ""):
        raise UserError("마감할 날짜를 골라 주세요.")
    s = day_summary(con, work_date)
    carry = s["carry"]
    for k in ("closed", "closes", "post_close_edits", "dongs"):
        s.pop(k, None)
    for k in ("sticker_issues", "fee_refused", "no_evidence"):  # 요약에는 건수만
        s[k] = len(s.get(k) or [])
    with tx(con):
        for c in carry:
            _carry(con, c, work_date)
        seq = (one(con, "SELECT MAX(seq) AS n FROM day_close_log WHERE work_date=?", (work_date,))["n"] or 0) + 1
        con.execute("""INSERT INTO day_close_log (id, work_date, seq, closed_at, closed_by, summary_json, note, carried_json, status)
            VALUES (?,?,?,?,?,?,?,?,'RECORDED')""", (str(uuid.uuid4()), work_date, seq, now(), ACTOR.get(), json.dumps(s, ensure_ascii=False), (note or "")[:300],
                                                   json.dumps([{"id": c["id"], "receipt_no": c["receipt_no"], "to": c["to"], "kind": c["kind"]} for c in carry], ensure_ascii=False)))
        audit(con, "day", work_date, "CLOSE", {"seq": seq, "open_items": len(s["no_result"]) + len(s["revisit"]) + s["conflicts"] + s["photos_missing"],
                                               "carried": len(carry)})
    return {**s, "seq": seq, "carried": carry}


def approve_close(con, work_date: str, note: str = "") -> dict:
    """팀장 확인(두 번째 단계). 마감을 기록한 사람은 확인할 수 없다. 다시 마감하면 새 회차를 다시 확인해야 한다."""
    c = close_info(con, work_date)
    if not c:
        raise UserError("아직 마감 기록이 없습니다. 담당자가 먼저 마감을 기록해야 합니다.")
    if c["status"] == "APPROVED":
        raise UserError("이미 확인한 마감입니다.")
    if c["closed_by"] and c["closed_by"] == ACTOR.get():
        raise UserError("마감을 기록한 사람은 확인할 수 없습니다. 팀장(검토자)이 확인해 주세요.")
    with tx(con):
        con.execute("UPDATE day_close_log SET status='APPROVED', approved_by=?, approved_at=?, approve_note=? WHERE id=?",
                    (ACTOR.get(), now(), (note or "")[:300], c["id"]))
        audit(con, "day", work_date, "APPROVE", {"seq": c["seq"]})
    out = dict(close_info(con, work_date))
    # 마감 확인 뒤: 보유기간 지난 개인정보 파기, 오래된 접속기록 정리, 자동 백업 (실패해도 확인은 그대로)
    from dgpc import backup as _bk, privacy as _pv
    try:
        out["pii_purge"] = _pv.purge_pii(con, trigger="CLOSE_APPROVE")["count"]
        _pv.purge_old_access(con)
    except Exception:  # noqa: BLE001
        out["pii_purge"] = None
    out["backup"] = {k: v for k, v in _bk.make_auto(con, "CLOSE_APPROVE").items() if k in ("ok", "file", "error")}
    return out


# ------------------------------------------------------------------ QR 실측 (가상 자료, 업무 자료와 분리)
def make_qr_test(con, device_id: str, count: int, addr_style: str = "mixed", chunk: int = 700, ec: str = "M", fps: int = 3) -> dict:
    """기관 휴대폰으로 QR 인식률·전송 시간을 재기 위한 가상 목록. 휴대폰은 실측 모드로 받고 저장하지 않는다."""
    from sample_data import make_daylist
    if count not in (20, 50, 100) or addr_style not in ("mixed", "short", "medium", "long") or chunk not in (400, 600, 800, 1200) or ec not in ("L", "M", "Q"):
        raise UserError("실측 조건 값이 올바르지 않습니다.")
    dev = one(con, "SELECT * FROM device WHERE id=? AND status='ACTIVE'", (device_id,))
    if not dev:
        raise UserError("사용 중인 휴대폰을 골라 주세요.")
    sample = make_daylist(count, today_kst(), 1, seed=count * 7 + chunk, addr_style=addr_style)
    for st in sample["stops"]:
        st["receipt_no"] = "TEST-" + st["receipt_no"][3:]
    payload = P.compact_daylist(sample, FEE_ROWS)
    payload["schema"] = "dg.qrtest/1"
    payload["params"] = {"count": count, "addr": addr_style, "chunk": chunk, "ec": ec, "fps": fps}
    tid = uuid.uuid4().bytes
    sign = pc_key(con, "sign")
    head = P.Header(P.TYPE_DAYLIST, tid, uuid.UUID(device_id).bytes, 0, 1, P.key_id(P.pub_raw(sign)))
    env, code = P.seal(payload, head, P.pub_from_raw(dev["kex_pub"]), sign)
    frames = P.split_frames(env, tid, chunk)
    test_id = str(uuid.UUID(bytes=tid))
    with tx(con):
        con.execute("INSERT INTO qr_test (id, device_id, params_json, frames_json, bytes, check_code, created_at) VALUES (?,?,?,?,?,?,?)",
                    (test_id, device_id, json.dumps(payload["params"]), json.dumps(frames), len(env), code[:4], now()))
    return {"test_id": test_id, "frames": len(frames), "bytes": len(env), "params": payload["params"]}


def record_qr_test(con, test_id: str, elapsed_s: float, reads: int, check: str, note: str = "") -> dict:
    t = one(con, "SELECT * FROM qr_test WHERE id=?", (test_id,))
    if not t:
        raise UserError("실측 기록을 찾을 수 없습니다.")
    verified = 1 if re.sub(r"\s", "", check or "").upper() == t["check_code"] else 0
    with tx(con):
        con.execute("UPDATE qr_test SET elapsed_s=?, reads=?, verified=?, note=?, recorded_at=? WHERE id=?",
                    (float(elapsed_s), int(reads), verified, note, now(), test_id))
    return {"verified": bool(verified)}


def list_qr_tests(con) -> list[dict]:
    out = rows(con, "SELECT id, device_id, params_json, bytes, created_at, elapsed_s, reads, verified, note, json_array_length(frames_json) AS frames FROM qr_test ORDER BY created_at DESC LIMIT 200")
    for r in out:
        r["params"] = json.loads(r.pop("params_json"))
    return out


# ------------------------------------------------------------------ 수납 (담당자 확인 기록) — 계산 금액·납부 금액·상태를 분리
PAY_METHODS = ("현장 카드", "현장 현금", "가상계좌", "무통장 입금", "스티커 구매", "기타")
LEGACY_PAY_METHODS = ("현장 납부",)  # 이전 버전 기록·시험용 (화면에는 내지 않음)
REFUND_PAY_METHODS = ("계좌 이체", "현금", "카드 취소", "기타")


def _fin():
    from dgpc import finance
    return finance


def payment_status(con, rid: str) -> dict:
    r = one(con, "SELECT * FROM request WHERE id=?", (rid,))
    final = derive(con, r)["fee"]["final"]
    fs = _fin().fee_summary(r, final)
    owed = fs["levied"]
    recs = rows(con, "SELECT * FROM payment WHERE request_id=? ORDER BY recorded_at", (rid,))
    live = [x for x in recs if not x["void_reason"]]
    paid = sum(x["amount"] for x in live if x["kind"] == "PAY") - sum(x["amount"] for x in live if x["kind"] == "REFUND")
    if r["status"] == "CANCELLED" and paid == 0:
        status = "NONE"
    elif r["status"] == "CANCELLED":  # 납부 뒤 취소: 돌려줄 돈이 남아 있음 (마감 수납 확인·'환불 필요' 목록)
        status = "REFUND_NEEDED" if paid > 0 else "OVERREFUNDED_REVIEW"
    elif owed is None:
        status = "AMOUNT_PENDING" if paid == 0 else "PAID_AMOUNT_PENDING"
    elif owed == 0 and paid == 0:
        status = "EXEMPT"  # 전액 감면
    elif paid == 0:
        status = "UNPAID"
    elif paid < owed:
        status = "UNDERPAID"
    elif paid == owed:
        status = "PAID"
    else:
        status = "OVERPAID_REVIEW"
    return {**fs, "paid": paid, "status": status, "records": recs, "levy_no": r.get("levy_no")}


def add_payment(con, rid: str, data: dict) -> dict:
    if not one(con, "SELECT 1 AS x FROM request WHERE id=?", (rid,)):
        raise UserError("접수를 찾을 수 없습니다.")
    flag = _after_close(con, rid)
    why = (data.get("reason") or "").strip()
    if flag and not why:
        raise UserError(f"마감한 날({flag['after_close']})의 접수입니다. 수납 기록 사유를 적어 주세요.")
    kind = data.get("kind", "PAY")
    amount = int(data.get("amount") or 0)
    method = data.get("method") or ""
    paid_on = data.get("paid_on") or today_kst()
    if kind not in ("PAY", "REFUND") or amount <= 0:
        raise UserError("금액을 1원 이상으로 적어 주세요.")
    if method not in PAY_METHODS + LEGACY_PAY_METHODS + (REFUND_PAY_METHODS if kind == "REFUND" else ()):
        raise UserError("납부 방법을 골라 주세요.")
    ref = (data.get("receipt_ref") or "").strip()[:40] or None
    if not re.fullmatch(r"\d{4}-\d\d-\d\d", paid_on):
        raise UserError("날짜 형식이 올바르지 않습니다.")
    pid = str(uuid.uuid4())
    with tx(con):
        con.execute("INSERT INTO payment (id, request_id, kind, amount, method, paid_on, note, recorded_at, receipt_ref, source) VALUES (?,?,?,?,?,?,?,?,?,'수기')",
                    (pid, rid, kind, amount, method, paid_on, (data.get("note") or "").strip()[:100], now(), ref))
        audit(con, "payment", pid, kind, {"request": rid, "amount": amount, "method": method, **({"ref": ref} if ref else {}), **({"reason": why[:200]} if why else {}), **flag})
    _fin().sync_refunds(con)
    return payment_status(con, rid)


def void_payment(con, pid: str, reason: str) -> dict:
    p = one(con, "SELECT * FROM payment WHERE id=?", (pid,))
    if not p or p["void_reason"]:
        raise UserError("정정할 수납 기록이 없습니다.")
    if not (reason or "").strip():
        raise UserError("정정 사유를 적어 주세요.")
    with tx(con):
        con.execute("UPDATE payment SET void_reason=?, voided_at=? WHERE id=?", (reason.strip(), now(), pid))
        audit(con, "payment", pid, "VOID", {"request": p["request_id"], "reason": reason.strip(), **_after_close(con, p["request_id"])})
    _fin().sync_refunds(con)
    return payment_status(con, p["request_id"])


# ------------------------------------------------------------------ 하차 기록·적재량 보정
def add_unload(con, data: dict) -> str:
    if not one(con, "SELECT 1 AS x FROM crew WHERE id=?", (data.get("crew_id"),)):
        raise UserError("팀을 골라 주세요.")
    place = (data.get("place") or "").strip()
    if not place:
        raise UserError("하차 장소(반입지)를 적어 주세요.")
    vol = float(data["volume_m3"]) if data.get("volume_m3") not in (None, "") else None
    kg = float(data["weight_kg"]) if data.get("weight_kg") not in (None, "") else None
    if vol is None and kg is None:
        raise UserError("부피나 중량 중 하나는 적어 주세요.")
    uid = str(uuid.uuid4())
    with tx(con):
        con.execute("INSERT INTO unload_log (id, crew_id, work_date, at_time, place, volume_m3, weight_kg, note, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (uid, data["crew_id"], data.get("work_date") or today_kst(), data.get("at_time") or None, place, vol, kg,
                     (data.get("note") or "").strip(), now()))
        audit(con, "unload", uid, "ADD", {"crew": data["crew_id"]})
    return uid


def list_unloads(con, work_date: str) -> list[dict]:
    return rows(con, """SELECT u.*, c.name AS crew_name FROM unload_log u JOIN crew c ON c.id=u.crew_id
        WHERE u.work_date=? AND u.void_reason IS NULL ORDER BY u.at_time""", (work_date,))


def volume_overrides(con) -> dict:
    return get_setting(con, "volume_overrides", {})


def save_volume_override(con, fee_rate_id: str, m3: float | None):
    if fee_rate_id not in FEE_ROWS:
        raise UserError("수수료표에 없는 품목입니다.")
    v = volume_overrides(con)
    if m3 in (None, ""):
        v.pop(fee_rate_id, None)
    else:
        m = float(m3)
        if not 0 < m <= 20:
            raise UserError("부피는 0보다 크고 20m³ 이하로 적어 주세요.")
        v[fee_rate_id] = m
    with tx(con):
        set_setting(con, "volume_overrides", v)
        audit(con, "volume", fee_rate_id, "OVERRIDE", {"m3": m3})


# ------------------------------------------------------------------ 사용자·역할 (접수 담당자·수거팀·운영 관리자)
import hashlib as _hl
import hmac as _hmac
import secrets as _secrets

ROLES = {"ADMIN": "운영 관리자", "INTAKE": "접수 담당자", "FIELD": "수거팀", "LEAD": "팀장(검토자)"}


def _pw_hash(pw: str, salt: bytes) -> str:
    return _hl.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1).hex()


def _check_pw_rules(pw: str):
    if len(pw or "") < 10:
        raise UserError("비밀번호는 10자 이상으로 정해 주세요.")


def users_exist(con) -> bool:
    return bool(one(con, "SELECT 1 AS x FROM app_user LIMIT 1"))


def migrate_legacy_login(con):
    """v0.4 이하의 단일 업무 비밀번호를 관리자 계정(admin)으로 옮긴다."""
    old = get_setting(con, "login")
    if old and not users_exist(con):
        with tx(con):
            con.execute("INSERT INTO app_user (id, username, display_name, role, pw_salt, pw_hash, created_at) VALUES (?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), "admin", "관리자", "ADMIN", old["salt"], old["hash"], now()))
            con.execute("DELETE FROM meta WHERE key='login'")


def create_user(con, username: str, display_name: str, role: str, password: str, must_change: bool = False, crew_id: str | None = None) -> str:
    username = (username or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{3,30}", username):
        raise UserError("아이디는 영문 소문자·숫자·.-_ 3~30자로 정해 주세요.")
    if role not in ROLES:
        raise UserError("역할을 골라 주세요.")
    if not (display_name or "").strip():
        raise UserError("이름을 적어 주세요.")
    _check_pw_rules(password)
    if one(con, "SELECT 1 AS x FROM app_user WHERE username=?", (username,)):
        raise UserError("이미 있는 아이디입니다.")
    if crew_id and not one(con, "SELECT 1 AS x FROM crew WHERE id=?", (crew_id,)):
        raise UserError("없는 수거팀입니다.")
    salt = _secrets.token_bytes(16)
    uid = str(uuid.uuid4())
    with tx(con):
        con.execute("INSERT INTO app_user (id, username, display_name, role, pw_salt, pw_hash, must_change, created_at, crew_id) VALUES (?,?,?,?,?,?,?,?,?)",
                    (uid, username, display_name.strip(), role, salt.hex(), _pw_hash(password, salt), 1 if must_change else 0, now(),
                     crew_id if role == "FIELD" else None))
        audit(con, "user", uid, "CREATE", {"username": username, "role": role})
    return uid


def check_login(con, username: str, password: str) -> dict | None:
    u = one(con, "SELECT * FROM app_user WHERE username=? AND active=1", ((username or "").strip().lower(),))
    if not u:
        _pw_hash(password or "", b"0" * 16)  # 아이디 유무에 따른 응답 시간 차이를 줄인다
        return None
    if not _hmac.compare_digest(_pw_hash(password or "", bytes.fromhex(u["pw_salt"])), u["pw_hash"]):
        return None
    con.execute("UPDATE app_user SET last_login_at=? WHERE id=?", (now(), u["id"]))
    return {"id": u["id"], "username": u["username"], "display_name": u["display_name"], "role": u["role"], "must_change": bool(u["must_change"]),
            "crew_id": u["crew_id"]}


THEMES = ("auto", "light", "dark")
FONT_SIZES = (13, 14, 15, 16)


def user_prefs(con, uid: str) -> dict:
    r = one(con, "SELECT prefs_json FROM app_user WHERE id=?", (uid,))
    p = json.loads(r["prefs_json"]) if r and r["prefs_json"] else {}
    return {"theme": p.get("theme") if p.get("theme") in THEMES else "auto", "font": p.get("font") if p.get("font") in FONT_SIZES else 14}


def save_prefs(con, uid: str, data: dict) -> dict:
    """사용자별 화면 설정(테마: 자동·밝게·어둡게, 글자 크기 13~16px). 서버(DB)에 저장해 어느 PC에서나 같게."""
    cur = user_prefs(con, uid)
    if "theme" in data:
        if data["theme"] not in THEMES:
            raise UserError("테마 값이 올바르지 않습니다.")
        cur["theme"] = data["theme"]
    if "font" in data:
        try:
            f = int(data["font"])
        except (TypeError, ValueError):
            f = 0
        if f not in FONT_SIZES:
            raise UserError("글자 크기 값이 올바르지 않습니다.")
        cur["font"] = f
    with tx(con):
        con.execute("UPDATE app_user SET prefs_json=? WHERE id=?", (json.dumps(cur), uid))
    return cur


def list_users(con) -> list[dict]:
    return rows(con, """SELECT u.id, u.username, u.display_name, u.role, u.active, u.must_change, u.created_at, u.last_login_at, u.crew_id, c.name AS crew_name
        FROM app_user u LEFT JOIN crew c ON c.id=u.crew_id ORDER BY u.created_at""")


def set_user_active(con, uid: str, active: bool, me: str):
    if uid == me and not active:
        raise UserError("자기 계정은 중지할 수 없습니다.")
    u = one(con, "SELECT role FROM app_user WHERE id=?", (uid,))
    if not u:
        raise UserError("사용자를 찾을 수 없습니다.")
    if not active and u["role"] == "ADMIN" and one(con, "SELECT COUNT(*) AS n FROM app_user WHERE role='ADMIN' AND active=1")["n"] <= 1:
        raise UserError("마지막 운영 관리자는 중지할 수 없습니다.")
    with tx(con):
        con.execute("UPDATE app_user SET active=? WHERE id=?", (1 if active else 0, uid))
        audit(con, "user", uid, "ACTIVE" if active else "DEACTIVATE", None)


def set_password(con, uid: str, password: str, must_change: bool = False, old: str | None = None):
    _check_pw_rules(password)
    u = one(con, "SELECT * FROM app_user WHERE id=?", (uid,))
    if not u:
        raise UserError("사용자를 찾을 수 없습니다.")
    if old is not None and not _hmac.compare_digest(_pw_hash(old, bytes.fromhex(u["pw_salt"])), u["pw_hash"]):
        raise UserError("현재 비밀번호가 맞지 않습니다.")
    salt = _secrets.token_bytes(16)
    with tx(con):
        con.execute("UPDATE app_user SET pw_salt=?, pw_hash=?, must_change=? WHERE id=?", (salt.hex(), _pw_hash(password, salt), 1 if must_change else 0, uid))
        audit(con, "user", uid, "PASSWORD", {"reset_by_admin": must_change})


AUDIT_ENTITIES = {"request": "접수", "payment": "수납", "batch": "배정", "bundle": "목록 전송", "import": "결과 가져오기", "event": "충돌 처리",
                  "photo": "사진", "photo_import": "사진 가져오기", "day": "마감", "device": "휴대폰", "crew": "수거팀", "user": "사용자",
                  "backup": "백업", "fee": "수수료표", "address": "주소 자료", "report": "보고서·내보내기", "unload": "하차", "volume": "적재 보정",
                  "dong": "행정동", "holiday": "휴무일", "settings": "설정", "contact": "민원·통화", "request_import": "접수 일괄 등록",
                  "privacy": "개인정보 파기", "access": "접속기록", "refund": "환불", "revenue": "세입·대사"}
_AUDIT_HIDE = ("name", "phone", "address", "memo", "location_note", "sha256", "content", "spot_note")


def _audit_where(entity="", date="", actor="", date_from="", date_to="", action="") -> tuple[str, list]:
    sql, args = " WHERE 1=1", []
    if entity:
        sql += " AND entity=?"; args.append(entity)
    if action:
        sql += " AND action=?"; args.append(action)
    if date:
        date_from = date_to = date
    for v, op in ((date_from, ">="), (date_to, "<")):  # 한국시간 날짜 → UTC 범위
        if not v:
            continue
        if not re.fullmatch(r"\d{4}-\d\d-\d\d", v):
            raise UserError("기간을 날짜로 골라 주세요.")
        d0 = dt.datetime.fromisoformat(v).replace(tzinfo=KST).astimezone(dt.timezone.utc)
        if op == "<":
            d0 += dt.timedelta(days=1)
        sql += f" AND at {op} ?"; args.append(d0.strftime("%Y-%m-%dT%H:%M:%SZ"))
    if actor:
        sql += " AND actor=?"; args.append(actor)
    return sql, args


def list_audit(con, entity: str = "", date: str = "", actor: str = "", limit: int = 300, date_from: str = "", date_to: str = "",
               action: str = "", offset: int = 0) -> list[dict]:
    w, args = _audit_where(entity, date, actor, date_from, date_to, action)
    return rows(con, "SELECT id, entity, entity_id, action, detail, at, actor FROM audit_log" + w + " ORDER BY id DESC LIMIT ? OFFSET ?",
                args + [int(limit), int(offset)])


def audit_page(con, f: dict) -> dict:
    """변경 이력 조회(쪽 나눔·전체 건수). f: entity, action, actor, date_from, date_to, page, size."""
    w, args = _audit_where(f.get("entity", ""), f.get("date", ""), f.get("actor", ""), f.get("date_from", ""), f.get("date_to", ""), f.get("action", ""))
    total = one(con, "SELECT COUNT(*) AS n FROM audit_log" + w, args)["n"]
    size = max(1, min(1000, int(f.get("size") or 100)))
    pages = max(1, -(-total // size))
    page = max(1, min(pages, int(f.get("page") or 1)))
    return {"rows": list_audit(con, limit=size, offset=(page - 1) * size, entity=f.get("entity", ""), date=f.get("date", ""), actor=f.get("actor", ""),
                               date_from=f.get("date_from", ""), date_to=f.get("date_to", ""), action=f.get("action", "")),
            "total": total, "page": page, "pages": pages, "size": size}


def _audit_detail_text(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        d = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(d, dict):
        return str(d)[:200]
    return "; ".join(f"{k}={json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v}" for k, v in d.items() if k not in _AUDIT_HIDE)[:500]


def audit_csv(con, f: dict) -> bytes:
    """거른 변경 이력 CSV(엑셀용 BOM). 이력 내용에는 원래 이름·연락처·주소가 없고, 혹시 있을 만한 칸도 빼고 내보낸다."""
    import csv
    import io
    w, args = _audit_where(f.get("entity", ""), f.get("date", ""), f.get("actor", ""), f.get("date_from", ""), f.get("date_to", ""), f.get("action", ""))
    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(["번호", "시각(한국시간)", "작업자", "대상", "작업", "대상 번호", "내용"])
    n = 0
    for a in rows(con, "SELECT id, entity, entity_id, action, detail, at, actor FROM audit_log" + w + " ORDER BY id DESC LIMIT 100000", args):
        t = dt.datetime.fromisoformat(a["at"].replace("Z", "+00:00")).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
        wr.writerow([a["id"], t, a["actor"] or "", AUDIT_ENTITIES.get(a["entity"], a["entity"]), a["action"], (a["entity_id"] or "")[:8], _audit_detail_text(a["detail"])])
        n += 1
    with tx(con):
        audit(con, "report", "audit", "EXPORT", {"rows": n, **{k: f[k] for k in ("entity", "actor", "date_from", "date_to") if f.get(k)}})
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


# ------------------------------------------------------------------ 민원·통화 기록 (접수별)
def list_contacts(con, rid: str) -> list[dict]:
    return rows(con, "SELECT id, at, kind, content, actor, recorded_at FROM contact_log WHERE request_id=? ORDER BY at DESC, recorded_at DESC", (rid,))


def add_contact(con, rid: str, data: dict) -> str:
    if not one(con, "SELECT 1 AS x FROM request WHERE id=?", (rid,)):
        raise UserError("접수를 찾을 수 없습니다.")
    kind = data.get("kind") or ""
    if kind not in CONTACT_KINDS:
        raise UserError("유형을 골라 주세요 (" + ", ".join(CONTACT_KINDS) + ").")
    content = (data.get("content") or "").strip()
    if not content:
        raise UserError("내용을 적어 주세요.")
    at = (data.get("at") or "").strip() or dt.datetime.now(KST).strftime("%Y-%m-%dT%H:%M")
    if not re.fullmatch(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d", at):
        raise UserError("일시 형식이 올바르지 않습니다.")
    cid = str(uuid.uuid4())
    with tx(con):
        con.execute("INSERT INTO contact_log (id, request_id, at, kind, content, actor, recorded_at) VALUES (?,?,?,?,?,?,?)",
                    (cid, rid, at.replace(" ", "T"), kind, content[:1000], ACTOR.get(), now()))
        audit(con, "contact", cid, "ADD", {"request": rid, "kind": kind})  # 내용은 이력에 넣지 않는다(개인정보가 섞일 수 있음)
    return cid


# ------------------------------------------------------------------ 일괄 처리 (접수 목록에서 고른 건들, 한 트랜잭션·건별 이력)
def _ids(con, ids) -> list[str]:
    ids = list(dict.fromkeys(i for i in (ids or []) if isinstance(i, str) and i))
    if not ids:
        raise UserError("처리할 접수를 하나 이상 고르세요.")
    if len(ids) > 1000:
        raise UserError("한 번에 1,000건까지 처리할 수 있습니다.")
    if any(not one(con, "SELECT 1 AS x FROM request WHERE id=?", (i,)) for i in ids):
        raise UserError("없는 접수가 포함되어 있습니다.")
    return ids


def _reschedule(con, rid: str, to: str, full: str, code: str):
    r = one(con, "SELECT * FROM request WHERE id=?", (rid,))
    if r["status"] in ("DONE", "CANCELLED"):
        raise UserError(f"{r['receipt_no']}: 수거 완료·취소된 접수는 예정일을 바꿀 수 없습니다.")
    if r["planned_date"] == to:
        return False
    flag = _after_close(con, rid)
    con.execute("INSERT INTO request_version VALUES (?,?,?,?,?)",
                (rid, r["req_ver"], json.dumps(_snapshot(con, rid), ensure_ascii=False), f"예정일 변경 · {full}", now()))
    act = one(con, "SELECT b.work_date FROM active_assignment a JOIN assignment_batch b ON b.id=a.batch_id WHERE a.request_id=?", (rid,))
    released = False
    if act and act["work_date"] != to:  # 다른 날 배정에 들어 있으면 풀어서 새 날짜에 다시 배정하게
        con.execute("DELETE FROM active_assignment WHERE request_id=?", (rid,))
        released = True
    con.execute("UPDATE request SET req_ver=req_ver+1, planned_date=?, status=CASE WHEN status='ASSIGNED' AND ? THEN 'RECEIVED' ELSE status END, updated_at=? WHERE id=?",
                (to, 1 if released else 0, now(), rid))
    audit(con, "request", rid, "RESCHEDULE", {"from": r["planned_date"], "to": to, "code": code, "reason": full, **({"released": True} if released else {}), **flag})
    return True


def reschedule_requests(con, ids, to: str, code: str | None = None, text: str = "") -> dict:
    """예정일 변경(여러 건, 한 트랜잭션). 사유 코드 필수. 다른 날 배정에 들어 있던 건은 배정을 풉니다."""
    if not re.fullmatch(r"\d{4}-\d\d-\d\d", to or ""):
        raise UserError("바꿀 수거 예정일을 골라 주세요.")
    code, text, full = _reason(POSTPONE_CODES, code, text, "예정일 변경")
    ids = _ids(con, ids)
    changed = 0
    with tx(con):
        for rid in ids:
            changed += bool(_reschedule(con, rid, to, full, code))
    area = _area()
    warn = []
    for dname in sorted({(one(con, "SELECT dong FROM request WHERE id=?", (i,)) or {}).get("dong") or "" for i in ids}):
        warn += area.capacity_warnings(con, to, dname or None)
    return {"changed": changed, "warnings": list(dict.fromkeys(warn))}


def cancel_requests(con, ids, code: str | None = None, text: str = "") -> dict:
    code, text, full = _reason(CANCEL_CODES, code, text, "취소")
    ids = _ids(con, ids)
    res = {"cancelled": 0, "skipped": 0, "field_notice_needed": 0, "refund_needed": []}
    with tx(con):
        for rid in ids:
            if one(con, "SELECT status FROM request WHERE id=?", (rid,))["status"] == "CANCELLED":
                res["skipped"] += 1
                continue
            r = _cancel(con, rid, code, text, full)
            res["cancelled"] += 1
            res["field_notice_needed"] += r["field_notice_needed"]
            if r["refund_needed"]:
                res["refund_needed"].append(one(con, "SELECT receipt_no FROM request WHERE id=?", (rid,))["receipt_no"])
    _fin().sync_refunds(con)
    return res


def set_requests_dong(con, ids, dong: str, reason: str = "") -> dict:
    """동 지정(여러 건). 마감한 날의 접수가 섞여 있으면 사유가 필요하다."""
    dong = (dong or "").strip() or None
    if dong and not _area().get_dong(con, dong):
        raise UserError("등록되지 않은 행정동입니다.")
    ids = _ids(con, ids)
    if any(_after_close(con, i) for i in ids) and not (reason or "").strip():
        raise UserError("마감한 날의 접수가 섞여 있습니다. 동 지정 사유를 적어 주세요.")
    n = 0
    with tx(con):
        for rid in ids:
            r = one(con, "SELECT * FROM request WHERE id=?", (rid,))
            if r["dong"] == dong:
                continue
            why = "동 지정" + (f" · {reason.strip()[:200]}" if (reason or "").strip() else "")
            con.execute("INSERT INTO request_version VALUES (?,?,?,?,?)", (rid, r["req_ver"], json.dumps(_snapshot(con, rid), ensure_ascii=False), why, now()))
            con.execute("UPDATE request SET req_ver=req_ver+1, dong=?, updated_at=? WHERE id=?", (dong, now(), rid))
            audit(con, "request", rid, "SET_DONG", {"from": r["dong"], "to": dong, **({"reason": reason.strip()[:200]} if (reason or "").strip() else {}), **_after_close(con, rid)})
            n += 1
    return {"changed": n}


EXPORT_COLS = ["접수번호", "부과번호", "접수일", "수거 예정일", "완료일", "행정동", "접수 경로", "상태", "상태 코드", "품목(현장 확인 수량)", "수거 수량 합계", "남은 수량 합계",
               "계산 금액(원)", "감면 사유", "감면율(%)", "감면액(원)", "부과 금액(원)", "납부 기록(원)", "수납 상태", "수납 상태 코드", "수수료표 버전", "수거팀"]
STATUS_LABEL = {"RECEIVED": "접수", "ASSIGNED": "배정", "IN_PROGRESS": "처리 중", "PARTIAL": "일부 수거", "DONE": "수거 완료", "NOT_COLLECTED": "미수거",
                "ON_HOLD": "보류", "CANCELLED": "취소"}
PAY_LABEL = {"NONE": "해당 없음", "AMOUNT_PENDING": "금액 확인 대기", "PAID_AMOUNT_PENDING": "납부 기록 있음·금액 미확정", "UNPAID": "미납", "UNDERPAID": "부족",
             "PAID": "납부 확인", "OVERPAID_REVIEW": "과납 확인 필요", "REFUND_NEEDED": "환불 필요", "OVERREFUNDED_REVIEW": "환불 초과 확인 필요", "EXEMPT": "전액 감면"}


def _export_row(con, r: dict) -> list:
    st = derive(con, r)
    ps = payment_status(con, r["id"])
    ver = one(con, "SELECT fee_version_id FROM request_item WHERE request_id=? AND fee_version_id IS NOT NULL LIMIT 1", (r["id"],))
    a = one(con, """SELECT c.name FROM assignment_stop s JOIN assignment_batch b ON b.id=s.batch_id JOIN crew c ON c.id=b.crew_id
        WHERE s.request_id=? ORDER BY b.created_at DESC LIMIT 1""", (r["id"],))
    blank = lambda v: "" if v is None else v
    return [r["receipt_no"], r.get("levy_no") or "", _kst_date(r["received_at"]), r["planned_date"], _kst_date(r.get("completed_at")) or "", r["dong"] or "", r["channel"] or "",
            STATUS_LABEL.get(r["status"], r["status"]), r["status"],
            "; ".join(f'{l["name"]} {l["spec"]} {l["qty_confirmed"]}{l["unit_label"]}' for l in st["lines"]),
            sum(l["qty_collected"] for l in st["lines"]), sum(l["remaining"] for l in st["lines"]),
            blank(ps["calculated"]), ps.get("reduction_label") or "", ps["reduction_rate"] or "", blank(ps["reduction"]) if ps["reduction_rate"] else "", blank(ps["levied"]),
            ps["paid"], PAY_LABEL.get(ps["status"], ps["status"]), ps["status"], ver["fee_version_id"] if ver else "", a["name"] if a else ""]


def export_requests(con, ids, include_pii: bool = False) -> bytes:
    """고른 접수 CSV(엑셀용 BOM). 기본은 통계 CSV와 같이 이름·연락처·주소 없이. 개인정보 포함은 접수 담당자·관리자만(서버가 막음),
    화면에서 확인을 받고, 건마다 내보낸 기록을 남긴다."""
    import csv
    import io
    ids = _ids(con, ids)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_COLS + (["이름", "연락처", "배출 주소", "상세 위치"] if include_pii else []))
    with tx(con):
        for rid in ids:
            r = one(con, "SELECT * FROM request WHERE id=?", (rid,))
            w.writerow(_export_row(con, r) + ([r["name"], r["phone"], r["address"], r["spot_note"]] if include_pii else []))
            audit(con, "request", rid, "EXPORT", {"pii": bool(include_pii)})
        if include_pii:
            from dgpc import privacy as _pv
            _pv.record_access(con, "EXPORT", "", len(ids), "고른 접수 CSV(개인정보 포함)")
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def print_requests(con, ids) -> dict:
    """고른 접수 목록 인쇄용 자료(이름·연락처·주소 포함 → 파쇄 안내, 건마다 출력 기록)."""
    ids = _ids(con, ids)
    out = []
    with tx(con):
        for rid in ids:
            r = one(con, "SELECT * FROM request WHERE id=?", (rid,))
            r["items"] = rows(con, "SELECT name, spec, qty, unit_label FROM request_item WHERE request_id=? AND removed=0", (rid,))
            r["assigned"] = one(con, """SELECT c.name, b.work_date FROM active_assignment a JOIN assignment_batch b ON b.id=a.batch_id
                JOIN crew c ON c.id=b.crew_id WHERE a.request_id=?""", (rid,))
            out.append(r)
            audit(con, "request", rid, "PRINT", None)
        from dgpc import privacy as _pv
        _pv.record_access(con, "PRINT", ids[0] if len(ids) == 1 else "", len(ids), "고른 접수 목록 인쇄")
    out.sort(key=lambda r: (r["planned_date"] or "9999", r["dong"] or "", r["receipt_no"]))
    return {"rows": out, "printed_at": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")}


# ------------------------------------------------------------------ 접수 일괄 등록 (CSV·엑셀에서 저장한 CSV)
IMPORT_COLS = ["이름", "연락처", "배출 주소", "상세 위치", "수거 예정일", "행정동", "접수 경로", "메모"] + [f"{k}{i}" for i in range(1, 6) for k in ("품목", "규격", "수량")]


def import_template() -> bytes:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(IMPORT_COLS)
    w.writerow(["가상신청", "010-0000-0000", "가상시 동구 예시로 1", "1층 현관 앞", "", "", "단체(공동주택)", "가상 예시 행(지우고 쓰세요)",
                "의자", "고정식 의자", "2", "장롱", "120cm 이상 1쪽당", "1"] + [""] * 9)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def _match_fee(con, name: str, spec: str) -> str | None:
    rows_now = fee_rows(con)
    n, sp = norm_addr(name), norm_addr(spec)
    hits = [r for r in rows_now.values() if norm_addr(r["name"]) == n and (not sp or norm_addr(r["spec"]) == sp)]
    return hits[0]["id"] if len(hits) == 1 else None


def parse_import(con, raw: bytes) -> list[dict]:
    """CSV를 읽어 행마다 접수 자료·오류·경고·중복 후보를 만든다(저장하지 않음)."""
    import csv
    import io
    from dgpc import geo
    text = geo.decode(raw)
    rd = list(csv.reader(io.StringIO(text)))
    rd = [r for r in rd if any(c.strip() for c in r)]
    if not rd:
        raise UserError("빈 파일입니다.")
    head = [c.strip() for c in rd[0]]
    for need in ("이름", "연락처", "배출 주소", "품목1"):
        if need not in head:
            raise UserError(f"서식의 머리줄이 다릅니다('{need}' 열 없음). [서식 내려받기]로 받은 파일을 쓰세요.")
    if len(rd) > 2001:
        raise UserError("한 번에 2,000행까지 넣을 수 있습니다.")
    col = {h: i for i, h in enumerate(head)}
    get = lambda r, k: (r[col[k]].strip() if k in col and col[k] < len(r) else "")
    area = _area()
    out, seen_phone, seen_addr = [], {}, {}
    for n, r in enumerate(rd[1:], start=2):
        errors, warns = [], []
        items = []
        for i in range(1, 6):
            nm, sp, q = get(r, f"품목{i}"), get(r, f"규격{i}"), get(r, f"수량{i}")
            if not nm:
                continue
            try:
                qty = int(q or "1")
                if qty <= 0:
                    raise ValueError
            except ValueError:
                errors.append(f"수량{i}이(가) 1 이상의 숫자가 아님")
                continue
            fid = _match_fee(con, nm, sp)
            if fid:
                items.append({"fee_rate_id": fid, "qty": qty})
            else:
                items.append({"name": (nm + (" " + sp if sp else ""))[:60], "qty": qty})
                warns.append(f"'{(nm + ' ' + sp).strip()}' 수수료표에서 찾지 못해 금액 확인 대기")
        data = {"name": get(r, "이름"), "phone": get(r, "연락처"), "address": get(r, "배출 주소"), "spot_note": get(r, "상세 위치"),
                "memo": get(r, "메모"), "planned_date": get(r, "수거 예정일"), "channel": get(r, "접수 경로") or "단체(공동주택)", "items": items}
        dname = get(r, "행정동")
        d = None
        try:
            d = validate(data)
            build_items(items, con)
        except UserError as e:
            errors.append(str(e))
        if dname:
            if not area.get_dong(con, dname):
                errors.append(f"등록되지 않은 행정동({dname})")
            data["dong"] = dname
        elif d:
            data["dong"] = area.detect_dong(con, d["address"])["dong"] or ""
            if not data["dong"] and area.list_dongs(con, active_only=True):
                warns.append("행정동을 정하지 못함")
        dups = []
        if d:
            dups = [x["receipt_no"] for x in find_duplicates(con, d["phone"], d["address"])]
            p_, a_ = d["phone"], norm_addr(d["address"])
            if p_ in seen_phone or a_ in seen_addr:
                dups.append(f"파일 {seen_phone.get(p_) or seen_addr.get(a_)}행")
            seen_phone.setdefault(p_, n); seen_addr.setdefault(a_, n)
            warns += area.capacity_warnings(con, d["planned_date"] or None, data.get("dong") or None)
        out.append({"line": n, "data": data, "errors": errors, "warnings": list(dict.fromkeys(warns)), "duplicates": dups,
                    "summary": {"name": data["name"], "phone": norm_phone(data["phone"]), "address": data["address"], "planned_date": data["planned_date"],
                                "dong": data.get("dong") or "", "channel": data["channel"], "items": len(items)}})
    return out


def import_requests(con, raw: bytes, preview: bool = True, skip_duplicates: bool = False) -> dict:
    """미리보기(오류·경고·중복 후보 표시) → 등록. 등록할 때 오류 있는 행은 건너뛰고(또는 중복 후보도 건너뛰고) 한 트랜잭션으로 넣는다."""
    rowsx = parse_import(con, raw)
    info = {"rows": len(rowsx), "errors": sum(1 for r in rowsx if r["errors"]), "duplicates": sum(1 for r in rowsx if r["duplicates"]),
            "ok": sum(1 for r in rowsx if not r["errors"])}
    if preview:
        return {**info, "lines": [{k: r[k] for k in ("line", "errors", "warnings", "duplicates", "summary")} for r in rowsx]}
    batch = str(uuid.uuid4())
    created, skipped = [], []
    with tx(con):
        for r in rowsx:
            if r["errors"] or (skip_duplicates and r["duplicates"]):
                skipped.append(r["line"])
                continue
            d = validate(r["data"])
            items = build_items(r["data"]["items"], con)
            dong = r["data"].get("dong") or None
            rid, receipt = _insert_request(con, d, items, r["data"], dong)
            audit(con, "request", rid, "CREATE", {"receipt_no": receipt, "channel": d["channel"], "import": batch[:8], **({"dong": dong} if dong else {})})
            created.append(receipt)
        audit(con, "request_import", batch, "IMPORT", {"rows": len(rowsx), "created": len(created), "skipped": len(skipped)})
    return {**info, "created": len(created), "receipts": created, "skipped_lines": skipped}


# ------------------------------------------------------------------ 사진 보관 기간 정리
def purge_candidates(con, today: str | None = None) -> list[dict]:
    """보관 기간이 지난 사진: 회수 완료 + 접수가 끝남(수거 완료·취소) 또는 그날 마감됨 + 회수일로부터 기간 경과."""
    days = settings(con)["photo_retention_days"]
    t = dt.date.fromisoformat(today or today_kst())
    cutoff = (dt.datetime.combine(t - dt.timedelta(days=days), dt.time()).replace(tzinfo=KST)).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return rows(con, """SELECT p.photo_id, p.request_id, p.bytes, p.recovered_at, r.receipt_no, r.status FROM photo p JOIN request r ON r.id=p.request_id
        WHERE p.recovery_status='RECOVERED' AND p.purged_at IS NULL AND p.data IS NOT NULL AND p.recovered_at < ?
          AND (r.status IN ('DONE','CANCELLED') OR EXISTS (SELECT 1 FROM day_close_log d WHERE d.work_date=r.planned_date))
        ORDER BY p.recovered_at""", (cutoff,))


def purge_photos(con, today: str | None = None) -> dict:
    c = purge_candidates(con, today)
    if not c:
        return {"count": 0, "bytes": 0}
    with tx(con):
        t = now()
        for p in c:
            con.execute("UPDATE photo SET data=NULL, purged_at=? WHERE photo_id=?", (t, p["photo_id"]))
        audit(con, "photo", "retention", "PURGE", {"count": len(c), "bytes": sum(p["bytes"] or 0 for p in c), "retention_days": settings(con)["photo_retention_days"]})
    return {"count": len(c), "bytes": sum(p["bytes"] or 0 for p in c)}


# ------------------------------------------------------------------ 백업
def backup_dir(con) -> Path:
    from dgpc import store as _store
    b = settings(con)["backup_dir"]
    return Path(b) if b else _store.state_dir() / "backups"


def make_backup(con, passphrase: str) -> dict:
    from dgpc import store as _store
    dest = backup_dir(con) / f"donggu-backup-{dt.datetime.now(KST).strftime('%Y%m%d-%H%M%S')}.dgbak"
    try:
        _store.backup_to(con, dest, passphrase)
    except ValueError as e:
        raise UserError(str(e))
    h = hashlib.sha256(dest.read_bytes()).hexdigest()
    with tx(con):
        audit(con, "backup", dest.name, "BACKUP", {"file": dest.name, "bytes": dest.stat().st_size, "sha256": h[:16]})
        set_setting(con, "last_backup_at", now())
    return {"file": str(dest), "name": dest.name, "bytes": dest.stat().st_size, "sha256": h}


def list_backups(con) -> list[dict]:
    d = backup_dir(con)
    if not d.exists():
        return []
    files = sorted([*d.glob("*.dgbak"), *d.glob("*.dgabk")], key=lambda f: f.stat().st_mtime, reverse=True)
    return [{"name": f.name, "bytes": f.stat().st_size, "auto": f.suffix == ".dgabk",
             "at": dt.datetime.fromtimestamp(f.stat().st_mtime, KST).strftime("%Y-%m-%d %H:%M")} for f in files]


def verify_backup(con, name: str, passphrase: str) -> dict:
    from dgpc import store as _store
    if "/" in name or "\\" in name or not name.endswith(".dgbak"):
        raise UserError("백업 파일 이름이 올바르지 않습니다.")
    try:
        bcon, n = _store.open_backup(backup_dir(con) / name, passphrase)
    except ValueError as e:
        raise UserError(str(e))
    bcon.close()
    return {"ok": True, "requests": n}


# ------------------------------------------------------------------ 운영 수수료표 (원문 표 + 승인된 개정)
# 원문 변환본(data/fee_table.json)은 고치지 않는다. 담당자가 확정한 단가·시행일·확인자는 개정(fee_revision)으로 쌓고,
# 다른 담당자가 승인한 뒤 시행일부터 적용한다. 이미 접수된 품목의 단가·버전은 바꾸지 않는다(원본 보존).
def _active_revision(con, today: str | None = None):
    t = today or today_kst()
    return one(con, """SELECT * FROM fee_revision WHERE status='APPROVED' AND base_version=? AND (effective_from IS NULL OR effective_from<=?)
        ORDER BY rev_no DESC LIMIT 1""", (FEE_TABLE["version_id"], t))


def fee_state(con, today: str | None = None) -> dict:
    rev = _active_revision(con, today) if con is not None else None
    if not rev:
        return {"version_id": FEE_TABLE["version_id"], "base_version": FEE_TABLE["version_id"], "rev_no": 0, "effective_from": FEE_TABLE["effective_from"],
                "confirmed_by": None, "approved_by": None, "overrides": {}}
    return {"version_id": f'{FEE_TABLE["version_id"]}-r{rev["rev_no"]}', "base_version": FEE_TABLE["version_id"], "rev_no": rev["rev_no"],
            "effective_from": rev["effective_from"], "confirmed_by": rev["confirmed_by"], "approved_by": rev["approved_by"],
            "overrides": json.loads(rev["overrides_json"])}


def fee_rows(con, today: str | None = None) -> dict:
    ov = fee_state(con, today)["overrides"]
    if not ov:
        return FEE_ROWS
    out = dict(FEE_ROWS)
    for rid, o in ov.items():
        if rid in out:
            out[rid] = {**out[rid], "price": int(o["price"]), "status": "CONFIRMED", "note": o.get("reason", ""), "overridden": True}
    return out


def fee_table_for_phone(con) -> dict:
    stt = fee_state(con)
    return {"version_id": stt["version_id"], "base_version": stt["base_version"], "label": FEE_TABLE["label"],
            "effective_from": stt["effective_from"], "overrides": {k: int(v["price"]) for k, v in stt["overrides"].items()}}


def list_fee_revisions(con) -> list[dict]:
    out = rows(con, "SELECT * FROM fee_revision ORDER BY rev_no DESC")
    act = _active_revision(con)
    for r in out:
        r["overrides"] = json.loads(r.pop("overrides_json"))
        r["active"] = bool(act and act["id"] == r["id"])
        r["version_id"] = f'{r["base_version"]}-r{r["rev_no"]}'
    return out


def review_rows() -> list[dict]:
    return [r for r in FEE_TABLE["rows"] if r["status"] != "CONFIRMED"]


def save_fee_draft(con, data: dict) -> str:
    """개정안 작성: 시행일·확인자·행별 확정 단가(사유 필수). 같은 사람이 승인할 수 없다."""
    eff = (data.get("effective_from") or "").strip()
    if not re.fullmatch(r"\d{4}-\d\d-\d\d", eff):
        raise UserError("시행일(적용 시작일)을 날짜로 적어 주세요.")
    who = (data.get("confirmed_by") or "").strip()
    if not who:
        raise UserError("원문을 대조한 확인자 이름을 적어 주세요.")
    ov = {}
    for rid, o in (data.get("overrides") or {}).items():
        if rid not in FEE_ROWS:
            raise UserError("수수료표에 없는 행입니다.")
        try:
            price = int(str(o.get("price")).replace(",", ""))
        except (TypeError, ValueError):
            raise UserError(f"{FEE_ROWS[rid]['name']} {FEE_ROWS[rid]['spec']}: 단가를 숫자로 적어 주세요.")
        if price <= 0:
            raise UserError("단가는 0원보다 커야 합니다. 모르는 단가는 확정하지 말고 확인 대기로 두세요.")
        reason = (o.get("reason") or "").strip()
        if not reason:
            raise UserError(f"{FEE_ROWS[rid]['name']} {FEE_ROWS[rid]['spec']}: 확정 근거(사유)를 적어 주세요.")
        ov[rid] = {"price": price, "reason": reason[:200]}
    # 이전 승인본의 확정값은 이어받는다(명시적으로 빼지 않는 한)
    prev = one(con, "SELECT overrides_json FROM fee_revision WHERE status='APPROVED' AND base_version=? ORDER BY rev_no DESC LIMIT 1", (FEE_TABLE["version_id"],))
    merged = {**(json.loads(prev["overrides_json"]) if prev else {}), **ov}
    for rid in data.get("remove") or []:
        merged.pop(rid, None)
    with tx(con):
        con.execute("DELETE FROM fee_revision WHERE status='DRAFT'")  # 작성 중인 안은 하나만
        n = (one(con, "SELECT MAX(rev_no) AS n FROM fee_revision")["n"] or 0) + 1
        rid_ = str(uuid.uuid4())
        con.execute("""INSERT INTO fee_revision (id, rev_no, base_version, status, effective_from, confirmed_by, note, overrides_json, created_by, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (rid_, n, FEE_TABLE["version_id"], "DRAFT", eff, who, (data.get("note") or "")[:500],
                                              json.dumps(merged, ensure_ascii=False), ACTOR.get(), now()))
        audit(con, "fee", rid_, "DRAFT", {"rev_no": n, "effective_from": eff, "rows": len(merged)})
    return rid_


def approve_fee_revision(con, rev_id: str, approve: bool = True, reason: str = ""):
    r = one(con, "SELECT * FROM fee_revision WHERE id=?", (rev_id,))
    if not r or r["status"] != "DRAFT":
        raise UserError("승인할 개정안이 없습니다.")
    if r["created_by"] == ACTOR.get():
        raise UserError("작성한 사람은 승인할 수 없습니다. 다른 담당자가 원문과 대조해 승인해 주세요.")
    if not approve and not reason.strip():
        raise UserError("반려 사유를 적어 주세요.")
    with tx(con):
        if approve:
            con.execute("UPDATE fee_revision SET status='APPROVED', approved_by=?, approved_at=? WHERE id=?", (ACTOR.get(), now(), rev_id))
        else:
            con.execute("UPDATE fee_revision SET status='REJECTED', approved_by=?, approved_at=?, reject_reason=? WHERE id=?", (ACTOR.get(), now(), reason.strip()[:200], rev_id))
        audit(con, "fee", rev_id, "APPROVE" if approve else "REJECT", {"rev_no": r["rev_no"]})


# ------------------------------------------------------------------ 로컬 주소 → 좌표
def import_addr_points(con, raw: bytes, region: str = "", columns: dict | None = None, preview: bool = False) -> dict:
    from dgpc import geo
    try:
        res = geo.read_points(raw, columns, region)
    except ValueError as e:
        raise UserError(str(e))
    pts = res["points"]
    if not pts:
        raise UserError("가져올 주소 좌표가 없습니다. 지역 이름(시군구) 거르기나 파일 형식을 확인하세요.")
    info = {"columns": {k: v for k, v in res["columns"].items()}, "delimiter": res["delimiter"], "count": len(pts),
            "sample": [{"sigungu": p[0], "road": p[1], "main": p[2], "sub": p[3], "lat": p[4], "lon": p[5], "dong_h": p[6], "dong_b": p[7], "bname": p[8],
                        "jibun": (f"{p[9]}" + (f"-{p[10]}" if p[10] else "")) if p[9] else ""} for p in pts[:5]],
            "has_dong": any(p[6] or p[7] for p in pts[:200]), "has_bname": any(p[8] for p in pts[:2000]), "has_jibun": any(p[9] for p in pts[:2000])}
    if preview:
        return info
    with tx(con):
        # 같은 주소가 이미 있으면 좌표는 그대로 두고, 비어 있던 행정동·법정동만 채운다
        con.executemany("""INSERT INTO addr_point (sigungu, road, main, sub, lat, lon, dong_h, dong_b, bname, jibun_main, jibun_sub, bname_cho)
            VALUES (?,?,?,?,?,?,NULLIF(?,''),NULLIF(?,''),NULLIF(?,''),?,?,NULLIF(?,''))
            ON CONFLICT(road, main, sub, sigungu) DO UPDATE SET dong_h=COALESCE(addr_point.dong_h, excluded.dong_h), dong_b=COALESCE(addr_point.dong_b, excluded.dong_b),
            bname=COALESCE(addr_point.bname, excluded.bname), bname_cho=COALESCE(addr_point.bname_cho, excluded.bname_cho),
            jibun_main=COALESCE(addr_point.jibun_main, excluded.jibun_main), jibun_sub=COALESCE(addr_point.jibun_sub, excluded.jibun_sub)""",
                        [(*p, chosung_of(re.sub(r"\s+", "", p[8] or ""))) for p in pts])
        audit(con, "address", "import", "IMPORT", {"count": len(pts), "region": region})
    info["total"] = one(con, "SELECT COUNT(*) AS n FROM addr_point")["n"]
    return info


def addr_stats(con) -> dict:
    return {"count": one(con, "SELECT COUNT(*) AS n FROM addr_point")["n"],
            "sigungu": [r["sigungu"] for r in rows(con, "SELECT DISTINCT sigungu FROM addr_point ORDER BY sigungu LIMIT 20")]}


def geocode(con, address: str) -> dict | None:
    """주소 문자열 → 건물 출입구 좌표. 외부로 아무것도 보내지 않는다."""
    from dgpc import geo
    pa = geo.parse_address(address)
    if not pa:
        return None
    cands = rows(con, "SELECT * FROM addr_point WHERE road=? AND main=? AND sub=?", (pa["road"], pa["main"], pa["sub"]))
    exact = True
    if not cands and pa["sub"]:
        cands, exact = rows(con, "SELECT * FROM addr_point WHERE road=? AND main=? ORDER BY sub", (pa["road"], pa["main"])), False
    if not cands:
        return None
    if len(cands) > 1 and pa["sigungu_hint"]:
        narrowed = [c for c in cands if pa["sigungu_hint"] in (c["sigungu"] or "")]
        cands = narrowed or cands
    if len({c["sigungu"] for c in cands}) > 1:
        return {"ambiguous": True, "options": [c["sigungu"] for c in cands]}
    c = cands[0]
    label = f'{c["road"]} {c["main"]}' + (f'-{c["sub"]}' if c["sub"] else "")
    return {"lat": c["lat"], "lon": c["lon"], "label": label, "exact": exact, "sigungu": c["sigungu"], "dong_h": c.get("dong_h") or "", "dong_b": c.get("dong_b") or ""}


def _addr_std(c: dict) -> str:
    """주소 자료 한 줄 → 표준 문자열 '시군구 도로명 본번-부번 (법정동, 건물명)'."""
    base = f'{c["sigungu"] + " " if c["sigungu"] else ""}{c["road"]} {c["main"]}' + (f'-{c["sub"]}' if c["sub"] else "")
    ref = ", ".join(x for x in (c.get("dong_b") or "", c.get("bname") or "") if x)
    return base + (f" ({ref})" if ref else "")


def addr_search(con, q: str, limit: int = 12) -> list[dict]:
    """주소 찾기 제안: 도로명(도로명+번호), 지번(법정동+번지), 건물명·아파트명(초성 가능). 이 PC의 주소 자료만 쓴다."""
    from dgpc import geo
    q = (q or "").strip()
    if len(q) < 2:
        return []
    found: list[tuple[str, dict]] = []
    seen = set()
    def add(kind, cs):
        for c in cs:
            k = (c["road"], c["main"], c["sub"], c["sigungu"])
            if k not in seen and len(found) < limit:
                seen.add(k); found.append((kind, c))
    pa = geo.parse_address(q)
    if pa:
        add("도로명", rows(con, "SELECT * FROM addr_point WHERE road=? AND main=? ORDER BY (sub=?) DESC, sub LIMIT ?", (pa["road"], pa["main"], pa["sub"], limit)))
    m = re.search(r"([가-힣0-9]+?(?:동|가|리))\s*(산\s*)?(\d+)(?:\s*-\s*(\d+))?\s*$", q)
    if m and not pa:
        args = [f"%{m.group(1)}%", int(m.group(3))]
        sql = "SELECT * FROM addr_point WHERE dong_b LIKE ? AND jibun_main=?"
        if m.group(4):
            sql += " AND jibun_sub=?"; args.append(int(m.group(4)))
        add("지번", rows(con, sql + " ORDER BY jibun_sub LIMIT ?", args + [limit]))
    qn = re.sub(r"\s+", "", q)
    if is_chosung(qn):
        add("건물명", rows(con, "SELECT * FROM addr_point WHERE bname_cho LIKE ? ORDER BY length(bname), bname LIMIT ?", (f"%{qn}%", limit)))
    else:
        add("건물명", rows(con, "SELECT * FROM addr_point WHERE REPLACE(bname,' ','') LIKE ? ORDER BY length(bname), bname LIMIT ?", (f"%{qn}%", limit)))
        if re.fullmatch(r"[가-힣A-Za-z0-9·.]+(대로|로|길)", qn):
            add("도로명", rows(con, "SELECT * FROM addr_point WHERE road=? ORDER BY main, sub LIMIT ?", (qn, limit)))
    area = _area()
    out = []
    for kind, c in found:
        addr = _addr_std(c)
        dong = c.get("dong_h") if c.get("dong_h") and area.get_dong(con, c["dong_h"]) else area.detect_dong(con, addr).get("dong")
        out.append({"kind": kind, "address": addr, "label": addr + (f' · 지번 {c["dong_b"] or ""} {c["jibun_main"]}{"-" + str(c["jibun_sub"]) if c["jibun_sub"] else ""}' if kind == "지번" else ""),
                    "lat": c["lat"], "lon": c["lon"], "dong": dong, "bname": c.get("bname") or ""})
    return out


def geocode_missing(con, date: str = "") -> dict:
    """위치 없는 접수를 주소 자료로 한꺼번에 찾는다(찾은 것은 출처 '주소 자료'로 표시, 지도에서 확인 필요)."""
    sql = "SELECT id, address FROM request WHERE lat IS NULL AND status NOT IN ('DONE','CANCELLED')" + (" AND planned_date=?" if date else "")
    found, missing = 0, 0
    for r in rows(con, sql, (date,) if date else ()):
        g = geocode(con, r["address"])
        if g and not g.get("ambiguous"):
            set_location(con, r["id"], g["lat"], g["lon"], source="ADDR")
            found += 1
        else:
            missing += 1
    return {"found": found, "missing": missing}


# ------------------------------------------------------------------ 통계·보고서 (개인정보 없이 집계)
def _kst_date(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(KST).date().isoformat()
    except ValueError:
        return iso[:10]


STATS_BASIS = {"planned": "수거 예정일", "received": "접수일", "completed": "완료일"}


def _basis_date(r: dict, basis: str) -> str | None:
    if basis == "received":
        return _kst_date(r["received_at"])
    if basis == "completed":
        return _kst_date(r.get("completed_at")) if r["status"] in FINAL else (_kst_date(r.get("cancelled_at")) if r["status"] == "CANCELLED" else None)
    return r["planned_date"]


def stats(con, start: str, end: str, basis: str = "planned") -> dict:
    """기간 집계(기준: 수거 예정일·접수일·완료일). 이름·연락처·주소는 쓰지 않는다."""
    for d in (start, end):
        if not re.fullmatch(r"\d{4}-\d\d-\d\d", d or ""):
            raise UserError("기간을 날짜로 골라 주세요.")
    if start > end:
        raise UserError("시작일이 끝일보다 늦습니다.")
    if basis not in STATS_BASIS:
        raise UserError("집계 기준을 골라 주세요.")
    fin = _fin()
    allr = rows(con, "SELECT * FROM request")
    reqs = []
    for r in allr:
        bd = _basis_date(r, basis)
        if bd and start <= bd <= end:
            r["_bd"] = bd
            reqs.append(r)
    by_day: dict[str, dict] = {}
    def day(d):
        return by_day.setdefault(d, {"date": d, "received": 0, "planned": 0, "done": 0, "partial": 0, "not_collected": 0, "cancelled": 0,
                                     "calculated": 0, "levied": 0, "paid": 0, "unload_m3": 0.0, "unload_kg": 0.0})
    for r in allr:
        d = _kst_date(r["received_at"])
        if d and start <= d <= end:
            day(d)["received"] += 1
    totals = {"requests": len(reqs), "by_status": {}, "calculated": 0, "reduction": 0, "levied": 0, "paid": 0, "amount_pending": 0, "unpaid": 0,
              "field_added_lines": 0, "lead_days_avg": None, "reduction_count": 0}
    items: dict[str, dict] = {}
    cats: dict[str, dict] = {}
    reds: dict[str, dict] = {}
    months: dict[str, dict] = {}
    leads = []
    crews: dict[str, dict] = {}
    dongs: dict[str, dict] = {}
    channels: dict[str, dict] = {}
    for r in reqs:
        dd = day(r["_bd"])
        mm = months.setdefault(r["_bd"][:7], {"month": r["_bd"][:7], "count": 0, "done": 0, "revisit": 0, "cancelled": 0, "calculated": 0, "reduction": 0, "levied": 0, "paid": 0})
        mm["count"] += 1
        dd["planned"] += 1
        dg = dongs.setdefault(r["dong"] or "", {"dong": r["dong"] or "", "planned": 0, "done": 0, "revisit": 0, "cancelled": 0, "levied": 0, "paid": 0})
        ch = channels.setdefault(r["channel"] or "전화", {"channel": r["channel"] or "전화", "planned": 0, "done": 0, "cancelled": 0})
        dg["planned"] += 1; ch["planned"] += 1
        dg["done"] += r["status"] == "DONE"; ch["done"] += r["status"] == "DONE"; mm["done"] += r["status"] == "DONE"
        rv = r["status"] in ("PARTIAL", "NOT_COLLECTED", "ON_HOLD")
        dg["revisit"] += rv; mm["revisit"] += rv
        dg["cancelled"] += r["status"] == "CANCELLED"; ch["cancelled"] += r["status"] == "CANCELLED"; mm["cancelled"] += r["status"] == "CANCELLED"
        totals["by_status"][r["status"]] = totals["by_status"].get(r["status"], 0) + 1
        key = {"DONE": "done", "PARTIAL": "partial", "NOT_COLLECTED": "not_collected", "CANCELLED": "cancelled"}.get(r["status"])
        if key:
            dd[key] += 1
        if r["status"] == "CANCELLED":
            continue
        st = derive(con, r)
        if r["status"] in ("DONE", "PARTIAL", "NOT_COLLECTED", "ON_HOLD"):
            ps = payment_status(con, r["id"])
            if ps["calculated"] is None:
                totals["amount_pending"] += 1
            else:
                totals["calculated"] += ps["calculated"]; dd["calculated"] += ps["calculated"]; mm["calculated"] += ps["calculated"]
                totals["levied"] += ps["levied"]; dd["levied"] += ps["levied"]; mm["levied"] += ps["levied"]; dg["levied"] += ps["levied"]
                if ps["reduction_rate"]:
                    totals["reduction"] += ps["reduction"]; mm["reduction"] += ps["reduction"]
                    rd = reds.setdefault(r["reduction_label"] or r["reduction_code"], {"label": r["reduction_label"] or r["reduction_code"], "rate": ps["reduction_rate"], "count": 0, "amount": 0})
                    rd["count"] += 1; rd["amount"] += ps["reduction"]
            totals["paid"] += ps["paid"]; dd["paid"] += ps["paid"]; mm["paid"] += ps["paid"]; dg["paid"] += ps["paid"]
            if ps["status"] in ("UNPAID", "UNDERPAID"):
                totals["unpaid"] += 1
        if r.get("reduction_code"):
            totals["reduction_count"] += 1
        for l in st["lines"]:
            if l["kind"] == "FIELD":
                totals["field_added_lines"] += 1
            k = f'{l["name"]}|{l["spec"]}'
            it = items.setdefault(k, {"name": l["name"], "spec": l["spec"], "unit_label": l["unit_label"], "requested": 0, "collected": 0})
            it["requested"] += l["qty_confirmed"]; it["collected"] += l["qty_collected"]
            cat = (FEE_ROWS.get(l.get("fee_rate_id") or "") or {}).get("category") or "표에 없는 품목"
            c_ = cats.setdefault(cat, {"category": cat, "lines": 0, "requested": 0, "collected": 0, "amount": 0})
            c_["lines"] += 1; c_["requested"] += l["qty_confirmed"]; c_["collected"] += l["qty_collected"]
            if l["price"] is not None:
                c_["amount"] += l["price"] * l["qty_confirmed"]
        rd_ = _kst_date(r["received_at"])
        if rd_ and r["status"] == "DONE" and r["planned_date"]:
            leads.append((dt.date.fromisoformat(r["planned_date"]) - dt.date.fromisoformat(rd_)).days)
        a = one(con, """SELECT c.name FROM assignment_stop s JOIN assignment_batch b ON b.id=s.batch_id JOIN crew c ON c.id=b.crew_id
            WHERE s.request_id=? ORDER BY b.created_at DESC LIMIT 1""", (r["id"],))
        if a:
            c = crews.setdefault(a["name"], {"crew": a["name"], "assigned": 0, "done": 0, "partial_or_not": 0})
            c["assigned"] += 1
            c["done"] += r["status"] == "DONE"
            c["partial_or_not"] += r["status"] in ("PARTIAL", "NOT_COLLECTED", "ON_HOLD")
    for u in rows(con, "SELECT * FROM unload_log WHERE work_date BETWEEN ? AND ? AND void_reason IS NULL", (start, end)):
        dd = day(u["work_date"])
        dd["unload_m3"] = round(dd["unload_m3"] + (u["volume_m3"] or 0), 2)
        dd["unload_kg"] = round(dd["unload_kg"] + (u["weight_kg"] or 0), 1)
    if leads:
        totals["lead_days_avg"] = round(sum(leads) / len(leads), 1)
    days = [by_day[k] for k in sorted(by_day)]
    totals["unload_m3"] = round(sum(d["unload_m3"] for d in days), 2)
    totals["unload_kg"] = round(sum(d["unload_kg"] for d in days), 1)
    totals["received"] = sum(d["received"] for d in days)
    rev = fin.revenue(con, start, end, "month")
    return {"start": start, "end": end, "basis": basis, "basis_label": STATS_BASIS[basis], "totals": totals, "days": days,
            "items": sorted(items.values(), key=lambda x: (-x["requested"], x["name"])), "crews": sorted(crews.values(), key=lambda c: c["crew"]),
            "dongs": sorted(dongs.values(), key=lambda x: (x["dong"] == "", x["dong"])),
            "channels": sorted(channels.values(), key=lambda x: CHANNELS.index(x["channel"]) if x["channel"] in CHANNELS else 99),
            "categories": sorted(cats.values(), key=lambda x: -x["requested"]), "reductions": sorted(reds.values(), key=lambda x: -x["amount"]),
            "months": [months[k] for k in sorted(months)], "revenue": {"methods": rev["methods"], "totals": rev["totals"]}}


def stats_csv(con, start: str, end: str, kind: str = "requests", basis: str = "planned") -> bytes:
    """엑셀에서 열리는 CSV(UTF-8 BOM). 이름·연락처·주소·메모는 넣지 않는다."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    if kind == "days":
        s = stats(con, start, end, basis)
        w.writerow(["날짜", "접수", "수거 예정", "수거 완료", "일부 수거", "미수거", "취소", "계산 금액(원)", "부과 금액(원)", "납부 기록(원)", "하차 부피(m3)", "하차 중량(kg)"])
        for d in s["days"]:
            w.writerow([d["date"], d["received"], d["planned"], d["done"], d["partial"], d["not_collected"], d["cancelled"], d["calculated"], d["levied"], d["paid"], d["unload_m3"], d["unload_kg"]])
    elif kind in ("dongs", "items", "categories", "months", "reductions", "channels"):
        s = stats(con, start, end, basis)
        spec = {"dongs": (["행정동", "건수", "수거 완료", "재방문·보류", "취소", "부과 금액(원)", "납부 기록(원)"], lambda x: [x["dong"] or "(동 미지정)", x["planned"], x["done"], x["revisit"], x["cancelled"], x["levied"], x["paid"]]),
                "items": (["품목", "규격", "단위", "확인 수량", "수거 수량"], lambda x: [x["name"], x["spec"], x["unit_label"], x["requested"], x["collected"]]),
                "categories": (["품목 분류", "품목 행", "확인 수량", "수거 수량", "계산 금액(원)"], lambda x: [x["category"], x["lines"], x["requested"], x["collected"], x["amount"]]),
                "months": (["월", "건수", "수거 완료", "재방문·보류", "취소", "계산 금액(원)", "감면액(원)", "부과 금액(원)", "납부 기록(원)"],
                           lambda x: [x["month"], x["count"], x["done"], x["revisit"], x["cancelled"], x["calculated"], x["reduction"], x["levied"], x["paid"]]),
                "reductions": (["감면 사유", "감면율(%)", "건수", "감면액(원)"], lambda x: [x["label"], x["rate"], x["count"], x["amount"]]),
                "channels": (["접수 경로", "건수", "수거 완료", "취소"], lambda x: [x["channel"], x["planned"], x["done"], x["cancelled"]])}[kind]
        w.writerow(spec[0])
        for x in s[kind]:
            w.writerow(spec[1](x))
    else:
        stats(con, start, end, basis)  # 기간 검사
        w.writerow(EXPORT_COLS)
        for r in sorted(rows(con, "SELECT * FROM request ORDER BY planned_date, receipt_no"), key=lambda r: (_basis_date(r, basis) or "", r["receipt_no"])):
            bd = _basis_date(r, basis)
            if bd and start <= bd <= end:
                w.writerow(_export_row(con, r))
    audit(con, "report", kind, "EXPORT", {"start": start, "end": end, "basis": basis})
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def dispatch_sheet(con, batch_id: str) -> dict:
    """인쇄용 일일 배차표(종이 예비본). 이름·연락처·주소가 들어가므로 업무 후 파쇄."""
    payload, b = build_daylist(con, batch_id)
    with tx(con):
        audit(con, "batch", batch_id, "PRINT", {"stops": len(payload["stops"])})
        from dgpc import privacy as _pv
        _pv.record_access(con, "PRINT", "", len(payload["stops"]), f"배차표 {b['work_date']} {b['crew_name']}")
    for st in payload["stops"]:
        st["status"] = one(con, "SELECT status FROM request WHERE id=?", (st["request_id"],))["status"]
    return {"work_date": payload["work_date"], "crew": payload["crew"], "assign_ver": payload["assign_ver"], "fee_version": payload["fee_table"]["version_id"],
            "stops": payload["stops"], "printed_at": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")}


# ------------------------------------------------------------------ 휴대폰 단독 방식 자료 → PC로 옮기기
def open_solo_backup(raw: bytes, password: str) -> dict:
    """휴대폰 단독 방식 백업(.dgb: 'DGB1'|salt16|iv12|AES-GCM, PBKDF2-SHA256 60만 회, deflate-raw JSON)을 연다."""
    import zlib
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(raw) < 48 or raw[:4] != b"DGB1":
        raise UserError("휴대폰 백업 파일(.dgb)이 아닙니다.")
    key = hashlib.pbkdf2_hmac("sha256", (password or "").encode(), raw[4:20], 600000, 32)
    try:
        plain = AESGCM(key).decrypt(raw[20:32], raw[32:], b"DGB1")
    except Exception:
        raise UserError("백업 비밀번호가 틀렸거나 파일이 손상되었습니다.")
    obj = json.loads(zlib.decompress(plain, -15).decode())
    if obj.get("format") != "dg.backup/1":
        raise UserError("지원하지 않는 백업 형식입니다.")
    return obj


def import_solo(con, raw: bytes, password: str, crew_id: str | None = None, preview: bool = False) -> dict:
    """단독 방식 휴대폰의 접수·현장 기록·사진을 PC 원본으로 옮긴다. 같은 접수(같은 ID)는 다시 들어오지 않는다."""
    obj = open_solo_backup(raw, password)
    state = obj.get("state") or {}
    if state.get("mode") != "SOLO":
        raise UserError("휴대폰 단독 방식의 백업이 아닙니다.")
    reqs = state.get("requests") or []
    events = state.get("events") or []
    photos = obj.get("photos") or {}
    new = [r for r in reqs if not one(con, "SELECT 1 AS x FROM request WHERE id=?", (r["request_id"],))]
    info = {"requests": len(reqs), "new": len(new), "already": len(reqs) - len(new), "events": len(events),
            "photos": len(photos), "unloads": sum(1 for e in events if e.get("kind") == "UNLOAD"), "created_at": obj.get("created_at")}
    if preview:
        return info
    if crew_id and not one(con, "SELECT 1 AS x FROM crew WHERE id=?", (crew_id,)):
        raise UserError("없는 수거팀입니다.")
    sha = hashlib.sha256(raw).hexdigest()
    if one(con, "SELECT 1 AS x FROM result_import WHERE package_sha256=?", (sha,)):
        return {**info, "duplicate_file": True}
    imp = str(uuid.uuid4())
    newids = {r["request_id"] for r in new}
    line_map: dict[tuple[str, str], str] = {}
    counts = {"requests": 0, "events": 0, "photos_recovered": 0, "unloads_applied": 0, "skipped": 0}
    with tx(con):
        con.execute("INSERT INTO result_import VALUES (?,?,?,?,?,NULL,NULL)", (imp, "USB", "SOLO", sha, now()))
        for r in new:
            # 휴대폰에서 보관 기간이 지나 개인정보를 지운 접수(pii_purged): 이름·연락처·주소가 비어 있어도 가명으로 옮긴다
            purged = bool(r.get("pii_purged")) or not ((r.get("name") or "").strip() and norm_phone(r.get("phone")) and (r.get("address") or "").strip())
            rr = {**r, "name": "(정리됨)", "phone": "00000000000", "address": "(정리됨)", "spot_note": "", "memo": "", "lat": None, "lon": None} if purged else r
            d = validate({**rr, "items": r.get("items") or [{"name": "(품목 없음)", "qty": 1}]})  # 품목 없는 취소 건도 옮긴다
            dong_s = (r.get("dong") or "").strip()[:20] or None
            rec = r["receipt_no"]
            if one(con, "SELECT 1 AS x FROM request WHERE receipt_no=?", (rec,)):
                rec = re.sub(r"-(\d+)$", r"-S\1", rec)  # PC 접수번호와 겹치면 단독 표시(S)
            from dgpc import store as _store
            dcode = (one(con, "SELECT code FROM dong WHERE name=?", (dong_s,)) or {}).get("code") if dong_s else None
            con.execute("""INSERT INTO request (id, receipt_no, req_ver, name, phone, address, spot_note, memo, lat, lon, geo_source,
                planned_date, appt_start, appt_end, must_today, urgent, priority, status, cancel_reason, received_at, updated_at, dong, levy_no, pii_purged_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,?,?,?,?,?,?,?)""",
                        (r["request_id"], rec, int(r.get("req_ver") or 1), d["name"], d["phone"], d["address"], d["spot_note"], d["memo"],
                         rr.get("lat"), rr.get("lon"), ({"ADDR": "ADDR", "GPS": "FIELD"}.get(r.get("geo_source"), "MANUAL")) if rr.get("lat") is not None else None, d["planned_date"] or None, None, None,
                         "CANCELLED" if r.get("status") == "CANCELLED" else "RECEIVED", None if purged else r.get("cancel_reason"),
                         r.get("received_at") or now(), r.get("updated_at") or now(), dong_s,
                         _store.next_levy_no(con, _store.levy_year(r.get("received_at")), dcode), (r.get("pii_purged") if isinstance(r.get("pii_purged"), str) else now()) if purged else None))
            for it in r.get("items") or []:
                # 품목 행 ID는 PC 전체에서 하나여야 한다. 휴대폰 단독 자료에서 이미 쓰인 ID(다른 접수에 같은 행 ID가 들어간 경우 등)는
                # 새 ID로 바꾸고, 그 접수의 현장 기록·사진이 가리키는 행 ID도 함께 바꾼다.
                lid = it["line_id"]
                if one(con, "SELECT 1 AS x FROM request_item WHERE line_id=?", (lid,)):
                    line_map[(r["request_id"], lid)] = lid = str(uuid.uuid4())
                    counts["lines_renumbered"] = counts.get("lines_renumbered", 0) + 1
                con.execute("""INSERT INTO request_item (line_id, request_id, fee_rate_id, fee_version_id, name, spec, unit, unit_label, qty,
                    unit_price, amount, price_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (lid, r["request_id"], it.get("fee_rate_id"), it.get("fee_version_id") or (FEE_TABLE["version_id"] if it.get("fee_rate_id") else None),
                             it["name"], it.get("spec") or "", it.get("unit") or "EA", it.get("unit_label") or "개", int(it["qty"]),
                             it.get("unit_price"), it.get("amount"), it.get("price_status") or "PENDING"))
            for v in r.get("versions") or []:
                snap = v.get("snapshot") or {}
                if purged and isinstance(snap, dict):
                    snap = {k: x for k, x in snap.items() if k not in ("name", "phone", "address", "spot_note", "memo", "lat", "lon")}
                con.execute("INSERT OR IGNORE INTO request_version VALUES (?,?,?,?,?)",
                            (r["request_id"], int(v.get("req_ver") or 1), json.dumps(snap, ensure_ascii=False), v.get("reason") or "휴대폰 단독 수정", v.get("at") or now()))
            counts["requests"] += 1
        for e in events:
            if one(con, "SELECT 1 AS x FROM result_event WHERE event_id=?", (e["event_id"],)):
                counts["skipped"] += 1
                continue
            if e.get("kind") == "UNLOAD":
                b = e.get("body") or {}
                ok_ = bool(crew_id) and (b.get("volume_m3") or b.get("weight_kg"))
                con.execute("""INSERT INTO result_event (event_id, import_id, device_id, device_seq, request_id, assign_ver, base_req_ver, kind,
                    body_json, apply_status, conflict_reason, device_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (e["event_id"], imp, "SOLO", e.get("seq") or 0, "", 0, None, "UNLOAD", json.dumps(b, ensure_ascii=False),
                             "APPLIED" if ok_ else "REJECTED", None if ok_ else "수거팀을 고르지 않아 하차 기록을 붙일 곳이 없음", e.get("ts")))
                if ok_:
                    con.execute("""INSERT OR IGNORE INTO unload_log (id, crew_id, work_date, at_time, place, volume_m3, weight_kg, note, recorded_at)
                        VALUES (?,?,?,?,?,?,?,?,?)""", (e["event_id"], crew_id, b.get("work_date") or today_kst(), (b.get("at_time") or "")[:5], (b.get("place") or "반입지")[:60],
                                                         b.get("volume_m3"), b.get("weight_kg"), "휴대폰 단독 기록", now()))
                    counts["unloads_applied"] += 1
                continue
            if e.get("request_id") not in newids:  # 이미 PC에 있던 접수의 기록은 덮어쓰지 않음
                counts["skipped"] += 1
                continue
            if isinstance(e.get("body"), dict) and (e["request_id"], e["body"].get("line_id")) in line_map:
                e = {**e, "body": {**e["body"], "line_id": line_map[(e["request_id"], e["body"]["line_id"])]}}
            con.execute("""INSERT INTO result_event (event_id, import_id, device_id, device_seq, request_id, assign_ver, base_req_ver, kind,
                body_json, apply_status, conflict_reason, device_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (e["event_id"], imp, "SOLO", e.get("seq") or 0, e["request_id"], 0, e.get("base_req_ver"), e["kind"],
                         json.dumps(e.get("body") or {}, ensure_ascii=False), "APPLIED", None, e.get("ts")))
            counts["events"] += 1
            if e["kind"] == "PHOTO_META":
                b = e.get("body") or {}
                data = base64.b64decode(photos[b["photo_id"]]) if b.get("photo_id") in photos else None
                good = data is not None and hashlib.sha256(data).hexdigest() == b.get("sha256")
                con.execute("INSERT OR IGNORE INTO photo (photo_id, request_id, phase, sha256, bytes, device_id, data, recovery_status, recovered_at, line_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (b.get("photo_id"), e["request_id"], b.get("phase"), b.get("sha256"), b.get("bytes"), "SOLO", data if good else None,
                             "RECOVERED" if good else "EXPECTED", now() if good else None, b.get("line_id")))
                counts["photos_recovered"] += good
            elif e["kind"] == "REVISIT_APPT":
                _apply_revisit(con, e["request_id"], e.get("body") or {})
        for rid in newids:
            _refresh_status(con, rid)
        con.execute("UPDATE result_import SET summary_json=? WHERE id=?", (json.dumps({"source": "SOLO", **counts}), imp))
        audit(con, "import", imp, "SOLO", counts)
    return {**info, **counts}



# ------------------------------------------------------------------ 유사 품목 적용 (표에 없는 품목의 단가를 근거와 함께 확정)
def link_similar(con, rid: str, line_id: str, fee_rate_id: str, reason: str, confirmed_by: str) -> str:
    r = one(con, "SELECT * FROM request WHERE id=?", (rid,))
    if not r:
        raise UserError("접수를 찾을 수 없습니다.")
    st = derive(con, r)
    line = next((l for l in st["lines"] if l["line_id"] == line_id), None)
    if not line:
        raise UserError("품목 행을 찾을 수 없습니다.")
    if line["price"] is not None:
        raise UserError("이미 단가가 정해진 품목입니다.")
    rows_now = fee_rows(con)
    row = rows_now.get(fee_rate_id)
    if not row or row["status"] != "CONFIRMED":
        raise UserError("단가가 확정된 수수료표 행만 적용할 수 있습니다.")
    if row["unit_label"] != line["unit_label"]:
        raise UserError(f"부과단위가 다릅니다({line['unit_label']} ↔ {row['unit_label']}). 단위가 같은 행을 고르거나 확인 대기로 두세요.")
    if not (reason or "").strip():
        raise UserError("적용 근거를 적어 주세요 (예: 같은 재질·크기, 담당 부서 확인).")
    if not (confirmed_by or "").strip():
        raise UserError("확인자를 적어 주세요.")
    lid = str(uuid.uuid4())
    with tx(con):
        con.execute("INSERT INTO item_link (id, request_id, line_id, fee_rate_id, fee_version_id, unit_price, reason, confirmed_by, actor, at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (lid, rid, line_id, fee_rate_id, fee_state(con)["version_id"], int(row["price"]), reason.strip()[:200], confirmed_by.strip()[:40], ACTOR.get(), now()))
        audit(con, "request", rid, "LINK_SIMILAR", {"line": line_id[:8], "fee_rate_id": fee_rate_id, "price": row["price"], "confirmed_by": confirmed_by.strip()[:40],
                                                    "reason": reason.strip()[:200], **_after_close(con, rid)})
    return lid


def unlink_similar(con, link_id: str, reason: str):
    lk = one(con, "SELECT * FROM item_link WHERE id=? AND void_reason IS NULL", (link_id,))
    if not lk:
        raise UserError("해제할 적용 기록이 없습니다.")
    if not (reason or "").strip():
        raise UserError("해제 사유를 적어 주세요.")
    with tx(con):
        con.execute("UPDATE item_link SET void_reason=?, voided_at=? WHERE id=?", (reason.strip()[:200], now(), link_id))
        audit(con, "request", lk["request_id"], "UNLINK_SIMILAR", {"line": lk["line_id"][:8], "reason": reason.strip()[:200]})



# ------------------------------------------------------------------ 수거팀 계정의 접근 범위 (자기 팀 배정만)
def set_user_crew(con, uid: str, crew_id: str | None):
    u = one(con, "SELECT role FROM app_user WHERE id=?", (uid,))
    if not u:
        raise UserError("사용자를 찾을 수 없습니다.")
    if u["role"] != "FIELD":
        raise UserError("수거팀 계정만 팀을 연결합니다.")
    if crew_id and not one(con, "SELECT 1 AS x FROM crew WHERE id=?", (crew_id,)):
        raise UserError("없는 수거팀입니다.")
    with tx(con):
        con.execute("UPDATE app_user SET crew_id=? WHERE id=?", (crew_id or None, uid))
        audit(con, "user", uid, "CREW", {"crew_id": crew_id})


def crew_request_ids(con, crew_id: str) -> set[str]:
    """이 팀에 배정된 적이 있는 접수 (현재 배정 또는 지난 배정)."""
    return {r["request_id"] for r in rows(con, """SELECT DISTINCT s.request_id FROM assignment_stop s JOIN assignment_batch b ON b.id=s.batch_id
        WHERE b.crew_id=?""", (crew_id,))}


def request_in_crew(con, rid: str, crew_id: str) -> bool:
    return bool(one(con, """SELECT 1 AS x FROM assignment_stop s JOIN assignment_batch b ON b.id=s.batch_id WHERE s.request_id=? AND b.crew_id=?""", (rid, crew_id)))


def photo_request(con, photo_id: str) -> str | None:
    r = one(con, "SELECT request_id FROM photo WHERE photo_id=?", (photo_id,))
    return r["request_id"] if r else None


def device_crew(con, data: bytes) -> str | None:
    try:
        head = P.Header.unpack(data)
    except P.ProtocolError:
        return None
    r = one(con, "SELECT id FROM crew WHERE device_id=?", (str(uuid.UUID(bytes=head.device_id)),))
    return r["id"] if r else None



# ------------------------------------------------------------------ 관할 확인 (동별·구별 관할 불일치 경고, 막지는 않음)
def jurisdiction_warnings(con, address: str) -> list[str]:
    area = (get_setting(con, "jurisdiction", "동구") or "").strip()
    if not area:
        return []
    a = re.sub(r"\(.*?\)", " ", address or "")
    head = re.split(r"[가-힣A-Za-z0-9·.]+(?:대로|로|길)\s*\d", a)[0]  # 도로명 앞부분(시·구·동)만 본다
    units = re.findall(r"([가-힣]+(?:구|군))(?=\s|$)", head)
    if units and area not in units:
        return [f"주소의 '{units[0]}'이(가) 관할({area})과 다릅니다. 관할 구역을 확인하세요."]
    return []
