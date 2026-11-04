"""개인정보 보호 업무: 보유기간 지난 접수의 가명 처리(파기)·파기 대장, 개인정보 접속기록(해시 사슬)·무결성 검사·월 점검.

원칙
- 파기는 이름·연락처·주소·상세 위치·메모·좌표·민원 내용·현장 메모·사진처럼 사람을 알아볼 수 있는 값만 지운다.
  접수번호·부과번호·행정동·품목·금액·날짜는 통계·세입 확인을 위해 남긴다(가명 처리).
- 보유기간(기본 3년, 처리 완료·취소일부터)은 정책 값이다. 기관이 조례·기록물 관리 기준에 맞춰 정한다.
- 접속기록은 누가 언제 어떤 개인정보 화면(상세·목록·출력·내보내기)을 열었는지 남기며, 목록은 건수와 조건만 남긴다.
  각 행은 이전 행의 해시를 이어 받아 고치거나 지우면 무결성 검사에서 드러난다. 2년 이상 보관한다.
- 변경 이력(audit_log)도 같은 방식으로 사슬을 잇는다(이 기능 전에 쌓인 행은 '이전 기록'으로 표시하고 검사에서 뺀다).
"""
from __future__ import annotations

import contextvars
import datetime as dt
import json
import uuid

from dgpc import service as S

CLIENT: contextvars.ContextVar[str] = contextvars.ContextVar("client", default="local")
ACCESS_ACTIONS = {"VIEW": "상세 조회", "LIST": "목록 조회", "PRINT": "출력", "EXPORT": "내보내기", "PHOTO": "사진 보기", "SEARCH": "주소·중복 조회"}
PURGED = "(파기)"
PURGED_PHONE = "00000000000"
_PII_EVENT_KEYS = ("text", "lat", "lon", "acc", "note", "location_note", "number_note")
_PII_SNAPSHOT_KEYS = ("name", "phone", "address", "spot_note", "memo", "lat", "lon", "cancel_reason")


# ------------------------------------------------------------------ 설정 값
def retention_years(con) -> int:
    return int(S.get_setting(con, "pii_retention_years", 3))


def access_retention_years(con) -> int:
    return max(2, int(S.get_setting(con, "access_log_retention_years", 2)))


def _kst_today() -> dt.date:
    return dt.date.fromisoformat(S.today_kst())


def _minus_years(d: dt.date, n: int) -> dt.date:
    try:
        return d.replace(year=d.year - n)
    except ValueError:  # 2월 29일
        return d.replace(year=d.year - n, day=28)


def _utc_start_of(d: dt.date) -> str:
    return dt.datetime.combine(d, dt.time()).replace(tzinfo=S.KST).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ 접속기록
def _access_fields(r: dict) -> list:
    return [r["id"], r["at"], r["actor"], r["action"], r["target"], r["count"], r["filter"], r["client"]]


def _access_anchor(con) -> str:
    a = S.get_setting(con, "access_chain_anchor", None)
    return a["hash"] if a else ""


