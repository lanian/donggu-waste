"""접수: 등록·수정(버전 보존)·취소·검색·상세·민원 기록·일괄 처리·유사 품목 연결·안내 글·CSV 일괄 등록.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import uuid

from dgpc import store as _store, area, finance, geo
from dgpc.addresses import address_warnings
from dgpc.core import (
    ACTOR, KST, UserError, _after_close, _date_ok, _secs, audit, check_req_ver, close_info, get_setting, is_chosung, norm_addr, norm_phone, now, one,
    require_request_pii_writable, rows, safe_csv_writer, today_kst, tx,
)
from dgpc.derive import derive, payment_status, prefetch
from dgpc.fees import FEE_ROWS, FEE_TABLE, fee_rows, fee_state

CHANNELS = ("전화", "방문", "온라인", "단체(공동주택)", "현장", "기타")  # 접수 경로 (현장 = 수거 중 휴대폰으로 받은 접수)
# 사유 코드 (코드 + 직접 입력 글). 기타(OTHER)는 글이 꼭 있어야 한다.
CANCEL_CODES = {"OUT_OF_AREA": "관할 밖", "RESIDENT": "주민 취소", "DUPLICATE": "중복 접수", "ITEM_REFUSED": "품목 불가", "OTHER": "기타"}
POSTPONE_CODES = {"RESIDENT": "주민 요청", "OVER_CAPACITY": "수거 한도 초과", "NO_ROUTE": "배정 불가(노선·시간)", "REVISIT": "재방문(미수거·보류)",
                  "HOLIDAY": "휴무일", "OTHER": "기타"}
CONTACT_KINDS = ("문의", "독촉", "변경요청", "불만")


def build_items(items: list[dict], con=None) -> list[dict]:
    """화면에서 받은 품목을 공식 수수료표 기준으로 다시 계산한다(화면 값의 단가를 믿지 않음)."""
    rows_now = fee_rows(con) if con is not None else FEE_ROWS
    ver = fee_state(con)["version_id"] if con is not None else FEE_TABLE["version_id"]
    out = []
    for it in items:
        try:
            qty = int(str(it.get("qty") or 0).strip())
        except ValueError:
            raise UserError("품목 수량은 1 이상의 정수로 적어 주세요.")
        if qty <= 0 or qty > 9999:
            raise UserError("품목 수량은 1~9999 사이의 정수여야 합니다.")
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
    if d["planned_date"] and not _date_ok(d["planned_date"]):
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
    # 그날 가장 큰 번호 + 1 (건수 + 1은 단독 휴대폰에서 옮겨 온 번호와 겹칠 수 있음)
    prefix = f"DG-{day}-"
    n = (con.execute("SELECT MAX(CAST(substr(receipt_no, ?) AS INTEGER)) FROM request WHERE receipt_no LIKE ?", (len(prefix) + 1, prefix + "%")).fetchone()[0] or 0) + 1
    receipt = f"DG-{day}-{n:04d}"
    dcode = (one(con, "SELECT code FROM dong WHERE name=?", (dong,)) or {}).get("code") if dong else None
    levy = _store.next_levy_no(con, today_kst()[:4], dcode)
    red = finance.apply_reduction_fields(con, data) or {}
    con.execute("""INSERT INTO request (id, receipt_no, req_ver, name, phone, address, spot_note, memo, lat, lon, geo_source,
        planned_date, appt_start, appt_end, must_today, urgent, priority, status, received_at, updated_at, dong, channel, levy_no,
        reduction_code, reduction_rate, reduction_label, reduction_proof, reduction_checker, reduction_at, reduction_round_unit)
        VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'RECEIVED',?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, receipt, d["name"], d["phone"], d["address"], d["spot_note"], d["memo"], d["lat"], d["lon"],
                 (data.get("geo_source") if data.get("geo_source") in ("MANUAL", "ADDR") else "MANUAL") if d["lat"] is not None else None, d["planned_date"] or None, d["appt_start"] or None,
                 d["appt_end"] or None, d["must_today"], d["urgent"], d["priority"], ts, ts, dong, d["channel"], levy,
                 red.get("reduction_code"), red.get("reduction_rate"), red.get("reduction_label"), red.get("reduction_proof"), red.get("reduction_checker"), red.get("reduction_at"),
                 red.get("reduction_round_unit")))
    for it in items:
        _insert_item(con, rid, it)
    return rid, receipt


