"""숫자 배출표 원장: 입고·기관 배부·접수 사용·무효와 변경 이력.

접수번호·현장 스티커 확인·수납 기록과 별도로 관리한다. 번호 표기는
문자열로 보존하고 앞자리 0을 뺀 키로 중복을 막는다. 주민 정보는 복사하지 않는다.
"""
from __future__ import annotations

import json
import re

from dgpc import core as C
from dgpc.collection import event_order as _usage_event_order
from dgpc.derive import FINAL

STATUSES = ("AVAILABLE", "DISTRIBUTED", "USED", "VOID")
PAGE_SIZE = 50
MAX_RANGE = 5000


def _collection_usage(items: list[dict], events: list[dict]) -> dict:
    """마지막 확정 시점의 누적 수거분. ITEM_RESULT는 증분이 아닌 스냅샷이다."""
    by = {i["line_id"]: {"line_id": i["line_id"], "name": i["name"], "spec": i["spec"],
                         "unit_label": i["unit_label"], "used": 0} for i in items}
    snapshot = {"used": 0, "at": None, "lines": []}
    for event in _usage_event_order(events):
        try:
            body = json.loads(event["body_json"])
        except (ValueError, TypeError):
            continue
        if not isinstance(body, dict):
            continue
        kind, line = event["kind"], body.get("line_id")
        if kind == "FIELD_ITEM_ADD" and isinstance(line, str) and line and line not in by:
            by[line] = {"line_id": line, "name": str(body.get("name") or "현장 추가 품목")[:60],
                        "spec": str(body.get("spec") or "")[:60], "unit_label": str(body.get("unit_label") or "개")[:10], "used": 0}
        elif kind == "ITEM_RESULT" and isinstance(line, str) and line in by:
            by[line]["used"] = min(C._int_clamp(body.get("qty_confirmed")), C._int_clamp(body.get("qty_collected")))
        elif kind == "ATTEMPT" and isinstance(body.get("outcome"), str) and body["outcome"] in FINAL:
            lines = [dict(value) for value in by.values() if value["used"] > 0]
            snapshot = {"used": sum(value["used"] for value in lines), "at": event["device_ts"], "lines": lines}
    return snapshot


def usage_page(con, filters: dict) -> dict:
    """실제 수거 1개=배출표 1장, 전체 기간의 접수별 누적 집계. 원장·수납에는 쓰지 않는다."""
    query, dong, page = filters.get("q", ""), filters.get("dong", ""), filters.get("page", 1)
    if not isinstance(query, str) or len(query) > 60:
        raise C.UserError("접수번호 조회 조건을 60자 이하로 적어 주세요.")
    if not isinstance(dong, str) or len(dong) > 100:
        raise C.UserError("행정동 조회 조건을 100자 이하로 적어 주세요.")
    if isinstance(page, bool) or not re.fullmatch(r"[0-9]{1,9}", str(page)):
        raise C.UserError("조회할 쪽 번호를 확인해 주세요.")
    where = ["EXISTS (SELECT 1 FROM result_event e WHERE e.request_id=r.id AND e.kind='ATTEMPT' AND e.apply_status='APPLIED')"]
    args = []
    for condition, value in (("instr(r.receipt_no,?)>0", query.strip()), ("r.dong=?", dong.strip())):
        if value:
            where.append(condition)
            args.append(value)
    # 취소·재배정·개인정보 파기 뒤에도 이미 수거한 배출표 사용 이력은 남는다.
    reqs = C.rows(con, "SELECT r.id,r.receipt_no,r.dong FROM request r WHERE " + " AND ".join(where), args)
    collected = []
    for chunk in C._chunks(reqs, 500):
        ids = [r["id"] for r in chunk]
        marks = ",".join("?" for _ in ids)
        items, events = {rid: [] for rid in ids}, {rid: [] for rid in ids}
        # removed 품목도 과거 수거분이 있으면 포함한다. 이름·전화·주소는 읽지 않는다.
        for item in C.rows(con, f"SELECT request_id,line_id,name,spec,unit_label FROM request_item WHERE request_id IN ({marks}) ORDER BY rowid", ids):
            items[item.pop("request_id")].append(item)
        for event in C.rows(con, f"""SELECT e.request_id,e.kind,e.body_json,e.device_ts,e.device_id,e.gen,e.device_seq
                FROM result_event e JOIN result_import i ON i.id=e.import_id
                WHERE e.request_id IN ({marks}) AND e.apply_status='APPLIED' AND e.kind IN ('FIELD_ITEM_ADD','ITEM_RESULT','ATTEMPT')
                ORDER BY i.imported_at,i.rowid,e.device_seq,e.rowid""", ids):
            events[event.pop("request_id")].append(event)
        for r in chunk:
            usage = _collection_usage(items[r["id"]], events[r["id"]])
            if usage["used"]:
                collected.append({**r, **usage})
    collected.sort(key=lambda r: (r["at"] or "", r["receipt_no"], r["id"]), reverse=True)
    total = len(collected)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, int(page)), pages)
    return {"rows": collected[(page - 1) * PAGE_SIZE:page * PAGE_SIZE], "total": total, "page": page, "pages": pages, "size": PAGE_SIZE,
            "totals": {"used": sum(r["used"] for r in collected), "requests": total},
            "source_note": "전체 기간의 마지막 확정 수거 수량을 접수별로 누적 집계합니다. 수거 1개당 1장이며, 미수거·확정 전 입력·미반영 충돌은 제외합니다."}


