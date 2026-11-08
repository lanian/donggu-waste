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

from dgpc import core as C, derive as D, results as R, settings as SR

CLIENT: contextvars.ContextVar[str] = contextvars.ContextVar("client", default="local")
ACCESS_ACTIONS = {"VIEW": "상세 조회", "LIST": "목록 조회", "PRINT": "출력", "EXPORT": "내보내기", "PHOTO": "사진 보기", "SEARCH": "주소·중복 조회"}
PURGED = "(파기)"
PURGED_PHONE = "00000000000"
_PII_EVENT_KEYS = ("text", "lat", "lon", "acc", "note", "location_note", "number_note")
_PII_SNAPSHOT_KEYS = ("name", "phone", "address", "spot_note", "memo", "lat", "lon", "cancel_reason")


# ------------------------------------------------------------------ 설정 값
def retention_years(con) -> int:
    return int(C.get_setting(con, "pii_retention_years"))


def access_retention_years(con) -> int:
    return max(2, int(C.get_setting(con, "access_log_retention_years")))


@SR.contribute("retention")
def _settings_part(con) -> dict:
    return {"pii_retention_years": C.get_setting(con, "pii_retention_years"),
            "access_log_retention_years": max(2, C.get_setting(con, "access_log_retention_years"))}


def _kst_today() -> dt.date:
    return dt.date.fromisoformat(C.today_kst())


def _minus_years(d: dt.date, n: int) -> dt.date:
    try:
        return d.replace(year=d.year - n)
    except ValueError:  # 2월 29일
        return d.replace(year=d.year - n, day=28)


def _utc_start_of(d: dt.date) -> str:
    return dt.datetime.combine(d, dt.time()).replace(tzinfo=C.KST).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ 접속기록
def _access_fields(r: dict) -> list:
    return [r["id"], r["at"], r["actor"], r["action"], r["target"], r["count"], r["filter"], r["client"]]


def _access_anchor(con) -> str:
    a = C.get_setting(con, "access_chain_anchor")
    return a["hash"] if a else ""