def record_access(con, action: str, target: str = "", count: int | None = None, filt: str = ""):
    """개인정보 조회 기록 한 줄(트랜잭션은 이 함수가 연다, 이미 열려 있으면 그 안에서)."""
    if action not in ACCESS_ACTIONS:
        raise ValueError(action)
    def ins():
        last = con.execute("SELECT id, hash FROM access_log ORDER BY id DESC LIMIT 1").fetchone()
        prev = last[1] if last else _access_anchor(con)
        rid = (last[0] if last else int((S.get_setting(con, "access_chain_anchor", None) or {}).get("id", 0))) + 1
        r = {"id": rid, "at": S.now(), "actor": S.ACTOR.get(), "action": action, "target": (target or "")[:80], "count": count,
             "filter": (filt or "")[:300], "client": CLIENT.get()}
        con.execute("INSERT INTO access_log (id, at, actor, action, target, count, filter, client, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (rid, r["at"], r["actor"], action, r["target"], count, r["filter"], r["client"], prev, S.chain_hash(prev, _access_fields(r))))
    if con.in_transaction:
        ins()
    else:
        with S.tx(con):
            ins()


# 목록 조건 중 값이 개인정보일 수 있는 칸(검색어)은 값 대신 '있음'만 남긴다
_FILTER_SAFE = ("date", "date_field", "date_from", "date_to", "status", "crew", "dong", "channel", "pay", "quick", "sort", "dir", "page", "size")


def filter_summary(f: dict) -> str:
    parts = [f"{k}={f[k]}" for k in _FILTER_SAFE if f.get(k) not in (None, "")]
    if (f.get("q") or "").strip():
        parts.append("q=(검색어 있음)")
    return "&".join(parts)


def access_where(f: dict) -> tuple[str, list]:
    sql, args = " WHERE 1=1", []
    for k, op in (("date_from", ">="), ("date_to", "<")):
        v = f.get(k)
        if v:
            d = dt.date.fromisoformat(v) + (dt.timedelta(days=1) if op == "<" else dt.timedelta())
            sql += f" AND at {op} ?"; args.append(_utc_start_of(d))
    if f.get("actor"):
        sql += " AND actor=?"; args.append(f["actor"])
    if f.get("action"):
        sql += " AND action=?"; args.append(f["action"])
    if f.get("target"):
        sql += " AND target LIKE ?"; args.append(f["target"] + "%")
    return sql, args


def access_page(con, f: dict) -> dict:
    w, args = access_where(f)
    total = S.one(con, "SELECT COUNT(*) AS n FROM access_log" + w, args)["n"]
    size = max(1, min(1000, int(f.get("size") or 100)))
    pages = max(1, -(-total // size))
    page = max(1, min(pages, int(f.get("page") or 1)))
    out = S.rows(con, "SELECT id, at, actor, action, target, count, filter, client FROM access_log" + w + " ORDER BY id DESC LIMIT ? OFFSET ?",
                 args + [size, (page - 1) * size])
    # 대상 접수 ID → 접수번호 (화면에서 읽기 쉽게)
    for r in out:
        if r["target"] and len(r["target"]) >= 32:
            q = S.one(con, "SELECT receipt_no FROM request WHERE id=?", (r["target"],))
            r["receipt_no"] = q["receipt_no"] if q else None
    return {"rows": out, "total": total, "page": page, "pages": pages, "size": size}


def access_csv(con, f: dict) -> bytes:
    import csv
    import io
    w, args = access_where(f)
    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(["번호", "시각(한국시간)", "사용자", "작업", "작업 코드", "대상(접수)", "건수", "조건", "접속 위치", "해시"])
    n = 0
    for r in S.rows(con, "SELECT a.*, q.receipt_no FROM access_log a LEFT JOIN request q ON q.id=a.target" + w.replace("WHERE 1=1", "WHERE 1=1") + " ORDER BY a.id DESC LIMIT 200000",
                    args):
        t = dt.datetime.fromisoformat(r["at"].replace("Z", "+00:00")).astimezone(S.KST).strftime("%Y-%m-%d %H:%M:%S")
        wr.writerow([r["id"], t, r["actor"], ACCESS_ACTIONS.get(r["action"], r["action"]), r["action"], r["receipt_no"] or (r["target"] or "")[:8],
                     "" if r["count"] is None else r["count"], r["filter"], r["client"], r["hash"][:16]])
        n += 1
    with S.tx(con):
        S.audit(con, "access", "csv", "EXPORT", {"rows": n, **{k: f[k] for k in ("actor", "action", "date_from", "date_to") if f.get(k)}})
    return ("﻿" + buf.getvalue()).encode("utf-8")


def purge_old_access(con) -> int:
    """보관 기간(2년 이상) 지난 접속기록 정리. 지운 마지막 행의 해시를 다음 사슬의 시작점(anchor)으로 남긴다."""
    cutoff = _utc_start_of(_minus_years(_kst_today(), access_retention_years(con)))
    last = con.execute("SELECT id, hash FROM access_log WHERE at < ? ORDER BY id DESC LIMIT 1", (cutoff,)).fetchone()
    if not last:
        return 0
    with S.tx(con):
        n = con.execute("DELETE FROM access_log WHERE id <= ?", (last[0],)).rowcount
        S.set_setting(con, "access_chain_anchor", {"id": last[0], "hash": last[1], "at": S.now()})
        S.audit(con, "access", "retention", "PURGE", {"count": n, "years": access_retention_years(con)})
    return n


# ------------------------------------------------------------------ 무결성 검사 (해시 사슬)
def _verify(rows: list[dict], fields, anchor: str) -> dict:
    prev = anchor
    for r in rows:
        if r["prev_hash"] != prev:
            return {"ok": False, "checked": rows.index(r), "broken_id": r["id"], "why": "이전 행과 이어지지 않음(행이 지워졌거나 순서가 바뀜)"}
        if S.chain_hash(prev, fields(r)) != r["hash"]:
            return {"ok": False, "checked": rows.index(r), "broken_id": r["id"], "why": "내용이 기록 당시와 다름(고쳐짐)"}
        prev = r["hash"]
    return {"ok": True, "checked": len(rows), "head": {"id": rows[-1]["id"], "hash": rows[-1]["hash"]} if rows else None}


def verify_access(con) -> dict:
    return _verify(S.rows(con, "SELECT * FROM access_log ORDER BY id"), _access_fields, _access_anchor(con))


def verify_audit(con) -> dict:
    rows = S.rows(con, "SELECT * FROM audit_log WHERE legacy=0 ORDER BY id")
    res = _verify(rows, S._audit_fields, S.get_setting(con, "audit_chain_anchor", "") or "")
    res["legacy"] = S.one(con, "SELECT COUNT(*) AS n FROM audit_log WHERE legacy=1")["n"]
    return res


def verify_all(con) -> dict:
    """두 사슬을 검사하고, 지난 점검 때의 마지막 행이 그대로 있는지(끝부분을 지웠는지)도 본다."""
    a, b = verify_access(con), verify_audit(con)
    last = S.one(con, "SELECT result_json FROM access_check ORDER BY at DESC LIMIT 1")
    if last:
        prev = json.loads(last["result_json"])
        for key, res, table in (("access", a, "access_log"), ("audit", b, "audit_log")):
            h = (prev.get(key) or {}).get("head")
            if res["ok"] and h:
                row = S.one(con, f"SELECT hash FROM {table} WHERE id=?", (h["id"],))
                anchor_ok = key == "access" and (S.get_setting(con, "access_chain_anchor", None) or {}).get("id", 0) >= h["id"]
                if (not row and not anchor_ok) or (row and row["hash"] != h["hash"]):
                    res.update(ok=False, broken_id=h["id"], why="지난 점검 때 있던 마지막 행이 없거나 바뀜")
    return {"access": a, "audit": b, "ok": a["ok"] and b["ok"], "at": S.now()}


def this_month() -> str:
    return S.today_kst()[:7]


def check_status(con) -> dict:
    m = this_month()
    done = S.one(con, "SELECT id, month, at, actor, note, result_json FROM access_check WHERE month=? ORDER BY at DESC LIMIT 1", (m,))
    if done:
        done["result"] = json.loads(done.pop("result_json"))
    return {"month": m, "done": done,
            "views_this_month": S.one(con, "SELECT COUNT(*) AS n FROM access_log WHERE at >= ?", (_utc_start_of(dt.date.fromisoformat(m + "-01")),))["n"]}


def record_check(con, note: str = "") -> dict:
    res = verify_all(con)
    with S.tx(con):
        con.execute("INSERT INTO access_check (id, month, at, actor, result_json, note) VALUES (?,?,?,?,?,?)",
                    (str(uuid.uuid4()), this_month(), S.now(), S.ACTOR.get(), json.dumps(res, ensure_ascii=False), (note or "")[:300]))
        S.audit(con, "access", this_month(), "CHECK", {"ok": res["ok"], "access_rows": res["access"]["checked"], "audit_rows": res["audit"]["checked"]})
    return res


def list_checks(con, limit: int = 24) -> list[dict]:
    out = S.rows(con, "SELECT month, at, actor, note, result_json FROM access_check ORDER BY at DESC LIMIT ?", (limit,))
    for r in out:
        r["ok"] = json.loads(r.pop("result_json")).get("ok")
    return out


# ------------------------------------------------------------------ 개인정보 보유기간·파기
def purge_criteria(con, today: str | None = None) -> dict:
    t = dt.date.fromisoformat(today) if today else _kst_today()
    yrs = retention_years(con)
    cutoff = _minus_years(t, yrs)
    return {"retention_years": yrs, "cutoff": cutoff.isoformat(), "basis": "처리 완료일(수거 완료)·취소일", "statuses": ["DONE", "CANCELLED"],
            "excludes": "환불 진행 중·환불 필요 건"}


def purge_candidates(con, today: str | None = None) -> list[dict]:
    c = purge_criteria(con, today)
    cut = _utc_start_of(dt.date.fromisoformat(c["cutoff"]))
    out = []
    for r in S.rows(con, """SELECT id, receipt_no, status FROM request WHERE pii_purged_at IS NULL AND status IN ('DONE','CANCELLED')
            AND COALESCE(CASE WHEN status='CANCELLED' THEN cancelled_at END, completed_at, updated_at) < ?
            AND NOT EXISTS (SELECT 1 FROM refund_case f WHERE f.request_id=request.id AND f.status IN ('DRAFT','REQUESTED','APPROVED'))
            ORDER BY received_at""", (cut,)):
        if r["status"] == "CANCELLED" and S.payment_status(con, r["id"])["status"] in ("REFUND_NEEDED",):
            continue
        out.append(r)
    return out


def purge_preview(con, today: str | None = None) -> dict:
    return {**purge_criteria(con, today), "count": len(purge_candidates(con, today)),
            "last": S.one(con, "SELECT at, count, actor FROM pii_purge_log ORDER BY at DESC LIMIT 1")}


def _scrub_event_body(raw: str) -> str:
    try:
        b = json.loads(raw)
    except ValueError:
        return "{}"
    if isinstance(b, dict):
        for k in _PII_EVENT_KEYS:
            if k in b:
                b[k] = None if k in ("lat", "lon", "acc") else ""
        b["pii_purged"] = True
    return json.dumps(b, ensure_ascii=False)


def _scrub_names(obj, ids: set):
    if isinstance(obj, dict):
        if obj.get("id") in ids and "name" in obj:
            obj["name"] = PURGED
        for v in obj.values():
            _scrub_names(v, ids)
    elif isinstance(obj, list):
        for v in obj:
            _scrub_names(v, ids)


def pseudonymize(con, rid: str, ts: str):
    """접수 한 건의 개인정보를 가명 처리(트랜잭션은 호출하는 쪽)."""
    con.execute("""UPDATE request SET name=?, phone=?, address=?, spot_note='', memo='', lat=NULL, lon=NULL, geo_source=NULL,
        cancel_reason=CASE WHEN cancel_reason IS NULL THEN NULL ELSE ? END, pii_purged_at=? WHERE id=?""", (PURGED, PURGED_PHONE, PURGED, PURGED, ts, rid))
    for ver, snap in con.execute("SELECT req_ver, snapshot_json FROM request_version WHERE request_id=?", (rid,)).fetchall():
        try:
            d = json.loads(snap)
        except ValueError:
            d = {}
        for k in _PII_SNAPSHOT_KEYS:
            d.pop(k, None)
        d["pii_purged"] = True
        con.execute("UPDATE request_version SET snapshot_json=? WHERE request_id=? AND req_ver=?", (json.dumps(d, ensure_ascii=False), rid, ver))
    con.execute("UPDATE contact_log SET content=? WHERE request_id=?", (PURGED, rid))
    for eid, body in con.execute("SELECT event_id, body_json FROM result_event WHERE request_id=?", (rid,)).fetchall():
        con.execute("UPDATE result_event SET body_json=? WHERE event_id=?", (_scrub_event_body(body), eid))
    con.execute("UPDATE photo SET data=NULL, lat=NULL, lon=NULL, purged_at=COALESCE(purged_at, ?) WHERE request_id=?", (ts, rid))
    con.execute("UPDATE payment SET note='' WHERE request_id=?", (rid,))
    con.execute("UPDATE refund_case SET account_last4=NULL, note='' WHERE request_id=?", (rid,))


def purge_pii(con, today: str | None = None, trigger: str = "MANUAL") -> dict:
    """보유기간 지난 접수 가명 처리 + 파기 대장 기록. 대상이 없어도 대장에는 남기지 않는다."""
    cands = purge_candidates(con, today)
    crit = purge_criteria(con, today)
    if not cands:
        return {"count": 0, **crit}
    ids = {c["id"] for c in cands}
    ts = S.now()
    with S.tx(con):
        for rid in ids:
            pseudonymize(con, rid, ts)
        # 마감 기록 요약 안의 이름, 오래된 QR 묶음(암호문)도 정리
        for cid, sj in con.execute("SELECT id, summary_json FROM day_close_log").fetchall():
            d = json.loads(sj or "{}")
            before = json.dumps(d, ensure_ascii=False)
            _scrub_names(d, ids)
            after = json.dumps(d, ensure_ascii=False)
            if after != before:
                con.execute("UPDATE day_close_log SET summary_json=? WHERE id=?", (after, cid))
        con.execute("UPDATE transfer_bundle SET frames_json=NULL WHERE work_date < ?", (crit["cutoff"],))
        pid = str(uuid.uuid4())
        con.execute("INSERT INTO pii_purge_log (id, at, count, actor, criteria_json, trigger) VALUES (?,?,?,?,?,?)",
                    (pid, ts, len(ids), S.ACTOR.get(), json.dumps(crit, ensure_ascii=False), trigger))
        S.audit(con, "privacy", pid, "PURGE_PII", {"count": len(ids), "cutoff": crit["cutoff"], "years": crit["retention_years"], "trigger": trigger})
    return {"count": len(ids), **crit}


def purge_log(con, limit: int = 200) -> list[dict]:
    out = S.rows(con, "SELECT id, at, count, actor, criteria_json, trigger FROM pii_purge_log ORDER BY at DESC LIMIT ?", (limit,))
    for r in out:
        r["criteria"] = json.loads(r.pop("criteria_json"))
    return out


def save_settings(con, data: dict):
    """보유기간(정책 값) 저장. 호출하는 쪽이 트랜잭션을 연다."""
    if "pii_retention_years" in data:
        n = int(data["pii_retention_years"])
        if not 1 <= n <= 10:
            raise S.UserError("개인정보 보유기간은 1~10년 사이로 정해 주세요.")
        S.set_setting(con, "pii_retention_years", n)
    if "access_log_retention_years" in data:
        n = int(data["access_log_retention_years"])
        if not 2 <= n <= 10:
            raise S.UserError("접속기록은 2년 이상 보관해야 합니다(2~10년).")
        S.set_setting(con, "access_log_retention_years", n)
