"""운영 보조 (v0.13): 백그라운드 작업 실행기, 처리할 일(대시보드), 운영 준비 점검, 진단, 예약 작업 상태, 최근 가져오기.

- 백그라운드 작업: 작업자 스레드 하나가 차례로 실행한다(수동 백업·개인정보 파기·사진 정리·전체 무결성 검사·백업 보관 정리·환불 전체 점검).
  API는 작업 번호를 바로 돌려주고, 화면은 GET /api/jobs/{id} 로 진행률·결과를 본다. 작업은 DB 잠금을 짧게 여러 번 잡는다.
- 예약 작업(서버 scheduler): 하루 한 번 개인정보 자동 파기(묶음 500건)·사진 보관 기간 정리·환불 전체 점검·재전송 기록 정리·빠른 무결성 검사,
  주 1회 전체 무결성 검사. 이틀 넘게 돌지 않았으면 대시보드에 경고(ops_status).
"""
from __future__ import annotations

import datetime as dt
import json
import queue
import threading
import time
import traceback
import uuid
from contextlib import nullcontext
from pathlib import Path

from dgpc import service as S

# ------------------------------------------------------------------ 백그라운드 작업 실행기 (작업자 1개)
JOB_KINDS = {"backup": "수동 백업", "backup_auto": "자동 백업(지금)", "backup_retention": "백업 보관 정리", "pii_purge": "개인정보 파기",
             "photo_purge": "사진 보관 기간 정리", "verify_full": "전체 무결성 검사", "refund_sync": "환불 전체 점검"}


class JobRunner:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.q: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, name="dg-jobs", daemon=True)
        self.thread.start()

    def submit(self, kind: str, fn, actor: str = "system", client: str = "local") -> str:
        """fn(job) → 결과(dict). job["progress"]·job["total"]을 바꿔 진행률을 알린다."""
        jid = uuid.uuid4().hex[:16]
        job = {"id": jid, "kind": kind, "label": JOB_KINDS.get(kind, kind), "status": "QUEUED", "progress": 0, "total": None, "result": None,
               "error": None, "actor": actor, "client": client, "created_at": S.now(), "started_at": None, "finished_at": None}
        with self.lock:
            for k in [k for k, v in self.jobs.items() if v["finished_at"] and time.time() - v.get("_done", 0) > 6 * 3600]:
                self.jobs.pop(k, None)  # 끝난 지 6시간 넘은 작업 기록 정리(메모리)
            self.jobs[jid] = job
        self.q.put((job, fn))
        self.start()
        return jid

    def get(self, jid: str) -> dict | None:
        with self.lock:
            j = self.jobs.get(jid)
            return {k: v for k, v in j.items() if not k.startswith("_")} if j else None

    def run_now(self, job: dict, fn):
        """작업 하나 실행(작업자 스레드에서, 시험에서는 직접)."""
        from dgpc import privacy as PV
        job.update(status="RUNNING", started_at=S.now())
        tok, tok2 = S.ACTOR.set(job["actor"]), PV.CLIENT.set(job["client"])
        try:
            job["result"] = fn(job)
            job["status"] = "DONE"
        except S.UserError as e:
            job.update(status="FAILED", error=str(e))
        except Exception as e:  # noqa: BLE001 — 내용(개인정보 가능)은 화면에 내지 않음
            job.update(status="FAILED", error=f"작업 실패({e.__class__.__name__})")
            try:
                from dgpc import server as SV
                SV.LOG.error("작업 오류 %s\n%s", job["kind"], SV.scrub(traceback.format_exc()))
            except Exception:  # noqa: BLE001
                pass
        finally:
            S.ACTOR.reset(tok); PV.CLIENT.reset(tok2)
            job["finished_at"] = S.now()
            job["_done"] = time.time()
        return job

    def _loop(self):
        while True:
            job, fn = self.q.get()
            self.run_now(job, fn)


JOBS = JobRunner()