def record_access(con, action: str, target: str = "", count: int | None = None, filt: str = ""):
    """개인정보 조회 기록 한 줄(트랜잭션은 이 함수가 연다, 이미 열려 있으면 그 안에서)."""
    if action not in ACCESS_ACTIONS:
        raise ValueError(action)
    if C.is_readonly(con):  # 조회 전용 연결: 서버 잠금 안에서 쓰기 연결로
        client = CLIENT.get()
        def fwd(wc):
            tok = CLIENT.set(client)
            try:
                record_access(wc, action, target, count, filt)
            finally:
                CLIENT.reset(tok)
        return C.write(con, fwd)
    def ins():
        last = con.execute("SELECT id, hash FROM access_log ORDER BY id DESC LIMIT 1").fetchone()
        prev = last[1] if last else _access_anchor(con)
        rid = (last[0] if last else int((C.get_setting(con, "access_chain_anchor") or {}).get("id", 0))) + 1
        r = {"id": rid, "at": C.now(), "actor": C.ACTOR.get(), "action": action, "target": (target or "")[:80], "count": count,
             "filter": (filt or "")[:300], "client": CLIENT.get()}
        con.execute("INSERT INTO access_log (id, at, actor, action, target, count, filter, client, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (rid, r["at"], r["actor"], action, r["target"], count, r["filter"], r["client"], prev, C.chain_hash(prev, _access_fields(r))))
    if con.in_transaction:
        ins()
    else:
        with C.tx(con):
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
    total = C.one(con, "SELECT COUNT(*) AS n FROM access_log" + w, args)["n"]
    size = max(1, min(1000, int(f.get("size") or 100)))
    pages = max(1, -(-total // size))
    page = max(1, min(pages, int(f.get("page") or 1)))
    out = C.rows(con, "SELECT id, at, actor, action, target, count, filter, client FROM access_log" + w + " ORDER BY id DESC LIMIT ? OFFSET ?",
                 args + [size, (page - 1) * size])
    # 대상 접수 ID → 접수번호 (화면에서 읽기 쉽게)
    for r in out:
        if r["target"] and len(r["target"]) >= 32:
            q = C.one(con, "SELECT receipt_no FROM request WHERE id=?", (r["target"],))
            r["receipt_no"] = q["receipt_no"] if q else None
    return {"rows": out, "total": total, "page": page, "pages": pages, "size": size}


def access_csv(con, f: dict) -> bytes:
    import io
    w, args = access_where(f)
    buf = io.StringIO()
    wr = C.safe_csv_writer(buf)
    wr.writerow(["번호", "시각(한국시간)", "사용자", "작업", "작업 코드", "대상(접수)", "건수", "조건", "접속 위치", "해시"])
    n = 0
    for r in C.rows(con, "SELECT a.*, q.receipt_no FROM access_log a LEFT JOIN request q ON q.id=a.target" + w.replace("WHERE 1=1", "WHERE 1=1") + " ORDER BY a.id DESC LIMIT 200000",
                    args):
        t = dt.datetime.fromisoformat(r["at"].replace("Z", "+00:00")).astimezone(C.KST).strftime("%Y-%m-%d %H:%M:%S")
        wr.writerow([r["id"], t, r["actor"], ACCESS_ACTIONS.get(r["action"], r["action"]), r["action"], r["receipt_no"] or (r["target"] or "")[:8],
                     "" if r["count"] is None else r["count"], r["filter"], r["client"], r["hash"][:16]])
        n += 1
    with C.tx(con):
        C.audit(con, "access", "csv", "EXPORT", {"rows": n, **{k: f[k] for k in ("actor", "action", "date_from", "date_to") if f.get(k)}})
    return ("﻿" + buf.getvalue()).encode("utf-8")


def purge_old_access(con) -> int:
    """보관 기간(2년 이상) 지난 접속기록 정리. 지운 마지막 행의 해시를 다음 사슬의 시작점(anchor)으로 남긴다."""
    cutoff = _utc_start_of(_minus_years(_kst_today(), access_retention_years(con)))
    last = con.execute("SELECT id, hash FROM access_log WHERE at < ? ORDER BY id DESC LIMIT 1", (cutoff,)).fetchone()
    if not last:
        return 0
    with C.tx(con):
        n = con.execute("DELETE FROM access_log WHERE id <= ?", (last[0],)).rowcount
        C.set_setting(con, "access_chain_anchor", {"id": last[0], "hash": last[1], "at": C.now()})
        C.audit(con, "access", "retention", "PURGE", {"count": n, "years": access_retention_years(con)})
    return n


# ------------------------------------------------------------------ 무결성 검사 (해시 사슬)
# 빠른 검사: 저장한 검사점(마지막으로 확인한 행의 번호·해시)부터 새로 쌓인 행만 이어서 확인한다(검사점 행이 그대로인지도 본다).
# 전체 검사: 처음(또는 정리 뒤 시작점)부터 끝까지 — 오래 걸리므로 백그라운드 작업(서버 jobs)·주 1회 예약 작업으로.
CKPT_KEYS = {"access": "chain_ckpt_access", "audit": "chain_ckpt_audit"}


def _verify_stream(cur, fields, anchor: str, progress=None) -> dict:
    """커서에서 묶음으로 읽으며 사슬 확인(전부 메모리에 올리지 않음)."""
    cols = [c[0] for c in cur.description]
    prev, n, last = anchor, 0, None
    while True:
        batch = cur.fetchmany(5000)
        if not batch:
            break
        for row in batch:
            r = dict(zip(cols, row))
            if r["prev_hash"] != prev:
                return {"ok": False, "checked": n, "broken_id": r["id"], "why": "이전 행과 이어지지 않음(행이 지워졌거나 순서가 바뀜)"}
            if C.chain_hash(prev, fields(r)) != r["hash"]:
                return {"ok": False, "checked": n, "broken_id": r["id"], "why": "내용이 기록 당시와 다름(고쳐짐)"}
            prev, last, n = r["hash"], r, n + 1
        if progress:
            progress(n)
    return {"ok": True, "checked": n, "head": {"id": last["id"], "hash": last["hash"]} if last else None}


def _chain(which: str):
    if which == "access":
        return "access_log", "", _access_fields
    return "audit_log", " AND legacy=0", C._audit_fields


def _start(con, which: str) -> tuple[int, str]:
    """사슬 처음(정리 뒤 시작점 포함): (시작 번호, 이전 해시)."""
    if which == "access":
        a = C.get_setting(con, "access_chain_anchor")
        return (int(a.get("id", 0)), a["hash"]) if a else (0, "")
    return 0, C.get_setting(con, "audit_chain_anchor") or ""


def verify_chain(con, which: str, incremental: bool = False, progress=None) -> dict:
    table, extra, fields = _chain(which)
    start_id, prev = _start(con, which)
    res_from = start_id
    if incremental:
        ck = C.get_setting(con, CKPT_KEYS[which])
        if ck and ck.get("id", 0) > start_id:
            row = C.one(con, f"SELECT * FROM {table} WHERE id=?", (ck["id"],))
            if not row:
                return {"ok": False, "checked": 0, "broken_id": ck["id"], "why": "검사점 행이 없어짐(지워짐)", "incremental": True, "from_id": ck["id"]}
            if row["hash"] != ck["hash"] or C.chain_hash(row["prev_hash"] or "", fields(row)) != row["hash"]:
                return {"ok": False, "checked": 0, "broken_id": ck["id"], "why": "검사점 행이 바뀜(고쳐짐)", "incremental": True, "from_id": ck["id"]}
            start_id, prev, res_from = ck["id"], ck["hash"], ck["id"]
    cur = con.execute(f"SELECT * FROM {table} WHERE id > ?{extra} ORDER BY id", (start_id,))
    res = _verify_stream(cur, fields, prev, progress)
    if res["ok"] and not res["head"] and incremental and res_from:
        res["head"] = {"id": res_from, "hash": prev}
    res.update(incremental=incremental, from_id=res_from)
    return res


def verify_access(con) -> dict:
    return verify_chain(con, "access")


def verify_audit(con) -> dict:
    res = verify_chain(con, "audit")
    res["legacy"] = C.one(con, "SELECT COUNT(*) AS n FROM audit_log WHERE legacy=1")["n"]
    return res


def save_checkpoints(con, res: dict):
    """확인이 끝난 끝 행을 검사점으로 저장(다음 빠른 검사의 출발점)."""
    def w(wc):
        for key in ("access", "audit"):
            r = res.get(key) or {}
            if r.get("ok") and r.get("head"):
                C.set_setting(wc, CKPT_KEYS[key], {**r["head"], "at": C.now()})
        C.set_setting(wc, "last_full_verify" if res.get("full") else "last_quick_verify", {"at": C.now(), "ok": res.get("ok")})
    C.write(con, w)


def verify_all(con, full: bool = True, progress=None, save: bool = True) -> dict:
    """두 사슬을 검사하고, 지난 점검 때의 마지막 행이 그대로 있는지(끝부분을 지웠는지)도 본다.
    full=False: 검사점부터 새 행만(빠름). 결과가 정상이면 검사점을 앞으로 옮긴다."""
    a = verify_chain(con, "access", not full, progress)
    b = verify_chain(con, "audit", not full, progress)
    b["legacy"] = C.one(con, "SELECT COUNT(*) AS n FROM audit_log WHERE legacy=1")["n"]
    last = C.one(con, "SELECT result_json FROM access_check ORDER BY at DESC LIMIT 1")
    if last:
        prev = json.loads(last["result_json"])
        for key, res, table in (("access", a, "access_log"), ("audit", b, "audit_log")):
            h = (prev.get(key) or {}).get("head")
            if res["ok"] and h:
                row = C.one(con, f"SELECT hash FROM {table} WHERE id=?", (h["id"],))
                anchor_ok = key == "access" and (C.get_setting(con, "access_chain_anchor") or {}).get("id", 0) >= h["id"]
                if (not row and not anchor_ok) or (row and row["hash"] != h["hash"]):
                    res.update(ok=False, broken_id=h["id"], why="지난 점검 때 있던 마지막 행이 없거나 바뀜")
    out = {"access": a, "audit": b, "ok": a["ok"] and b["ok"], "at": C.now(), "full": full}
    if save:
        save_checkpoints(con, out)
    return out


def this_month() -> str:
    return C.today_kst()[:7]


def check_status(con) -> dict:
    m = this_month()
    done = C.one(con, "SELECT id, month, at, actor, note, result_json FROM access_check WHERE month=? ORDER BY at DESC LIMIT 1", (m,))
    if done:
        done["result"] = json.loads(done.pop("result_json"))
    return {"month": m, "done": done,
            "views_this_month": C.one(con, "SELECT COUNT(*) AS n FROM access_log WHERE at >= ?", (_utc_start_of(dt.date.fromisoformat(m + "-01")),))["n"]}


def record_check(con, note: str = "") -> dict:
    """월 점검 기록. 사슬은 검사점부터 빠르게 확인한다(전체 검사는 백그라운드 작업·주 1회 예약 작업). 마지막 전체 검사 결과를 함께 남긴다."""
    res = verify_all(con, full=False)
    res["last_full"] = C.get_setting(con, "last_full_verify")
    with C.tx(con):
        con.execute("INSERT INTO access_check (id, month, at, actor, result_json, note) VALUES (?,?,?,?,?,?)",
                    (str(uuid.uuid4()), this_month(), C.now(), C.ACTOR.get(), json.dumps(res, ensure_ascii=False), (note or "")[:300]))
        C.audit(con, "access", this_month(), "CHECK", {"ok": res["ok"], "access_rows": res["access"]["checked"], "audit_rows": res["audit"]["checked"]})
    return res


def list_checks(con, limit: int = 24) -> list[dict]:
    out = C.rows(con, "SELECT month, at, actor, note, result_json FROM access_check ORDER BY at DESC LIMIT ?", (limit,))
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


def purge_candidates(con, today: str | None = None, ids=None) -> list[dict]:
    c = purge_criteria(con, today)
    cut = _utc_start_of(dt.date.fromisoformat(c["cutoff"]))
    out = []
    extra, args = "", [cut]
    if ids is not None:
        extra = " AND id IN (SELECT value FROM json_each(?))"; args.append(json.dumps(list(ids)))
    for r in C.rows(con, f"""SELECT id, receipt_no, status, pay_status FROM request WHERE pii_purged_at IS NULL AND status IN ('DONE','CANCELLED')
            AND COALESCE(CASE WHEN status='CANCELLED' THEN cancelled_at END, completed_at, updated_at) < ?{extra}
            AND NOT EXISTS (SELECT 1 FROM refund_case f WHERE f.request_id=request.id AND f.status IN ('DRAFT','REQUESTED','APPROVED'))
            ORDER BY received_at""", args):
        if r["status"] == "CANCELLED" and (r["pay_status"] == "REFUND_NEEDED" or (r["pay_status"] is None and D.payment_status(con, r["id"])["status"] == "REFUND_NEEDED")):
            continue
        out.append({k: r[k] for k in ("id", "receipt_no", "status")})
    return out


def purge_preview(con, today: str | None = None) -> dict:
    return {**purge_criteria(con, today), "count": len(purge_candidates(con, today)),
            "last": C.one(con, "SELECT at, count, actor FROM pii_purge_log ORDER BY at DESC LIMIT 1")}


_REQ_KINDS = ("FIELD_INTAKE", "CORRECTION")  # 휴대폰 현장 접수·정정 요청: 이름·연락처·주소가 본문에 들어 있음


def _scrub_event_body(raw: str, kind: str | None = None) -> str:
    try:
        b = json.loads(raw)
    except ValueError:
        return "{}"
    if isinstance(b, dict) and kind in _REQ_KINDS:
        b.pop("reason", None)  # 정정 요청의 자유 사유에도 주민 개인정보가 들어갈 수 있다.
        return json.dumps(R._strip_request_pii(kind, b), ensure_ascii=False)
    if isinstance(b, dict):
        for k in _PII_EVENT_KEYS:
            if k in b:
                b[k] = None if k in ("lat", "lon", "acc") else ""
        b["pii_purged"] = True
    return json.dumps(b, ensure_ascii=False)


_SUMMARY_PII = ("name", "phone", "address")  # 마감 기록 요약(day_close_log.summary_json) 안 접수 항목의 개인정보 칸


def _scrub_names(obj, ids: set, receipts: set = frozenset()) -> bool:
    """요약 안에서 id(또는 접수번호)가 ids(receipts)에 든 항목의 이름·연락처·주소를 지운다. 바꾼 것이 있으면 True."""
    hit = False
    if isinstance(obj, dict):
        if obj.get("id") in ids or (obj.get("id") is None and obj.get("receipt_no") in receipts):
            for k in _SUMMARY_PII:
                if k in obj and obj[k] not in (PURGED, PURGED_PHONE, "", None):
                    obj[k] = PURGED_PHONE if k == "phone" else PURGED
                    hit = True
        for v in obj.values():
            hit = _scrub_names(v, ids, receipts) or hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _scrub_names(v, ids, receipts) or hit
    return hit


def _summary_refs(obj, out: list):
    """요약 안에서 개인정보 칸(이름·연락처·주소)이 아직 남은 항목의 (id, 접수번호)를 모은다."""
    if isinstance(obj, dict):
        if any(k in obj and obj[k] not in (PURGED, PURGED_PHONE, "", None) for k in _SUMMARY_PII) and (obj.get("id") or obj.get("receipt_no")):
            out.append((obj.get("id"), obj.get("receipt_no")))
        for v in obj.values():
            _summary_refs(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _summary_refs(v, out)


def scrub_close_summaries(con, ids=None) -> int:
    """마감 기록 요약 안의 파기된 접수 개인정보를 지운다(트랜잭션은 호출하는 쪽). 고친 마감 기록 수를 돌려준다.
    ids를 주면 그 접수(이번 묶음에서 파기한 것)만, 없으면 '이미 파기됐는데 요약에 이름이 남은 것'을 모두 찾아 고친다
    (예전 버전에서 파기 도중 멈춰 요약 정리가 빠진 경우 복구)."""
    if ids is not None:
        ids = list(ids)
        if not ids:
            return 0
        found = con.execute("""SELECT id, summary_json FROM day_close_log WHERE summary_json IS NOT NULL
            AND EXISTS (SELECT 1 FROM json_each(?) j WHERE instr(day_close_log.summary_json, j.value) > 0)""", (json.dumps(ids),)).fetchall()
        rec = {r[0] for r in con.execute("SELECT receipt_no FROM request WHERE id IN (SELECT value FROM json_each(?)) AND receipt_no IS NOT NULL",
                                         (json.dumps(ids),)).fetchall()}
        targets = [(cid, sj, set(ids), rec) for cid, sj in found]
    else:
        targets = []
        for cid, sj in con.execute("SELECT id, summary_json FROM day_close_log WHERE summary_json IS NOT NULL").fetchall():
            try:
                d = json.loads(sj)
            except ValueError:
                continue
            refs: list = []
            _summary_refs(d, refs)
            if not refs:
                continue
            rid = [r[0] for r in refs if r[0]]
            rno = [r[1] for r in refs if r[1] and not r[0]]
            gone = con.execute("""SELECT id, receipt_no FROM request WHERE pii_purged_at IS NOT NULL
                AND (id IN (SELECT value FROM json_each(?)) OR receipt_no IN (SELECT value FROM json_each(?)))""",
                               (json.dumps(rid), json.dumps(rno))).fetchall()
            if gone:
                targets.append((cid, sj, {g[0] for g in gone}, {g[1] for g in gone if g[1]}))
    n = 0
    for cid, sj, sid, srec in targets:
        try:
            d = json.loads(sj)
        except ValueError:
            continue
        if _scrub_names(d, sid, srec):
            con.execute("UPDATE day_close_log SET summary_json=? WHERE id=?", (json.dumps(d, ensure_ascii=False), cid))
            n += 1
    return n


def pseudonymize(con, rid: str, ts: str):
    """접수 한 건의 개인정보를 가명 처리(트랜잭션은 호출하는 쪽)."""
    con.execute("""UPDATE request SET req_ver=req_ver+1, name=?, phone=?, address=?, spot_note='', memo='', lat=NULL, lon=NULL, geo_source=NULL,
        cancel_reason=CASE WHEN cancel_reason IS NULL THEN NULL ELSE ? END, pii_purged_at=COALESCE(pii_purged_at, ?) WHERE id=?""", (PURGED, PURGED_PHONE, PURGED, PURGED, ts, rid))
    for ver, snap in con.execute("SELECT req_ver, snapshot_json FROM request_version WHERE request_id=?", (rid,)).fetchall():
        try:
            d = json.loads(snap)
        except ValueError:
            d = {}
        if not isinstance(d, dict):
            d = {}
        for k in _PII_SNAPSHOT_KEYS:
            d.pop(k, None)
        d["pii_purged"] = True
        con.execute("UPDATE request_version SET snapshot_json=? WHERE request_id=? AND req_ver=?", (json.dumps(d, ensure_ascii=False), rid, ver))
    con.execute("UPDATE contact_log SET content=? WHERE request_id=?", (PURGED, rid))
    con.execute("UPDATE request_version SET reason=? WHERE request_id=? AND reason IS NOT NULL", (PURGED, rid))  # 수정 사유 글
    con.execute("UPDATE audit_note SET note=NULL WHERE request_id=?", (rid,))  # 변경 이력의 사유 글(해시는 남아 사슬 검사는 그대로)
    con.execute("UPDATE result_event SET resolved_reason=? WHERE request_id=? AND resolved_reason IS NOT NULL", (PURGED, rid))  # 충돌 판단 사유
    for eid, kind, body, status in con.execute("SELECT event_id, kind, body_json, apply_status FROM result_event WHERE request_id=?", (rid,)).fetchall():
        con.execute("UPDATE result_event SET body_json=? WHERE event_id=?", (_scrub_event_body(body, kind), eid))
        if status == "CONFLICT" and kind in _REQ_KINDS:  # 처리하지 않은 정정 요청은 값이 지워져 반영할 수 없으므로 정리
            con.execute("UPDATE result_event SET apply_status='REJECTED', resolved_reason=?, resolved_at=? WHERE event_id=?", ("보유기간 경과로 개인정보 파기(미처리 요청)", ts, eid))
    con.execute("UPDATE photo SET data=NULL, lat=NULL, lon=NULL, purged_at=COALESCE(purged_at, ?) WHERE request_id=?", (ts, rid))
    # 사람이 적은 사유·메모 글. void_reason 은 '취소됨' 표시도 겸하므로(IS NULL = 유효) 비우지 않고 (파기)로 바꾼다
    con.execute("UPDATE payment SET note='', void_reason=CASE WHEN void_reason IS NULL THEN NULL ELSE ? END WHERE request_id=?", (PURGED, rid))
    con.execute("""UPDATE refund_case SET account_last4=NULL, note='', approve_note=CASE WHEN approve_note IS NULL THEN NULL ELSE ? END,
        void_reason=CASE WHEN void_reason IS NULL THEN NULL ELSE ? END WHERE request_id=?""", (PURGED, PURGED, rid))
    con.execute("UPDATE item_link SET reason=?, void_reason=CASE WHEN void_reason IS NULL THEN NULL ELSE ? END WHERE request_id=?", (PURGED, PURGED, rid))


def _pii_repair_candidates(con, ids=None) -> list[dict]:
    """이미 파기된 접수 중 재유입·누락된 민감 값만 고른다. 깨끗한 행은 버전·감사를 다시 쓰지 않는다."""
    extra = " AND r.id IN (SELECT value FROM json_each(?5))" if ids is not None else ""
    args = [PURGED, PURGED_PHONE, json.dumps(_PII_SNAPSHOT_KEYS), json.dumps(_PII_EVENT_KEYS)]
    if ids is not None:
        args.append(json.dumps(list(ids)))
    return C.rows(con, """SELECT r.id, r.pii_purged_at FROM request r WHERE r.pii_purged_at IS NOT NULL AND (
        r.name NOT IN (?1,'(정리됨)','') OR r.phone NOT IN (?2,'') OR r.address NOT IN (?1,'(정리됨)','')
        OR COALESCE(r.spot_note,'')<>'' OR COALESCE(r.memo,'')<>'' OR r.lat IS NOT NULL OR r.lon IS NOT NULL
        OR COALESCE(r.cancel_reason,?1)<>?1
        OR EXISTS (SELECT 1 FROM contact_log c WHERE c.request_id=r.id AND c.content IS NOT ?1)
        OR EXISTS (SELECT 1 FROM audit_note n WHERE n.request_id=r.id AND n.note IS NOT NULL)
        OR EXISTS (SELECT 1 FROM request_version v WHERE v.request_id=r.id AND (COALESCE(v.reason,?1)<>?1
            OR NOT json_valid(v.snapshot_json)
            OR EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(v.snapshot_json) THEN v.snapshot_json ELSE '{}' END) j
                       WHERE j.key IN (SELECT value FROM json_each(?3)))))
        OR EXISTS (SELECT 1 FROM photo p WHERE p.request_id=r.id AND
            (p.data IS NOT NULL OR p.lat IS NOT NULL OR p.lon IS NOT NULL OR p.purged_at IS NULL))
        OR EXISTS (SELECT 1 FROM payment p WHERE p.request_id=r.id AND (COALESCE(p.note,'')<>'' OR COALESCE(p.void_reason,?1)<>?1))
        OR EXISTS (SELECT 1 FROM refund_case f WHERE f.request_id=r.id AND (f.account_last4 IS NOT NULL OR COALESCE(f.note,'')<>''
            OR COALESCE(f.approve_note,?1)<>?1 OR COALESCE(f.void_reason,?1)<>?1))
        OR EXISTS (SELECT 1 FROM item_link l WHERE l.request_id=r.id AND (COALESCE(l.reason,?1)<>?1 OR COALESCE(l.void_reason,?1)<>?1))
        OR EXISTS (SELECT 1 FROM result_event e, json_each(CASE WHEN json_valid(e.body_json) THEN e.body_json ELSE '{}' END) j
            WHERE e.request_id=r.id AND ((j.key IN (SELECT value FROM json_each(?4)) AND j.value IS NOT NULL AND j.value<>'')
                OR (e.kind IN ('FIELD_INTAKE','CORRECTION') AND (j.key IN ('name','phone','address','spot_note','memo')
                    OR (j.key='reason' AND COALESCE(j.value,'')<>'') OR (j.key='fields' AND j.type='object')
                    OR (j.key='items' AND j.type='array')))))
        )""" + extra, args)


def _purge_orphan_requests(con, cutoff: str) -> int:
    """접수를 만들지 못한(충돌함에 남은) 휴대폰 현장 접수 기록: 접수가 없어 위의 파기 대상에 잡히지 않으므로,
    보유기간 기준일보다 오래된 것은 개인정보를 지우고 처리 끝(거부)으로 둔다."""
    rows_ = con.execute("""SELECT e.event_id, e.kind, e.body_json FROM result_event e LEFT JOIN request r ON r.id=e.request_id
        WHERE e.kind IN ('FIELD_INTAKE','CORRECTION') AND e.apply_status='CONFLICT' AND r.id IS NULL AND COALESCE(e.device_ts,'') < ?""", (cutoff,)).fetchall()
    if not rows_:
        return 0
    ts = C.now()
    with C.tx(con):
        for eid, kind, body in rows_:
            con.execute("UPDATE result_event SET body_json=?, apply_status='REJECTED', resolved_reason=?, resolved_at=? WHERE event_id=?",
                        (_scrub_event_body(body, kind), "보유기간 경과로 개인정보 파기(접수 없는 현장 요청)", ts, eid))
        C.audit(con, "privacy", "orphan-requests", "PURGE_PII", {"count": len(rows_), "cutoff": cutoff})
    return len(rows_)


def _purge_old_notes(con, cutoff: str) -> int:
    """어느 접수에도 묶이지 않은, 사람이 적은 글 중 보유기간 기준일보다 오래된 것을 지운다(트랜잭션은 호출하는 쪽).
    - 변경 이력 사유 글 중 접수를 알 수 없는 것(가져오기 단위 충돌 일괄 처리 등). 해시는 남아 사슬 검사는 그대로.
    - 마감 기록의 메모·팀장 확인 메모(그날 주민 이름을 적었을 수 있음). 작업일 기준."""
    n = con.execute("""UPDATE audit_note SET note=NULL WHERE request_id IS NULL AND note IS NOT NULL
        AND audit_id IN (SELECT id FROM audit_log WHERE at < ?)""", (_utc_start_of(dt.date.fromisoformat(cutoff)),)).rowcount
    n += con.execute("""UPDATE day_close_log SET note=CASE WHEN note IS NULL THEN NULL ELSE ?1 END, approve_note=CASE WHEN approve_note IS NULL THEN NULL ELSE ?1 END
        WHERE work_date < ?2 AND (COALESCE(note, ?1) <> ?1 OR COALESCE(approve_note, ?1) <> ?1)""", (PURGED, cutoff)).rowcount
    return n


def purge_pii(con, today: str | None = None, trigger: str = "MANUAL", lock=None, batch: int = 500, progress=None) -> dict:
    """보유기간 지난 접수 가명 처리 + 파기 대장 기록(묶음마다 한 줄). 대상이 없어도 대장에는 남기지 않는다.
    500건씩 한 트랜잭션으로 처리하고 lock(서버 DB 잠금)을 묶음 사이에 놓아 화면이 오래 멈추지 않게 한다."""
    from contextlib import nullcontext
    guard = lock if lock is not None else nullcontext()
    with guard:
        cands = purge_candidates(con, today)
        crit = purge_criteria(con, today)
        orphans = _purge_orphan_requests(con, crit["cutoff"])
    done: list[str] = []
    ids = [c["id"] for c in cands]
    for i in range(0, len(ids), batch):
        with guard:
            ch = [c["id"] for c in purge_candidates(con, today, ids[i:i + batch])]  # 그 사이 바뀐 건(환불 생김 등)은 빼고
            if not ch:
                continue
            ts = C.now()
            with C.tx(con):
                for rid in ch:
                    pseudonymize(con, rid, ts)
                # 마감 기록 요약 안의 이름도 같은 트랜잭션에서(중간에 멈춰도 파기된 접수의 이름이 요약에 남지 않게)
                scrub_close_summaries(con, ch)
                pid = str(uuid.uuid4())
                con.execute("INSERT INTO pii_purge_log (id, at, count, actor, criteria_json, trigger) VALUES (?,?,?,?,?,?)",
                            (pid, ts, len(ch), C.ACTOR.get(), json.dumps(crit, ensure_ascii=False), trigger))
                C.audit(con, "privacy", pid, "PURGE_PII", {"count": len(ch), "cutoff": crit["cutoff"], "years": crit["retention_years"], "trigger": trigger})
            done += ch
        if progress:
            progress(len(done), len(ids))
    with guard:
        repair_ids = [r["id"] for r in _pii_repair_candidates(con)]
    repaired_requests = 0
    for i in range(0, len(repair_ids), batch):
        with guard:
            pending = _pii_repair_candidates(con, repair_ids[i:i + batch])
            if not pending:
                continue
            with C.tx(con):
                for request in pending:
                    pseudonymize(con, request["id"], request["pii_purged_at"])
                scrub_close_summaries(con, [r["id"] for r in pending])
                C.audit(con, "privacy", "purged-reentry", "PURGE_PII", {"repaired_requests": len(pending)})
            repaired_requests += len(pending)
    with guard:
        with C.tx(con):
            # 복구: 이미 파기됐는데 마감 기록 요약에 이름·연락처·주소가 남은 것(예전 버전에서 파기 도중 멈춘 경우 등)을 매번 찾아 지운다
            repaired = scrub_close_summaries(con)
            if repaired:
                C.audit(con, "privacy", "close-summary", "PURGE_PII", {"repaired_close_logs": repaired})
            notes = _purge_old_notes(con, crit["cutoff"])
            if notes:
                C.audit(con, "privacy", "notes", "PURGE_PII", {"notes": notes, "cutoff": crit["cutoff"]})
            if done:
                # 오래된 QR 묶음(암호문)도 정리
                con.execute("UPDATE transfer_bundle SET frames_json=NULL WHERE work_date < ?", (crit["cutoff"],))
            C.set_setting(con, "last_pii_purge_run", {"at": C.now(), "count": len(done), "trigger": trigger})
        if done:
            C.incremental_vacuum(con)
    return {"count": len(done), **crit, **({"orphan_requests": orphans} if orphans else {}), **({"repaired_close_logs": repaired} if repaired else {}),
            **({"repaired_requests": repaired_requests} if repaired_requests else {}), **({"notes": notes} if notes else {})}


def purge_log(con, limit: int = 200) -> list[dict]:
    out = C.rows(con, "SELECT id, at, count, actor, criteria_json, trigger FROM pii_purge_log ORDER BY at DESC LIMIT ?", (limit,))
    for r in out:
        r["criteria"] = json.loads(r.pop("criteria_json"))
    return out


def save_settings(con, data: dict):
    """보유기간(정책 값) 저장. 호출하는 쪽이 트랜잭션을 연다."""
    if "pii_retention_years" in data:
        n = int(data["pii_retention_years"])
        if not 1 <= n <= 10:
            raise C.UserError("개인정보 보유기간은 1~10년 사이로 정해 주세요.")
        C.set_setting(con, "pii_retention_years", n)
    if "access_log_retention_years" in data:
        n = int(data["access_log_retention_years"])
        if not 2 <= n <= 10:
            raise C.UserError("접속기록은 2년 이상 보관해야 합니다(2~10년).")
        C.set_setting(con, "access_log_retention_years", n)