def create_request(con, data: dict) -> dict:
    d = validate(data)
    items = build_items(data["items"], con)
    dong, dwarn = _resolve_dong(con, data, d["address"])
    red = finance.apply_reduction_fields(con, data)
    with tx(con):
        rid, receipt = _insert_request(con, d, items, data, dong)
        audit(con, "request", rid, "CREATE", {"receipt_no": receipt, "channel": d["channel"], **({"dong": dong} if dong else {}),
                                              **({"reduction": red["reduction_code"], "rate": red["reduction_rate"], "proof": red["reduction_proof"]} if red else {}),
                                              **_secs("intake_secs", data.get("intake_secs"))})
    warn = address_warnings(con, d["address"]) + dwarn + area.capacity_warnings(con, d["planned_date"] or None, dong, rid, area.request_volume(con, rid))
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


def update_request(con, rid: str, data: dict, reason: str) -> dict:
    """접수 수정. data["req_ver"](화면이 연 버전)가 꼭 있어야 하고 지금 버전과 같아야 한다(다른 사람이 먼저 고쳤으면 StaleError).
    버전 확인·현장 결과가 있는 품목 잠금 확인은 쓰기 트랜잭션 안에서 한다."""
    cur = one(con, "SELECT * FROM request WHERE id=?", (rid,))
    if not cur:
        raise UserError("접수를 찾을 수 없습니다.")
    if cur["status"] == "CANCELLED":
        raise UserError("취소된 접수는 수정할 수 없습니다.")
    if not (reason or "").strip():
        raise UserError("수정 사유를 적어 주세요." + (" (마감한 날의 접수입니다)" if close_info(con, cur["planned_date"]) else ""))
    d = validate({"channel": cur["channel"], **data})
    items = build_items(data["items"], con)
    with tx(con):
        require_request_pii_writable(con, rid)
        check_req_ver(con, rid, data.get("req_ver"))
        cur = one(con, "SELECT * FROM request WHERE id=?", (rid,))
        if cur["status"] == "CANCELLED":
            raise UserError("취소된 접수는 수정할 수 없습니다.")
        dong, dwarn = _resolve_dong(con, data, d["address"]) if "dong" in data or d["address"] != cur["address"] else (cur["dong"], [])
        locked = locked_lines(con, rid)
        old_items = {i["line_id"]: i for i in rows(con, "SELECT * FROM request_item WHERE request_id=? AND removed=0", (rid,))}
        new_ids = {i["line_id"] for i in items}
        for lid in locked:
            if lid not in old_items:
                continue
            nw = next((i for i in items if i["line_id"] == lid), None)
            # 수량·삭제뿐 아니라 품목(수수료표 행·이름)도: 수거한 뒤 같은 줄의 단가가 다른 품목 것으로 바뀌지 않게
            if nw is None or nw["qty"] != old_items[lid]["qty"] or nw["fee_rate_id"] != old_items[lid]["fee_rate_id"]                     or (not nw["fee_rate_id"] and nw["name"] != old_items[lid]["name"]):
                raise UserError(f"현장 결과가 있는 품목({old_items[lid]['name']})은 바꾸거나 지울 수 없습니다.")
        flag = _after_close(con, rid)
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
                o = old_items[it["line_id"]]
                if o["fee_rate_id"] and o["fee_rate_id"] == it["fee_rate_id"] and o["price_status"] == "CONFIRMED":
                    # 이미 받은 품목은 접수 때 단가·수수료표 버전을 그대로 둔다(그 뒤 개정으로 바뀌지 않음). 수량만 바뀌면 같은 단가로 다시 셈
                    con.execute("UPDATE request_item SET qty=?, amount=? WHERE line_id=?", (it["qty"], o["unit_price"] * it["qty"], it["line_id"]))
                else:
                    # 품목을 바꾼 경우도 있으므로 이름·규격·단위까지 새 품목으로(전에는 단가만 바뀌고 이름은 옛 품목으로 남았다)
                    con.execute("""UPDATE request_item SET qty=?, amount=?, unit_price=?, price_status=?, fee_version_id=?, fee_rate_id=?, name=?, spec=?,
                        unit=?, unit_label=? WHERE line_id=?""",
                                (it["qty"], it["amount"], it["unit_price"], it["price_status"], it["fee_version_id"], it["fee_rate_id"], it["name"],
                                 it["spec"], it["unit"], it["unit_label"], it["line_id"]))
            else:
                _insert_item(con, rid, it)
        released = False
        if (d["planned_date"] or None) != cur["planned_date"]:  # 예정일을 바꾸면 옛 날짜 배정을 푼다(일괄 예정일 변경과 같게)
            released = bool(con.execute("DELETE FROM active_assignment WHERE request_id=?", (rid,)).rowcount)
            if released and cur["status"] == "ASSIGNED":
                con.execute("UPDATE request SET status='RECEIVED' WHERE id=?", (rid,))
        audit(con, "request", rid, "UPDATE", {"reason": reason.strip(), "from_ver": cur["req_ver"], **({"released": True} if released else {}), **flag})
    warn = address_warnings(con, d["address"]) + dwarn
    if (d["planned_date"] or None) != cur["planned_date"] or dong != cur["dong"]:
        warn += area.capacity_warnings(con, d["planned_date"] or None, dong, rid, area.request_volume(con, rid))
    return {"id": rid, "resend_needed": released or bool(one(con, "SELECT 1 AS x FROM active_assignment WHERE request_id=?", (rid,))),
            "warnings": warn, "after_close": bool(flag), "req_ver": cur["req_ver"] + 1}


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


