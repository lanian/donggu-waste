"""동별 접수 후속조치. 최초 저장 전에는 기존 기록으로 후보를 찾는다."""
from __future__ import annotations

import json
import re

from . import area, core as C, dong_work as DW
from .derive import derive
from .results import _apply_revisit

_STATES = ("OPEN", "IN_PROGRESS", "DONE")
_CONTACTS = "kind IN ('독촉','변경요청')"
_UNFINISHED = "('PARTIAL','NOT_COLLECTED','ON_HOLD')"
_TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_PAGE_SIZE = 15


def _queue_sql(where):
    # 이전 조치를 닫은 뒤 새 통화나 수거 시도가 들어온 경우만 다시 표시한다.
    # 방문 예약·마감 이월·수납으로 접수 버전이 바뀌어도 같은 조치는 열리지 않는다.
    return f"""WITH source AS (
        SELECT r.id,r.receipt_no,r.dong,r.status AS request_status,r.pii_purged_at,
            f.assignee_id, f.due_date, f.next_action, f.status AS saved_status, f.version,
            COALESCE(f.contact_count,0) AS handled_contacts, COALESCE(f.attempt_count,0) AS handled_attempts,
            f.outcome AS handled_outcome, u.display_name AS assignee,
            (SELECT COUNT(*) FROM contact_log WHERE request_id=r.id AND {_CONTACTS}) AS contact_count,
            (SELECT COUNT(*) FROM result_event WHERE request_id=r.id AND kind='ATTEMPT'
                AND apply_status='APPLIED') AS attempt_count,
            CASE WHEN r.status IN {_UNFINISHED} THEN r.status
                WHEN r.status NOT IN ('DONE','CANCELLED') AND r.completed_at IS NOT NULL THEN (
                    SELECT json_extract(a.detail,'$.to') FROM audit_log a WHERE a.entity='request'
                    AND a.entity_id=r.id AND a.action='STATUS' AND json_extract(a.detail,'$.to') IN {_UNFINISHED}
                    ORDER BY a.id DESC LIMIT 1
                ) END AS outcome
        FROM request r LEFT JOIN request_followup f ON r.id=f.request_id
        LEFT JOIN app_user u ON u.id=f.assignee_id WHERE {where}
    ), queue AS (
        SELECT *, CASE
            WHEN saved_status IN ('OPEN','IN_PROGRESS') THEN saved_status
            WHEN pii_purged_at IS NULL AND (contact_count>handled_contacts OR
                (outcome IS NOT NULL AND (outcome IS NOT handled_outcome OR attempt_count>handled_attempts)))
                THEN 'NEEDS_ACTION'
            ELSE saved_status END AS status
        FROM source
    )"""


def _assignees(con, dong_ids):
    return C.rows(con, """SELECT u.id,u.display_name AS name FROM app_user u
        WHERE u.active=1 AND (u.role IN ('ADMIN','INTAKE') OR (u.role='DONG' AND EXISTS (
            SELECT 1 FROM app_user_dong a JOIN dong d ON d.id=a.dong_id AND d.active=1
            WHERE a.user_id=u.id AND a.dong_id IN (SELECT value FROM json_each(?))
        ))) ORDER BY u.display_name,u.id""", (json.dumps(dong_ids),))


def list_followups(con, user, query):
    u = DW.actor(con, user)
    selected = query.get("dong") or "all"
    where, args = DW._selection(con, u, selected)
    dongs = DW.safe_dongs(con, u)
    assignees = _assignees(con, [d["id"] for d in dongs if selected in ("all", d["id"])])
    state, assigned, overdue = query.get("status", "open"), query.get("assignee"), query.get("overdue", "0")
    page_text = query.get("page", "1")
    if state not in ("open", "done") or overdue not in ("0", "1") or not re.fullmatch(r"[1-9]\d{0,5}", page_text):
        raise C.UserError("후속조치 조회 조건을 확인해 주세요.")
    page = int(page_text)
    filters = ["status='DONE'" if state == "done" else "status IN ('NEEDS_ACTION','OPEN','IN_PROGRESS')"]
    if assigned == "-":
        filters.append("assignee_id IS NULL")
    elif assigned:
        filters.append("assignee_id=?")
        args.append(assigned)
    today = C.today_kst()
    if overdue == "1":
        filters.append("status IN ('OPEN','IN_PROGRESS') AND due_date<?")
        args.append(today)
    out = C.rows(con, _queue_sql(f"r.pii_purged_at IS NULL AND ({where})") + """
        SELECT id,receipt_no,dong,status,assignee,outcome,
            CASE WHEN status<>'NEEDS_ACTION' THEN due_date END AS due_date,
            (contact_count>handled_contacts) AS new_contact
        FROM queue WHERE """ + " AND ".join(filters) + """
        ORDER BY status='NEEDS_ACTION', due_date, id LIMIT ? OFFSET ?""", [*args, _PAGE_SIZE + 1, (page - 1) * _PAGE_SIZE])
    return {"rows": out[:_PAGE_SIZE], "has_next": len(out) > _PAGE_SIZE, "dongs": dongs,
            "assignees": assignees, "today": today}


