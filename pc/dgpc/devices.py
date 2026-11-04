"""PC 키·휴대폰 기기 등록(지문 확인)·수거팀·팀 범위 조회·QR 실측.
"""
from __future__ import annotations

import json
import re
import uuid
from cryptography.hazmat.primitives import serialization

import dgproto as P
from dgpc.core import UserError, audit, now, one, rows, today_kst, tx
from dgpc.fees import FEE_ROWS

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
def device_fingerprint(con, dev_id: str, kex: bytes, sig: bytes) -> str:
    return P.fingerprint(P.pub_raw(pc_key(con, "sign")), P.pub_raw(pc_key(con, "kex")), uuid.UUID(dev_id).bytes, kex, sig)


def register_device(con, code: str, label: str = "", confirm: bool = False) -> dict:
    """기기 등록: 공개키를 넣되 '지문 확인 대기'. 담당자가 두 화면의 9자리 지문이 같음을 확인(confirm_device)해야 목록을 보낼 수 있다."""
    try:
        dev, kex, sig = P.parse_device_code(code)
    except P.ProtocolError as e:
        raise UserError(f"기기 코드가 올바르지 않습니다 ({e.code}).")
    dev_id = str(uuid.UUID(bytes=dev))
    old = one(con, "SELECT * FROM device WHERE id=?", (dev_id,))
    if old and old["status"] != "ACTIVE":  # 분실·중지 기기를 같은 코드로 되살리지 않는다
        raise UserError("분실·사용 중지 처리한 기기입니다. 다시 쓰려면 휴대폰에서 [모든 자료 지우기] 후 새로 등록하세요.")
    if old and (bytes(old["kex_pub"]), bytes(old["sign_pub"])) != (kex, sig):
        raise UserError("같은 기기 번호에 다른 키가 들어왔습니다. 등록하지 않았습니다(바꿔치기 의심). 휴대폰에서 다시 시작하세요.")
    with tx(con):
        if not old:
            con.execute("INSERT INTO device (id, kex_pub, sign_pub, status, registered_at, label, confirmed_at) VALUES (?,?,?,'ACTIVE',?,?,NULL)",
                        (dev_id, kex, sig, now(), label))
            audit(con, "device", dev_id, "REGISTER", {"label": label})
        elif label and label != old["label"]:
            con.execute("UPDATE device SET label=? WHERE id=?", (label, dev_id))
    if confirm:
        confirm_device(con, dev_id)
    cur = one(con, "SELECT confirmed_at FROM device WHERE id=?", (dev_id,))
    return {"device_id": dev_id, "fingerprint": device_fingerprint(con, dev_id, kex, sig), "confirmed": bool(cur["confirmed_at"])}


def confirm_device(con, dev_id: str):
    """두 화면의 지문이 같다고 담당자가 확인 → 사용 시작."""
    d = one(con, "SELECT status, confirmed_at FROM device WHERE id=?", (dev_id,))
    if not d or d["status"] != "ACTIVE":
        raise UserError("확인할 기기가 없습니다.")
    if d["confirmed_at"]:
        return
    with tx(con):
        con.execute("UPDATE device SET confirmed_at=? WHERE id=?", (now(), dev_id))
        audit(con, "device", dev_id, "CONFIRM", None)


def device_ready(dev) -> bool:
    return bool(dev) and dev["status"] == "ACTIVE" and bool(dev.get("confirmed_at"))


def set_device_status(con, dev_id: str, status: str):
    if status not in ("ACTIVE", "LOST", "RETIRED"):
        raise UserError("기기 상태 값이 올바르지 않습니다.")
    with tx(con):
        con.execute("UPDATE device SET status=? WHERE id=?", (status, dev_id))
        audit(con, "device", dev_id, "STATUS", {"status": status})


