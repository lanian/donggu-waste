"""현장 결과: 결과 파일 반영(이벤트 한 번만)·충돌함·현장 접수/정정·단독 휴대폰 자료 옮기기.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import uuid
import zlib
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import dgproto as P
from dgpc import store as _store, area, label_auto
from dgpc.core import ACTOR, UserError, _after_close, _date_ok, _int_clamp, _num, _sane_ts, audit, norm_phone, now, one, rows, today_kst, tx
from dgpc.derive import FINAL, derive
from dgpc.devices import _open_from_device, _require_active_device
from dgpc.fees import fee_rows, fee_state
from dgpc.intake import CANCEL_CODES, _cancel, _insert_item, _insert_request, _reason, _resolve_dong, _snapshot, build_items, validate

def _refresh_status(con, rid, new_attempt: bool = True, new_location: bool = False):
    """현장 결과에 따라 업무 상태를 갱신한다. 최종 결과가 나오면 활성 배정을 풀어 재배정할 수 있게 한다.
    new_attempt=False: 이번에 들어온 기록에 수거 결과(ATTEMPT)가 없음. 재방문으로 다시 배정된 접수라면 지난 방문의 결과로
    되돌리지 않는다(도착·연락 기록만 먼저 와도 배정이 풀리고 '미수거'로 돌아가던 문제).
    new_location=True일 때만 이번에 반영된 위치를 원본에 쓴다(주소 정정으로 비운 위치를 과거 기록으로 복원하지 않음)."""
    r = one(con, "SELECT * FROM request WHERE id=?", (rid,))
    if not r or r["status"] == "CANCELLED":
        return
    st = derive(con, r)
    new = r["status"]
    stale_outcome = not new_attempt and bool(one(con, "SELECT 1 AS x FROM active_assignment WHERE request_id=?", (rid,)))
    if st["outcome"] in FINAL and not stale_outcome:
        new = st["outcome"]
    elif st["has_field_records"]:
        new = "IN_PROGRESS"
    if new_location and st["field_loc"] and st["field_loc"]["lat"] is not None:
        con.execute("UPDATE request SET lat=?, lon=?, geo_source=? WHERE id=?",
                    (st["field_loc"]["lat"], st["field_loc"]["lon"], "ADDR" if st["field_loc"].get("source") == "ADDR" else "FIELD", rid))
    if new != r["status"]:
        # 완료일 = 휴대폰에서 최종 결과(ATTEMPT)를 남긴 시각. 형식이 틀리거나 미래(+10분 넘음)면 지금
        done_at = _sane_ts(st.get("outcome_ts")) if new in FINAL else None
        con.execute("UPDATE request SET status=?, updated_at=?, completed_at=CASE WHEN ? IN ('DONE','PARTIAL','NOT_COLLECTED','ON_HOLD') THEN ? ELSE completed_at END WHERE id=?",
                    (new, now(), new, done_at or now(), rid))
        audit(con, "request", rid, "STATUS", {"from": r["status"], "to": new})
    if new in FINAL:
        con.execute("DELETE FROM active_assignment WHERE request_id=?", (rid,))


KNOWN_KINDS = {"CONTACT", "FIELD_ITEM_ADD", "ITEM_RESULT", "ATTEMPT", "LOCATION_FIX", "FIELD_MEMO", "PHOTO_META", "REVISIT_APPT", "DIAL_INTENT", "UNLOAD",
               "ARRIVE", "STICKER_CHECK", "FEE_NOTICE", "SMS_INTENT", "NAV_INTENT", "FIELD_INTAKE", "CORRECTION", "CANCEL_REQUEST"}
# v0.12 휴대폰(PC 연결 방식)의 요청 기록: 원본(PC)을 바로 바꾸지 않고 사무실 확인을 거친다
REVIEW_KINDS = {"CORRECTION": "현장 정정 요청 — 사무실 확인 필요", "CANCEL_REQUEST": "현장 취소 요청 — 사무실 확인 필요"}
CORRECTABLE = ("name", "phone", "address", "spot_note", "planned_date")
# 취소된 접수라도 충돌함으로 보내지 않는 기록(연락·사진·도착·문자/길안내 앱 열기 — 결과를 바꾸지 않음)
CANCELLED_OK_KINDS = ("CONTACT", "DIAL_INTENT", "PHOTO_META", "ARRIVE", "SMS_INTENT", "NAV_INTENT")


def prepare_result(con, data: bytes) -> dict:
    """결과 파일 검증·복호(시간이 걸리는 서명 확인)만. 서버는 이 단계를 DB 잠금 밖(조회 전용 연결)에서 한다."""
    sha = hashlib.sha256(data).hexdigest()
    prev = one(con, "SELECT summary_json, release_code FROM result_import WHERE package_sha256=?", (sha,))
    if prev:
        return {"sha": sha, "duplicate": {"duplicate_file": True, **json.loads(prev["summary_json"] or "{}"), "release_code": prev["release_code"]}}
    dev_id, head, payload, release = _open_from_device(con, data, P.TYPE_RESULT)
    if payload.get("schema") != "dg.result/1":
        raise UserError("결과 파일 형식이 아닙니다.")
    return {"sha": sha, "dev_id": dev_id, "payload": payload, "release": release}


def import_result(con, data: bytes, channel: str = "MAIL", prepared: dict | None = None) -> dict:
    prep = prepared or prepare_result(con, data)
    if prep.get("duplicate"):
        return prep["duplicate"]
    sha, dev_id, payload, release = prep["sha"], prep["dev_id"], prep["payload"], prep["release"]
    if one(con, "SELECT 1 AS x FROM result_import WHERE package_sha256=?", (sha,)):  # 잠금 밖에서 검증하는 사이 같은 파일이 먼저 들어옴
        prev = one(con, "SELECT summary_json, release_code FROM result_import WHERE package_sha256=?", (sha,))
        return {"duplicate_file": True, **json.loads(prev["summary_json"] or "{}"), "release_code": prev["release_code"]}
    crew = one(con, "SELECT id FROM crew WHERE device_id=?", (dev_id,))
    imp = str(uuid.uuid4())
    counts = {"applied": 0, "skipped_duplicate": 0, "conflict": 0, "photos_expected": 0}
    touched, attempted, located = set(), set(), set()  # 이번 파일에서 결과(ATTEMPT)·위치(LOCATION_FIX)가 실제 반영된 접수
    with tx(con):
        _require_active_device(con, dev_id)
        con.execute("INSERT INTO result_import (id, channel, device_id, package_sha256, imported_at, summary_json, release_code) VALUES (?,?,?,?,?,NULL,?)",
                    (imp, channel, dev_id, sha, now(), release))
        # 휴대폰 기록 세대(v0.13, 백업 복원마다 새 세대): 결과 파일 머리 "gens" = [[세대, 그 세대 첫 번호], …] (없으면 받은 가장 작은 번호)
        for g_, start_ in P.norm_gens(payload.get("gens")):
            _note_gen(con, dev_id, g_, start_)

        def seen(ev):
            # PC가 이 기기·세대의 이 번호를 받았다는 표시(release_info 계산용). 이미 받은 기록(event_id 같음)도 넣는다
            # — 휴대폰이 복원하며 번호를 옮긴 기록도 PC에 있으면 '받음'이다.
            sq = ev.get("seq")
            if isinstance(sq, bool) or not isinstance(sq, int) or sq < 1:
                return
            g_ = P.norm_gen(ev.get("gen"))
            con.execute("INSERT OR IGNORE INTO result_seq (device_id, gen, seq) VALUES (?,?,?)", (dev_id, g_ or "", sq))
            if g_:
                _note_gen(con, dev_id, g_, sq)

        def insert_event(ev, request_id, assign_ver, base_req_ver, kind, body, status, reason):
            # 휴대폰을 백업에서 되살리면 순번이 다시 쓰일 수 있다: event_id가 새것이면 받고 '순번 재사용' 경고만 남긴다(충돌 아님)
            warn = None
            old = one(con, "SELECT event_id FROM result_event WHERE device_id=? AND device_seq=? AND event_id!=? LIMIT 1", (dev_id, ev["seq"], ev["event_id"]))
            if old:
                warn = f"순번 {ev['seq']} 재사용(휴대폰 복원 추정) — 앞 기록 {old['event_id'][:8]}"
                counts["seq_reused"] = counts.get("seq_reused", 0) + 1
            con.execute("""INSERT INTO result_event (event_id, import_id, device_id, device_seq, request_id, assign_ver, base_req_ver, kind,
                body_json, apply_status, conflict_reason, device_ts, warning, gen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (ev["event_id"], imp, dev_id, ev["seq"], request_id, assign_ver, base_req_ver, kind, json.dumps(body, ensure_ascii=False),
                         status, reason, ev.get("device_ts"), warn, P.norm_gen(ev.get("gen"))))

        def one_event(ev):
            if not isinstance(ev.get("seq"), int):
                ev["seq"] = int(ev.get("seq") or 0)
            seen(ev)
            if one(con, "SELECT 1 AS x FROM result_event WHERE event_id=?", (ev["event_id"],)):
                counts["skipped_duplicate"] += 1
                return
            if ev["kind"] == "UNLOAD":  # 휴대폰에서 기록한 하차 (접수와 무관, 팀 단위)
                b = ev.get("body") if isinstance(ev.get("body"), dict) else {}
                ok_ = bool(crew) and (_num(b.get("volume_m3")) or _num(b.get("weight_kg")))
                insert_event(ev, "", ev.get("assign_ver") or 0, None, "UNLOAD", b, "APPLIED" if ok_ else "REJECTED",
                             None if ok_ else "팀에 연결되지 않은 휴대폰이거나 부피·중량 없음")
                if ok_:
                    con.execute("""INSERT OR IGNORE INTO unload_log (id, crew_id, work_date, at_time, place, volume_m3, weight_kg, note, recorded_at)
                        VALUES (?,?,?,?,?,?,?,?,?)""", (ev["event_id"], crew["id"], b.get("work_date") or payload.get("work_date") or today_kst(),
                                                         str(b.get("at_time") or "")[:5], str(b.get("place") or "반입지")[:60],
                                                         _num(b.get("volume_m3")) or None, _num(b.get("weight_kg")) or None,
                                                         ("휴대폰 기록 " + str(b.get("note") or "")).strip()[:200], now()))
                    counts["unloads"] = counts.get("unloads", 0) + 1
                return
            ev.setdefault("request_id", "")
            if not isinstance(ev.get("body"), dict):
                ev["body"] = {}
            req = one(con, "SELECT status, req_ver, origin_device, pii_purged_at FROM request WHERE id=?", (ev["request_id"],))
            if req and req["pii_purged_at"]:
                # 이미 파기한 접수는 늦은 기록으로 되살리지 않는다. 순번·이벤트 ID는 받아 두어 재전송과 수신 확인을 유지한다.
                insert_event(ev, ev["request_id"], ev.get("assign_ver") or 0, ev.get("base_req_ver"), ev["kind"],
                             {"pii_purged": True}, "REJECTED", "개인정보가 파기된 접수의 기록")
                counts["purged"] = counts.get("purged", 0) + 1
                return
            if ev["kind"] == "FIELD_INTAKE" and ev["request_id"]:  # 휴대폰 현장 접수 → PC 원본에 접수를 만든다(사무실 확인 표시)
                reason, note = None, None
                if one(con, "SELECT 1 AS x FROM request WHERE id=?", (ev["request_id"],)):
                    note = "이미 PC에 있는 접수(다시 만들지 않음)"
                else:
                    try:
                        with tx(con):
                            _field_request(con, ev["request_id"], ev["body"], dev_id, ev.get("device_ts"))
                        counts["field_intakes"] = counts.get("field_intakes", 0) + 1
                        touched.add(ev["request_id"])
                    except UserError as e:
                        reason = f"현장 접수 확인 필요: {e}"
                        counts["review"] = counts.get("review", 0) + 1
                insert_event(ev, ev["request_id"], 0, None, "FIELD_INTAKE", ev["body"] if reason else _strip_request_pii("FIELD_INTAKE", ev["body"]),
                             "CONFLICT" if reason else "APPLIED", reason or note)
                return
            if not ev["request_id"] and ev.get("kind") not in KNOWN_KINDS:
                # 앞으로 생길 팀 단위 기록(알 수 없는 종류): 접수와 무관하게 보관만 하고 병합을 막지 않는다
                insert_event(ev, "", ev.get("assign_ver") or 0, None, str(ev.get("kind") or "?")[:40], ev["body"], "APPLIED", None)
                counts["stored_other"] = counts.get("stored_other", 0) + 1
                return
            reason, info = None, None
            # 이 휴대폰이 현장에서 접수한 건: PC 목록에 들어오기 전(갱신본 0)에 남긴 결과는 배정 기록 없이도 받는다
            own = bool(req) and req.get("origin_device") == dev_id
            if not req:
                reason = "PC에 없는 접수"
            elif req["status"] == "CANCELLED" and ev["kind"] not in CANCELLED_OK_KINDS:
                reason = "취소된 접수의 결과"
            elif ev["kind"] in REVIEW_KINDS:
                reason = REVIEW_KINDS[ev["kind"]]
            elif ev["kind"] in ("ITEM_RESULT", "FIELD_ITEM_ADD", "ATTEMPT", "LOCATION_FIX", "REVISIT_APPT"):
                # 위치 보정·재방문 약속(예정일 변경)도 수거 결과와 같은 팀·갱신본 검사를 거친다
                act = one(con, """SELECT b.assign_ver, b.crew_id FROM active_assignment a JOIN assignment_batch b ON b.id=a.batch_id
                    WHERE a.request_id=?""", (ev["request_id"],))
                done_before = req and req["status"] in FINAL
                if not crew and not own:
                    reason = "팀에 연결되지 않은 휴대폰의 결과"
                elif act and crew and act["crew_id"] != crew["id"]:
                    reason = "다른 팀에 다시 배정된 접수"
                elif act and ev.get("assign_ver") is not None and ev["assign_ver"] < act["assign_ver"] and not (own and not ev["assign_ver"]):
                    # v0.13: 지금 배정도 같은 팀이면 옛 갱신본의 결과도 반영한다(다른 팀으로 옮겼거나 뺀 경우만 충돌)
                    info = f"이전 갱신본({ev['assign_ver']} < 현재 {act['assign_ver']})의 결과 — 같은 팀 배정 유지로 반영"
                elif not act and not done_before and not own and not one(con, """SELECT 1 AS x FROM assignment_stop s JOIN assignment_batch b ON b.id=s.batch_id
                        WHERE s.request_id=? AND b.crew_id=?""", (ev["request_id"], crew["id"] if crew else "")):
                    reason = "이 팀에 배정된 적 없는 접수"
                elif not act and not done_before and not own and crew and ev.get("assign_ver"):
                    # 그 목록(갱신본) 뒤에 같은 날 새 갱신본이 나왔는데 이 접수가 빠졌으면, 옛 목록의 결과는 사무실이 확인
                    b0 = one(con, """SELECT b.work_date FROM assignment_stop s JOIN assignment_batch b ON b.id=s.batch_id
                        WHERE s.request_id=? AND b.crew_id=? AND b.assign_ver=? ORDER BY b.created_at DESC LIMIT 1""", (ev["request_id"], crew["id"], ev["assign_ver"]))
                    newer = b0 and one(con, "SELECT MAX(assign_ver) AS v FROM assignment_batch WHERE crew_id=? AND work_date=?", (crew["id"], b0["work_date"]))
                    if newer and (newer["v"] or 0) > ev["assign_ver"]:
                        reason = f"새 목록(갱신본 {newer['v']})에서 빠진 접수의 옛 목록 결과"
            if not reason and ev['kind'] in ('ITEM_RESULT', 'FIELD_ITEM_ADD', 'ATTEMPT', 'LOCATION_FIX', 'REVISIT_APPT'):
                barrier = one(con, """SELECT json_extract(body_json,'$.pc_req_ver') AS version FROM result_event
                    WHERE request_id=? AND device_id='PC-COLLECTION' AND kind='ATTEMPT' AND apply_status='APPLIED'
                    ORDER BY rowid DESC LIMIT 1""", (ev['request_id'],))
                version = ev.get('base_req_ver')
                if barrier and (isinstance(version, bool) or not isinstance(version, int) or version < barrier['version']
                                or type(ev['body'].get('pc_collection_ver')) is not int or ev['body']['pc_collection_ver'] != barrier['version']):
                    reason = 'PC 수거 입력 전의 휴대폰 결과 — 수량을 확인한 뒤 반영해 주세요.'
            if not reason and ev["kind"] == "ITEM_RESULT":
                reason = _missing_line(con, ev["request_id"], ev["body"].get("line_id"))
            if ev["kind"] in ("FIELD_ITEM_ADD", "ITEM_RESULT"):  # 충돌함에 들어가는 것도 정리해서 저장(반영할 때 다시 정리)
                _sanitize_item_event(con, ev["kind"], ev["body"])
            status = "CONFLICT" if reason else "APPLIED"
            note = reason or info or (f"접수 수정(버전 {ev.get('base_req_ver')}→{req['req_ver']}) 뒤에 기록된 결과" if req and ev.get("base_req_ver") and ev["base_req_ver"] != req["req_ver"] else None)
            if reason and ev["kind"] in REVIEW_KINDS:
                counts["review"] = counts.get("review", 0) + 1  # 정정·취소 요청: 충돌이 아니라 사무실 확인 대상
            else:
                counts["conflict" if reason else "applied"] += 1
            insert_event(ev, ev["request_id"], ev.get("assign_ver") or 0, ev.get("base_req_ver"), ev["kind"], ev.get("body", {}), status, note)
            if ev["kind"] == "PHOTO_META" and not reason:
                _photo_meta(con, ev["request_id"], ev.get("body", {}), dev_id)
                counts["photos_expected"] += 1
            if not reason:
                touched.add(ev["request_id"])
                if ev["kind"] == "ATTEMPT":
                    attempted.add(ev["request_id"])
                if ev["kind"] == "LOCATION_FIX":
                    located.add(ev["request_id"])
                if ev["kind"] == "REVISIT_APPT":
                    _apply_revisit(con, ev["request_id"], ev.get("body", {}))
        for ev in payload.get("events", []):
            # 기록 하나가 DB 제약에 걸려도 같은 파일의 다른 결과까지 버리지 않는다: 기록마다 저장점, 실패한 기록은 충돌함으로
            if not isinstance(ev, dict):
                continue
            con.execute("SAVEPOINT ev1")
            try:
                one_event(ev)
                con.execute("RELEASE ev1")
            except Exception as ex:  # noqa: BLE001
                con.execute("ROLLBACK TO ev1"); con.execute("RELEASE ev1")
                counts["conflict"] += 1
                try:
                    seen(ev)
                    rid = str(ev.get("request_id") or "")
                    purged = one(con, "SELECT 1 AS x FROM request WHERE id=? AND pii_purged_at IS NOT NULL", (rid,))
                    con.execute("""INSERT INTO result_event (event_id, import_id, device_id, device_seq, request_id, assign_ver, base_req_ver, kind,
                        body_json, apply_status, conflict_reason, device_ts, gen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (str(ev.get("event_id") or uuid.uuid4()), imp, dev_id,
                                 ev["seq"] if isinstance(ev.get("seq"), int) and not isinstance(ev.get("seq"), bool) else -int.from_bytes(os.urandom(4), "big") - 1,
                                 rid,
                                 _int_clamp(ev.get("assign_ver"), 0, 10 ** 9), ev.get("base_req_ver") if isinstance(ev.get("base_req_ver"), int) else None,
                                 str(ev.get("kind") or "?")[:40],
                                 json.dumps({"pii_purged": True} if purged else ev.get("body") if isinstance(ev.get("body"), dict) else {}, ensure_ascii=False),
                                 "REJECTED" if purged else "CONFLICT",
                                 f"기록을 반영하지 못함(순번 {ev.get('seq')}): {type(ex).__name__}", ev.get("device_ts"), P.norm_gen(ev.get("gen"))))
                except Exception:  # noqa: BLE001
                    counts["failed"] = counts.get("failed", 0) + 1
        for rid in touched:
            _refresh_status(con, rid, new_attempt=rid in attempted, new_location=rid in located)
        assigned = label_auto.sync(con, touched)
        counts.update(labels_assigned=assigned["new_assigned"], labels_pending=assigned["pending"], labels_voided=assigned["voided"])
        con.execute("UPDATE result_import SET summary_json=? WHERE id=?", (json.dumps(counts), imp))
        audit(con, "import", imp, "RESULT", counts)
    return {**counts, "release_code": release, "import_id": imp}


def _note_gen(con, dev_id: str, gen: str, start: int):
    """기기·세대의 첫 번호(작은 쪽을 남김)와 마지막으로 받은 시각."""
    t = now()
    con.execute("""INSERT INTO device_gen (device_id, gen, start_seq, first_seen_at, last_seen_at) VALUES (?,?,?,?,?)
        ON CONFLICT(device_id, gen) DO UPDATE SET start_seq=MIN(device_gen.start_seq, excluded.start_seq), last_seen_at=excluded.last_seen_at""",
                (dev_id, gen, start, t, t))


def _photo_meta(con, rid: str, b: dict, dev_id: str):
    con.execute("INSERT OR IGNORE INTO photo (photo_id, request_id, phase, sha256, bytes, device_id, line_id, taken_at, lat, lon, acc) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (b.get("photo_id"), rid, b.get("phase"), b.get("sha256"), b.get("bytes"), dev_id, b.get("line_id"),
                 (b.get("taken_at") or b.get("file_ts") or None), _num(b.get("lat")), _num(b.get("lon")), _num(b.get("acc"))))


def _missing_line(con, rid: str, line_id) -> str | None:
    """수거 결과(ITEM_RESULT)가 가리키는 품목 줄이 지금 접수에 없으면 충돌 이유, 있으면 None.
    있는 줄 = 지워지지 않은 접수 품목 또는 이미 반영된 현장 추가 품목. 없는 줄의 결과를 '반영'으로 두면 계산에서는 조용히 빠져
    사무실이 모르고 지나가므로(예: 목록을 보낸 뒤 접수를 고쳐 품목을 뺌) 충돌함에서 확인하게 한다."""
    if line_id and (one(con, "SELECT 1 AS x FROM request_item WHERE line_id=? AND request_id=? AND removed=0", (line_id, rid))
                    or one(con, """SELECT 1 AS x FROM result_event WHERE request_id=? AND kind='FIELD_ITEM_ADD' AND apply_status='APPLIED'
                        AND json_extract(body_json, '$.line_id')=?""", (rid, line_id))):
        return None
    if line_id and one(con, "SELECT 1 AS x FROM request_item WHERE line_id=? AND request_id=? AND removed=1", (line_id, rid)):
        return "접수 수정으로 빠진 품목의 수거 결과"
    return "접수에 없는 품목 줄의 수거 결과"


def _sanitize_item_event(con, kind: str, b: dict):
    """휴대폰이 보낸 현장 품목·수량을 그대로 믿지 않는다: 수량은 0~9999 정수, 현장 추가 단가는 PC 수수료표로 다시 정한다."""
    if kind == "ITEM_RESULT":
        for k in ("qty_confirmed", "qty_collected"):
            if k in b:
                b[k] = _int_clamp(b[k])
        if b.get("qty_collected", 0) > b.get("qty_confirmed", 0):
            b["qty_collected"] = b.get("qty_confirmed", 0)
        return
    b["qty"] = _int_clamp(b.get("qty"))
    r = fee_rows(con).get(b.get("fee_rate_id") or "")
    if r:
        ok_ = r["status"] == "CONFIRMED"
        b.update(name=r["name"], spec=r["spec"], unit=r["unit"], unit_label=r["unit_label"], unit_price=r["price"],
                 price_status="CONFIRMED" if ok_ else "PENDING", fee_version_id=fee_state(con)["version_id"])
    else:  # 표에 없는 품목: 휴대폰 단가를 쓰지 않고 금액 확인 대기
        b.update(fee_rate_id=None, unit_price=None, price_status="PENDING")


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
    released = False
    if date != r["planned_date"]:  # 날짜가 바뀌면 옛 날짜 배정을 푼다(그날 마감 점검·새 날짜 배정이 막히지 않게)
        released = bool(con.execute("DELETE FROM active_assignment WHERE request_id=?", (rid,)).rowcount)
        if released and r["status"] == "ASSIGNED":
            con.execute("UPDATE request SET status='RECEIVED' WHERE id=?", (rid,))
    audit(con, "request", rid, "REVISIT_APPT", {"date": date, "start": start, "end": end, **({"released": True} if released else {})})


def list_conflicts(con, include_resolved=False):
    sql = """SELECT e.event_id, e.kind, e.body_json, e.conflict_reason, e.apply_status, e.resolved_reason, e.device_ts, e.assign_ver,
        r.receipt_no, r.name, r.status AS request_status, e.request_id, e.import_id, e.warning FROM result_event e LEFT JOIN request r ON r.id=e.request_id
        WHERE e.conflict_reason IS NOT NULL AND e.kind NOT IN ('DIAL_INTENT')"""
    if not include_resolved:
        sql += " AND e.apply_status='CONFLICT'"
    out = rows(con, sql + " ORDER BY e.device_ts")
    for c in out:  # 정정 요청: 지금 PC 원본 값을 나란히 보여 준다
        if c["kind"] == "CORRECTION" and c["request_id"]:
            r = one(con, "SELECT name, phone, address, spot_note, planned_date FROM request WHERE id=?", (c["request_id"],))
            c["current"] = r or {}
    return out


def resolve_conflict(con, event_id: str, action: str, reason: str):
    e = one(con, "SELECT * FROM result_event WHERE event_id=?", (event_id,))
    if not e or e["apply_status"] != "CONFLICT":
        raise UserError("처리할 충돌 기록이 없습니다.")
    if action not in ("APPLY", "REJECT"):
        raise UserError("반영 또는 거부를 골라 주세요.")
    if not (reason or "").strip():
        raise UserError("판단 사유를 적어 주세요.")
    try:
        body = json.loads(e["body_json"] or "{}")
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if action == "APPLY" and e["kind"] == "ITEM_RESULT" and e["request_id"]:
        miss = _missing_line(con, e["request_id"], body.get("line_id"))
        if miss:  # 반영해도 계산에 들어갈 줄이 없다: '반영됨'으로 보이기만 하고 아무 일도 없으므로 막는다
            raise UserError(f"{miss}라 반영할 수 없습니다. 거부한 뒤, 실제로 수거했으면 접수 품목을 고치세요.")
    with tx(con):
        if action == "APPLY" and one(con, "SELECT 1 AS x FROM request WHERE id=? AND pii_purged_at IS NOT NULL", (e["request_id"],)):
            raise UserError("개인정보가 파기된 접수는 현장 기록을 반영할 수 없습니다. [거부]로 정리하세요.")
        con.execute("UPDATE result_event SET apply_status=?, resolved_reason=?, resolved_at=? WHERE event_id=?",
                    ("APPLIED" if action == "APPLY" else "REJECTED", reason.strip(), now(), event_id))
        if action == "APPLY":
            if e["kind"] in ("FIELD_ITEM_ADD", "ITEM_RESULT"):
                # 직접 가져올 때와 똑같이: 수량은 0~9999 정수, 현장 추가 단가는 PC 수수료표(휴대폰 단가를 믿지 않음)
                _sanitize_item_event(con, e["kind"], body)
                con.execute("UPDATE result_event SET body_json=? WHERE event_id=?", (json.dumps(body, ensure_ascii=False), event_id))
            if e["kind"] == "FIELD_INTAKE" and not one(con, "SELECT 1 AS x FROM request WHERE id=?", (e["request_id"],)):
                _field_request(con, e["request_id"], body, e["device_id"], e["device_ts"])
            if e["kind"] == "CORRECTION":
                _apply_correction(con, e["request_id"], body, reason.strip())
            if e["kind"] == "CANCEL_REQUEST":
                code, text, full = _reason(CANCEL_CODES, body.get("code") or "OTHER", body.get("text") or "현장 요청", "취소")
                _cancel(con, e["request_id"], code, text, f"{full} (현장 요청 · 사무실 확인: {reason.strip()})"[:300])
            if e["kind"] == "REVISIT_APPT":
                _apply_revisit(con, e["request_id"], body)
            if e["kind"] == "PHOTO_META":
                _photo_meta(con, e["request_id"], body, e["device_id"])
            if e["request_id"]:
                _refresh_status(con, e["request_id"], new_attempt=e["kind"] == "ATTEMPT", new_location=e["kind"] == "LOCATION_FIX")
        if e["kind"] in ("FIELD_INTAKE", "CORRECTION"):  # 처리한 요청 기록에서는 개인정보를 지운다(원본은 접수·버전 이력에)
            con.execute("UPDATE result_event SET body_json=? WHERE event_id=?", (json.dumps(_strip_request_pii(e["kind"], body), ensure_ascii=False), event_id))
        audit(con, "event", event_id, "RESOLVE_" + action, {"reason": reason.strip(), "kind": e["kind"], **({"request": e["request_id"]} if e["request_id"] else {})})
        if e["request_id"] and action == "APPLY":
            label_auto.sync(con, [e["request_id"]])


def resolve_conflicts_bulk(con, action: str, reason: str, request_id: str = "", import_id: str = "") -> dict:
    """한 접수(또는 한 가져오기)의 남은 충돌을 한 사유로 한꺼번에 반영·거부. 반영할 수 없는 기록(예: 형식이 틀린 정정 요청)은 건너뛰고 이유를 알려 준다."""
    if bool(request_id) == bool(import_id):
        raise UserError("접수 또는 가져오기 하나를 골라 주세요.")
    if action not in ("APPLY", "REJECT"):
        raise UserError("반영 또는 거부를 골라 주세요.")
    if not (reason or "").strip():
        raise UserError("판단 사유를 적어 주세요.")
    col = "request_id" if request_id else "import_id"
    evs = rows(con, f"SELECT event_id, kind FROM result_event WHERE {col}=? AND apply_status='CONFLICT' ORDER BY device_ts, device_seq", (request_id or import_id,))
    if not evs:
        raise UserError("처리할 충돌 기록이 없습니다.")
    done, failed = 0, []
    with tx(con):
        for e in evs:
            try:
                with tx(con):
                    resolve_conflict(con, e["event_id"], action, reason)
                done += 1
            except UserError as ex:
                failed.append({"event_id": e["event_id"], "kind": e["kind"], "error": str(ex)})
        audit(con, "event", request_id or import_id, "RESOLVE_BULK_" + action, {"reason": reason.strip(), "count": done, "failed": len(failed),
                                                                              "by": "request" if request_id else "import"})
    return {"resolved": done, "failed": failed}


# ------------------------------------------------------------------ 휴대폰(PC 연결 방식)의 현장 접수·정정 요청·취소 요청
def _strip_request_pii(kind: str, b: dict) -> dict:
    """반영·거부한 요청 기록에 남길 내용(개인정보 없이). 실제 값은 접수 원본과 버전 이력에 있다."""
    keep = {k: b[k] for k in ("worker", "temp_no") if b.get(k)}
    if kind == "FIELD_INTAKE":
        it = b.get("items")
        return {**keep, "planned_date": b.get("planned_date"), "items": it if isinstance(it, int) else len(it or []), "pii_purged": True}
    if kind == "CORRECTION":
        f = b.get("fields") or {}
        if not isinstance(f, dict):  # 이미 지운 기록(칸 이름만 남음): 그대로
            return {**keep, "fields": f if isinstance(f, str) else ",".join(map(str, f)), **({"date": b["date"]} if b.get("date") else {}),
                    "reason": (b.get("reason") or "")[:100], "pii_purged": True}
        return {**keep, "fields": ",".join(k for k in CORRECTABLE if k in f), **({"date": f["planned_date"]} if f.get("planned_date") else {}),
                "reason": (b.get("reason") or "")[:100], "pii_purged": True}
    return b


def _field_request(con, rid: str, b: dict, dev_id: str, device_ts: str | None = None) -> str:
    """휴대폰 현장 접수로 PC 원본 접수를 만든다(트랜잭션은 호출하는 쪽). 금액은 PC 수수료표로 다시 계산하고,
    PC 표에 없는 품목은 '확인 대기'로 둔다(0원·최저가로 두지 않음). 품목 행 ID는 휴대폰 것을 그대로 써서 현장 결과가 이어진다."""
    if not re.fullmatch(r"[0-9a-f-]{36}", rid or ""):
        raise UserError("접수 ID 형식이 올바르지 않습니다.")
    fr = fee_rows(con)
    items = []
    for it in (b.get("items") or [])[:50]:
        if not isinstance(it, dict):
            continue
        x = {"line_id": str(it.get("line_id") or "")[:40] or None, "qty": it.get("qty"), "name": str(it.get("name") or "")[:60],
             "spec": str(it.get("spec") or "")[:60], "unit": str(it.get("unit") or "EA")[:10], "unit_label": str(it.get("unit_label") or "개")[:10]}
        if it.get("fee_rate_id") in fr:
            x["fee_rate_id"] = it["fee_rate_id"]
        elif not x["name"]:
            continue
        items.append(x)
    data = {k: str(b.get(k) or "")[:300] for k in ("name", "phone", "address", "spot_note", "memo", "planned_date")}
    ch = b.get("channel") if b.get("channel") in ("현장", "전화", "방문") else "현장"  # 휴대폰에서 고른 접수 경로
    data.update(channel=ch, items=items, appt_start=b.get("eta") if re.fullmatch(r"\d\d:\d\d", str(b.get("eta") or "")) else None)
    if _num(b.get("lat")) is not None and _num(b.get("lon")) is not None:
        data.update(lat=_num(b["lat"]), lon=_num(b["lon"]), geo_source="ADDR")
    dong_s = str(b.get("dong") or "").strip()[:20]
    if dong_s and area.get_dong(con, dong_s):
        data["dong"] = dong_s
        det = area.detect_dong(con, data["address"])  # PC 주소 자료(건물 단위)로 정한 동이 다르면 그쪽을 쓴다(사무실 확인 대상)
        if det.get("source") == "ADDR" and det.get("dong") and det["dong"] != dong_s:
            data["dong"] = det["dong"]
    d = validate(data)
    built = build_items(items, con)
    for it in built:
        if one(con, "SELECT 1 AS x FROM request_item WHERE line_id=?", (it["line_id"],)):
            raise UserError("품목 행 ID가 이미 PC에 있습니다(같은 기록을 다시 보냈는지 확인).")
    dong, _ = _resolve_dong(con, data, d["address"])
    _, receipt = _insert_request(con, d, built, data, dong, rid=rid)
    con.execute("UPDATE request SET origin_device=? WHERE id=?", (dev_id, rid))
    # 접수 시각 = 휴대폰에서 받은 때(PC로 가져온 때가 아님) — 처리 일수·기간 집계가 맞게. 미래 시각은 쓰지 않는다
    if device_ts and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z", str(device_ts)) and str(device_ts) <= now():
        con.execute("UPDATE request SET received_at=? WHERE id=?", (device_ts, rid))
    audit(con, "request", rid, "CREATE", {"receipt_no": receipt, "channel": ch, "origin": "FIELD", "device": (dev_id or "")[:8],
                                          **({"temp_no": str(b["temp_no"])[:30]} if b.get("temp_no") else {}), **({"dong": dong} if dong else {})})
    return receipt


def _apply_correction(con, rid: str, b: dict, office_reason: str):
    """현장 정정 요청(이름·연락처·주소·상세 위치·예정일)을 원본에 반영한다. 이전 내용은 버전으로 보존(트랜잭션은 호출하는 쪽)."""
    r = one(con, "SELECT * FROM request WHERE id=?", (rid,))
    if not r:
        raise UserError("접수를 찾을 수 없습니다.")
    if r["status"] == "CANCELLED":
        raise UserError("취소된 접수는 고칠 수 없습니다. [거부]로 정리하세요.")
    f = {k: str(v).strip() for k, v in (b.get("fields") or {}).items() if k in CORRECTABLE and v is not None}
    new = {k: (f[k] if k in f else (r[k] or "")) for k in CORRECTABLE}
    new["phone"] = norm_phone(new["phone"])
    if not new["name"]:
        raise UserError("이름이 비어 있습니다.")
    if not re.fullmatch(r"0\d{8,10}", new["phone"]):
        raise UserError("요청한 연락처 형식이 올바르지 않습니다.")
    if not new["address"]:
        raise UserError("주소가 비어 있습니다.")
    if new["planned_date"] and not re.fullmatch(r"\d{4}-\d\d-\d\d", new["planned_date"]):
        raise UserError("요청한 예정일 형식이 올바르지 않습니다.")
    changed = [k for k in CORRECTABLE if (new[k] or None) != (r[k] or None)]
    if not changed:
        raise UserError("PC 원본과 같은 내용입니다. [거부]로 정리하세요.")
    flag = _after_close(con, rid)
    con.execute("INSERT INTO request_version VALUES (?,?,?,?,?)",
                (rid, r["req_ver"], json.dumps(_snapshot(con, rid), ensure_ascii=False), f"현장 정정 요청 반영: {office_reason}"[:200], now()))
    dong, lat, lon, geo, status = r["dong"], r["lat"], r["lon"], r["geo_source"], r["status"]
    if "address" in changed:  # 주소가 바뀌면 동을 다시 정하고, 이전 주소의 위치는 쓰지 않는다(현장 위치 보정이 있으면 다시 반영됨)
        dong = area.detect_dong(con, new["address"])["dong"] or r["dong"]
        lat = lon = geo = None
    if "planned_date" in changed and con.execute("DELETE FROM active_assignment WHERE request_id=?", (rid,)).rowcount and status == "ASSIGNED":
        status = "RECEIVED"
    con.execute("""UPDATE request SET req_ver=req_ver+1, name=?, phone=?, address=?, spot_note=?, planned_date=?, dong=?, lat=?, lon=?, geo_source=?,
        status=?, updated_at=? WHERE id=?""", (new["name"], new["phone"], new["address"], new["spot_note"], new["planned_date"] or None, dong, lat, lon, geo,
                                               status, now(), rid))
    audit(con, "request", rid, "UPDATE", {"reason": f"현장 정정 요청: {office_reason}"[:200], "from_ver": r["req_ver"], "fields": changed, "origin": "FIELD", **flag})


def field_intakes(con) -> list[dict]:
    """휴대폰에서 현장 접수했는데 아직 사무실이 확인하지 않은 접수."""
    return rows(con, """SELECT id, receipt_no, name, address, planned_date, status, received_at, dong FROM request
        WHERE origin_device IS NOT NULL AND field_checked_at IS NULL ORDER BY received_at""")


def check_field_intake(con, rid: str) -> dict:
    """현장 접수를 사무실이 확인했다고 표시(주소·품목·금액을 본 뒤). 내용 수정은 일반 접수 수정으로."""
    r = one(con, "SELECT origin_device, field_checked_at FROM request WHERE id=?", (rid,))
    if not r or not r["origin_device"]:
        raise UserError("휴대폰 현장 접수가 아닙니다.")
    if r["field_checked_at"]:
        raise UserError("이미 확인한 접수입니다.")
    with tx(con):
        con.execute("UPDATE request SET field_checked_at=?, field_checked_by=? WHERE id=?", (now(), ACTOR.get(), rid))
        audit(con, "request", rid, "FIELD_CHECK", {})
    return {"ok": True}


# ------------------------------------------------------------------ 휴대폰 단독 방식 자료 → PC로 옮기기
def open_solo_backup(raw: bytes, password: str) -> dict:
    """휴대폰 단독 방식 백업(.dgb: 'DGB1'|salt16|iv12|AES-GCM, PBKDF2-SHA256 60만 회, deflate-raw JSON)을 연다."""
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


def _solo_receipt(con, rec: str) -> str:
    """단독 휴대폰 접수번호가 PC에 이미 있으면 S 표시(…-S0001), 그것도 있으면 …-S0001-2, -3 … 빈 번호까지."""
    exists = lambda x: one(con, "SELECT 1 AS x FROM request WHERE receipt_no=?", (x,))
    if not exists(rec):
        return rec
    base = re.sub(r"-(\d+)$", r"-S\1", rec) if re.search(r"-\d+$", rec) else rec + "-S"
    cand, n = base, 2
    while exists(cand):
        cand, n = f"{base}-{n}", n + 1
    return cand


def _solo_items(con, r: dict) -> list[dict]:
    """단독 휴대폰 품목 → PC 수수료표로 다시 계산(build_items). 표에 없는 품목은 이름으로 '금액 확인 대기'. 수량이 틀리면 UserError."""
    fr = fee_rows(con)
    src = r.get("items") or []
    if not isinstance(src, list):
        raise UserError("품목 형식이 올바르지 않습니다.")
    out = []
    for it in src:
        if not isinstance(it, dict):
            raise UserError("품목 형식이 올바르지 않습니다.")
        q = it.get("qty")
        if isinstance(q, bool) or not re.fullmatch(r"\d{1,4}", str(q if q is not None else "").strip()) or int(str(q).strip()) <= 0:
            raise UserError(f"품목 '{str(it.get('name') or '')[:20]}' 수량이 1~9999 정수가 아닙니다({str(q)[:10]}).")
        x = {"line_id": str(it.get("line_id") or "")[:40] or None, "qty": int(str(q).strip()), "name": str(it.get("name") or "")[:60],
             "spec": str(it.get("spec") or "")[:60], "unit": str(it.get("unit") or "EA")[:10], "unit_label": str(it.get("unit_label") or "개")[:10]}
        if it.get("fee_rate_id") in fr:
            x["fee_rate_id"] = it["fee_rate_id"]
        elif not x["name"]:
            raise UserError("품목 이름이 없습니다.")
        out.append(x)
    return build_items(out, con) if out else []


def _solo_check(con, r) -> tuple[dict | None, str | None]:
    """단독 접수 한 건 검사 → (넣을 자료, 오류). 오류가 있으면 그 건만 건너뛴다(미리보기에 행별로 표시)."""
    if not isinstance(r, dict):
        return None, "접수 형식이 올바르지 않습니다."
    if not isinstance(r.get("request_id"), str) or not re.fullmatch(r"[0-9A-Za-z-]{8,64}", r["request_id"]):
        return None, "접수 ID가 없거나 형식이 틀립니다."
    if not isinstance(r.get("receipt_no"), str) or not r["receipt_no"].strip():
        return None, "접수번호가 없습니다."
    purged = bool(r.get("pii_purged")) or not (str(r.get("name") or "").strip() and norm_phone(str(r.get("phone") or "")) and str(r.get("address") or "").strip())
    rr = {**r, "name": "(정리됨)", "phone": "00000000000", "address": "(정리됨)", "spot_note": "", "memo": "", "lat": None, "lon": None} if purged else r
    try:
        items = _solo_items(con, r)
        d = validate({**{k: (str(rr.get(k)) if rr.get(k) is not None else None) for k in ("name", "phone", "address", "spot_note", "memo", "planned_date", "appt_start", "appt_end")},
                      "lat": _num(rr.get("lat")), "lon": _num(rr.get("lon")), "items": items or [{"name": "(품목 없음)", "qty": 1}]})  # 품목 없는 취소 건도 옮긴다
    except UserError as e:
        return None, str(e)
    if not items:
        items = build_items([{"name": "(품목 없음)", "qty": 1}], con)
    return {"r": r, "rr": rr, "d": d, "items": items, "purged": purged}, None


def prepare_solo(raw: bytes, password: str) -> dict:
    """단독 백업 열기(PBKDF2 60만 회 — 오래 걸림). 서버는 DB 잠금 밖에서 한다."""
    return open_solo_backup(raw, password)


def import_solo(con, raw: bytes, password: str, crew_id: str | None = None, preview: bool = False, opened: dict | None = None) -> dict:
    """단독 방식 휴대폰의 접수·현장 기록·사진을 PC 원본으로 옮긴다. 같은 접수(같은 ID)는 다시 들어오지 않는다.
    접수마다 검사해서 틀린 건(수량 0·접수번호 없음 등)은 미리보기에 행별 오류로 보여 주고 옮길 때 건너뛴다."""
    obj = opened or open_solo_backup(raw, password)
    state = obj.get("state") or {}
    if state.get("mode") != "SOLO":
        raise UserError("휴대폰 단독 방식의 백업이 아닙니다.")
    reqs = state.get("requests") or []
    events = [e for e in (state.get("events") or []) if isinstance(e, dict) and e.get("event_id")]
    photos = obj.get("photos") or {}
    if not isinstance(reqs, list) or not isinstance(photos, dict):
        raise UserError("단독 백업 형식이 올바르지 않습니다.")
    checked, errors = [], []
    for r in reqs:
        c, err = _solo_check(con, r)
        if err:
            errors.append({"request_id": str((r or {}).get("request_id") or "")[:8] if isinstance(r, dict) else "", "receipt_no": str((r or {}).get("receipt_no") or "") if isinstance(r, dict) else "",
                           "error": err})
        else:
            checked.append(c)
    new = [c for c in checked if not one(con, "SELECT 1 AS x FROM request WHERE id=?", (c["r"]["request_id"],))]
    # 단독 휴대폰마다 기기 표시를 따로(순번이 1부터라 여러 대를 가져오면 겹친다): 가장 이른 기록 ID로 만든 고정 값
    first = min(events, key=lambda e: e.get("seq") if isinstance(e.get("seq"), int) else 0)["event_id"] if events else \
        ",".join(sorted(str(r.get("request_id")) for r in reqs if isinstance(r, dict)))
    solo_dev = "SOLO:" + hashlib.sha256(str(first).encode()).hexdigest()[:12]
    info = {"requests": len(reqs), "new": len(new), "already": len(checked) - len(new), "invalid": len(errors), "errors": errors, "events": len(events),
            "photos": len(photos), "unloads": sum(1 for e in events if e.get("kind") == "UNLOAD"), "created_at": obj.get("created_at")}
    if preview:
        return info
    if crew_id and not one(con, "SELECT 1 AS x FROM crew WHERE id=?", (crew_id,)):
        raise UserError("없는 수거팀입니다.")
    sha = hashlib.sha256(raw).hexdigest()
    if one(con, "SELECT 1 AS x FROM result_import WHERE package_sha256=?", (sha,)):
        return {**info, "duplicate_file": True}
    imp = str(uuid.uuid4())
    newids = {c["r"]["request_id"] for c in new}
    purgedids = {c["r"]["request_id"] for c in new if c["purged"]}
    located = set()
    line_map: dict[tuple[str, str], str] = {}
    counts = {"requests": 0, "events": 0, "photos_recovered": 0, "unloads_applied": 0, "skipped": 0, "renamed": 0}
    with tx(con):
        con.execute("INSERT INTO result_import (id, channel, device_id, package_sha256, imported_at, summary_json, release_code) VALUES (?,?,?,?,?,NULL,NULL)",
                    (imp, "USB", "SOLO", sha, now()))
        for c in new:
            r, d, purged = c["r"], c["d"], c["purged"]
            dong_s = str(r.get("dong") or "").strip()[:20] or None
            rec = _solo_receipt(con, r["receipt_no"].strip()[:40])
            counts["renamed"] += rec != r["receipt_no"].strip()[:40]
            dcode = (one(con, "SELECT code FROM dong WHERE name=?", (dong_s,)) or {}).get("code") if dong_s else None
            rv = r.get("req_ver")
            con.execute("""INSERT INTO request (id, receipt_no, req_ver, name, phone, address, spot_note, memo, lat, lon, geo_source,
                planned_date, appt_start, appt_end, must_today, urgent, priority, status, cancel_reason, received_at, updated_at, dong, levy_no, pii_purged_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,?,?,?,?,?,?,?)""",
                        (r["request_id"], rec, rv if isinstance(rv, int) and rv > 0 else 1, d["name"], d["phone"], d["address"], d["spot_note"], d["memo"],
                         d["lat"], d["lon"], ({"ADDR": "ADDR", "GPS": "FIELD"}.get(r.get("geo_source"), "MANUAL")) if d["lat"] is not None else None,
                         d["planned_date"] or None, None, None,
                         "CANCELLED" if r.get("status") == "CANCELLED" else "RECEIVED", None if purged else (str(r.get("cancel_reason"))[:200] if r.get("cancel_reason") else None),
                         _sane_ts(r.get("received_at")) or now(), _sane_ts(r.get("updated_at")) or now(), dong_s,
                         _store.next_levy_no(con, _store.levy_year(r.get("received_at") if isinstance(r.get("received_at"), str) else None), dcode),
                         (r.get("pii_purged") if isinstance(r.get("pii_purged"), str) else now()) if purged else None))
            for it in c["items"]:
                # 품목 행 ID는 PC 전체에서 하나여야 한다. 이미 쓰인 ID는 새 ID로 바꾸고, 그 접수의 현장 기록·사진이 가리키는 행 ID도 함께 바꾼다.
                lid = it["line_id"]
                if one(con, "SELECT 1 AS x FROM request_item WHERE line_id=?", (lid,)):
                    line_map[(r["request_id"], lid)] = it["line_id"] = str(uuid.uuid4())
                    counts["lines_renumbered"] = counts.get("lines_renumbered", 0) + 1
                _insert_item(con, r["request_id"], it)
            for v in r.get("versions") or []:
                if not isinstance(v, dict):
                    continue
                snap = v.get("snapshot") or {}
                if purged and isinstance(snap, dict):
                    snap = {k: x for k, x in snap.items() if k not in ("name", "phone", "address", "spot_note", "memo", "lat", "lon", "cancel_reason")}
                vv = v.get("req_ver")
                con.execute("INSERT OR IGNORE INTO request_version VALUES (?,?,?,?,?)",
                            (r["request_id"], vv if isinstance(vv, int) else 1, json.dumps(snap, ensure_ascii=False), "(파기)" if purged else str(v.get("reason") or "휴대폰 단독 수정")[:200],
                             _sane_ts(v.get("at")) or now()))
            counts["requests"] += 1
        for e in events:
            if one(con, "SELECT 1 AS x FROM result_event WHERE event_id=?", (e["event_id"],)):
                counts["skipped"] += 1
                continue
            body = e.get("body") if isinstance(e.get("body"), dict) else {}
            seq = e.get("seq") if isinstance(e.get("seq"), int) else 0
            if e.get("kind") == "UNLOAD":
                ok_ = bool(crew_id) and (_num(body.get("volume_m3")) or _num(body.get("weight_kg")))
                con.execute("""INSERT INTO result_event (event_id, import_id, device_id, device_seq, request_id, assign_ver, base_req_ver, kind,
                    body_json, apply_status, conflict_reason, device_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (e["event_id"], imp, solo_dev, seq, "", 0, None, "UNLOAD", json.dumps(body, ensure_ascii=False),
                             "APPLIED" if ok_ else "REJECTED", None if ok_ else "수거팀을 고르지 않아 하차 기록을 붙일 곳이 없음", e.get("ts")))
                if ok_:
                    con.execute("""INSERT OR IGNORE INTO unload_log (id, crew_id, work_date, at_time, place, volume_m3, weight_kg, note, recorded_at)
                        VALUES (?,?,?,?,?,?,?,?,?)""", (e["event_id"], crew_id, body.get("work_date") if _date_ok(str(body.get("work_date") or "")) else today_kst(),
                                                         str(body.get("at_time") or "")[:5], str(body.get("place") or "반입지")[:60],
                                                         _num(body.get("volume_m3")) or None, _num(body.get("weight_kg")) or None, "휴대폰 단독 기록", now()))
                    counts["unloads_applied"] += 1
                continue
            if e.get("request_id") not in newids:  # 이미 PC에 있던 접수의 기록은 덮어쓰지 않음
                counts["skipped"] += 1
                continue
            if (e["request_id"], body.get("line_id")) in line_map:
                body = {**body, "line_id": line_map[(e["request_id"], body["line_id"])]}
            kind = str(e.get("kind") or "?")[:40]
            private_event = e["request_id"] in purgedids and kind in ("FIELD_INTAKE", "CORRECTION", "CANCEL_REQUEST", "FIELD_MEMO", "LOCATION_FIX", "PHOTO_META")
            if private_event:
                # 단독 백업도 파기 경계를 유지한다. 익명 수거·금액 기록은 남기되 위치·메모·사진·정정 원문은 되살리지 않는다.
                body = {"pii_purged": True}
            else:
                if e["request_id"] in purgedids:
                    body = dict(body)
                    for key in ("text", "lat", "lon", "acc", "note", "location_note", "number_note"):
                        if key in body:
                            body[key] = None if key in ("lat", "lon", "acc") else ""
                    body["pii_purged"] = True
                if kind in ("FIELD_ITEM_ADD", "ITEM_RESULT"):  # 휴대폰 단가·수량을 그대로 믿지 않음(PC 수수료표, 0~9999)
                    body = dict(body)
                    _sanitize_item_event(con, kind, body)
            con.execute("""INSERT INTO result_event (event_id, import_id, device_id, device_seq, request_id, assign_ver, base_req_ver, kind,
                body_json, apply_status, conflict_reason, device_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (e["event_id"], imp, solo_dev, seq, e["request_id"], 0, e.get("base_req_ver") if isinstance(e.get("base_req_ver"), int) else None, kind,
                         json.dumps(body, ensure_ascii=False), "REJECTED" if private_event else "APPLIED", "개인정보가 파기된 접수의 기록" if private_event else None, e.get("ts")))
            counts["events"] += 1
            if private_event:
                counts["purged"] = counts.get("purged", 0) + 1
                continue
            if kind == "LOCATION_FIX":
                located.add(e["request_id"])
            if kind == "PHOTO_META":
                data = None
                if body.get("photo_id") in photos:
                    try:
                        data = base64.b64decode(photos[body["photo_id"]])
                    except (TypeError, ValueError):
                        data = None
                good = data is not None and hashlib.sha256(data).hexdigest() == body.get("sha256")
                con.execute("INSERT OR IGNORE INTO photo (photo_id, request_id, phase, sha256, bytes, device_id, data, recovery_status, recovered_at, line_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (body.get("photo_id"), e["request_id"], body.get("phase"), body.get("sha256"), body.get("bytes"), "SOLO", data if good else None,
                             "RECOVERED" if good else "EXPECTED", now() if good else None, body.get("line_id")))
                counts["photos_recovered"] += good
            elif kind == "REVISIT_APPT":
                _apply_revisit(con, e["request_id"], body)
        for rid in newids:
            _refresh_status(con, rid, new_location=rid in located)
        assigned = label_auto.sync(con, newids)
        counts.update(labels_assigned=assigned["new_assigned"], labels_pending=assigned["pending"], labels_voided=assigned["voided"])
        con.execute("UPDATE result_import SET summary_json=? WHERE id=?", (json.dumps({"source": "SOLO", **counts, "invalid": len(errors)}), imp))
        audit(con, "import", imp, "SOLO", {**counts, "invalid": len(errors)})
    return {**info, **counts}
