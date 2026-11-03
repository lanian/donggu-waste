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


def audit(con, entity, entity_id, action, detail=None):
    con.execute("INSERT INTO audit_log (entity, entity_id, action, detail, at, actor) VALUES (?,?,?,?,?,?)",
                (entity, entity_id, action, json.dumps(detail, ensure_ascii=False) if detail is not None else None, now(), ACTOR.get()))


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
        "service_min_base": get_setting(con, "service_min_base", 5),
        "service_min_per_item": get_setting(con, "service_min_per_item", 3),
        "photo_retention_days": get_setting(con, "photo_retention_days", 90),
        "unload_site": get_setting(con, "unload_site", None),
        "end_site": get_setting(con, "end_site", None),
        "jurisdiction": get_setting(con, "jurisdiction", "동구"),
        "unload_min": get_setting(con, "unload_min", 20),
        "max_unloads": get_setting(con, "max_unloads", 2),
        "backup_dir": get_setting(con, "backup_dir", ""),
        "fee_version": fee_state(con)["version_id"], "fee_label": FEE_TABLE["label"],
        "fee_checked_on": FEE_TABLE["source_checked_on"], "fee_effective_from": fee_state(con)["effective_from"],
    }


def save_settings(con, data: dict):
    with tx(con):
        if "depot" in data:
            d = data["depot"]
            set_setting(con, "depot", {"lat": float(d["lat"]), "lon": float(d["lon"]), "label": str(d.get("label", "차고지"))})
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
    if not data.get("items"):
        raise UserError("버릴 물건을 하나 이상 추가해 주세요.")
    return d


def find_duplicates(con, phone: str, address: str, exclude_id: str | None = None) -> list[dict]:
    p, a = norm_phone(phone), norm_addr(address)
    out = []
    for r in rows(con, "SELECT id, receipt_no, name, phone, address, planned_date, status FROM request WHERE status NOT IN ('DONE','CANCELLED')"):
        if r["id"] != exclude_id and (r["phone"] == p or norm_addr(r["address"]) == a):
            out.append(r)
    return out


def create_request(con, data: dict) -> dict:
    d = validate(data)
    items = build_items(data["items"], con)
    rid, ts = str(uuid.uuid4()), now()
    day = today_kst().replace("-", "")
    with tx(con):
        n = con.execute("SELECT COUNT(*) FROM request WHERE receipt_no LIKE ?", (f"DG-{day}-%",)).fetchone()[0] + 1
        receipt = f"DG-{day}-{n:04d}"
        con.execute("""INSERT INTO request (id, receipt_no, req_ver, name, phone, address, spot_note, memo, lat, lon, geo_source,
            planned_date, appt_start, appt_end, must_today, urgent, priority, status, received_at, updated_at)
            VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'RECEIVED',?,?)""",
                    (rid, receipt, d["name"], d["phone"], d["address"], d["spot_note"], d["memo"], d["lat"], d["lon"],
                     (data.get("geo_source") if data.get("geo_source") in ("MANUAL", "ADDR") else "MANUAL") if d["lat"] is not None else None, d["planned_date"] or None, d["appt_start"] or None,
                     d["appt_end"] or None, d["must_today"], d["urgent"], d["priority"], ts, ts))
        for it in items:
            _insert_item(con, rid, it)
        audit(con, "request", rid, "CREATE", {"receipt_no": receipt})
    return {"id": rid, "receipt_no": receipt, "warnings": jurisdiction_warnings(con, d["address"])}


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


