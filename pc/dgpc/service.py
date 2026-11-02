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
        "unload_min": get_setting(con, "unload_min", 20),
        "max_unloads": get_setting(con, "max_unloads", 2),
        "backup_dir": get_setting(con, "backup_dir", ""),
        "fee_version": FEE_TABLE["version_id"], "fee_label": FEE_TABLE["label"],
        "fee_checked_on": FEE_TABLE["source_checked_on"], "fee_effective_from": FEE_TABLE["effective_from"],
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
        if "unload_site" in data:
            u = data["unload_site"] or {}
            if u.get("lat") in (None, "") or u.get("lon") in (None, ""):
                set_setting(con, "unload_site", None)
            else:
                lat, lon = float(u["lat"]), float(u["lon"])
                if not (33 <= lat <= 39 and 124 <= lon <= 132):
                    raise UserError("반입지 좌표가 국내 범위를 벗어났습니다.")
                set_setting(con, "unload_site", {"lat": lat, "lon": lon, "label": str(u.get("label") or "반입지")[:40]})
        for k, lo, hi in (("unload_min", 0, 180), ("max_unloads", 0, 5)):
            if k in data:
                set_setting(con, k, min(hi, max(lo, int(data[k]))))
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


def build_items(items: list[dict]) -> list[dict]:
    """화면에서 받은 품목을 공식 수수료표 기준으로 다시 계산한다(화면 값의 단가를 믿지 않음)."""
    out = []
    for it in items:
        qty = int(it.get("qty") or 0)
        if qty <= 0:
            raise UserError("품목 수량은 1 이상이어야 합니다.")
        rid = it.get("fee_rate_id")
        line_id = it.get("line_id") or str(uuid.uuid4())
        if rid:
            r = FEE_ROWS.get(rid)
            if not r:
                raise UserError("수수료표에 없는 품목입니다. 다시 골라 주세요.")
            confirmed = r["status"] == "CONFIRMED"
            out.append({"line_id": line_id, "fee_rate_id": rid, "fee_version_id": FEE_TABLE["version_id"], "name": r["name"],
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
    items = build_items(data["items"])
    rid, ts = str(uuid.uuid4()), now()
    day = today_kst().replace("-", "")
    with tx(con):
        n = con.execute("SELECT COUNT(*) FROM request WHERE receipt_no LIKE ?", (f"DG-{day}-%",)).fetchone()[0] + 1
        receipt = f"DG-{day}-{n:04d}"
        con.execute("""INSERT INTO request (id, receipt_no, req_ver, name, phone, address, spot_note, memo, lat, lon, geo_source,
            planned_date, appt_start, appt_end, must_today, urgent, priority, status, received_at, updated_at)
            VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'RECEIVED',?,?)""",
                    (rid, receipt, d["name"], d["phone"], d["address"], d["spot_note"], d["memo"], d["lat"], d["lon"],
                     "MANUAL" if d["lat"] is not None else None, d["planned_date"] or None, d["appt_start"] or None,
                     d["appt_end"] or None, d["must_today"], d["urgent"], d["priority"], ts, ts))
        for it in items:
            _insert_item(con, rid, it)
        audit(con, "request", rid, "CREATE", {"receipt_no": receipt})
    return {"id": rid, "receipt_no": receipt}


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
    items = build_items(data["items"])
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
                     "MANUAL" if d["lat"] is not None and (d["lat"], d["lon"]) != (cur["lat"], cur["lon"]) else None,
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
    return {"id": rid, "resend_needed": bool(one(con, "SELECT 1 AS x FROM active_assignment WHERE request_id=?", (rid,)))}


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
    r["photos"] = rows(con, "SELECT photo_id, phase, recovery_status, purged_at FROM photo WHERE request_id=?", (rid,))
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
    for l in lines:
        l["remaining"] = max(0, l["qty_confirmed"] - l["qty_collected"])
    received = sum(l["amount_received"] or 0 for l in lines if l["kind"] == "RECEIVED")
    pending = [l for l in lines if l["price"] is None and l["qty_confirmed"] > 0]
    subtotal = sum(l["price"] * l["qty_confirmed"] for l in lines if l["price"] is not None)
    qty_by_unit: dict[str, int] = {}
    for l in lines:
        qty_by_unit[l["unit_label"]] = qty_by_unit.get(l["unit_label"], 0) + l["qty_confirmed"]
    return {"lines": lines, "contact": contact, "outcome": outcome, "field_loc": loc, "has_field_records": n > 0,
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
def confirm_plan(con, work_date: str, plan: dict[str, list[dict]]) -> list[dict]:
    """plan = {crew_id: [{request_id, eta}, ...]} 순서대로. 팀별로 새 배정 버전을 만든다."""
    crews = {c["id"]: c for c in list_crews(con)}
    for cid in plan:
        if cid not in crews:
            raise UserError("없는 팀이 포함되어 있습니다.")
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
            con.execute("INSERT INTO assignment_batch VALUES (?,?,?,?,'ACTIVE',?,?)",
                        (bid, cid, work_date, prev + 1, json.dumps(stops, ensure_ascii=False), now()))
            for i, s in enumerate(stops, 1):
                con.execute("INSERT INTO assignment_stop VALUES (?,?,?,?)", (bid, s["request_id"], i, s.get("eta")))
                con.execute("INSERT INTO active_assignment VALUES (?,?) ON CONFLICT(request_id) DO UPDATE SET batch_id=excluded.batch_id",
                            (s["request_id"], bid))
                con.execute("UPDATE request SET status=CASE WHEN status IN ('RECEIVED','PARTIAL','NOT_COLLECTED','ON_HOLD') THEN 'ASSIGNED' ELSE status END, planned_date=?, updated_at=? WHERE id=?",
                            (work_date, now(), s["request_id"]))
            out.append({"batch_id": bid, "crew_id": cid, "assign_ver": prev + 1, "count": len(stops)})
            audit(con, "batch", bid, "CONFIRM", {"crew": crews[cid]["name"], "date": work_date, "ver": prev + 1, "n": len(stops)})
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
    for s in rows(con, "SELECT request_id, seq, eta FROM assignment_stop WHERE batch_id=? ORDER BY seq", (batch_id,)):
        r = one(con, "SELECT * FROM request WHERE id=?", (s["request_id"],))
        if r["status"] == "CANCELLED":
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
        stops.append({"seq": s["seq"], "request_id": r["id"], "receipt_no": r["receipt_no"], "req_ver": r["req_ver"],
                      "name": r["name"], "phone": r["phone"], "address": r["address"], "spot_note": r["spot_note"], "memo": r["memo"],
                      "lat": r["lat"], "lon": r["lon"], "eta": s["eta"] or (r["appt_start"] or ""),
                      "appt": [r["appt_start"], r["appt_end"]] if r["appt_start"] else None,
                      "revisit": revisit, "assign_ver": b["assign_ver"], "items": items})
    payload = {"schema": "dg.daylist/1", "work_date": b["work_date"], "crew": b["crew_name"], "assign_ver": b["assign_ver"],
               "fee_table": {"version_id": FEE_TABLE["version_id"], "label": FEE_TABLE["label"]}, "stops": stops}
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
                con.execute("INSERT OR IGNORE INTO photo (photo_id, request_id, phase, sha256, bytes, device_id) VALUES (?,?,?,?,?,?)",
                            (b.get("photo_id"), ev["request_id"], b.get("phase"), b.get("sha256"), b.get("bytes"), dev_id))
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
def day_summary(con, work_date: str) -> dict:
    reqs = rows(con, "SELECT * FROM request WHERE planned_date=?", (work_date,))
    by_status: dict[str, int] = {}
    pending_fee, revisit, no_result = [], [], []
    assigned_ids = {r["request_id"] for r in rows(con, """SELECT s.request_id FROM assignment_stop s JOIN assignment_batch b ON b.id=s.batch_id
        WHERE b.work_date=? AND b.status='ACTIVE'""", (work_date,))}
    for r in reqs:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        st = derive(con, r)
        if st["fee"]["pending_count"] and r["status"] != "CANCELLED":
            pending_fee.append({"receipt_no": r["receipt_no"], "name": r["name"], "count": st["fee"]["pending_count"]})
        if r["status"] in ("PARTIAL", "NOT_COLLECTED", "ON_HOLD"):
            revisit.append({"receipt_no": r["receipt_no"], "name": r["name"], "status": r["status"], "remaining": st["remaining_total"],
                            "next_date": r["planned_date"] if r["planned_date"] != work_date else None, "appt": [r["appt_start"], r["appt_end"]] if r["appt_start"] else None})
        if r["id"] in assigned_ids and r["status"] == "ASSIGNED":
            no_result.append({"receipt_no": r["receipt_no"], "name": r["name"]})
    photos = rows(con, """SELECT p.photo_id, r.receipt_no FROM photo p JOIN request r ON r.id=p.request_id
        WHERE r.planned_date=? AND p.recovery_status!='RECOVERED'""", (work_date,))
    conflicts = [c for c in list_conflicts(con) if one(con, "SELECT 1 AS x FROM request WHERE id=? AND planned_date=?", (c["request_id"], work_date))]
    unconfirmed = rows(con, "SELECT id, work_date, assign_ver FROM transfer_bundle WHERE work_date=? AND receipt_confirmed_at IS NULL", (work_date,))
    closed = one(con, "SELECT * FROM day_close WHERE work_date=?", (work_date,))
    unpaid = []
    for r in reqs:
        if r["status"] in ("DONE", "PARTIAL", "NOT_COLLECTED", "ON_HOLD"):
            ps = payment_status(con, r["id"])
            if ps["status"] in ("UNPAID", "UNDERPAID", "OVERPAID_REVIEW", "AMOUNT_PENDING", "PAID_AMOUNT_PENDING"):
                unpaid.append({"receipt_no": r["receipt_no"], "name": r["name"], "status": ps["status"], "calculated": ps["calculated"], "paid": ps["paid"]})
    unloads = list_unloads(con, work_date)
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


def create_user(con, username: str, display_name: str, role: str, password: str, must_change: bool = False) -> str:
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
    salt = _secrets.token_bytes(16)
    uid = str(uuid.uuid4())
    with tx(con):
        con.execute("INSERT INTO app_user (id, username, display_name, role, pw_salt, pw_hash, must_change, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (uid, username, display_name.strip(), role, salt.hex(), _pw_hash(password, salt), 1 if must_change else 0, now()))
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
    return {"id": u["id"], "username": u["username"], "display_name": u["display_name"], "role": u["role"], "must_change": bool(u["must_change"])}


def list_users(con) -> list[dict]:
    return rows(con, "SELECT id, username, display_name, role, active, must_change, created_at, last_login_at FROM app_user ORDER BY created_at")


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