def list_devices(con):
    out = rows(con, "SELECT id, status, registered_at, label, confirmed_at, kex_pub, sign_pub FROM device ORDER BY registered_at")
    for d in out:
        kex, sig = bytes(d.pop("kex_pub")), bytes(d.pop("sign_pub"))
        if not d["confirmed_at"] and d["status"] == "ACTIVE":  # 지문 확인 대기: 화면을 다시 그려도 지문이 보이게
            d["fingerprint"] = device_fingerprint(con, d["id"], kex, sig)
    return out


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
    try:
        m3 = float(data.get("capacity_m3") or 8)
    except (TypeError, ValueError):
        raise UserError("적재 용량은 숫자(m³)로 적어 주세요.")
    if not 0.5 <= m3 <= 100:
        raise UserError("적재 용량은 0.5~100m³ 사이로 적어 주세요.")
    try:
        kg = float(data.get("capacity_kg") or 0)
    except (TypeError, ValueError):
        raise UserError("최대 적재 중량은 숫자(kg)로 적어 주세요.")
    if not 0 <= kg <= 30000:
        raise UserError("최대 적재 중량은 0~30000kg 사이로 적어 주세요 (0 = 따지지 않음).")
    with tx(con):
        con.execute("""INSERT INTO crew (id, name, device_id, capacity_m3, capacity_kg, shift_start, shift_end, active) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name, device_id=excluded.device_id, capacity_m3=excluded.capacity_m3,
            capacity_kg=excluded.capacity_kg, shift_start=excluded.shift_start, shift_end=excluded.shift_end, active=excluded.active""",
                    (cid, name, dev, m3, kg, data.get("shift_start") or "09:00",
                     data.get("shift_end") or "17:00", 1 if data.get("active", True) else 0))
        audit(con, "crew", cid, "SAVE", {"name": name, "device_id": dev})
    return cid


def list_crews(con):
    return rows(con, "SELECT * FROM crew ORDER BY name")


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
    if not dev["confirmed_at"]:
        raise UserError("지문 확인 전인 휴대폰의 파일입니다. 설정 → 기기에서 지문을 확인한 뒤 다시 가져오세요.")
    try:
        head, payload, release = P.open_envelope(data, P.pub_from_raw(dev["sign_pub"]), pc_key(con, "kex"))
    except P.ProtocolError as e:
        raise UserError(f"파일 검증에 실패했습니다({e.code}). 아무것도 반영하지 않았습니다.")
    if head.btype != expect_type:
        raise UserError("파일 종류가 다릅니다 (결과 파일과 사진 파일을 구분해 넣어 주세요).")
    return dev_id, head, payload, release


# ------------------------------------------------------------------ QR 실측 (가상 자료, 업무 자료와 분리)
def make_qr_test(con, device_id: str, count: int, addr_style: str = "mixed", chunk: int = 700, ec: str = "M", fps: int = 3) -> dict:
    """기관 휴대폰으로 QR 인식률·전송 시간을 재기 위한 가상 목록. 휴대폰은 실측 모드로 받고 저장하지 않는다."""
    from sample_data import make_daylist
    if count not in (20, 50, 100) or addr_style not in ("mixed", "short", "medium", "long") or chunk not in (400, 600, 800, 1200) or ec not in ("L", "M", "Q"):
        raise UserError("실측 조건 값이 올바르지 않습니다.")
    dev = one(con, "SELECT * FROM device WHERE id=? AND status='ACTIVE' AND confirmed_at IS NOT NULL", (device_id,))
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


def crew_request_ids(con, crew_id: str) -> set[str]:
    """이 팀에 배정된 적이 있는 접수 (현재 배정 또는 지난 배정)."""
    return {r["request_id"] for r in rows(con, """SELECT DISTINCT s.request_id FROM assignment_stop s JOIN assignment_batch b ON b.id=s.batch_id
        WHERE b.crew_id=?""", (crew_id,))}


def request_in_crew(con, rid: str, crew_id: str) -> bool:
    return bool(one(con, """SELECT 1 AS x FROM assignment_stop s JOIN assignment_batch b ON b.id=s.batch_id WHERE s.request_id=? AND b.crew_id=?""", (rid, crew_id)))


def device_crew(con, data: bytes) -> str | None:
    try:
        head = P.Header.unpack(data)
    except P.ProtocolError:
        return None
    r = one(con, "SELECT id FROM crew WHERE device_id=?", (str(uuid.UUID(bytes=head.device_id)),))
    return r["id"] if r else None