def update_request(con, rid: str, data: dict, reason: str) -> dict:
    cur = one(con, "SELECT * FROM request WHERE id=?", (rid,))
    if not cur:
        raise UserError("접수를 찾을 수 없습니다.")
    if cur["status"] == "CANCELLED":
        raise UserError("취소된 접수는 수정할 수 없습니다.")
    if not (reason or "").strip():
        raise UserError("수정 사유를 적어 주세요.")
    d = validate(data)
    items = build_items(data["items"], con)
    locked = locked_lines(con, rid)
    old_items = {i["line_id"]: i for i in rows(con, "SELECT * FROM request_item WHERE request_id=? AND removed=0", (rid,))}
    new_ids = {i["line_id"] for i in items}
    for lid in locked:
        if lid in old_items and (lid not in new_ids or next(i for i in items if i["line_id"] == lid)["qty"] != old_items[lid]["qty"]):
            raise UserError(f"현장 결과가 있는 품목({old_items[lid]['name']})은 바꾸거나 지울 수 없습니다.")
    with tx(con):
        con.execute("INSERT INTO request_version VALUES (?,?,?,?,?)",
                    (rid, cur["req_ver"], json.dumps(_snapshot(con, rid), ensure_ascii=False), reason.strip(), now()))
        con.execute("""UPDATE request SET req_ver=req_ver+1, name=?, phone=?, address=?, spot_note=?, memo=?, lat=?, lon=?,
            geo_source=COALESCE(?, geo_source), planned_date=?, appt_start=?, appt_end=?, must_today=?, urgent=?, priority=?, updated_at=? WHERE id=?""",
                    (d["name"], d["phone"], d["address"], d["spot_note"], d["memo"], d["lat"], d["lon"],
                     (data.get("geo_source") if data.get("geo_source") in ("MANUAL", "ADDR") else "MANUAL") if d["lat"] is not None and (d["lat"], d["lon"]) != (cur["lat"], cur["lon"]) else None,
                     d["planned_date"] or None, d["appt_start"] or None, d["appt_end"] or None, d["must_today"], d["urgent"],
                     d["priority"], now(), rid))
        for lid in old_items:
            if lid not in new_ids:
                con.execute("UPDATE request_item SET removed=1 WHERE line_id=?", (lid,))
        for it in items:
            if it["line_id"] in old_items:
                con.execute("UPDATE request_item SET qty=?, amount=?, unit_price=?, price_status=? WHERE line_id=?",
                            (it["qty"], it["amount"], it["unit_price"], it["price_status"], it["line_id"]))
            else:
                _insert_item(con, rid, it)
        audit(con, "request", rid, "UPDATE", {"reason": reason.strip(), "from_ver": cur["req_ver"]})
    return {"id": rid, "resend_needed": bool(one(con, "SELECT 1 AS x FROM active_assignment WHERE request_id=?", (rid,))),
            "warnings": jurisdiction_warnings(con, d["address"])}


def cancel_request(con, rid: str, reason: str) -> dict:
    cur = one(con, "SELECT status FROM request WHERE id=?", (rid,))
    if not cur:
        raise UserError("접수를 찾을 수 없습니다.")
    if not (reason or "").strip():
        raise UserError("취소 사유를 적어 주세요.")
    act = one(con, "SELECT a.batch_id, b.crew_id FROM active_assignment a JOIN assignment_batch b ON b.id=a.batch_id WHERE a.request_id=?", (rid,))
    sent = act and one(con, "SELECT 1 AS x FROM transfer_bundle WHERE batch_id=?", (act["batch_id"],))
    with tx(con):
        con.execute("UPDATE request SET status='CANCELLED', cancel_reason=?, updated_at=? WHERE id=?", (reason.strip(), now(), rid))
        con.execute("DELETE FROM active_assignment WHERE request_id=?", (rid,))
        audit(con, "request", rid, "CANCEL", {"reason": reason.strip(), "was": cur["status"]})
    return {"field_notice_needed": bool(sent)}


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