def job_fn(kind: str, con, lock, read_con_factory=None, **kw):
    """작업 종류 → 실행 함수. con = 쓰기 연결, lock = 서버 DB 잠금(묶음마다 잡음)."""
    from dgpc import backup as BK, finance as FN, privacy as PV
    guard = lock if lock is not None else nullcontext()

    def prog(job):
        def p(n, total=None):
            job["progress"] = n
            if total is not None:
                job["total"] = total
        return p

    if kind == "backup":
        def f(job):
            with guard:
                r = S.make_backup(con, kw.get("passphrase", ""))
            return {k: r[k] for k in ("name", "bytes", "sha256")}
        return f
    if kind == "backup_auto":
        return lambda job: BK.make_auto(con, "MANUAL", lock=lock)
    if kind == "backup_retention":
        def f(job):
            with guard:
                return {"deleted": BK.run_retention(con)}
        return f
    if kind == "pii_purge":
        return lambda job: PV.purge_pii(con, trigger=kw.get("trigger", "MANUAL"), lock=lock, progress=prog(job))
    if kind == "photo_purge":
        return lambda job: S.purge_photos(con, lock=lock, trigger=kw.get("trigger", "MANUAL"), progress=prog(job))
    if kind == "verify_full":
        def f(job):
            rc = read_con_factory() if read_con_factory else None
            try:
                c = rc or con
                g = nullcontext() if rc else guard
                with g:
                    res = PV.verify_all(c, full=True, progress=prog(job), save=False)
                with guard:
                    PV.save_checkpoints(con, res)
                    with S.tx(con):
                        S.audit(con, "access", "verify", "VERIFY_FULL", {"ok": res["ok"], "access_rows": res["access"]["checked"], "audit_rows": res["audit"]["checked"]})
                return res
            finally:
                if rc is not None:
                    rc.close()
        return f
    if kind == "refund_sync":
        def f(job):
            with guard:
                ids = sorted({x["request_id"] for x in S.rows(con, "SELECT DISTINCT request_id FROM payment WHERE void_reason IS NULL")}
                             | {x["request_id"] for x in S.rows(con, "SELECT DISTINCT request_id FROM refund_case WHERE status IN ('DRAFT','REQUESTED','APPROVED')")})
            job["total"] = len(ids)
            made = closed = 0
            for i in range(0, len(ids), 500):
                with guard:
                    r = FN.sync_refunds(con, ids[i:i + 500])
                made += r["created"]; closed += r["closed"]
                job["progress"] = min(len(ids), i + 500)
            with guard:
                with S.tx(con):
                    S.set_setting(con, "last_refund_sync", {"at": S.now(), "created": made, "closed": closed})
            return {"created": made, "closed": closed, "checked": len(ids)}
        return f
    raise S.UserError("알 수 없는 작업입니다.")


# ------------------------------------------------------------------ 예약 작업 (하루 1회·주 1회)
def daily_tasks(con, lock=None, read_con_factory=None, today: str | None = None) -> dict:
    """서버 예약 작업이 하루 한 번 부른다(시험에서는 직접). 실패해도 다음 일은 계속한다."""
    from dgpc import backup as BK, privacy as PV
    guard = lock if lock is not None else nullcontext()
    out: dict = {}
    tok = S.ACTOR.set("system")
    try:
        for name, fn in (("pii_purge", lambda: PV.purge_pii(con, today=today, trigger="SCHEDULE", lock=lock)["count"]),
                         ("photo_purge", lambda: S.purge_photos(con, today=today, lock=lock, trigger="SCHEDULE")["count"]),
                         ("refund_sync", lambda: JOBS.run_now({"actor": "system", "client": "local", "kind": "refund_sync", "progress": 0},
                                                              job_fn("refund_sync", con, lock))["result"]),
                         ("idem", lambda: _locked(guard, lambda: S.purge_idem(con))),
                         ("access_retention", lambda: _locked(guard, lambda: PV.purge_old_access(con))),
                         ("backup_retention", lambda: len(_locked(guard, lambda: BK.run_retention(con)))),
                         ("verify_quick", lambda: _verify_quick(con, lock, read_con_factory))):
            try:
                out[name] = fn()
            except Exception as e:  # noqa: BLE001
                out[name] = {"error": e.__class__.__name__}
        weekday = dt.date.fromisoformat(today or S.today_kst()).weekday()
        last_full = (S.get_setting(con, "last_full_verify", None) or {}).get("at")
        if weekday == 6 or not last_full or _age_days(last_full) >= 7:  # 일요일 또는 7일 넘게 전체 검사가 없었으면
            try:
                out["verify_full"] = JOBS.run_now({"actor": "system", "client": "local", "kind": "verify_full", "progress": 0},
                                                  job_fn("verify_full", con, lock, read_con_factory))["result"]["ok"]
            except Exception as e:  # noqa: BLE001
                out["verify_full"] = {"error": e.__class__.__name__}
        with guard:
            with S.tx(con):
                S.set_setting(con, "last_daily_tasks", {"at": S.now(), "result": {k: v if isinstance(v, (int, bool, dict)) else str(v) for k, v in out.items()}})
    finally:
        S.ACTOR.reset(tok)
    return out


def _locked(guard, fn):
    with guard:
        return fn()


