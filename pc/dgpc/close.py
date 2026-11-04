"""마감: 마감 점검 요약·이월·마감 기록(회차)·팀장 확인.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid

from dgpc import backup as _bk, privacy as _pv, area
from dgpc.assign import list_unloads
from dgpc.core import ACTOR, KST, LABELS, UserError, _secs, audit, close_info, now, one, rows, today_kst, tx
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
    """마감한 뒤 그날 접수·수납을 고친 기록(변경 이력의 '마감 후 수정' 표시)."""
    out = []
    for a in rows(con, """SELECT id, entity, entity_id, action, detail, at, actor FROM audit_log
            WHERE after_close=? AND entity IN ('request','payment') ORDER BY id""", (work_date,)):
        d = json.loads(a["detail"] or "{}")
        rid = a["entity_id"] if a["entity"] == "request" else d.get("request")
        rq = one(con, "SELECT receipt_no FROM request WHERE id=?", (rid,)) if rid else None
        out.append({"at": a["at"], "actor": a["actor"], "entity": a["entity"], "action": a["action"], "request_id": rid,
                    "receipt_no": rq["receipt_no"] if rq else "", "reason": d.get("reason") or ""})
    return out


def crews_without_result(con, work_date: str) -> list[dict]:
    """그날 목록을 휴대폰에 보냈는데(전송 묶음 있음) 마지막으로 보낸 뒤 그 휴대폰 결과 파일을 하나도 가져오지 않은 팀(마감 때 경고, 막지 않음)."""
    out = []
    for b in rows(con, """SELECT t.device_id, MAX(t.displayed_at) AS sent_at, c.id AS crew_id, c.name AS crew FROM transfer_bundle t
            JOIN assignment_batch b ON b.id=t.batch_id JOIN crew c ON c.id=b.crew_id WHERE t.work_date=? GROUP BY t.device_id, c.id ORDER BY c.name""", (work_date,)):
        if not one(con, "SELECT 1 AS x FROM result_import WHERE device_id=? AND imported_at > ? LIMIT 1", (b["device_id"], b["sent_at"])):
            out.append({"crew_id": b["crew_id"], "crew": b["crew"], "sent_at": b["sent_at"], "work_date": work_date})
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
        if r["id"] in assigned_ids and r["status"] == "ASSIGNED":
            no_result.append({"id": r["id"], "receipt_no": r["receipt_no"], "name": r["name"]})
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
    unconfirmed = rows(con, """SELECT t.id, t.work_date, t.assign_ver FROM transfer_bundle t LEFT JOIN assignment_batch b ON b.id=t.batch_id
        WHERE t.work_date=? AND t.receipt_confirmed_at IS NULL""" + (" AND b.crew_id=?" if crew_id else ""), (work_date, crew_id) if crew_id else (work_date,))
    unpaid = []
    for r in reqs:
        if r["status"] in ("DONE", "PARTIAL", "NOT_COLLECTED", "ON_HOLD", "CANCELLED"):
            ps = payment_core(r, derived[r["id"]]["fee"]["final"], pre["payments"][r["id"]], pre["partial"][r["id"]])
            if ps["status"] in ("UNPAID", "UNDERPAID", "OVERPAID_REVIEW", "AMOUNT_PENDING", "PAID_AMOUNT_PENDING", "REFUND_NEEDED", "OVERREFUNDED_REVIEW"):
                unpaid.append({"id": r["id"], "receipt_no": r["receipt_no"], "name": r["name"], "status": ps["status"], "calculated": ps["calculated"],
                               "levied": ps["levied"], "paid": ps["paid"]})
    if not crew_id:  # 예정일이 없거나 다른 날인 접수라도 이날 취소했고 환불할 돈이 남았으면 함께 본다
        d0 = dt.datetime.fromisoformat(work_date).replace(tzinfo=KST).astimezone(dt.timezone.utc)
        rng = (d0.strftime("%Y-%m-%dT%H:%M:%SZ"), (d0 + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        for r in rows(con, """SELECT * FROM request WHERE status='CANCELLED' AND cancelled_at>=? AND cancelled_at<? AND COALESCE(planned_date,'')!=?
                AND pay_status='REFUND_NEEDED'""", (*rng, work_date)):
            ps = payment_status(con, r["id"])
            if ps["status"] == "REFUND_NEEDED":
                unpaid.append({"id": r["id"], "receipt_no": r["receipt_no"], "name": r["name"], "status": ps["status"], "calculated": ps["calculated"],
                               "levied": ps["levied"], "paid": ps["paid"]})
    unloads = [u for u in list_unloads(con, work_date) if not crew_id or u["crew_id"] == crew_id]
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
    closes = rows(con, "SELECT id, seq, closed_at, closed_by, note, carried_json, status, approved_by, approved_at, approve_note FROM day_close_log WHERE work_date=? ORDER BY seq", (work_date,))
    for c in closes:
        c["carried"] = len(json.loads(c.pop("carried_json") or "[]"))
    cap = area.capacity_settings(con)
    crews_nr = [c for c in crews_without_result(con, work_date) if not crew_id or c["crew_id"] == crew_id]
    return {"work_date": work_date, "total": len(reqs), "by_status": by_status, "no_result": no_result, "pending_fee": pending_fee,
            "crews_no_result": crews_nr,
            "revisit": revisit, "photos_missing": len(photos), "conflicts": len(conflicts), "unconfirmed_bundles": len(unconfirmed),
            "field_intakes": len(field_intakes(con)),
            "payments": unpaid, "unloads": unloads, "sticker_issues": sticker_issues, "fee_refused": fee_refused, "no_evidence": no_evidence,
            "unload_total": {"volume_m3": round(sum(u["volume_m3"] or 0 for u in unloads), 2), "weight_kg": round(sum(u["weight_kg"] or 0 for u in unloads), 1)},
            "carry": [] if crew_id else carry_candidates(con, work_date), "dongs": dongs,
            "capacity": {"limit": cap["daily_count"], "booked": sum(1 for r in reqs if r["status"] != "CANCELLED")},
            "working": area.working_reason(con, work_date),
            "post_close_edits": [] if crew_id else _post_close_edits(con, work_date),
            "closes": closes, "closed": closes[-1] if closes else None}


def close_day(con, work_date: str, note: str = "", close_secs=None) -> dict:
    """마감 기록(담당자). 끝나지 않은 건은 다음 업무일로 예정일을 옮기고(마감 이월), 기록은 회차로 쌓는다(덮어쓰지 않음).
    팀장(검토자)이 approve_close 로 확인한다."""
    if not re.fullmatch(r"\d{4}-\d\d-\d\d", work_date or ""):
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
        con.execute("""INSERT INTO day_close_log (id, work_date, seq, closed_at, closed_by, summary_json, note, carried_json, status)
            VALUES (?,?,?,?,?,?,?,?,'RECORDED')""", (str(uuid.uuid4()), work_date, seq, now(), ACTOR.get(), json.dumps(s, ensure_ascii=False), (note or "")[:300],
                                                   json.dumps([{"id": c["id"], "receipt_no": c["receipt_no"], "to": c["to"], "kind": c["kind"]} for c in carry], ensure_ascii=False)))
        audit(con, "day", work_date, "CLOSE", {"seq": seq, "open_items": len(s["no_result"]) + len(s["revisit"]) + s["conflicts"] + s["photos_missing"],
                                               "carried": len(carry), **_secs("close_secs", close_secs)})
    warns = [f"{c['crew']}: 목록을 보냈는데 결과 파일을 아직 가져오지 않았습니다." for c in s.get("crews_no_result") or []]
    return {**s, "seq": seq, "carried": carry, "warnings": warns}


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
    try:
        out["pii_purge"] = _pv.purge_pii(con, trigger="CLOSE_APPROVE")["count"]
        _pv.purge_old_access(con)
    except Exception:  # noqa: BLE001
        out["pii_purge"] = None
    out["backup"] = {k: v for k, v in _bk.make_auto(con, "CLOSE_APPROVE").items() if k in ("ok", "file", "error")}
    return out
