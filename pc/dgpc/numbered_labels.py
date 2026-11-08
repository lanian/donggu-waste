"""숫자 배출표 원장: 입고·기관 배부·접수 사용·무효와 변경 이력.

접수번호·현장 스티커 확인·수납 기록과 별도로 관리한다. 번호 표기는
문자열로 보존하고 앞자리 0을 뺀 키로 중복을 막는다. 주민 정보는 복사하지 않는다.
"""
from __future__ import annotations

import json
import re

from dgpc import core as C

STATUSES = ("AVAILABLE", "DISTRIBUTED", "USED", "VOID")
PAGE_SIZE = 50
MAX_RANGE = 5000


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


def _record(con, label: dict, action: str, previous: str | None, at: str, reason: str | None = None):
    detail = {"number": label["number"], "from": previous, "to": label["status"]}
    if label["request_id"]:
        detail["request"] = label["request_id"]
    if label["line_id"]:
        detail["line"] = label["line_id"]
    if reason:
        detail["reason"] = reason
    # 자유 사유는 audit_note에만 남긴다. 동일 쓰기 트랜잭션 안이므로 방금 쓴 감사 행과 안전하게 연결된다.
    C.audit(con, "numbered_label", label["number_key"], action, detail)
    audit_id = con.execute("SELECT id FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    con.execute("""INSERT INTO numbered_label_event
        (number_key, action, from_status, to_status, request_id, line_id, at, actor, audit_id)
        VALUES (?,?,?,?,?,?,?,?,?)""", (label["number_key"], action, previous, label["status"],
                                         label["request_id"], label["line_id"], at, C.ACTOR.get(), audit_id))


def receive_range(con, start, end) -> dict:
    """숫자 구간 입고. 별표기 중복을 포함한 한 장의 오류도 전체 입고를 되돌린다."""
    _writable(con)
    numbers = _range(start, end)
    keys = json.dumps([key for _, key in numbers])
    with C.tx(con):
        if C.one(con, "SELECT number FROM numbered_label WHERE number_key IN (SELECT value FROM json_each(?)) LIMIT 1", (keys,)):
            raise C.UserError("이미 입고된 번호와 겹칩니다 (앞자리 0이 다른 같은 번호 포함).")
        at = C.now()
        for number, key in numbers:
            con.execute("""INSERT INTO numbered_label
                (number_key, number, status, destination, received_at, updated_at)
                VALUES (?,?,'AVAILABLE','',?,?)""", (key, number, at, at))
            _record(con, _label(con, key), "RECEIVE", None, at)
    return {"count": len(numbers), "numbers": [number for number, _ in numbers]}


def distribute_range(con, start, end, destination) -> dict:
    """사용 가능한 번호 구간을 기관에 배부한다. 수납·접수 상태는 바꾸지 않는다."""
    _writable(con)
    numbers = _range(start, end)
    if not isinstance(destination, str) or not 1 <= len(destination.strip()) <= 100:
        raise C.UserError("배부처 기관명을 1~100자로 적어 주세요.")
    destination = destination.strip()
    keys = json.dumps([key for _, key in numbers])
    with C.tx(con):
        labels = C.rows(con, "SELECT * FROM numbered_label WHERE number_key IN (SELECT value FROM json_each(?))", (keys,))
        if len(labels) != len(numbers):
            raise C.UserError("구간에 입고되지 않은 번호가 있습니다.")
        if any(label["status"] != "AVAILABLE" for label in labels):
            raise C.UserError("배부할 구간의 모든 번호가 사용 가능 상태여야 합니다.")
        at = C.now()
        for label in labels:
            con.execute("""UPDATE numbered_label SET status='DISTRIBUTED', destination=?, distributed_at=?, updated_at=?
                WHERE number_key=?""", (destination, at, at, label["number_key"]))
            _record(con, {**label, "status": "DISTRIBUTED"}, "DISTRIBUTE", "AVAILABLE", at)
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
        request = C.one(con, "SELECT status, pii_purged_at FROM request WHERE id=?", (request_id,))
        if not request:
            raise C.UserError("접수를 찾을 수 없습니다.")
        if request["status"] == "CANCELLED" or request["pii_purged_at"]:
            raise C.UserError("취소되었거나 개인정보가 파기된 접수에는 사용할 수 없습니다.")
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
