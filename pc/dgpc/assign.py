"""배정: 노선 확정·부분 수정·배정 목록·휴대폰 목록 묶음(서명·암호화)·수신 확인·배차표·하차·적재 점검.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid

import dgproto as P
from dgpc import privacy as _pv
from dgpc.core import KST, StaleError, UserError, _chunks, _date_ok, _int_clamp, _num, audit, get_setting, now, one, rows, today_kst, tx
from dgpc.derive import derive, request_round_unit
from dgpc.devices import list_crews, pc_key
from dgpc.fees import FEE_ROWS, fee_table_for_phone, volume_overrides

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
            out.append(_write_batch(con, cid, work_date, stops, crews[cid]["name"], {**({"adjusted": True, "reason": adjust_reason.strip()[:200]} if adjusted else {})}))
        _release_dropped(con, replaced)
    return out


def _clean_stop(x: dict) -> dict:
    u = x.get("unload_after")
    item = {"request_id": x["request_id"], "eta": x.get("eta") or ""}
    if isinstance(u, dict) and u.get("label"):
        item["unload_after"] = {"eta": str(u.get("eta") or "")[:5], "label": str(u["label"])[:40],
                                "minutes": _int_clamp(u.get("minutes"), 0, 600), "volume_m3": _num(u.get("volume_m3")) or 0.0}
    return item


def _write_batch(con, cid: str, work_date: str, stops: list[dict], crew_name: str, extra: dict | None = None, action: str = "CONFIRM") -> dict:
    """팀의 그날 배정 새 버전(assign_ver = 지난 최대 + 1)을 만들고 이전 버전은 SUPERSEDED. 트랜잭션은 호출하는 쪽."""
    prev = one(con, "SELECT MAX(assign_ver) AS v FROM assignment_batch WHERE crew_id=? AND work_date=?", (cid, work_date))["v"] or 0
    con.execute("UPDATE assignment_batch SET status='SUPERSEDED' WHERE crew_id=? AND work_date=? AND status='ACTIVE'", (cid, work_date))
    bid = str(uuid.uuid4())
    clean = [_clean_stop(x) for x in stops]
    con.execute("INSERT INTO assignment_batch (id, crew_id, work_date, assign_ver, status, route_json, created_at) VALUES (?,?,?,?,'ACTIVE',?,?)",
                (bid, cid, work_date, prev + 1, json.dumps(clean, ensure_ascii=False), now()))
    for i, st in enumerate(clean, 1):
        con.execute("INSERT INTO assignment_stop VALUES (?,?,?,?)", (bid, st["request_id"], i, st["eta"] or None))
        con.execute("INSERT INTO active_assignment VALUES (?,?) ON CONFLICT(request_id) DO UPDATE SET batch_id=excluded.batch_id", (st["request_id"], bid))
        con.execute("UPDATE request SET status=CASE WHEN status IN ('RECEIVED','PARTIAL','NOT_COLLECTED','ON_HOLD') THEN 'ASSIGNED' ELSE status END, planned_date=?, updated_at=? WHERE id=?",
                    (work_date, now(), st["request_id"]))
    audit(con, "batch", bid, action, {"crew": crew_name, "date": work_date, "ver": prev + 1, "n": len(clean), **(extra or {})})
    return {"batch_id": bid, "crew_id": cid, "assign_ver": prev + 1, "count": len(clean)}


def _release_dropped(con, old_batch_ids):
    """이전 버전에만 있고 새 버전에서 빠진 접수는 활성 배정을 풀고 접수 상태로 되돌린다."""
    old_batch_ids = list(old_batch_ids)
    if not old_batch_ids:
        return
    for rid, in con.execute("SELECT request_id FROM active_assignment WHERE batch_id IN (%s)" % ",".join("?" * len(old_batch_ids)), old_batch_ids).fetchall():
        con.execute("DELETE FROM active_assignment WHERE request_id=?", (rid,))
        con.execute("UPDATE request SET status='RECEIVED', updated_at=? WHERE id=? AND status='ASSIGNED'", (now(), rid))


def current_assignment(con, work_date: str) -> list[dict]:
    """그날 확정된(ACTIVE) 배정: 팀별 갱신본·순서·예상 도착·하차 경유. 부분 수정 화면의 시작점."""
    out = rows(con, """SELECT b.id AS batch_id, b.crew_id, c.name AS crew_name, b.assign_ver, b.route_json, b.created_at FROM assignment_batch b
        JOIN crew c ON c.id=b.crew_id WHERE b.work_date=? AND b.status='ACTIVE' ORDER BY c.name""", (work_date,))
    for b in out:
        route = {x["request_id"]: x for x in json.loads(b.pop("route_json") or "[]") if isinstance(x, dict)}
        b["stops"] = rows(con, """SELECT s.seq, s.eta, r.id AS request_id, r.receipt_no, r.status, r.dong, r.lat, r.lon FROM assignment_stop s
            JOIN request r ON r.id=s.request_id WHERE s.batch_id=? ORDER BY s.seq""", (b["batch_id"],))
        for st in b["stops"]:
            if route.get(st["request_id"], {}).get("unload_after"):
                st["unload_after"] = route[st["request_id"]]["unload_after"]
        sent = one(con, "SELECT MAX(displayed_at) AS t FROM transfer_bundle WHERE batch_id=?", (b["batch_id"],))
        b["sent_at"] = sent["t"] if sent else None
    return out


ASSIGN_OPS = ("insert", "remove", "reorder", "move")


def edit_assignment(con, work_date: str, op: str, data: dict) -> dict:
    """확정된 배정을 다시 계산하지 않고 고친다: insert(팀 끝·지정 위치에 넣기) · remove(빼기) · reorder(순서 바꾸기) · move(다른 팀으로).
    고친 팀마다 새 갱신본(assign_ver+1)을 만든다(휴대폰에는 다시 보내야 함). data["assign_ver"]: {팀ID: 화면이 본 갱신본}이 지금과 다르면 StaleError."""
    if not _date_ok(work_date or ""):
        raise UserError("배정 날짜를 골라 주세요.")
    if op not in ASSIGN_OPS:
        raise UserError("배정 수정 종류가 올바르지 않습니다.")
    reason = str(data.get("reason") or "").strip()[:200]
    crews = {c["id"]: c for c in list_crews(con)}
    rid = data.get("request_id") or ""
    with tx(con):
        cur = {b["crew_id"]: b for b in current_assignment(con, work_date)}
        seen = data.get("assign_ver") or {}
        if isinstance(seen, dict):
            for cid, v in seen.items():
                have = cur.get(cid, {}).get("assign_ver", 0)
                if str(v) != str(have):
                    raise StaleError("배정이 다른 곳에서 바뀌었습니다. 다시 열어 확인하세요", crew_id=cid, assign_ver=have)

        def stops_of(cid):
            return [{"request_id": x["request_id"], "eta": x["eta"] or "", **({"unload_after": x["unload_after"]} if x.get("unload_after") else {})}
                    for x in cur.get(cid, {}).get("stops", [])]

        def where(r_):
            return next((cid for cid, b in cur.items() if any(x["request_id"] == r_ for x in b["stops"])), None)

        def place(lst, item, pos):
            try:
                pos = int(pos) if pos not in (None, "") else None
            except (TypeError, ValueError):
                raise UserError("넣을 위치는 숫자(1부터)입니다.")
            if pos is None or pos > len(lst):
                lst.append(item)
            else:
                lst.insert(max(0, pos - 1), item)

        changes: dict[str, list] = {}
        if op in ("insert", "move"):
            to = data.get("crew_id") if op == "insert" else data.get("to_crew_id")
            if to not in crews:
                raise UserError("넣을 팀을 골라 주세요.")
            req = one(con, "SELECT status FROM request WHERE id=?", (rid,))
            if not req:
                raise UserError("접수를 찾을 수 없습니다.")
            if req["status"] in ("CANCELLED", "DONE"):
                raise UserError("취소·완료된 접수는 배정할 수 없습니다.")
            src = where(rid)
            if op == "insert":
                if src:
                    raise UserError(f"이미 이날 {crews[src]['name']} 배정에 들어 있습니다. [다른 팀으로 옮기기]를 쓰세요.")
                act = one(con, """SELECT b.work_date, c.name FROM active_assignment a JOIN assignment_batch b ON b.id=a.batch_id JOIN crew c ON c.id=b.crew_id
                    WHERE a.request_id=?""", (rid,))
                if act:
                    raise UserError(f"이미 다른 배정({act['work_date']} {act['name']})에 들어 있는 접수입니다. 먼저 그 배정에서 빼 주세요.")
            else:
                if not src:
                    raise UserError("이날 배정에 없는 접수입니다.")
                if src == to:
                    raise UserError("같은 팀입니다. 순서 바꾸기를 쓰세요.")
                changes[src] = [x for x in stops_of(src) if x["request_id"] != rid]
            lst = changes.get(to) or stops_of(to)
            place(lst, {"request_id": rid, "eta": str(data.get("eta") or "")[:5]}, data.get("position"))
            changes[to] = lst
        elif op == "remove":
            src = where(rid)
            if not src:
                raise UserError("이날 배정에 없는 접수입니다.")
            changes[src] = [x for x in stops_of(src) if x["request_id"] != rid]
        else:  # reorder
            cid = data.get("crew_id")
            if cid not in cur:
                raise UserError("순서를 바꿀 팀의 배정이 없습니다.")
            order = [x for x in (data.get("order") or []) if isinstance(x, str)]
            old = stops_of(cid)
            if sorted(order) != sorted(x["request_id"] for x in old) or len(set(order)) != len(order):
                raise UserError("순서 목록이 지금 배정과 다릅니다(빠지거나 더해진 접수). 다시 열어 확인하세요.")
            by = {x["request_id"]: x for x in old}
            changes[cid] = [by[i] for i in order]
        out, old_ids = [], []
        for cid, lst in changes.items():
            if cid in cur:
                old_ids.append(cur[cid]["batch_id"])
            out.append(_write_batch(con, cid, work_date, lst, crews[cid]["name"], {"edit": op, **({"reason": reason} if reason else {}),
                                                                                   **({"request": rid} if rid else {})}, action="EDIT"))
        _release_dropped(con, old_ids)
    return {"batches": out, "resend_needed": [b["crew_id"] for b in out]}


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
    us = get_setting(con, "unload_site")  # 반입지 좌표를 함께 보낸다(휴대폰 길안내용, 설정에 있을 때만)
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
                          "unit": l["unit"], "unit_label": l["unit_label"], "qty": l["qty_confirmed"], "qty_received": l["qty_received"],
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
        # "levy_no": 부과번호 → "lv". 감면액 끝수 단위(접수에 저장된 값, 10원일 때만) → "ru"
        red_ = {"reduction": {"rate": int(r["reduction_rate"] or 0), "label": r["reduction_label"] or "",
                              **({"round_unit": 10} if request_round_unit(r) == 10 else {})}} if r.get("reduction_code") else {}
        stops.append({**extra_u, **red_, "seq": s["seq"], "request_id": r["id"], "receipt_no": r["receipt_no"], "req_ver": r["req_ver"], "dong": r["dong"] or "", "levy_no": r.get("levy_no") or "",
                      "name": r["name"], "phone": r["phone"], "address": r["address"], "spot_note": r["spot_note"], "memo": r["memo"],
                      "lat": r["lat"], "lon": r["lon"], "eta": s["eta"] or (r["appt_start"] or ""),
                      "appt": [r["appt_start"], r["appt_end"]] if r["appt_start"] else None,
                      "revisit": revisit, "assign_ver": b["assign_ver"], "items": items})
    payload = {"schema": "dg.daylist/1", "work_date": b["work_date"], "crew": b["crew_name"], "assign_ver": b["assign_ver"],
               "fee_table": fee_table_for_phone(con), "stops": stops}
    if b.get("device_id"):  # 휴대폰이 지워도 되는 것: PC가 이미 받은 기록 순번·회수한 사진 (dgproto가 서명해 싣는다)
        payload["rel"] = release_info(con, b["device_id"])
    return payload, b


def release_info(con, device_id: str) -> dict:
    """휴대폰에 알려 줄 '안전하게 받은 것': seq = 이 기기 기록 1..seq를 PC가 빠짐없이 저장한(반영·충돌·거부 모두) 마지막 순번,
    ph = 이 기기에서 회수했지만 아직 휴대폰에 알리지 않은(수신 확인된 목록에 실리지 않은) 사진 ID."""
    # 빠짐없이 받은 마지막 번호: 1부터 이어진 구간의 끝(중간 번호가 빠졌으면 그 앞에서 멈춤 — 휴대폰은 이 번호까지 지운다)
    seq = con.execute("""SELECT MIN(e.device_seq) FROM result_event e WHERE e.device_id=? AND e.device_seq >= 1
        AND NOT EXISTS (SELECT 1 FROM result_event x WHERE x.device_id=e.device_id AND x.device_seq=e.device_seq+1)""", (device_id,)).fetchone()[0]
    if not con.execute("SELECT 1 FROM result_event WHERE device_id=? AND device_seq=1 LIMIT 1", (device_id,)).fetchone():
        seq = 0
    ph = [r[0] for r in con.execute("""SELECT photo_id FROM photo WHERE device_id=? AND recovery_status='RECOVERED' AND rel_acked_at IS NULL
        ORDER BY recovered_at LIMIT 500""", (device_id,)).fetchall()]
    return {"seq": max(0, seq or 0), "ph": ph}


def make_bundle(con, batch_id: str | None = None, payload: dict | None = None, device_id: str | None = None,
                chunk: int = 700) -> dict:
    if batch_id:
        payload, b = build_daylist(con, batch_id)
        device_id = b["device_id"]
        if not device_id:
            raise UserError("이 팀에 연결된 휴대폰이 없습니다. 설정에서 기기를 연결하세요.")
    rel = payload.get("rel")
    if payload.get("schema") == "dg.daylist/1":
        payload = P.compact_daylist(payload, FEE_ROWS)  # QR 장수를 줄이는 압축 형식
    dev = one(con, "SELECT * FROM device WHERE id=?", (device_id,))
    if not dev or dev["status"] != "ACTIVE":
        raise UserError("휴대폰이 등록되어 있지 않거나 사용 중지 상태입니다.")
    if not dev["confirmed_at"]:
        raise UserError("지문 확인 전인 휴대폰입니다. 설정 → 기기에서 두 화면의 지문이 같은지 보고 [지문 같음 · 사용 시작]을 누르세요.")
    bundle_id = uuid.uuid4().bytes
    sign = pc_key(con, "sign")
    head = P.Header(P.TYPE_DAYLIST, bundle_id, uuid.UUID(device_id).bytes, payload["assign_ver"], 1, P.key_id(P.pub_raw(sign)))
    env, code = P.seal(payload, head, P.pub_from_raw(dev["kex_pub"]), sign)
    frames = P.split_frames(env, bundle_id, chunk)
    bid = str(uuid.UUID(bytes=bundle_id))
    with tx(con):
        con.execute("""INSERT INTO transfer_bundle (id, device_id, batch_id, work_date, assign_ver, chunk_count, envelope_sha256, receipt_code,
            frames_json, displayed_at, receipt_confirmed_at, rel_seq, rel_json) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?)""",
                    (bid, device_id, batch_id, payload["work_date"], payload["assign_ver"], len(frames),
                     hashlib.sha256(env).hexdigest(), code, json.dumps(frames), now(),
                     (rel or {}).get("seq"), json.dumps(rel) if rel else None))
        audit(con, "bundle", bid, "DISPLAY", {"batch": batch_id, "frames": len(frames)})
    return {"bundle_id": bid, "frames": frames, "bytes": len(env), "count": len(payload.get("s") or payload.get("stops") or []),
            "work_date": payload["work_date"], "assign_ver": payload["assign_ver"]}


def confirm_receipt(con, bundle: str, code: str) -> str:
    r = rows(con, "SELECT id, receipt_code, rel_json FROM transfer_bundle WHERE id LIKE ?", (bundle + "%",))
    if len(r) != 1:
        raise UserError("전송 묶음을 찾을 수 없습니다.")
    c_ = re.sub(r"[\s-]", "", code or "").upper()
    if c_.startswith("R") and len(c_) == len(r[0]["receipt_code"].replace("-", "")) + 1:  # 휴대폰 화면의 앞 글자 'R-'(수신 코드)를 붙여 넣어도
        c_ = c_[1:]
    if c_ != r[0]["receipt_code"].replace("-", "").upper():
        raise UserError("수신 코드가 맞지 않습니다. 수신이 확인되지 않았습니다.")
    with tx(con):
        con.execute("UPDATE transfer_bundle SET receipt_confirmed_at=?, frames_json=NULL WHERE id=?", (now(), r[0]["id"]))  # 받은 것이 확인되면 QR 조각(암호문)은 더 둘 까닭이 없다
        rel = json.loads(r[0]["rel_json"] or "{}")
        for ch in _chunks(rel.get("ph") or []):  # 이 목록으로 휴대폰에 '회수 완료'를 알린 사진
            con.execute(f"UPDATE photo SET rel_acked_at=? WHERE rel_acked_at IS NULL AND photo_id IN ({','.join('?' * len(ch))})", [now(), *ch])
        audit(con, "bundle", r[0]["id"], "RECEIPT", None)
    return r[0]["id"]


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


def load_check(con, start: str = "", end: str = "") -> dict:
    """적재 추정 ↔ 하차 기록(반입지 계근표·부피) 비교. 날마다: 그날 예정이던 수거 완료·일부 수거 건의 '수거한 수량'으로 낸 추정치와
    그날 하차 기록 합. 재방문 건은 전에 수거한 수량까지 들어가는 근사치다(보정값을 고칠지 판단하는 참고용). 개인정보 없음."""
    from dgpc import routing  # OR-Tools 를 불러오는 무거운 모듈: 쓸 때만
    if not end:
        end = today_kst()
    if not start:
        start = (dt.date.fromisoformat(end) - dt.timedelta(days=29)).isoformat()
    ov = volume_overrides(con)
    ul = rows(con, """SELECT work_date, SUM(COALESCE(volume_m3,0)) AS m3, SUM(COALESCE(weight_kg,0)) AS kg, COUNT(*) AS n,
        SUM(weight_kg IS NOT NULL) AS kn, SUM(volume_m3 IS NOT NULL) AS vn FROM unload_log
        WHERE void_reason IS NULL AND work_date BETWEEN ? AND ? GROUP BY work_date ORDER BY work_date""", (start, end))
    days = []
    for u in ul:
        em3 = ekg = 0.0
        for r in rows(con, "SELECT * FROM request WHERE planned_date=? AND status IN ('DONE','PARTIAL')", (u["work_date"],)):
            for l in derive(con, r)["lines"]:
                it = {"name": l["name"], "spec": l["spec"], "qty": l["qty_collected"], "fee_rate_id": l.get("fee_rate_id")}
                if l["qty_collected"]:
                    em3 += routing.volume_of(it, ov); ekg += routing.weight_of(it)
        days.append({"date": u["work_date"], "unloads": u["n"], "est_m3": round(em3, 1), "est_kg": int(ekg),
                     "rec_m3": round(u["m3"], 1) if u["vn"] else None, "rec_kg": int(u["kg"]) if u["kn"] else None})
    def ratio(rec, est):
        pairs = [(d[rec], d[est]) for d in days if d[rec] is not None and d[est]]
        return round(100 * sum(a for a, _ in pairs) / sum(b for _, b in pairs)) if pairs else None
    return {"start": start, "end": end, "days": days, "m3_pct": ratio("rec_m3", "est_m3"), "kg_pct": ratio("rec_kg", "est_kg"),
            "est_m3": round(sum(d["est_m3"] for d in days), 1), "est_kg": sum(d["est_kg"] for d in days),
            "rec_m3": round(sum(d["rec_m3"] or 0 for d in days), 1), "rec_kg": sum(d["rec_kg"] or 0 for d in days)}


def dispatch_sheet(con, batch_id: str) -> dict:
    """인쇄용 일일 배차표(종이 예비본). 이름·연락처·주소가 들어가므로 업무 후 파쇄."""
    payload, b = build_daylist(con, batch_id)
    with tx(con):
        audit(con, "batch", batch_id, "PRINT", {"stops": len(payload["stops"])})
        _pv.record_access(con, "PRINT", "", len(payload["stops"]), f"배차표 {b['work_date']} {b['crew_name']}")
    for st in payload["stops"]:
        st["status"] = one(con, "SELECT status FROM request WHERE id=?", (st["request_id"],))["status"]
    return {"work_date": payload["work_date"], "crew": payload["crew"], "assign_ver": payload["assign_ver"], "fee_version": payload["fee_table"]["version_id"],
            "stops": payload["stops"], "printed_at": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")}