def _case(con, rid):
    return C.one(con, _queue_sql("r.id=?") + "SELECT * FROM queue", (rid,))


def _signal(case):
    return [case["contact_count"], case["attempt_count"], case["outcome"]]


def _appointment(con, request):
    value = {"date": request["planned_date"], "start": request["appt_start"], "end": request["appt_end"]}
    logged = C.one(con, """SELECT action,detail FROM audit_log WHERE entity='request' AND entity_id=?
        AND action IN ('REVISIT_APPT','CARRY','RESCHEDULE') ORDER BY id DESC LIMIT 1""", (request["id"],))
    data = json.loads(logged["detail"]) if logged and logged["action"] == "REVISIT_APPT" else {}
    return {**value, "confirmed": bool(value["date"] and all(data.get(k) == v for k, v in value.items()))}


def detail(con, user, rid):
    u = DW.actor(con, user)
    DW.require_request(con, u, rid)
    case = _case(con, rid)
    if not case["status"]:
        return {"status": None}
    request = C.one(con, "SELECT id,req_ver,planned_date,appt_start,appt_end FROM request WHERE id=?", (rid,))
    dong = area.get_dong(con, case["dong"])
    field = derive(con, request)
    lines = [{k: l[k] for k in ("name", "spec", "unit_label", "remaining", "reason")} for l in field["lines"] if l["remaining"]]
    history = C.rows(con, """SELECT at,actor,action,detail FROM audit_log
        WHERE entity='request_followup' AND entity_id=? ORDER BY id DESC""", (rid,))
    for row in history:
        row.update(json.loads(row.pop("detail")))
    purged = bool(case["pii_purged_at"])
    contacts = [] if purged else C.rows(con, f"""SELECT at,kind,content FROM contact_log WHERE request_id=? AND {_CONTACTS}
        ORDER BY recorded_at DESC,rowid DESC""", (rid,))
    keys = ("status", "assignee_id", "assignee", "due_date", "request_status", "outcome")
    return {**{k: case[k] for k in keys},
            **({"due_date": None} if case["status"] == "NEEDS_ACTION" else {}),
            "next_action": None if purged or case["status"] == "NEEDS_ACTION" else case["next_action"],
            "version": case["version"] or 0, "signal": _signal(case), "history": history, "remaining": lines,
            "appointment": _appointment(con, request), "req_ver": request["req_ver"],
            "contacts": contacts, "assignees": _assignees(con, [dong["id"]] if dong else []),
            "field_contact": field["contact"],
            "collection_note": next((m for m in reversed(field["memos"]) if m.startswith("PC 수거 입력: ")), None) if not purged and case["outcome"] else None,
            "editable": u["role"] in DW.WRITE_ROLES and not purged}


def _metadata(case):
    return {"assignee_id": case["assignee_id"], "assignee": case["assignee"],
            "due_date": case["due_date"], "status": case["saved_status"]}