def _number(value) -> tuple[str, str]:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,30}", value):
        raise C.UserError("배출표 번호를 1~30자리 숫자 문자열로 적어 주세요 (앞자리 0 유지).")
    return value, value.lstrip("0") or "0"


def _range(start, end) -> list[tuple[str, str]]:
    first, _ = _number(start)
    last, _ = _number(end)
    if len(first) != len(last):
        raise C.UserError("시작 번호와 끝 번호의 자릿수를 같게 적어 주세요.")
    lo, hi = int(first), int(last)
    if hi < lo:
        raise C.UserError("끝 번호는 시작 번호 이상이어야 합니다.")
    if hi - lo + 1 > MAX_RANGE:
        raise C.UserError("한 번에 5,000장 이하의 구간을 처리해 주세요.")
    return [(str(value).zfill(len(first)), str(value)) for value in range(lo, hi + 1)]


def _writable(con):
    if C.is_readonly(con):
        raise C.UserError("조회 전용 연결에서는 배출표를 변경할 수 없습니다.")


def _label(con, key: str) -> dict:
    label = C.one(con, "SELECT * FROM numbered_label WHERE number_key=?", (key,))
    if not label:
        raise C.UserError("입고된 배출표 번호를 찾을 수 없습니다.")
    return label


def _record(con, label: dict, action: str, previous: str | None, at: str, reason: str | None = None, *, extra: dict | None = None, event_line_id: str | None = None):
    detail = {"number": label["number"], "from": previous, "to": label["status"]}
    if label["request_id"]:
        detail["request"] = label["request_id"]
    if label["line_id"]:
        detail["line"] = label["line_id"]
    if reason:
        detail["reason"] = reason
    if extra:
        detail.update(extra)
    if event_line_id:
        detail["line"] = event_line_id
    # 자유 사유는 audit_note에만 남긴다. 동일 쓰기 트랜잭션 안이므로 방금 쓴 감사 행과 안전하게 연결된다.
    C.audit(con, "numbered_label", label["number_key"], action, detail)
    audit_id = con.execute("SELECT id FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    con.execute("""INSERT INTO numbered_label_event
        (number_key, action, from_status, to_status, request_id, line_id, at, actor, audit_id)
        VALUES (?,?,?,?,?,?,?,?,?)""", (label["number_key"], action, previous, label["status"],
                                         label["request_id"], event_line_id or label["line_id"], at, C.ACTOR.get(), audit_id))


def _stock_dong(con, dong_id=None, destination=""):
    if dong_id not in (None, ""):
        if not isinstance(dong_id, str):
            raise C.UserError("재고를 관리할 행정동을 골라 주세요.")
        dong = C.one(con, "SELECT id,name FROM dong WHERE id=?", (dong_id,))
        if not dong:
            raise C.UserError("재고를 관리할 행정동을 찾지 못했습니다.")
        return dong
    return C.one(con, "SELECT id,name FROM dong WHERE name=?", (destination,)) if destination else None


def _stock_at(day=None):
    if day in (None, ""):
        return C.now()
    if not isinstance(day, str) or not C._date_ok(day) or not "2020-01-01" <= day <= C.today_kst():
        raise C.UserError("수령·배부일을 2020년 이후부터 오늘까지의 날짜로 골라 주세요.")
    return C._kst_range(day, day)[0]


def _sync_stock(con, *, dong_id=None, request_ids=None):
    from dgpc import label_auto
    return label_auto.sync(con, request_ids, dong_id=dong_id)


def receive_range(con, start, end, *, dong_id=None, received_on=None) -> dict:
    """숫자 구간 입고. 별표기 중복을 포함한 한 장의 오류도 전체 입고를 되돌린다."""
    _writable(con)
    numbers = _range(start, end)
    dong, at = _stock_dong(con, dong_id), _stock_at(received_on)
    keys = json.dumps([key for _, key in numbers])
    with C.tx(con):
        if C.one(con, "SELECT number FROM numbered_label WHERE number_key IN (SELECT value FROM json_each(?)) LIMIT 1", (keys,)):
            raise C.UserError("이미 입고된 번호와 겹칩니다 (앞자리 0이 다른 같은 번호 포함).")
        for number, key in numbers:
            con.execute("""INSERT INTO numbered_label
                (number_key, number, status, destination, received_at, updated_at)
                VALUES (?,?,'AVAILABLE','',?,?)""", (key, number, at, at))
            _record(con, _label(con, key), "RECEIVE", None, at)
            if dong:
                con.execute("""UPDATE numbered_label SET status='DISTRIBUTED',destination=?,stock_dong_id=?,distributed_at=?
                    WHERE number_key=?""", (dong["name"], dong["id"], at, key))
                _record(con, _label(con, key), "DISTRIBUTE", "AVAILABLE", at, extra={"stock_dong_id": dong["id"]})
        if dong:
            _sync_stock(con, dong_id=dong["id"])
    return {"count": len(numbers), "numbers": [number for number, _ in numbers]}


def distribute_range(con, start, end, destination, *, dong_id=None, distributed_on=None) -> dict:
    """사용 가능한 번호 구간을 기관에 배부한다. 수납·접수 상태는 바꾸지 않는다."""
    _writable(con)
    numbers = _range(start, end)
    if not isinstance(destination, str) or not 1 <= len(destination.strip()) <= 100:
        raise C.UserError("배부처 기관명을 1~100자로 적어 주세요.")
    destination = destination.strip()
    dong = _stock_dong(con, dong_id, destination)
    if dong:
        destination = dong["name"]
    at = _stock_at(distributed_on)
    keys = json.dumps([key for _, key in numbers])
    with C.tx(con):
        labels = C.rows(con, "SELECT * FROM numbered_label WHERE number_key IN (SELECT value FROM json_each(?))", (keys,))
        if len(labels) != len(numbers):
            raise C.UserError("구간에 입고되지 않은 번호가 있습니다.")
        if any(label["status"] != "AVAILABLE" for label in labels):
            raise C.UserError("배부할 구간의 모든 번호가 사용 가능 상태여야 합니다.")
        if any((C._kst_date(label["received_at"]) or "9999") > C._kst_date(at) for label in labels):
            raise C.UserError("배부일은 입고일보다 빠를 수 없습니다.")
        for label in labels:
            con.execute("""UPDATE numbered_label SET status='DISTRIBUTED', destination=?, stock_dong_id=?, distributed_at=?, updated_at=?
                WHERE number_key=?""", (destination, dong["id"] if dong else None, at, C.now(), label["number_key"]))
            _record(con, _label(con, label["number_key"]), "DISTRIBUTE", "AVAILABLE", at, extra={"stock_dong_id": dong["id"] if dong else None})
        if dong:
            _sync_stock(con, dong_id=dong["id"])
    return {"count": len(numbers)}


def assign_stock_dong(con, start, end, dong_id, received_on=None) -> dict:
    """기존 공용·기관 재고의 동과 실제 수령일을 명시한다. 수동 사용 연결은 보존한다."""
    _writable(con)
    numbers, dong, at = _range(start, end), _stock_dong(con, dong_id), _stock_at(received_on)
    if not dong:
        raise C.UserError("재고를 관리할 행정동을 골라 주세요.")
    with C.tx(con):
        labels = C.rows(con, "SELECT * FROM numbered_label WHERE number_key IN (SELECT value FROM json_each(?))", (json.dumps([k for _, k in numbers]),))
        if len(labels) != len(numbers):
            raise C.UserError("구간에 입고되지 않은 번호가 있습니다.")
        if any((C._kst_date(label["received_at"]) or "9999") > C._kst_date(at) for label in labels):
            raise C.UserError("동 수령일은 원장 입고일보다 빠를 수 없습니다.")
        for label in labels:
            if label["stock_dong_id"] and label["stock_dong_id"] != dong["id"]:
                raise C.UserError("다른 동에 지정된 재고는 이 구간에서 옮길 수 없습니다.")
            if label["request_id"]:
                req = C.one(con, "SELECT dong FROM request WHERE id=?", (label["request_id"],))
                if req and req["dong"] and req["dong"] != dong["name"]:
                    raise C.UserError("다른 동의 접수에 사용한 번호가 포함되어 있습니다.")
        for label in labels:
            status = "DISTRIBUTED" if label["status"] == "AVAILABLE" else label["status"]
            if (label["stock_dong_id"], label["destination"], label["distributed_at"], label["status"]) == (dong["id"], dong["name"], at, status):
                continue
            con.execute("UPDATE numbered_label SET stock_dong_id=?,destination=?,distributed_at=?,status=?,updated_at=? WHERE number_key=?",
                        (dong["id"], dong["name"], at, status, C.now(), label["number_key"]))
            _record(con, _label(con, label["number_key"]), "DISTRIBUTE", label["status"], at,
                    extra={"stock_dong_id": dong["id"], "stock_only": True})
        _sync_stock(con, dong_id=dong["id"])
    return {"count": len(numbers)}


def use_number(con, number, request_id, line_id=None) -> dict:
    """유효한 접수(선택적으로 품목)에 사용한다. 같은 대상 재요청은 기록을 더 쌓지 않는다."""
    _writable(con)
    _, key = _number(number)
    if not isinstance(request_id, str) or not request_id.strip():
        raise C.UserError("사용할 접수를 골라 주세요.")
    if line_id in (None, ""):
        line_id = None
    elif not isinstance(line_id, str):
        raise C.UserError("사용할 품목을 다시 골라 주세요.")
    with C.tx(con):
        label = _label(con, key)
        if label["status"] == "VOID":
            raise C.UserError("무효 처리된 배출표는 사용할 수 없습니다.")
        request = C.one(con, "SELECT status, pii_purged_at,dong FROM request WHERE id=?", (request_id,))
        if not request:
            raise C.UserError("접수를 찾을 수 없습니다.")
        if request["status"] == "CANCELLED" or request["pii_purged_at"]:
            raise C.UserError("취소되었거나 개인정보가 파기된 접수에는 사용할 수 없습니다.")
        if label["stock_dong_id"] and request["dong"] != _stock_dong(con, label["stock_dong_id"])["name"]:
            raise C.UserError("다른 동의 재고는 이 접수에 사용할 수 없습니다.")
        if line_id and not C.one(con, "SELECT 1 AS x FROM request_item WHERE line_id=? AND request_id=? AND removed=0", (line_id, request_id)):
            raise C.UserError("이 접수의 유효한 품목을 골라 주세요.")
        if label["status"] == "USED":
            if (label["request_id"], label["line_id"]) == (request_id, line_id):
                return {"label": label, "unchanged": True}
            raise C.UserError("이미 다른 접수 또는 품목에 사용한 배출표입니다.")
        at = C.now()
        con.execute("""UPDATE numbered_label SET status='USED', request_id=?, line_id=?, used_at=?, updated_at=?
            WHERE number_key=?""", (request_id, line_id, at, at, key))
        _record(con, {**label, "status": "USED", "request_id": request_id, "line_id": line_id}, "USE", label["status"], at)
        if label["stock_dong_id"]:
            _sync_stock(con, request_ids=[request_id])
        result = _label(con, key)
    return {"label": result, "unchanged": False}


def void_number(con, number, reason) -> dict:
    """배출표를 무효 처리하고 사유를 남긴다. 무효 상태는 되돌리지 않는다."""
    _writable(con)
    _, key = _number(number)
    if not isinstance(reason, str) or not reason.strip():
        raise C.UserError("무효 처리 사유를 적어 주세요.")
    with C.tx(con):
        label = _label(con, key)
        if label["status"] == "VOID":
            raise C.UserError("이미 무효 처리된 배출표입니다.")
        at = C.now()
        con.execute("UPDATE numbered_label SET status='VOID', voided_at=?, updated_at=? WHERE number_key=?", (at, at, key))
        _record(con, {**label, "status": "VOID"}, "VOID", label["status"], at, reason.strip()[:300])
        if label["request_id"] and label["stock_dong_id"]:
            _sync_stock(con, request_ids=[label["request_id"]])
        result = _label(con, key)
    return {"label": result}


def label_page(con, f: dict | None = None) -> dict:
    """원장 조회. 한 쪽 50장, 숫자 순서, counts는 필터와 무관한 전체 상태 합계."""
    if f is not None and not isinstance(f, dict):
        raise C.UserError("배출표 조회 조건을 확인해 주세요.")
    f = f or {}
    status = f.get("status") or ""
    if status and status not in STATUSES:
        raise C.UserError("배출표 상태를 다시 골라 주세요.")
    query = f.get("q", "")
    if not isinstance(query, str):
        raise C.UserError("조회할 번호를 숫자 문자열로 적어 주세요.")
    if query:
        _number(query)
    destination = f.get("destination") or ""
    if not isinstance(destination, str) or len(destination) > 100:
        raise C.UserError("배부처 조회 조건을 100자 이하로 적어 주세요.")
    page = f.get("page", 1)
    if isinstance(page, bool) or not re.fullmatch(r"[0-9]{1,9}", str(page)):
        raise C.UserError("조회할 쪽 번호를 확인해 주세요.")
    page = max(1, int(page))
    counts = {"ALL": 0, **{state: 0 for state in STATUSES}}
    for row in C.rows(con, "SELECT status, COUNT(*) AS n FROM numbered_label GROUP BY status"):
        counts[row["status"]] = row["n"]
        counts["ALL"] += row["n"]
    where, args = [], []
    for condition, value in (("status=?", status), ("instr(number,?)>0", query), ("instr(destination,?)>0", destination.strip())):
        if value:
            where.append(condition)
            args.append(value)
    sql = " WHERE " + " AND ".join(where) if where else ""
    total = con.execute("SELECT COUNT(*) FROM numbered_label" + sql, args).fetchone()[0]
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    labels = C.rows(con, "SELECT * FROM numbered_label" + sql + " ORDER BY length(number_key), number_key LIMIT ? OFFSET ?",
                    [*args, PAGE_SIZE, (page - 1) * PAGE_SIZE])
    return {"rows": labels, "total": total, "page": page, "pages": pages, "size": PAGE_SIZE, "counts": counts}


def label_history(con, number) -> dict:
    """한 번호의 최근 변경 20건. 사유는 파기 가능한 audit_note에서 조회할 때만 합친다."""
    _, key = _number(number)
    label = _label(con, key)
    events = C.rows(con, """SELECT e.*, CASE WHEN n.audit_id IS NULL THEN NULL
        WHEN n.note IS NULL THEN '(파기)' ELSE n.note END AS reason
        FROM numbered_label_event e LEFT JOIN audit_note n ON n.audit_id=e.audit_id
        WHERE e.number_key=? ORDER BY e.id DESC LIMIT 20""", (key,))
    return {"label": label, "rows": events}