def cancel_request(con, rid: str, reason: str = "", code: str | None = None, req_ver=None) -> dict:
    """접수 취소(사유 코드 + 직접 입력). 이미 납부한 금액이 있으면 환불 필요로 알려 준다(마감 수납 확인·'환불 필요' 목록).
    req_ver를 주면 화면이 연 버전과 같을 때만 취소한다(다르면 StaleError). 환불 건 맞추기는 같은 트랜잭션 안에서."""
    code, text, full = _reason(CANCEL_CODES, code, reason, "취소")
    with tx(con):
        if req_ver not in (None, ""):
            check_req_ver(con, rid, req_ver)
        res = _cancel(con, rid, code, text, full)
    return res


SORTABLE = {"receipt_no": "receipt_no", "planned_date": "COALESCE(planned_date,'9999')", "received_at": "received_at", "name": "name",
            "dong": "COALESCE(dong,'')", "status": "status", "channel": "channel", "address": "address"}
QUICK = ("ALL", "TODAY", "NOLOC", "FEE", "UNASSIGNED", "REFUND", "FIELD", "OVERDUE", "NODATE")
_NOT_FINAL = "status NOT IN ('DONE','PARTIAL','NOT_COLLECTED','ON_HOLD','CANCELLED')"


def _quick_sql(key: str) -> tuple[str, list]:
    """빠른 조회 조건(SQL). 오늘 날짜는 인자로."""
    t = today_kst()
    return {
        "TODAY": ("planned_date=?", [t]),
        "NOLOC": ("lat IS NULL", []),
        "FEE": ("fee_final IS NULL", []),
        "UNASSIGNED": ("status NOT IN ('DONE','CANCELLED') AND NOT EXISTS (SELECT 1 FROM active_assignment a WHERE a.request_id=request.id)", []),
        "REFUND": ("status='CANCELLED' AND pay_status='REFUND_NEEDED'", []),
        "FIELD": ("origin_device IS NOT NULL AND field_checked_at IS NULL", []),
        "OVERDUE": (f"planned_date < ? AND {_NOT_FINAL}", [t]),       # 지난 예정일인데 최종 결과 없음
        "NODATE": (f"planned_date IS NULL AND {_NOT_FINAL}", []),      # 예정일 없음
    }.get(key, ("1=1", []))