def save(con, user, rid, data):
    with C.tx(con):
        u = DW.actor(con, user, write=True)
        DW.require_request(con, u, rid)
        C.require_request_pii_writable(con, rid)
        case = _case(con, rid)
        status, version = data.get("status"), data.get("version")
        assignee, due, action = (data.get(key) for key in ("assignee_id", "due_date", "next_action"))
        if status not in _STATES or type(version) is not int or version < 0:
            raise C.UserError("처리 상태를 선택하고 후속조치를 다시 확인해 주세요.")
        if not isinstance(assignee, str) or not C._date_ok(due) or not isinstance(action, str) or not 0 < len(action.strip()) <= 500:
            raise C.UserError("담당자·처리기한·다음 조치(500자 이내)를 입력해 주세요.")
        action = action.strip()
        current = case["version"] or 0
        previous = (case["assignee_id"], case["due_date"], case["next_action"], case["saved_status"])
        if current and version == current - 1 and previous == (assignee, due, action, status):
            return {"version": current}  # 저장 성공 뒤 응답을 받지 못한 재시도
        if version != current or data.get("signal") != _signal(case):
            raise C.StaleError("후속조치, 현장 결과 또는 통화 기록이 바뀌었습니다. 다시 불러와 확인해 주세요.")
        if case["status"] in (None, "DONE"):
            raise C.UserError("현재 처리할 후속조치가 없습니다.")
        dong = area.get_dong(con, case["dong"])
        candidates = {a["id"]: a["name"] for a in _assignees(con, [dong["id"]] if dong else [])}
        if assignee not in candidates:
            raise C.UserError("이 접수의 행정동을 담당할 수 있는 사용자를 선택해 주세요.")
        if case["status"] == status and previous == (assignee, due, action, status):
            return {"version": current}
        con.execute("""INSERT INTO request_followup (request_id, assignee_id, due_date, next_action, status, version,
            contact_count, attempt_count, outcome, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(request_id) DO UPDATE SET assignee_id=excluded.assignee_id, due_date=excluded.due_date,
            next_action=excluded.next_action, status=excluded.status, version=excluded.version,
            contact_count=excluded.contact_count, attempt_count=excluded.attempt_count, outcome=excluded.outcome, updated_at=excluded.updated_at""",
                    (rid, assignee, due, action, status, current + 1, *_signal(case), C.now()))
        operation = "COMPLETE" if status == "DONE" else "CREATE" if not current else "REOPEN" if case["saved_status"] == "DONE" else "UPDATE"
        C.audit(con, "request_followup", rid, operation, {"before": _metadata(case) if current else None,
                "after": {"assignee_id": assignee, "assignee": candidates[assignee], "due_date": due, "status": status}})
        return {"version": current + 1}


def confirm_revisit(con, user, rid, data):
    with C.tx(con):
        u = DW.actor(con, user, write=True)
        DW.require_request(con, u, rid)
        C.require_request_pii_writable(con, rid)
        request = C.one(con, "SELECT * FROM request WHERE id=?", (rid,))
        if request["status"] in ("DONE", "CANCELLED") or _case(con, rid)["status"] in (None, "DONE"):
            raise C.UserError("처리할 후속조치가 있는 미완료 접수에서 재방문을 확정해 주세요.")
        date, start, end = data.get("date"), data.get("start") or None, data.get("end") or None
        if not C._date_ok(date) or date < C.today_kst():
            raise C.UserError("오늘 이후의 재방문 날짜를 골라 주세요.")
        if (start and (not isinstance(start, str) or not _TIME.fullmatch(start))
                or end and (not isinstance(end, str) or not _TIME.fullmatch(end) or not start or end <= start)):
            raise C.UserError("올바른 방문 시간을 입력해 주세요. 종료 시각은 시작 시각보다 늦어야 합니다.")
        current = _appointment(con, request)
        expected = data.get("req_ver")
        # 응답 유실로 일정 확정을 재전송할 수 있다. 일정이 일치할 때만 쓴 기록 없이 성공한다.
        same = current == {"date": date, "start": start, "end": end, "confirmed": True}
        if not (same and type(expected) is int and expected in (request["req_ver"] - 1, request["req_ver"])):
            C.check_req_ver(con, rid, expected)
            _apply_revisit(con, rid, {"date": date, "start": start, "end": end}, office=True)
        result = C.one(con, "SELECT id,req_ver,status,planned_date,appt_start,appt_end FROM request WHERE id=?", (rid,))
        return {"req_ver": result["req_ver"], "request_status": result["status"], "appointment": _appointment(con, result)}