def _verify_quick(con, lock, read_con_factory):
    from dgpc import privacy as PV
    guard = lock if lock is not None else nullcontext()
    with guard:
        res = PV.verify_all(con, full=False)
    return res["ok"]


def _age_days(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 86400


def ops_status(con) -> dict:
    """대시보드 경고용: 예약 작업(자동 파기·사진 정리·무결성 검사·백업)이 언제 마지막으로 돌았는지. 2일 넘으면 stale."""
    from dgpc import backup as BK
    items = []
    for key, label, setting, limit in (("pii_purge", "개인정보 자동 파기", "last_pii_purge_run", 2), ("photo_purge", "사진 보관 기간 정리", "last_photo_purge_run", 2),
                                       ("daily", "하루 예약 작업", "last_daily_tasks", 2), ("verify_quick", "무결성 빠른 검사", "last_quick_verify", 2),
                                       ("verify_full", "무결성 전체 검사", "last_full_verify", 8)):
        v = S.get_setting(con, setting, None) or {}
        age = _age_days(v.get("at"))
        items.append({"key": key, "label": label, "last_at": v.get("at"), "age_days": round(age, 1) if age is not None else None,
                      "stale": age is None or age > limit, **({"ok": v.get("ok")} if "ok" in v else {}), **({"count": v.get("count")} if "count" in v else {})})
    b = BK.status(con)
    items.append({"key": "backup", "label": "백업", "last_at": b["last_at"], "age_days": round(b["age_hours"] / 24, 1) if b["age_hours"] is not None else None,
                  "stale": b["stale"]})
    return {"items": items, "warnings": [f"{x['label']}: " + ("기록 없음" if x["last_at"] is None else f"{x['age_days']}일 전") for x in items if x["stale"]]}


# ------------------------------------------------------------------ 처리할 일 (대시보드)
def pending(con, today: str | None = None) -> dict:
    """처리할 일 건수와 가장 오래된 것: 마감 확인 대기·환불 결재 대기·마감 안 한 지난 날짜·지난 예정일 미처리·예정일 없음·결과 미회수 팀."""
    t = today or S.today_kst()
    out = []
    r = S.one(con, """SELECT COUNT(*) AS n, MIN(work_date) AS oldest FROM day_close_log d WHERE status='RECORDED'
        AND seq=(SELECT MAX(seq) FROM day_close_log x WHERE x.work_date=d.work_date)""")
    out.append({"key": "close_approve", "label": "마감 확인 대기", "count": r["n"], "oldest": r["oldest"], "link": "close"})
    r = S.one(con, "SELECT COUNT(*) AS n, MIN(requested_at) AS oldest FROM refund_case WHERE status='REQUESTED'")
    out.append({"key": "refund_approve", "label": "환불 결재 대기", "count": r["n"], "oldest": S._kst_date(r["oldest"]), "link": "refunds"})
    since = (dt.date.fromisoformat(t) - dt.timedelta(days=90)).isoformat()
    r = S.one(con, """SELECT COUNT(*) AS n, MIN(planned_date) AS oldest FROM (SELECT DISTINCT planned_date FROM request
        WHERE planned_date >= ? AND planned_date < ? AND status!='CANCELLED' AND planned_date NOT IN (SELECT work_date FROM day_close_log))""", (since, t))
    out.append({"key": "unclosed_days", "label": "마감 안 한 지난 날짜(90일)", "count": r["n"], "oldest": r["oldest"], "link": "close"})
    qs, qa = S._quick_sql("OVERDUE")
    r = S.one(con, f"SELECT COUNT(*) AS n, MIN(planned_date) AS oldest FROM request WHERE {qs}", qa)
    out.append({"key": "overdue", "label": "지난 예정일 미처리", "count": r["n"], "oldest": r["oldest"], "link": "requests?quick=OVERDUE"})
    qs, qa = S._quick_sql("NODATE")
    r = S.one(con, f"SELECT COUNT(*) AS n, MIN(received_at) AS oldest FROM request WHERE {qs}", qa)
    out.append({"key": "nodate", "label": "예정일 없음", "count": r["n"], "oldest": S._kst_date(r["oldest"]), "link": "requests?quick=NODATE"})
    nr = []
    for d in [x["work_date"] for x in S.rows(con, "SELECT DISTINCT work_date FROM transfer_bundle WHERE work_date BETWEEN ? AND ? ORDER BY work_date",
                                              ((dt.date.fromisoformat(t) - dt.timedelta(days=7)).isoformat(), t))]:
        nr += S.crews_without_result(con, d)
    out.append({"key": "no_result_crews", "label": "결과 미회수 팀(7일)", "count": len(nr), "oldest": min((x["work_date"] for x in nr), default=None),
                "link": "import", "crews": nr[:20]})
    r = S.one(con, "SELECT COUNT(*) AS n, MIN(device_ts) AS oldest FROM result_event WHERE apply_status='CONFLICT'")
    out.append({"key": "conflicts", "label": "충돌함", "count": r["n"], "oldest": S._kst_date(r["oldest"]), "link": "conflicts"})
    return {"today": t, "items": out, "total": sum(x["count"] for x in out)}


# ------------------------------------------------------------------ 운영 준비 점검
def readiness(con) -> dict:
    """운영 시작 전 확인 목록: ok와 고칠 곳(fix: 화면 키)."""
    from dgpc import backup as BK
    items = []

    def add(key, label, ok, fix, note=""):
        items.append({"key": key, "label": label, "ok": bool(ok), "fix": fix, "note": note})
    crews = [c for c in S.list_crews(con) if c["active"]]
    add("crews", "수거팀·차량(적재 용량) 등록", crews and all((c["capacity_m3"] or 0) > 0 for c in crews), "settings/crews",
        f"사용 중 팀 {len(crews)}개")
    devs = {d["id"]: d for d in S.rows(con, "SELECT id, status, confirmed_at FROM device")}
    bad = [c["name"] for c in crews if not (c["device_id"] and S.device_ready(devs.get(c["device_id"])))]
    add("devices", "휴대폰 지문 확인·팀 연결", crews and not bad, "settings/devices", ("연결·확인 필요: " + ", ".join(bad[:5])) if bad else "")
    add("depot", "차고지 위치(기본값 아님)", S.get_setting(con, "depot", None) is not None, "settings/route")
    add("unload_site", "반입지 위치", S.get_setting(con, "unload_site", None) is not None, "settings/route")
    from dgpc import area as A
    add("dongs", "행정동 등록", bool(A.list_dongs(con, active_only=True)), "settings/dongs")
    addr = S.one(con, "SELECT COUNT(*) AS n FROM addr_point")["n"]
    add("address", "주소 자료 또는 행안부 주소 승인키", addr > 0 or bool(S.secret(con, "juso_search_key")), "settings/address", f"주소 자료 {addr:,}곳")
    add("backup_key", "자동 백업 키", BK.key_info(con) is not None, "settings/backup")
    fs = S.fee_state(con)
    add("fee", "수수료표 시행일", bool(fs.get("effective_from")), "fee", f"버전 {fs['version_id']} · 시행 {fs.get('effective_from') or '-'}")
    ci = S.get_setting(con, "contact_info", None)
    add("contact", "문의처(접수증·안내 문구)", bool(ci) and ci != S.CONTACT_PLACEHOLDER and "설정에서" not in ci, "settings/notice")
    adm = S.rows(con, "SELECT username, must_change, last_login_at FROM app_user WHERE role='ADMIN' AND active=1")
    add("admin_password", "관리자 비밀번호 변경(임시 비밀번호 아님)", adm and not any(a["must_change"] for a in adm), "users")
    return {"items": items, "ok": all(x["ok"] for x in items), "done": sum(x["ok"] for x in items), "total": len(items)}


# ------------------------------------------------------------------ 진단 (관리자)
DIAG_TABLES = ("request", "request_item", "request_version", "result_event", "result_import", "photo", "payment", "refund_case", "audit_log", "audit_note",
               "access_log", "assignment_batch", "assignment_stop", "transfer_bundle", "contact_log", "addr_point", "idem", "travel_cache")


def diag(con, state_dir: Path | None = None) -> dict:
    from dgpc import store
    d = state_dir or store.state_dir()
    size = lambda p: p.stat().st_size if p.exists() else 0
    db = d / "pc.db"
    counts = {}
    for t in DIAG_TABLES:
        try:
            counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:  # noqa: BLE001
            counts[t] = None
    ph = S.one(con, "SELECT COUNT(*) AS n, COALESCE(SUM(bytes),0) AS b FROM photo WHERE data IS NOT NULL")
    pages = con.execute("PRAGMA page_count").fetchone()[0]
    free = con.execute("PRAGMA freelist_count").fetchone()[0]
    psz = con.execute("PRAGMA page_size").fetchone()[0]
    g = lambda k: (S.get_setting(con, k, None) or {})
    return {"db_bytes": size(db), "wal_bytes": size(Path(str(db) + "-wal")), "photos": {"count": ph["n"], "bytes": ph["b"]},
            "free_bytes": free * psz, "page_bytes": pages * psz, "auto_vacuum": con.execute("PRAGMA auto_vacuum").fetchone()[0],
            "schema_version": con.execute("PRAGMA user_version").fetchone()[0], "tables": counts,
            "last": {"backup": S.get_setting(con, "last_backup_at", None), "verify_quick": g("last_quick_verify").get("at"), "verify_full": g("last_full_verify").get("at"),
                     "pii_purge": g("last_pii_purge_run").get("at"), "photo_purge": g("last_photo_purge_run").get("at"), "daily_tasks": g("last_daily_tasks").get("at"),
                     "refund_sync": g("last_refund_sync").get("at")}}


# ------------------------------------------------------------------ 최근 가져오기 (확인 코드 다시 보기)
def recent_imports(con, limit: int = 30, crew_id: str | None = None) -> list[dict]:
    """최근 결과·사진 가져오기: 팀·기기·시각·건수·휴대폰 확인 코드(결과/사진)·휴대폰이 받았는지(phone_confirmed).
    phone_confirmed: 담당자가 '휴대폰에 입력함'을 눌렀거나, 그 뒤 같은 기기로 보낸 목록(받은 순번 포함)을 휴대폰이 수신 확인한 경우."""
    limit = max(1, min(200, int(limit or 30)))
    dev_filter = ""
    args: list = []
    if crew_id is not None:
        dev = (S.one(con, "SELECT device_id FROM crew WHERE id=?", (crew_id,)) or {}).get("device_id") or "-"
        dev_filter = " WHERE device_id=?"; args = [dev]
    res = S.rows(con, f"""SELECT id, 'RESULT' AS kind, channel, device_id, imported_at, summary_json, release_code, phone_confirmed_at FROM result_import{dev_filter}
        ORDER BY imported_at DESC LIMIT ?""", args + [limit])
    phs = S.rows(con, f"""SELECT id, 'PHOTO' AS kind, 'USB' AS channel, device_id, imported_at, summary_json, release_code, phone_confirmed_at FROM photo_import{dev_filter}
        ORDER BY imported_at DESC LIMIT ?""", args + [limit])
    crews = {c["device_id"]: c["name"] for c in S.list_crews(con) if c["device_id"]}
    labels = {d["id"]: d["label"] for d in S.rows(con, "SELECT id, label FROM device")}
    out = []
    for r in sorted(res + phs, key=lambda x: x["imported_at"], reverse=True)[:limit]:
        counts = json.loads(r.pop("summary_json") or "{}")
        if r["kind"] == "RESULT" and counts.get("source") == "SOLO":
            r["kind"] = "SOLO"
        auto = None
        if r["kind"] == "RESULT":
            mx = S.one(con, "SELECT MAX(device_seq) AS s FROM result_event WHERE import_id=?", (r["id"],))["s"]
            if mx is not None:
                auto = S.one(con, """SELECT receipt_confirmed_at AS t FROM transfer_bundle WHERE device_id=? AND rel_seq >= ? AND receipt_confirmed_at IS NOT NULL
                    ORDER BY receipt_confirmed_at LIMIT 1""", (r["device_id"], mx))
        elif r["kind"] == "PHOTO":
            left = S.one(con, """SELECT COUNT(*) AS n FROM photo WHERE device_id=? AND recovery_status='RECOVERED' AND rel_acked_at IS NULL AND recovered_at <= ?""",
                         (r["device_id"], r["imported_at"]))["n"]
            auto = {"t": "목록으로 알림"} if left == 0 and counts.get("recovered") else None
        out.append({**r, "counts": counts, "crew": crews.get(r["device_id"]), "device_label": labels.get(r["device_id"]),
                    "release_code": r["release_code"], "photo_code": r["release_code"] if r["kind"] == "PHOTO" else None,
                    "phone_confirmed": bool(r["phone_confirmed_at"] or auto), "phone_confirmed_by": "MANUAL" if r["phone_confirmed_at"] else ("DAYLIST" if auto else None)})
    return out


def confirm_import(con, import_id: str) -> dict:
    """담당자가 휴대폰에 확인 코드를 넣었다고 표시."""
    with S.tx(con):
        n = con.execute("UPDATE result_import SET phone_confirmed_at=COALESCE(phone_confirmed_at, ?) WHERE id=?", (S.now(), import_id)).rowcount
        n += con.execute("UPDATE photo_import SET phone_confirmed_at=COALESCE(phone_confirmed_at, ?) WHERE id=?", (S.now(), import_id)).rowcount
        if not n:
            raise S.UserError("가져오기 기록을 찾을 수 없습니다.")
        S.audit(con, "import", import_id, "PHONE_CONFIRM", None)
    return {"ok": True}