def _enrich(con, r: dict, pre: dict | None = None) -> dict:
    r["items"] = (pre["items"].get(r["id"]) if pre else None) or rows(con, "SELECT * FROM request_item WHERE request_id=? AND removed=0", (r["id"],))
    r["state"] = derive(con, r, pre)
    r["warnings"] = address_warnings(con, r["address"])
    r["assigned_to"] = (one(con, """SELECT c.name, b.work_date FROM active_assignment a JOIN assignment_batch b ON b.id=a.batch_id
        JOIN crew c ON c.id=b.crew_id WHERE a.request_id=?""", (r["id"],)) or None)
    return r


def _request_sql(f: dict, scope_ids=None) -> tuple[str, list]:
    """조회조건 → WHERE (검색어·초성·날짜 범위·상태·팀·동·경로·수납 상태·수거팀 범위). 빠른 조회는 따로."""
    sql, args = " WHERE 1=1", []
    toks = (f.get("q") or "").split()
    q = " ".join(t for t in toks if not is_chosung(t))
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
        sql += " AND active_crew_id=?"; args.append(f["crew"])
    if f.get("dong"):
        if f["dong"] == "-":
            sql += " AND dong IS NULL"
        else:
            sql += " AND dong=?"; args.append(f["dong"])
    if f.get("channel"):
        sql += " AND channel=?"; args.append(f["channel"])
    if f.get("pay"):
        sql += " AND pay_status=?"; args.append(f["pay"])
    if q:
        qp = norm_phone(q)
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql += " AND (name LIKE ? ESCAPE '\\' OR address LIKE ? ESCAPE '\\' OR receipt_no LIKE ? ESCAPE '\\'" + (" OR phone LIKE ?" if qp else "") + ")"
        args += [f"%{esc}%"] * 3 + ([f"%{qp}%"] if qp else [])
    for t in toks:  # 초성 검색: 이름·주소의 초성 (예: ㄱㅅㅎ → 가상하나) — 저장한 초성 열(chosung)
        if is_chosung(t):
            sql += " AND instr(COALESCE(chosung,''), ?) > 0"; args.append(t)
    if scope_ids is not None:
        sql += " AND id IN (SELECT value FROM json_each(?))"; args.append(json.dumps(sorted(scope_ids)))
    return sql, args