def list_requests(con, q: str = "", date: str = "", status: str = "") -> list[dict]:
    sql, args = "SELECT * FROM request WHERE 1=1", []
    cho = [t for t in (q or "").split() if is_chosung(t)]
    q = " ".join(t for t in (q or "").split() if not is_chosung(t))
    if date:
        sql += " AND planned_date=?"; args.append(date)
    if status == "OPEN":
        sql += " AND status NOT IN ('DONE','CANCELLED')"
    elif status:
        sql += " AND status=?"; args.append(status)
    if q:
        qp = norm_phone(q)
        sql += " AND (name LIKE ? OR address LIKE ? OR receipt_no LIKE ?" + (" OR phone LIKE ?" if qp else "") + ")"
        args += [f"%{q}%"] * 3 + ([f"%{qp}%"] if qp else [])
    out = rows(con, sql + " ORDER BY COALESCE(planned_date,'9999'), received_at DESC" + ("" if cho else " LIMIT 500"), args)
    if cho:  # 초성 검색: 이름·주소의 초성 (예: ㄱㅅㅎ → 가상하나)
        out = [r for r in out if all(t in chosung_of(re.sub(r"\s+", "", (r["name"] or "") + "|" + (r["address"] or ""))) for t in cho)][:500]
    for r in out:
        r["items"] = rows(con, "SELECT * FROM request_item WHERE request_id=? AND removed=0", (r["id"],))
        r["state"] = derive(con, r)
        r["warnings"] = jurisdiction_warnings(con, r["address"])
        r["assigned_to"] = (one(con, """SELECT c.name, b.work_date FROM active_assignment a JOIN assignment_batch b ON b.id=a.batch_id
            JOIN crew c ON c.id=b.crew_id WHERE a.request_id=?""", (r["id"],)) or None)
    return out


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
    r["photos"] = rows(con, "SELECT photo_id, phase, recovery_status, purged_at, line_id FROM photo WHERE request_id=?", (rid,))
    r["payment"] = payment_status(con, rid)
    return r


