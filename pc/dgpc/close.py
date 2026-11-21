"""마감: 마감 점검 요약·이월·마감 기록(회차)·팀장 확인.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid

from dgpc import backup as _bk, privacy as _pv, area
from dgpc.assign import list_unloads
from dgpc.core import ACTOR, KST, LABELS, UserError, _date_ok, _secs, audit, close_info, get_setting, idempotent, now, one, rows, today_kst, tx
from dgpc.derive import derive, payment_core, payment_status, prefetch
from dgpc.devices import crew_request_ids
from dgpc.intake import _snapshot
from dgpc.results import field_intakes, list_conflicts

# ------------------------------------------------------------------ 일일 마감
CARRY_KIND = {"RECEIVED": "미배정", "ASSIGNED": "결과 없음", "IN_PROGRESS": "최종 결과 없음", "PARTIAL": "일부 수거 · 재방문 약속 없음",
              "NOT_COLLECTED": "미수거 · 재방문 약속 없음", "ON_HOLD": "보류 · 재방문 약속 없음"}


def carry_candidates(con, work_date: str) -> list[dict]:
    """마감 때 다음 업무일로 넘길 접수: 그날 예정인데 끝나지 않은 건(미배정, 결과 없음, 재방문 약속 없는 일부·미수거·보류).
    재방문 약속이 있는 건은 이미 그 날짜로 옮겨져 있어 여기에 나오지 않는다."""
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
    """마감한 뒤 그날 접수·수납·환불 지급을 고친 기록(변경 이력의 '마감 후 수정' 표시)."""
    out = []
    for a in rows(con, """SELECT id, entity, entity_id, action, detail, at, actor FROM audit_log
            WHERE after_close=? AND entity IN ('request','payment','refund') ORDER BY id""", (work_date,)):
        d = json.loads(a["detail"] or "{}")
        rid = a["entity_id"] if a["entity"] == "request" else d.get("request")
        rq = one(con, "SELECT receipt_no FROM request WHERE id=?", (rid,)) if rid else None
        out.append({"at": a["at"], "actor": a["actor"], "entity": a["entity"], "request_id": rid, "receipt_no": rq["receipt_no"] if rq else "",
                    # 환불의 PAY 는 '지급'(수납의 '납부'와 다름). 대사 반영은 사유 대신 출처를 보인다
                    "action": "REFUND_PAY" if a["entity"] == "refund" and a["action"] == "PAY" else a["action"],
                    "reason": d.get("reason") or (f"{d['amount']:,}원" if a["entity"] == "refund" and isinstance(d.get("amount"), int) else "")
                    or (f"출처 {d['source']}" if d.get("source") else "")})
    return out


def _recovery_visits(con, batch_ids):
    """전송 이력으로 확인되는 최종 결과와 PC 직접 수거를 접수·회차에 연결한다."""
    return rows(con, """WITH visits AS (
        SELECT s.request_id, b.id AS batch_id, b.crew_id, b.work_date, b.created_at,
            r.planned_date, r.receipt_no, r.name, a.batch_id AS active_batch_id
        FROM assignment_batch b JOIN assignment_stop s ON s.batch_id=b.id
        JOIN request r ON r.id=s.request_id LEFT JOIN active_assignment a ON a.request_id=r.id
        WHERE b.id IN (SELECT value FROM json_each(?)) AND r.status<>'CANCELLED'
    ), attempts AS (
        SELECT e.event_id,e.request_id,e.assign_ver,e.device_id,e.base_req_ver,
            i.imported_at,COALESCE(e.resolved_at,i.imported_at) AS applied_at,
            CASE WHEN julianday(e.device_ts)<=julianday(i.imported_at,'+10 minutes')
                AND e.device_ts>='2020' THEN julianday(e.device_ts) END AS device_at,
            log.id AS import_audit_id,json_extract(log.detail,'$.work_date') AS file_date
        FROM result_event e JOIN result_import i ON i.id=e.import_id
        LEFT JOIN audit_log log ON log.entity='import' AND log.entity_id=i.id AND log.action='RESULT'
        WHERE e.request_id IN (SELECT request_id FROM visits)
            AND e.kind='ATTEMPT' AND e.apply_status='APPLIED' AND e.assign_ver>0
            AND e.device_id=i.device_id
            AND json_extract(e.body_json,'$.outcome') IN ('DONE','PARTIAL','NOT_COLLECTED','ON_HOLD')
    ), origins AS (
        SELECT e.event_id,e.request_id,e.applied_at,e.file_date,e.device_at,b.id AS batch_id,b.work_date,
            COUNT(*) OVER (PARTITION BY e.event_id) AS candidates
        FROM attempts e JOIN assignment_stop s ON s.request_id=e.request_id
        JOIN assignment_batch b ON b.id=s.batch_id AND b.assign_ver=e.assign_ver
        LEFT JOIN request_version v ON v.request_id=e.request_id AND v.req_ver=e.base_req_ver-1
        WHERE EXISTS (SELECT 1 FROM transfer_bundle t
            LEFT JOIN audit_log sent ON sent.entity='bundle' AND sent.entity_id=t.id AND sent.action='DISPLAY'
            WHERE t.batch_id=b.id
            AND t.work_date=b.work_date AND t.assign_ver=b.assign_ver AND t.device_id=e.device_id
            AND t.displayed_at<=e.imported_at AND (v.at IS NULL OR t.displayed_at>=v.at)
            AND (sent.id IS NULL OR e.import_audit_id IS NULL OR sent.id<e.import_audit_id)
            AND (e.device_at IS NULL OR julianday(t.displayed_at)<=e.device_at)
            AND (json_type(sent.detail,'$.stop_versions') IS NULL OR EXISTS (
                SELECT 1 FROM json_each(sent.detail,'$.stop_versions') AS stop
                WHERE stop.key=e.request_id AND (e.base_req_ver IS NULL OR stop.value=e.base_req_ver))))
    ), mobile AS (
        -- 날짜마다 갱신본 번호가 다시 시작한다. 접수·갱신본·기기만으로 후보가 둘이면 임의로 택하지 않는다.
        SELECT request_id,batch_id,MAX(CASE WHEN verified THEN applied_at END) AS at,
            MAX(NOT verified) AS uncertain
        FROM (SELECT *, candidates=1 AND (CASE WHEN file_date IS NULL THEN device_at IS NOT NULL ELSE file_date=work_date END) AS verified
            FROM origins)
        GROUP BY request_id,batch_id
    ), pc_entry AS (
        SELECT v.request_id,v.batch_id,MAX(e.at) AS at
        FROM visits v JOIN audit_log e ON e.entity_id=v.request_id AND e.entity='request' AND e.action='PC_COLLECTION'
        LEFT JOIN request_version prior ON prior.request_id=v.request_id AND prior.req_ver=json_extract(e.detail,'$.req_ver')-1
        WHERE json_extract(e.detail,'$.collected_on')=v.work_date AND (
            json_extract(e.detail,'$.batch_id')=v.batch_id OR (
                json_type(e.detail,'$.batch_id') IS NULL
                AND json_extract(prior.snapshot_json,'$.active_crew_id')=v.crew_id
                AND json_extract(prior.snapshot_json,'$.planned_date')=v.work_date AND v.created_at<=e.at
                AND (SELECT COUNT(*) FROM assignment_stop s JOIN assignment_batch b ON b.id=s.batch_id
                    WHERE s.request_id=v.request_id AND b.crew_id=v.crew_id AND b.work_date=v.work_date AND b.created_at<=e.at)=1
            )
        ) GROUP BY v.request_id,v.batch_id
    )
    SELECT v.request_id,v.batch_id,v.receipt_no,v.name,m.at AS mobile_at,p.at AS pc_at,m.uncertain
    FROM visits v LEFT JOIN mobile m ON m.request_id=v.request_id AND m.batch_id=v.batch_id
    LEFT JOIN pc_entry p ON p.request_id=v.request_id AND p.batch_id=v.batch_id
    WHERE m.at IS NOT NULL OR m.uncertain OR p.at IS NOT NULL OR v.active_batch_id=v.batch_id
        OR (v.active_batch_id IS NULL AND v.planned_date=v.work_date)
        OR EXISTS (SELECT 1 FROM audit_log e WHERE e.entity='request' AND e.entity_id=v.request_id AND e.action='CARRY'
            AND e.at>=v.created_at AND json_extract(e.detail,'$.from')=v.work_date)
    ORDER BY v.receipt_no""", (json.dumps(batch_ids),))


def _team_recovery(con, work_date, crew_id=None):
    batches = rows(con, """SELECT b.id AS batch_id,b.work_date,b.crew_id,c.name AS crew,b.assign_ver,
        t.displayed_at AS sent_at,t.receipt_confirmed_at
        FROM assignment_batch b JOIN crew c ON c.id=b.crew_id
        LEFT JOIN transfer_bundle t ON t.id=(
            SELECT id FROM transfer_bundle WHERE batch_id=b.id AND device_id=c.device_id
                AND work_date=b.work_date AND assign_ver=b.assign_ver AND displayed_at IS NOT NULL
            ORDER BY displayed_at DESC,rowid DESC LIMIT 1
        ) WHERE b.work_date=? AND b.status='ACTIVE'""" + (" AND b.crew_id=?" if crew_id is not None else "") +
        " ORDER BY c.name,b.id", (work_date,) if crew_id is None else (work_date, crew_id))
    grouped = {b["batch_id"]: [] for b in batches}
    if grouped:
        for visit in _recovery_visits(con, list(grouped)):
            grouped[visit["batch_id"]].append(visit)
    outstanding = []
    for b in batches:
        visits = grouped[b["batch_id"]]
        waiting = [v for v in visits if not v["mobile_at"] and not v["pc_at"]]
        applied = sum(bool(v["mobile_at"]) for v in visits)
        pc = sum(bool(v["pc_at"]) and not v["mobile_at"] for v in visits)
        last = max((at for v in visits for at in (v["mobile_at"], v["pc_at"]) if at), default=None)
        stage = "EMPTY" if not visits else "COMPLETE" if not waiting else "PARTIAL" if applied or pc else "WAITING"
        receipt = "UNSENT" if not b["sent_at"] else "CONFIRMED" if b["receipt_confirmed_at"] else "UNCONFIRMED"
        b.update(total=len(visits), applied=applied, pc=pc, missing=len(waiting),
                 uncertain=sum(bool(v["uncertain"]) for v in waiting), last_applied_at=last,
                 stage=stage, receipt_status=receipt)
        outstanding.extend({"id": v["request_id"], "receipt_no": v["receipt_no"], "name": v["name"]} for v in waiting)
    return batches, outstanding


def _missing_teams(recovery):
    return [{k: b[k] for k in ("crew_id", "crew", "sent_at", "work_date", "assign_ver", "missing")}
            for b in recovery if b["sent_at"] and b["missing"]]


def crews_without_result(con, work_date: str) -> list[dict]:
    return _missing_teams(_team_recovery(con, work_date)[0])


def day_summary(con, work_date: str, crew_id: str | None = None) -> dict:
    """crew_id: None = 전체(사무실), 문자열 = 그 팀 범위. 빈 문자열(팀이 연결되지 않은 수거팀 계정)은 아무 접수도 보이지 않는다."""
    scoped = crew_id is not None
    reqs = rows(con, "SELECT * FROM request WHERE planned_date=?", (work_date,))
    if scoped:
        mine = crew_request_ids(con, crew_id)
        reqs = [r for r in reqs if r["id"] in mine]
    by_status: dict[str, int] = {}
    pending_fee, revisit = [], []
    result_recovery, no_result = _team_recovery(con, work_date, crew_id)
    pre = prefetch(con, [r["id"] for r in reqs], ("items", "events", "links", "payments", "partial"))
    derived = {}
    for r in reqs:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        st = derived[r["id"]] = derive(con, r, pre)
        if st["fee"]["pending_count"] and r["status"] != "CANCELLED":
            pending_fee.append({"id": r["id"], "receipt_no": r["receipt_no"], "name": r["name"], "count": st["fee"]["pending_count"]})
        if r["status"] in ("PARTIAL", "NOT_COLLECTED", "ON_HOLD"):
            revisit.append({"id": r["id"], "receipt_no": r["receipt_no"], "name": r["name"], "status": r["status"], "remaining": st["remaining_total"],
                            "next_date": r["planned_date"] if r["planned_date"] != work_date else None, "appt": [r["appt_start"], r["appt_end"]] if r["appt_start"] else None})
    # 휴대폰 v0.10 기록 확인: 스티커 없음·불일치, 추가 금액 고지 거부, 미수거인데 사진도 사진 없는 사유도 없음
    sticker_issues, fee_refused, no_evidence = [], [], []
    for r in reqs:
        if r["status"] == "CANCELLED":
            continue
        fx = derived[r["id"]]["field"]
        bad = [v for v in fx["stickers"].values() if v["result"] in ("MISSING", "MISMATCH")]
        if bad:
            sticker_issues.append({"id": r["id"], "receipt_no": r["receipt_no"], "name": r["name"], "count": len(bad),
                                   "kinds": sorted({LABELS["sticker"]["MISSING"] if v["result"] == "MISSING" else LABELS["sticker"]["MISMATCH"] for v in bad}),
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
    unpaid = []
    for r in reqs:
        if r["status"] in ("DONE", "PARTIAL", "NOT_COLLECTED", "ON_HOLD", "CANCELLED"):
            ps = payment_core(r, derived[r["id"]]["fee"]["final"], pre["payments"][r["id"]], pre["partial"][r["id"]])
            if ps["status"] in ("UNPAID", "UNDERPAID", "OVERPAID_REVIEW", "AMOUNT_PENDING", "PAID_AMOUNT_PENDING", "REFUND_NEEDED", "OVERREFUNDED_REVIEW"):
                unpaid.append({"id": r["id"], "receipt_no": r["receipt_no"], "name": r["name"], "status": ps["status"], "calculated": ps["calculated"],
                               "levied": ps["levied"], "paid": ps["paid"]})
    if not scoped:  # 예정일이 없거나 다른 날인 접수라도 이날 취소했고 환불할 돈이 남았으면 함께 본다
        d0 = dt.datetime.fromisoformat(work_date).replace(tzinfo=KST).astimezone(dt.timezone.utc)
        rng = (d0.strftime("%Y-%m-%dT%H:%M:%SZ"), (d0 + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        for r in rows(con, """SELECT * FROM request WHERE status='CANCELLED' AND cancelled_at>=? AND cancelled_at<? AND COALESCE(planned_date,'')!=?
                AND pay_status='REFUND_NEEDED'""", (*rng, work_date)):
            ps = payment_status(con, r["id"])
            if ps["status"] == "REFUND_NEEDED":
                unpaid.append({"id": r["id"], "receipt_no": r["receipt_no"], "name": r["name"], "status": ps["status"], "calculated": ps["calculated"],
                               "levied": ps["levied"], "paid": ps["paid"]})
    unloads = [u for u in list_unloads(con, work_date) if not scoped or u["crew_id"] == crew_id]
    # 동별 현황 (오늘 건수·완료·미처리·한도 대비)
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
    closes = rows(con, """SELECT id, seq, closed_at, closed_by, note, carried_json, status, approved_by, approved_at, approve_note, approval_required
        FROM day_close_log WHERE work_date=? ORDER BY seq""", (work_date,))
    for c in closes:
        c["carried"] = len(json.loads(c.pop("carried_json") or "[]"))
        c["approval_required"] = bool(c["approval_required"])
    cap = area.capacity_settings(con)
    crews_nr = _missing_teams(result_recovery)
    return {"work_date": work_date, "total": len(reqs), "by_status": by_status, "no_result": no_result, "pending_fee": pending_fee,
            "crews_no_result": crews_nr, "result_recovery": result_recovery,
            "revisit": revisit, "photos_missing": len(photos), "conflicts": len(conflicts),
            "unconfirmed_bundles": sum(b["receipt_status"] == "UNCONFIRMED" for b in result_recovery),
            "field_intakes": len(field_intakes(con)),
            "payments": unpaid, "unloads": unloads, "sticker_issues": sticker_issues, "fee_refused": fee_refused, "no_evidence": no_evidence,
            "unload_total": {"volume_m3": round(sum(u["volume_m3"] or 0 for u in unloads), 2), "weight_kg": round(sum(u["weight_kg"] or 0 for u in unloads), 1)},
            "carry": [] if scoped else carry_candidates(con, work_date), "dongs": dongs,
            "capacity": {"limit": cap["daily_count"], "booked": sum(1 for r in reqs if r["status"] != "CANCELLED")},
            "working": area.working_reason(con, work_date),
            "post_close_edits": [] if scoped else _post_close_edits(con, work_date),
            "closes": closes, "closed": closes[-1] if closes else None, "close_approval_required": get_setting(con, "close_approval_required")}


def close_day(con, work_date: str, note: str = "", close_secs=None, *, client_req_id=None) -> dict:
    """미처리 건 이월과 마감 회차를 한 번 기록한 뒤, 1인 마감이면 커밋된 DB를 백업한다."""
    with tx(con):
        result = idempotent(con, ACTOR.get(), "close", client_req_id, lambda: _record_day(con, work_date, note, close_secs))
    if result.get("approval_required", True):
        return result
    # 요청 키를 재사용한 경우 지금 열려 있는 날짜·회차와 무관하게 원래 회차의 결과를 반환한다.
    round_key = (result["work_date"], result["seq"])
    # 파일을 내보내기 시작하는 것과 실제 완료는 구분한다. 강제 종료 후 재전송해도 같은 백업을 두 번 만들지 않는다.
    with tx(con):
        saved = one(con, """SELECT json_extract(summary_json, '$.completion') AS completion
            FROM day_close_log WHERE work_date=? AND seq=?""", round_key)
        if saved["completion"]:
            return {**result, **json.loads(saved["completion"])}
        _set_completion(con, round_key, {"pii_purge": None, "backup": {"ok": False,
            "error": "마감은 기록되었습니다. 자동 백업 완료 여부는 설정 → 백업의 실행 결과와 보관 목록에서 확인해 주세요."}})
    return {**result, **_finish_close(con, "CLOSE_SOLO", round_key)}


def _set_completion(con, round_key, completion):
    con.execute("""UPDATE day_close_log SET summary_json=json_set(summary_json, '$.completion', json(?))
        WHERE work_date=? AND seq=?""", (json.dumps(completion, ensure_ascii=False), *round_key))


def _record_day(con, work_date, note, close_secs):
    if not _date_ok(work_date or ""):
        raise UserError("마감할 날짜를 골라 주세요.")
    if work_date > today_kst():  # 미래 날짜를 마감하면 배정이 풀리고 예정일이 옮겨진다
        raise UserError("아직 오지 않은 날은 마감할 수 없습니다.")
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
        con.execute("""INSERT INTO day_close_log (id, work_date, seq, closed_at, closed_by, summary_json, note, status, approval_required, carried_json)
            VALUES (?,?,?,?,?,?,?,'RECORDED',?,?)""", (str(uuid.uuid4()), work_date, seq, now(), ACTOR.get(), json.dumps(s, ensure_ascii=False), (note or "")[:300],
                s["close_approval_required"], json.dumps([{"id": c["id"], "receipt_no": c["receipt_no"], "to": c["to"], "kind": c["kind"]} for c in carry], ensure_ascii=False)))
        audit(con, "day", work_date, "CLOSE", {"seq": seq, "open_items": len(s["no_result"]) + len(s["revisit"]) + s["conflicts"] + s["photos_missing"],
                                               "carried": len(carry), "approval_required": s["close_approval_required"], **_secs("close_secs", close_secs)})
    warns = [f"{c['crew']}: 갱신본 {c['assign_ver']}에 반영되지 않은 결과가 {c['missing']}건 있습니다." for c in s["crews_no_result"]]
    return {**s, "seq": seq, "carried": carry, "warnings": warns, "approval_required": s["close_approval_required"]}


def approve_close(con, work_date: str, note: str = "") -> dict:
    """팀장 확인(두 번째 단계). 마감을 기록한 사람은 확인할 수 없다. 다시 마감하면 새 회차를 다시 확인해야 한다."""
    with tx(con):
        c = close_info(con, work_date)
        if not c:
            raise UserError("아직 마감 기록이 없습니다. 담당자가 먼저 마감을 기록해야 합니다.")
        if c["status"] == "APPROVED":
            raise UserError("이미 확인한 마감입니다.")
        if not c["approval_required"]:
            raise UserError("이 회차는 담당자 1인 마감으로 완료되었습니다.")
        if not get_setting(con, "close_approval_required"):
            raise UserError("현재 일일 마감에서 팀장 확인을 사용하지 않습니다.")
        if c["closed_by"] and c["closed_by"] == ACTOR.get():
            raise UserError("마감을 기록한 사람은 확인할 수 없습니다. 팀장(검토자)이 확인해 주세요.")
        con.execute("UPDATE day_close_log SET status='APPROVED', approved_by=?, approved_at=?, approve_note=? WHERE id=?",
                    (ACTOR.get(), now(), (note or "")[:300], c["id"]))
        audit(con, "day", work_date, "APPROVE", {"seq": c["seq"]})
        out = close_info(con, work_date)
    return {**out, **_finish_close(con, "CLOSE_APPROVE")}


def _finish_close(con, reason, round_key=None):
    # 마감 완료 후 보유기간 지난 개인정보·접속 기록 정리와 백업. 실패해도 마감 기록은 남긴다.
    out = {}
    try:
        out["pii_purge"] = _pv.purge_pii(con, trigger=reason)["count"]
        _pv.purge_old_access(con)
    except Exception:  # noqa: BLE001
        out["pii_purge"] = None
    def record_result(result):
        out["backup"] = {k: v for k, v in result.items() if k in ("ok", "file", "error")}
        if round_key:
            _set_completion(con, round_key, out)

    _bk.make_auto(con, reason, on_record=record_result)
    return out