def search_requests(con, f: dict | None = None, scope_ids: set[str] | None = None, with_counts: bool = True) -> dict:
    """접수 목록 조회(서버 쪽 쪽 나눔, SQL로 거르고 세고 자른다 — 접수마다 계산하지 않음). f: q, date, date_field(planned|received), date_from, date_to,
    status, crew, dong, channel, pay(수납 상태), quick(ALL·TODAY·NOLOC·FEE·UNASSIGNED·REFUND·FIELD·OVERDUE·NODATE), sort, dir, page(1부터), size.
    돌려줌: rows(이 쪽), total(조건에 맞는 전체), counts(빠른 조회별 건수)."""
    f = dict(f or {})
    quick = f.get("quick") or "ALL"
    if quick not in QUICK:
        raise UserError("빠른 조회 값이 올바르지 않습니다.")
    where, args = _request_sql(f, scope_ids)
    counts = {}
    if with_counts:
        parts, cargs = [], []
        for k in QUICK[1:]:
            qs, qa = _quick_sql(k)
            parts.append(f"COALESCE(SUM(CASE WHEN {qs} THEN 1 ELSE 0 END),0)")
            cargs += qa
        r = con.execute(f"SELECT COUNT(*), {', '.join(parts)} FROM request" + where, cargs + args).fetchone()
        counts = {"ALL": r[0], **{k: r[i + 1] for i, k in enumerate(QUICK[1:])}}
    if quick != "ALL":
        qs, qa = _quick_sql(quick)
        where += f" AND ({qs})"; args = args + qa
    if with_counts and quick == "ALL":
        total = counts["ALL"]
    else:
        total = con.execute("SELECT COUNT(*) FROM request" + where, args).fetchone()[0]
        counts.setdefault("ALL", total if quick == "ALL" else con.execute("SELECT COUNT(*) FROM request" + _request_sql(f, scope_ids)[0], _request_sql(f, scope_ids)[1]).fetchone()[0])
    key = SORTABLE.get(f.get("sort") or "", None)
    desc = (f.get("dir") or "").lower() == "desc"
    order = f" ORDER BY {key} {'DESC' if desc else 'ASC'}, received_at DESC" if key else " ORDER BY COALESCE(planned_date,'9999'), received_at DESC"
    try:
        size = max(1, min(500, int(f.get("size") or 50)))
        page_ = int(f.get("page") or 1)
    except (TypeError, ValueError):
        raise UserError("쪽 번호·쪽 크기는 숫자입니다.")
    pages = max(1, -(-total // size))
    page = max(1, min(pages, page_))
    part = rows(con, "SELECT * FROM request" + where + order + " LIMIT ? OFFSET ?", args + [size, (page - 1) * size])
    pre = prefetch(con, [r["id"] for r in part], ("items", "events", "links"))
    part = [_enrich(con, r, pre) for r in part]
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
    from dgpc.pc_collection import preview as collection_preview
    r['collection'] = collection_preview(con, r)
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
        require_request_pii_writable(con, rid)
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
    if not _date_ok(to or ""):
        raise UserError("바꿀 수거 예정일을 골라 주세요.")
    code, text, full = _reason(POSTPONE_CODES, code, text, "예정일 변경")
    ids = _ids(con, ids)
    changed = 0
    with tx(con):
        for rid in ids:
            changed += bool(_reschedule(con, rid, to, full, code))
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
    return res


def set_requests_dong(con, ids, dong: str, reason: str = "") -> dict:
    """동 지정(여러 건). 마감한 날의 접수가 섞여 있으면 사유가 필요하다."""
    dong = (dong or "").strip() or None
    if dong and not area.get_dong(con, dong):
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


# ------------------------------------------------------------------ 접수 일괄 등록 (CSV·엑셀에서 저장한 CSV)
IMPORT_COLS = ["이름", "연락처", "배출 주소", "상세 위치", "수거 예정일", "행정동", "접수 경로", "메모"] + [f"{k}{i}" for i in range(1, 6) for k in ("품목", "규격", "수량")]


def import_template() -> bytes:
    buf = io.StringIO()
    w = safe_csv_writer(buf)
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
        con.execute("UPDATE request SET updated_at=? WHERE id=?", (now(), rid))  # 목록 전송 뒤 단가가 정해지면 '재전송 필요'
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


# ------------------------------------------------------------------ 주민 안내 문구 (문자·전화 안내용, 이름 없음)
def notice_text(con, rid: str) -> str:
    """주민에게 보낼 안내 글(문자·전화 안내 원고): 접수번호·배출일·부과 금액·납부 방법/기한·배출 안내·문의처. 이름·연락처·주소는 넣지 않는다."""
    r = one(con, "SELECT * FROM request WHERE id=?", (rid,))
    if not r:
        raise UserError("접수를 찾을 수 없습니다.")
    cfg = {k: get_setting(con, k) for k in ("jurisdiction", "payment_guide", "disposal_guide", "contact_info")}
    ps = payment_status(con, rid, r=r)
    won = lambda v: "금액 확인 중(확정되면 다시 안내)" if v is None else f"{v:,}원"
    appt = f" {r['appt_start']}~{r['appt_end'] or ''}" if r["appt_start"] else ""
    lines = [f"[{cfg['jurisdiction'] or '동구'} 대형폐기물 배출 안내]",
             f"접수번호: {r['receipt_no']}" + (f" (부과번호 {r['levy_no']})" if r.get("levy_no") else ""),
             f"배출일: {(r['planned_date'] or '미정(정해지면 안내)') + appt}"]
    if r["status"] == "CANCELLED":
        lines.append("이 접수는 취소되었습니다." + (" 납부하신 금액은 환불 절차로 안내합니다." if ps["paid"] > 0 else ""))
    else:
        lines.append(f"부과 금액: {won(ps['levied'])}" + (f" (감면 {ps['reduction_rate']}% 적용)" if ps["reduction_rate"] else ""))
        if ps["paid"]:
            lines.append(f"납부 확인: {ps['paid']:,}원")
        lines.append(f"납부 방법·기한: {cfg['payment_guide']}")
        lines.append(f"배출 안내: {cfg['disposal_guide']}")
    lines.append(f"문의: {cfg['contact_info']}")
    return "\n".join(lines) + "\n"