# ------------------------------------------------------------------ 현장 결과로 현재 상태 계산
def _events_for(con, rid):
    return rows(con, """SELECT e.kind, e.body_json FROM result_event e JOIN result_import i ON i.id=e.import_id
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
    for e in _events_for(con, req["id"]):
        b = json.loads(e["body_json"])
        k = e["kind"]
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
            loc = {"lat": b.get("lat"), "lon": b.get("lon")}
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
    return {"lines": lines, "contact": contact, "outcome": outcome, "field_loc": loc, "has_field_records": n > 0, "memos": memos,
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
        con.execute("UPDATE request SET lat=?, lon=?, geo_source='FIELD' WHERE id=?", (st["field_loc"]["lat"], st["field_loc"]["lon"], rid))
    if new != r["status"]:
        con.execute("UPDATE request SET status=?, updated_at=? WHERE id=?", (new, now(), rid))
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
        stops.append({**extra_u, "seq": s["seq"], "request_id": r["id"], "receipt_no": r["receipt_no"], "req_ver": r["req_ver"],
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
            req = one(con, "SELECT status, req_ver FROM request WHERE id=?", (ev["request_id"],))
            reason = None
            if not req:
                reason = "PC에 없는 접수"
            elif req["status"] == "CANCELLED" and ev["kind"] not in ("CONTACT", "DIAL_INTENT", "PHOTO_META"):
                reason = "취소된 접수의 결과"
            elif ev["kind"] in ("ITEM_RESULT", "FIELD_ITEM_ADD", "ATTEMPT"):
                act = one(con, """SELECT b.assign_ver, b.crew_id FROM active_assignment a JOIN assignment_batch b ON b.id=a.batch_id
                    WHERE a.request_id=?""", (ev["request_id"],))
                done_before = req and req["status"] in FINAL
                if act and crew and act["crew_id"] != crew["id"]:
                    reason = "다른 팀에 다시 배정된 접수"
                elif act and ev.get("assign_ver") is not None and ev["assign_ver"] < act["assign_ver"]:
                    reason = f"오래된 목록(갱신본 {ev['assign_ver']} < 현재 {act['assign_ver']})의 결과"
                elif not act and not done_before and not one(con, """SELECT 1 AS x FROM assignment_stop s JOIN assignment_batch b ON b.id=s.batch_id
                        WHERE s.request_id=? AND b.crew_id=?""", (ev["request_id"], crew["id"] if crew else "")):
                    reason = "이 팀에 배정된 적 없는 접수"
            status = "CONFLICT" if reason else "APPLIED"
            note = reason or (f"접수 수정(버전 {ev.get('base_req_ver')}→{req['req_ver']}) 뒤에 기록된 결과" if req and ev.get("base_req_ver") and ev["base_req_ver"] != req["req_ver"] else None)
            counts["conflict" if reason else "applied"] += 1
            con.execute("""INSERT INTO result_event (event_id, import_id, device_id, device_seq, request_id, assign_ver, base_req_ver, kind,
                body_json, apply_status, conflict_reason, device_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (ev["event_id"], imp, dev_id, ev["seq"], ev["request_id"], ev.get("assign_ver") or 0, ev.get("base_req_ver"),
                         ev["kind"], json.dumps(ev.get("body", {}), ensure_ascii=False), status, note, ev.get("device_ts")))
            if ev["kind"] == "PHOTO_META" and not reason:
                b = ev.get("body", {})
                con.execute("INSERT OR IGNORE INTO photo (photo_id, request_id, phase, sha256, bytes, device_id, line_id) VALUES (?,?,?,?,?,?,?)",
                            (b.get("photo_id"), ev["request_id"], b.get("phase"), b.get("sha256"), b.get("bytes"), dev_id, b.get("line_id")))
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
    return rows(con, sql + " ORDER BY e.device_ts")


def resolve_conflict(con, event_id: str, action: str, reason: str):
    e = one(con, "SELECT * FROM result_event WHERE event_id=?", (event_id,))
    if not e or e["apply_status"] != "CONFLICT":
        raise UserError("처리할 충돌 기록이 없습니다.")
    if action not in ("APPLY", "REJECT"):
        raise UserError("반영 또는 거부를 골라 주세요.")
    if not (reason or "").strip():
        raise UserError("판단 사유를 적어 주세요.")
    with tx(con):
        con.execute("UPDATE result_event SET apply_status=?, resolved_reason=?, resolved_at=? WHERE event_id=?",
                    ("APPLIED" if action == "APPLY" else "REJECTED", reason.strip(), now(), event_id))
        if action == "APPLY":
            if e["kind"] == "REVISIT_APPT":
                _apply_revisit(con, e["request_id"], json.loads(e["body_json"]))
            if e["kind"] == "PHOTO_META":
                b = json.loads(e["body_json"])
                con.execute("INSERT OR IGNORE INTO photo (photo_id, request_id, phase, sha256, bytes, device_id) VALUES (?,?,?,?,?,?)",
                            (b.get("photo_id"), e["request_id"], b.get("phase"), b.get("sha256"), b.get("bytes"), e["device_id"]))
            _refresh_status(con, e["request_id"])
        audit(con, "event", event_id, "RESOLVE_" + action, {"reason": reason.strip()})


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
    ids = {r["id"] for r in reqs}
    photos = [x for x in rows(con, """SELECT p.photo_id, p.request_id, r.receipt_no FROM photo p JOIN request r ON r.id=p.request_id
        WHERE r.planned_date=? AND p.recovery_status!='RECOVERED'""", (work_date,)) if x["request_id"] in ids]
    conflicts = [c for c in list_conflicts(con) if c["request_id"] in ids]
    unconfirmed = rows(con, """SELECT t.id, t.work_date, t.assign_ver FROM transfer_bundle t LEFT JOIN assignment_batch b ON b.id=t.batch_id
        WHERE t.work_date=? AND t.receipt_confirmed_at IS NULL""" + (" AND b.crew_id=?" if crew_id else ""), (work_date, crew_id) if crew_id else (work_date,))
    closed = one(con, "SELECT * FROM day_close WHERE work_date=?", (work_date,))
    unpaid = []
    for r in reqs:
        if r["status"] in ("DONE", "PARTIAL", "NOT_COLLECTED", "ON_HOLD"):
            ps = payment_status(con, r["id"])
            if ps["status"] in ("UNPAID", "UNDERPAID", "OVERPAID_REVIEW", "AMOUNT_PENDING", "PAID_AMOUNT_PENDING"):
                unpaid.append({"id": r["id"], "receipt_no": r["receipt_no"], "name": r["name"], "status": ps["status"], "calculated": ps["calculated"], "paid": ps["paid"]})
    unloads = [u for u in list_unloads(con, work_date) if not crew_id or u["crew_id"] == crew_id]
    return {"work_date": work_date, "total": len(reqs), "by_status": by_status, "no_result": no_result, "pending_fee": pending_fee,
            "revisit": revisit, "photos_missing": len(photos), "conflicts": len(conflicts), "unconfirmed_bundles": len(unconfirmed),
            "payments": unpaid, "unloads": unloads,
            "unload_total": {"volume_m3": round(sum(u["volume_m3"] or 0 for u in unloads), 2), "weight_kg": round(sum(u["weight_kg"] or 0 for u in unloads), 1)},
            "closed": closed}


def close_day(con, work_date: str, note: str = "") -> dict:
    s = day_summary(con, work_date)
    s.pop("closed", None)
    with tx(con):
        con.execute("INSERT INTO day_close VALUES (?,?,?,?) ON CONFLICT(work_date) DO UPDATE SET closed_at=excluded.closed_at, summary_json=excluded.summary_json, note=excluded.note",
                    (work_date, now(), json.dumps(s, ensure_ascii=False), note))
        audit(con, "day", work_date, "CLOSE", {"open_items": len(s["no_result"]) + len(s["revisit"]) + s["conflicts"] + s["photos_missing"]})
    return s


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
PAY_METHODS = ("현장 납부", "무통장 입금", "기타")


def payment_status(con, rid: str) -> dict:
    r = one(con, "SELECT * FROM request WHERE id=?", (rid,))
    final = derive(con, r)["fee"]["final"]
    recs = rows(con, "SELECT * FROM payment WHERE request_id=? ORDER BY recorded_at", (rid,))
    live = [x for x in recs if not x["void_reason"]]
    paid = sum(x["amount"] for x in live if x["kind"] == "PAY") - sum(x["amount"] for x in live if x["kind"] == "REFUND")
    if r["status"] == "CANCELLED" and paid == 0:
        status = "NONE"
    elif final is None:
        status = "AMOUNT_PENDING" if paid == 0 else "PAID_AMOUNT_PENDING"
    elif paid == 0:
        status = "UNPAID"
    elif paid < final:
        status = "UNDERPAID"
    elif paid == final:
        status = "PAID"
    else:
        status = "OVERPAID_REVIEW"
    return {"calculated": final, "paid": paid, "status": status, "records": recs}


def add_payment(con, rid: str, data: dict) -> dict:
    if not one(con, "SELECT 1 AS x FROM request WHERE id=?", (rid,)):
        raise UserError("접수를 찾을 수 없습니다.")
    kind = data.get("kind", "PAY")
    amount = int(data.get("amount") or 0)
    method = data.get("method") or ""
    paid_on = data.get("paid_on") or today_kst()
    if kind not in ("PAY", "REFUND") or amount <= 0:
        raise UserError("금액을 1원 이상으로 적어 주세요.")
    if method not in PAY_METHODS:
        raise UserError("납부 방법을 골라 주세요.")
    if not re.fullmatch(r"\d{4}-\d\d-\d\d", paid_on):
        raise UserError("날짜 형식이 올바르지 않습니다.")
    pid = str(uuid.uuid4())
    with tx(con):
        con.execute("INSERT INTO payment (id, request_id, kind, amount, method, paid_on, note, recorded_at) VALUES (?,?,?,?,?,?,?,?)",
                    (pid, rid, kind, amount, method, paid_on, (data.get("note") or "").strip(), now()))
        audit(con, "payment", pid, kind, {"request": rid, "amount": amount, "method": method})
    return payment_status(con, rid)


def void_payment(con, pid: str, reason: str) -> dict:
    p = one(con, "SELECT * FROM payment WHERE id=?", (pid,))
    if not p or p["void_reason"]:
        raise UserError("정정할 수납 기록이 없습니다.")
    if not (reason or "").strip():
        raise UserError("정정 사유를 적어 주세요.")
    with tx(con):
        con.execute("UPDATE payment SET void_reason=?, voided_at=? WHERE id=?", (reason.strip(), now(), pid))
        audit(con, "payment", pid, "VOID", {"reason": reason.strip()})
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

ROLES = {"ADMIN": "운영 관리자", "INTAKE": "접수 담당자", "FIELD": "수거팀"}


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


def list_audit(con, entity: str = "", date: str = "", actor: str = "", limit: int = 300) -> list[dict]:
    sql, args = "SELECT id, entity, entity_id, action, detail, at, actor FROM audit_log WHERE 1=1", []
    if entity:
        sql += " AND entity=?"; args.append(entity)
    if date:  # 한국시간 날짜 → UTC 범위
        d0 = dt.datetime.fromisoformat(date).replace(tzinfo=KST).astimezone(dt.timezone.utc)
        sql += " AND at >= ? AND at < ?"; args += [d0.strftime("%Y-%m-%dT%H:%M:%SZ"), (d0 + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")]
    if actor:
        sql += " AND actor=?"; args.append(actor)
    return rows(con, sql + " ORDER BY id DESC LIMIT ?", args + [limit])


# ------------------------------------------------------------------ 사진 보관 기간 정리
def purge_candidates(con, today: str | None = None) -> list[dict]:
    """보관 기간이 지난 사진: 회수 완료 + 접수가 끝남(수거 완료·취소) 또는 그날 마감됨 + 회수일로부터 기간 경과."""
    days = settings(con)["photo_retention_days"]
    t = dt.date.fromisoformat(today or today_kst())
    cutoff = (dt.datetime.combine(t - dt.timedelta(days=days), dt.time()).replace(tzinfo=KST)).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return rows(con, """SELECT p.photo_id, p.request_id, p.bytes, p.recovered_at, r.receipt_no, r.status FROM photo p JOIN request r ON r.id=p.request_id
        WHERE p.recovery_status='RECOVERED' AND p.purged_at IS NULL AND p.data IS NOT NULL AND p.recovered_at < ?
          AND (r.status IN ('DONE','CANCELLED') OR EXISTS (SELECT 1 FROM day_close d WHERE d.work_date=r.planned_date))
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
    return {"file": str(dest), "name": dest.name, "bytes": dest.stat().st_size, "sha256": h}


def list_backups(con) -> list[dict]:
    d = backup_dir(con)
    if not d.exists():
        return []
    return [{"name": f.name, "bytes": f.stat().st_size, "at": dt.datetime.fromtimestamp(f.stat().st_mtime, KST).strftime("%Y-%m-%d %H:%M")}
            for f in sorted(d.glob("*.dgbak"), reverse=True)]


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
            "sample": [{"sigungu": p[0], "road": p[1], "main": p[2], "sub": p[3], "lat": p[4], "lon": p[5]} for p in pts[:5]]}
    if preview:
        return info
    with tx(con):
        con.executemany("INSERT OR IGNORE INTO addr_point (sigungu, road, main, sub, lat, lon) VALUES (?,?,?,?,?,?)", pts)
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
    return {"lat": c["lat"], "lon": c["lon"], "label": label, "exact": exact, "sigungu": c["sigungu"]}


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


def stats(con, start: str, end: str) -> dict:
    """기간(수거 예정일 기준) 집계. 이름·연락처·주소는 쓰지 않는다."""
    for d in (start, end):
        if not re.fullmatch(r"\d{4}-\d\d-\d\d", d or ""):
            raise UserError("기간을 날짜로 골라 주세요.")
    if start > end:
        raise UserError("시작일이 끝일보다 늦습니다.")
    reqs = rows(con, "SELECT * FROM request WHERE planned_date BETWEEN ? AND ?", (start, end))
    received = rows(con, "SELECT received_at FROM request")
    by_day: dict[str, dict] = {}
    def day(d):
        return by_day.setdefault(d, {"date": d, "received": 0, "planned": 0, "done": 0, "partial": 0, "not_collected": 0, "cancelled": 0,
                                     "calculated": 0, "paid": 0, "unload_m3": 0.0, "unload_kg": 0.0})
    for r in received:
        d = _kst_date(r["received_at"])
        if d and start <= d <= end:
            day(d)["received"] += 1
    totals = {"requests": len(reqs), "by_status": {}, "calculated": 0, "paid": 0, "amount_pending": 0, "unpaid": 0,
              "field_added_lines": 0, "lead_days_avg": None}
    items: dict[str, dict] = {}
    leads = []
    crews: dict[str, dict] = {}
    for r in reqs:
        dd = day(r["planned_date"])
        dd["planned"] += 1
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
                totals["calculated"] += ps["calculated"]; dd["calculated"] += ps["calculated"]
            totals["paid"] += ps["paid"]; dd["paid"] += ps["paid"]
            if ps["status"] in ("UNPAID", "UNDERPAID"):
                totals["unpaid"] += 1
        for l in st["lines"]:
            if l["kind"] == "FIELD":
                totals["field_added_lines"] += 1
            k = f'{l["name"]}|{l["spec"]}'
            it = items.setdefault(k, {"name": l["name"], "spec": l["spec"], "unit_label": l["unit_label"], "requested": 0, "collected": 0})
            it["requested"] += l["qty_confirmed"]; it["collected"] += l["qty_collected"]
        rd = _kst_date(r["received_at"])
        if rd and r["status"] == "DONE":
            leads.append((dt.date.fromisoformat(r["planned_date"]) - dt.date.fromisoformat(rd)).days)
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
    top = sorted(items.values(), key=lambda x: -x["requested"])[:15]
    return {"start": start, "end": end, "totals": totals, "days": days, "items": top, "crews": sorted(crews.values(), key=lambda c: c["crew"])}


def stats_csv(con, start: str, end: str, kind: str = "requests") -> bytes:
    """엑셀에서 열리는 CSV(UTF-8 BOM). 이름·연락처·주소·메모는 넣지 않는다."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    if kind == "days":
        s = stats(con, start, end)
        w.writerow(["날짜", "접수", "수거 예정", "수거 완료", "일부 수거", "미수거", "취소", "계산 금액(원)", "납부 기록(원)", "하차 부피(m3)", "하차 중량(kg)"])
        for d in s["days"]:
            w.writerow([d["date"], d["received"], d["planned"], d["done"], d["partial"], d["not_collected"], d["cancelled"], d["calculated"], d["paid"], d["unload_m3"], d["unload_kg"]])
    else:
        stats(con, start, end)  # 기간 검사
        w.writerow(["접수번호", "접수일", "수거 예정일", "상태", "품목(현장 확인 수량)", "수거 수량 합계", "남은 수량 합계", "계산 금액(원)", "납부 기록(원)", "수납 상태", "수수료표 버전", "수거팀"])
        for r in rows(con, "SELECT * FROM request WHERE planned_date BETWEEN ? AND ? ORDER BY planned_date, receipt_no", (start, end)):
            st = derive(con, r)
            ps = payment_status(con, r["id"])
            ver = one(con, "SELECT fee_version_id FROM request_item WHERE request_id=? AND fee_version_id IS NOT NULL LIMIT 1", (r["id"],))
            a = one(con, """SELECT c.name FROM assignment_stop s JOIN assignment_batch b ON b.id=s.batch_id JOIN crew c ON c.id=b.crew_id
                WHERE s.request_id=? ORDER BY b.created_at DESC LIMIT 1""", (r["id"],))
            w.writerow([r["receipt_no"], _kst_date(r["received_at"]), r["planned_date"], r["status"],
                        "; ".join(f'{l["name"]} {l["spec"]} {l["qty_confirmed"]}{l["unit_label"]}' for l in st["lines"]),
                        sum(l["qty_collected"] for l in st["lines"]), sum(l["remaining"] for l in st["lines"]),
                        "" if ps["calculated"] is None else ps["calculated"], ps["paid"], ps["status"], ver["fee_version_id"] if ver else "", a["name"] if a else ""])
    audit(con, "report", kind, "EXPORT", {"start": start, "end": end})
    return ("﻿" + buf.getvalue()).encode("utf-8")


def dispatch_sheet(con, batch_id: str) -> dict:
    """인쇄용 일일 배차표(종이 예비본). 이름·연락처·주소가 들어가므로 업무 후 파쇄."""
    payload, b = build_daylist(con, batch_id)
    with tx(con):
        audit(con, "batch", batch_id, "PRINT", {"stops": len(payload["stops"])})
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
    counts = {"requests": 0, "events": 0, "photos_recovered": 0, "unloads_applied": 0, "skipped": 0}
    with tx(con):
        con.execute("INSERT INTO result_import VALUES (?,?,?,?,?,NULL,NULL)", (imp, "USB", "SOLO", sha, now()))
        for r in new:
            d = validate({**r, "items": r.get("items") or [{"name": "(품목 없음)", "qty": 1}]})  # 품목 없는 취소 건도 옮긴다
            rec = r["receipt_no"]
            if one(con, "SELECT 1 AS x FROM request WHERE receipt_no=?", (rec,)):
                rec = re.sub(r"-(\d+)$", r"-S\1", rec)  # PC 접수번호와 겹치면 단독 표시(S)
            con.execute("""INSERT INTO request (id, receipt_no, req_ver, name, phone, address, spot_note, memo, lat, lon, geo_source,
                planned_date, appt_start, appt_end, must_today, urgent, priority, status, cancel_reason, received_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,?,?,?,?)""",
                        (r["request_id"], rec, int(r.get("req_ver") or 1), d["name"], d["phone"], d["address"], d["spot_note"], d["memo"],
                         r.get("lat"), r.get("lon"), "MANUAL" if r.get("lat") is not None else None, d["planned_date"] or None, None, None,
                         "CANCELLED" if r.get("status") == "CANCELLED" else "RECEIVED", r.get("cancel_reason"),
                         r.get("received_at") or now(), r.get("updated_at") or now()))
            for it in r.get("items") or []:
                con.execute("""INSERT INTO request_item (line_id, request_id, fee_rate_id, fee_version_id, name, spec, unit, unit_label, qty,
                    unit_price, amount, price_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (it["line_id"], r["request_id"], it.get("fee_rate_id"), it.get("fee_version_id") or (FEE_TABLE["version_id"] if it.get("fee_rate_id") else None),
                             it["name"], it.get("spec") or "", it.get("unit") or "EA", it.get("unit_label") or "개", int(it["qty"]),
                             it.get("unit_price"), it.get("amount"), it.get("price_status") or "PENDING"))
            for v in r.get("versions") or []:
                con.execute("INSERT OR IGNORE INTO request_version VALUES (?,?,?,?,?)",
                            (r["request_id"], int(v.get("req_ver") or 1), json.dumps(v.get("snapshot") or {}, ensure_ascii=False), v.get("reason") or "휴대폰 단독 수정", v.get("at") or now()))
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
        audit(con, "request", rid, "LINK_SIMILAR", {"line": line_id[:8], "fee_rate_id": fee_rate_id, "price": row["price"], "confirmed_by": confirmed_by.strip()[:40]})
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
