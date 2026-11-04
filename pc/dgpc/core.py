"""기반 계층: 트랜잭션·조회 도우미·변경 이력(해시 사슬)·설정 읽기/쓰기·시각·업무 오류·공통 검사.

모든 업무 모듈은 이 모듈 위에 선다. 이 모듈은 다른 업무 모듈을 부르지 않는다(요약 열 갱신 같은 위 계층 일은 on_dirty 로 등록받아 부른다).
"""
from __future__ import annotations

import contextlib
import contextvars
import csv
import datetime as dt
import hashlib
import itertools
import json
import re
from pathlib import Path

from dgpc import store as _store, settings as settings_reg

KST = dt.timezone(dt.timedelta(hours=9))


# CSV 수식 주입 막기: 이름·주소·사유 칸이 = + - @ 로 시작하면 엑셀이 수식으로 실행한다 → 앞에 ' 를 붙인다(숫자·'-'는 그대로)
def _csv_cell(v):
    if isinstance(v, str) and v and v[0] in "=+-@\t\r" and v != "-" and not re.fullmatch(r"-?\d+(\.\d+)?", v):
        return "'" + v
    return v


class _SafeCsv:
    def __init__(self, buf):
        self._w = csv.writer(buf)

    def writerow(self, row):
        return self._w.writerow([_csv_cell(x) for x in row])

    def writerows(self, rows_):
        for r in rows_:
            self.writerow(r)


def safe_csv_writer(buf):
    return _SafeCsv(buf)

ROOT = Path(__file__).resolve().parent.parent.parent
# PC 화면·휴대폰과 같은 표시 이름(접수 상태·연락·미수거 사유·스티커 확인). 휴대폰용 web/labels-data.js 는 tools/build_labels.py 가 만든다
LABELS = {k: v for k, v in json.loads((ROOT / "data" / "labels.json").read_text(encoding="utf8")).items() if k != "about"}


class UserError(Exception):
    """화면에 그대로 보여 줄 수 있는 업무 오류."""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_kst() -> str:
    return dt.datetime.now(KST).date().isoformat()


class StaleError(UserError):
    """다른 곳에서 먼저 바뀐 자료(낙관적 동시성). 서버는 409로 돌려준다."""

    def __init__(self, msg: str, **info):
        super().__init__(msg)
        self.info = info


_SP = itertools.count(1)


@contextlib.contextmanager
def tx(con):
    """쓰기 트랜잭션. 이미 트랜잭션 안이면 저장점(SAVEPOINT)으로 겹쳐 쓴다(겹친 쪽이 실패하면 그 부분만 되돌림).
    가장 바깥 트랜잭션이 끝나기 직전(COMMIT 전)에 바뀐 접수의 요약 열과 자동 환불 건을 같은 트랜잭션 안에서 맞춘다.
    조회 전용 연결에서는 아무것도 하지 않는다(쓰기는 WRITE_SINK로 넘김)."""
    if id(con) in _store.READONLY:
        yield
        return
    if con.in_transaction:
        sp = f"dg_sp{next(_SP)}"
        con.execute(f"SAVEPOINT {sp}")
        try:
            yield
            con.execute(f"RELEASE {sp}")
        except BaseException:
            con.execute(f"ROLLBACK TO {sp}")
            con.execute(f"RELEASE {sp}")
            raise
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
        if id(con) in _store.DIRTY_TRACKED:
            flush_dirty(con)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


# 위 계층이 등록하는 '바뀐 접수' 처리기(트랜잭션 안, COMMIT 직전에 부름). core 는 업무 모듈을 직접 부르지 않는다.
#   summary: derive.refresh_request_summary (접수 요약 열) → refunds: finance.sync_refunds (자동 환불 건). 이 순서로 부른다.
_DIRTY_ORDER = ("summary", "refunds")
_DIRTY_HANDLERS: dict = {}


def on_dirty(name: str, fn):
    """바뀐 접수 처리기 등록: fn(con, ids). derive·finance 모듈이 불러와질 때 스스로 등록한다."""
    assert name in _DIRTY_ORDER, name
    _DIRTY_HANDLERS[name] = fn


def flush_dirty(con):
    """트랜잭션 안: 트리거가 모은 접수의 요약 열을 다시 계산하고, 그 접수들만 자동 환불 건을 맞춘다."""
    missing = [k for k in _DIRTY_ORDER if k not in _DIRTY_HANDLERS]
    if missing:
        raise RuntimeError("바뀐 접수 처리기가 등록되지 않았습니다(" + ", ".join(missing) + "). dgpc.service 를 먼저 불러오세요.")
    for _ in range(4):
        ids = [r[0] for r in con.execute("SELECT DISTINCT id FROM temp.dirty_req").fetchall()]
        if not ids:
            return
        con.execute("DELETE FROM temp.dirty_req")
        ids = [i for i in ids if i]
        for k in _DIRTY_ORDER:
            _DIRTY_HANDLERS[k](con, ids)


def is_readonly(con) -> bool:
    return id(con) in _store.READONLY


# 조회 전용 연결에서 생긴 기록(변경 이력·접속기록)을 쓰기 연결로 넘기는 곳. 서버가 요청마다 정한다: fn(write_con) 을 잠금 안에서 실행.
WRITE_SINK: contextvars.ContextVar = contextvars.ContextVar("write_sink", default=None)


def write(con, fn):
    """기록 쓰기 한 묶음: 쓰기 연결이면 그 자리에서 트랜잭션으로, 조회 전용 연결이면 WRITE_SINK(서버 잠금 + 쓰기 연결)로 넘긴다."""
    if is_readonly(con):
        actor = ACTOR.get()
        def w(wc):
            tok = ACTOR.set(actor)
            try:
                with tx(wc):
                    return fn(wc)
            finally:
                ACTOR.reset(tok)
        return _sink(w)
    with tx(con):
        return fn(con)


def _sink(fn):
    s = WRITE_SINK.get()
    if s is None:
        raise RuntimeError("조회 전용 연결에서 기록을 남길 곳(WRITE_SINK)이 없습니다.")
    return s(fn)


def rows(con, sql, args=()):
    cur = con.execute(sql, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def one(con, sql, args=()):
    r = rows(con, sql, args)
    return r[0] if r else None


ACTOR: contextvars.ContextVar[str] = contextvars.ContextVar("actor", default="system")


def chain_hash(prev: str, fields: list) -> str:
    """해시 사슬: sha256(이전 해시 + 이 행 내용(JSON)). 한 행을 고치거나 지우면 그 뒤 사슬이 모두 어긋난다."""
    body = json.dumps(fields, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256((prev + body).encode("utf-8")).hexdigest()


def _audit_fields(r: dict) -> list:
    return [r["id"], r["entity"], r["entity_id"], r["action"], r["detail"], r["at"], r["actor"]]


def audit(con, entity, entity_id, action, detail=None):
    if is_readonly(con):  # 조회 전용 연결: 서버 잠금 안에서 쓰기 연결로(한 트랜잭션)
        return write(con, lambda wc: audit(wc, entity, entity_id, action, detail))
    prev = con.execute("SELECT hash FROM audit_log WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    prev = prev[0] if prev else (get_setting(con, "audit_chain_anchor") or "")
    note = note_sha = None
    if isinstance(detail, dict) and isinstance(detail.get("reason"), str) and detail["reason"].strip():
        # 사람이 적은 사유 글은 변경 이력 본문(지울 수 없는 해시 사슬) 대신 따로 두고 그 해시만 넣는다 → 보유기간 파기 때 글만 지울 수 있음
        note = detail["reason"][:300]
        note_sha = hashlib.sha256(note.encode()).hexdigest()[:16]
        detail = {**{k: v for k, v in detail.items() if k != "reason"}, "note_sha": note_sha}
    d = json.dumps(detail, ensure_ascii=False) if detail is not None else None
    at, actor = now(), ACTOR.get()
    ac = detail.get("after_close") if isinstance(detail, dict) and isinstance(detail.get("after_close"), str) else None
    cur = con.execute("INSERT INTO audit_log (entity, entity_id, action, detail, at, actor, prev_hash, legacy, after_close) VALUES (?,?,?,?,?,?,?,0,?)",
                      (entity, entity_id, action, d, at, actor, prev, ac))
    rid = cur.lastrowid
    con.execute("UPDATE audit_log SET hash=? WHERE id=?",
                (chain_hash(prev, _audit_fields({"id": rid, "entity": entity, "entity_id": entity_id, "action": action, "detail": d, "at": at, "actor": actor})), rid))
    if note is not None:
        req = entity_id if entity == "request" else (detail.get("request") if isinstance(detail, dict) else None)
        con.execute("INSERT INTO audit_note (audit_id, request_id, note, note_sha) VALUES (?,?,?,?)", (rid, req, note, note_sha))


# ------------------------------------------------------------------ 설정 (등록표: settings.py)
_NO_DEFAULT = object()


def get_setting(con, key, default=_NO_DEFAULT):
    """등록된 설정 읽기. default 를 주지 않으면 등록표(settings.SETTINGS)의 기본값. 등록되지 않은 키는 KeyError."""
    s = settings_reg.spec(key)
    r = one(con, "SELECT value FROM meta WHERE key=?", (key,))
    if r:
        return json.loads(r["value"])
    return s.get_default() if default is _NO_DEFAULT else default


def set_setting(con, key, value):
    """등록된 설정 쓰기(형식 검사 뒤). 호출하는 쪽이 트랜잭션을 연다."""
    settings_reg.check(key, value)
    con.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)))



# ------------------------------------------------------------------ 접수
def norm_phone(p: str) -> str:
    return re.sub(r"\D", "", p or "")


def norm_addr(a: str) -> str:
    return re.sub(r"\s+", "", a or "")


# ------------------------------------------------------------------ 마감 통제: 마감한 날의 접수·수납을 고치면 사유 필수 + "마감 후 수정" 표시
def close_info(con, work_date: str | None) -> dict | None:
    """그날의 가장 최근 마감 회차(없으면 None)."""
    if not work_date:
        return None
    return one(con, "SELECT * FROM day_close_log WHERE work_date=? ORDER BY seq DESC LIMIT 1", (work_date,))


def _after_close(con, rid: str) -> dict:
    """그 접수의 예정일이 이미 마감된 날이면 이력에 붙일 표시 {'after_close': 날짜}."""
    r = one(con, "SELECT planned_date FROM request WHERE id=?", (rid,))
    return {"after_close": r["planned_date"]} if r and close_info(con, r["planned_date"]) else {}


STALE_MSG = "다른 곳에서 바뀌었습니다. 다시 열어 확인하세요"


def check_req_ver(con, rid: str, base, required: bool = True):
    """화면이 열었을 때의 접수 버전(req_ver)과 지금 버전이 같은지(트랜잭션 안에서 부른다). 다르면 StaleError(서버는 409)."""
    cur = one(con, "SELECT req_ver FROM request WHERE id=?", (rid,))
    if not cur:
        raise UserError("접수를 찾을 수 없습니다.")
    if base in (None, ""):
        if required:
            raise StaleError("접수 버전(req_ver)이 없습니다. 화면을 새로 고친 뒤 다시 저장하세요.", req_ver=cur["req_ver"], request_id=rid)
        return cur["req_ver"]
    try:
        b = int(base)
    except (TypeError, ValueError):
        raise UserError("접수 버전(req_ver)이 올바르지 않습니다.")
    if b != cur["req_ver"]:
        raise StaleError(STALE_MSG, req_ver=cur["req_ver"], request_id=rid)
    return b


_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"


def chosung_of(s: str) -> str:
    return "".join(_CHO[(ord(ch) - 0xAC00) // 588] if 0 <= ord(ch) - 0xAC00 < 11172 else ch for ch in (s or ""))


def is_chosung(s: str) -> bool:
    return any("ㄱ" <= ch <= "ㅎ" for ch in s or "") and all("ㄱ" <= ch <= "ㅎ" or ch.isascii() for ch in s)


def _chunks(ids, n=500):
    ids = list(ids)
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def chosung_key(name: str | None, address: str | None) -> str:
    """초성 검색용 열 값: 이름|주소(공백 없이)의 초성."""
    return chosung_of(re.sub(r"\s+", "", (name or "") + "|" + (address or "")))


def _sane_ts(ts) -> str | None:
    """휴대폰 시각(UTC ISO) → 'YYYY-MM-DDTHH:MM:SSZ'. 형식 오류·지금+10분보다 미래·2020년 이전이면 None."""
    if not isinstance(ts, str):
        return None
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        return None
    t = t.astimezone(dt.timezone.utc)
    if t > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10) or t.year < 2020:
        return None
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _int_clamp(v, lo=0, hi=9999) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return lo


def _secs(key: str, v) -> dict:
    """시범 운영 비교용 걸린 시간(초). 화면에서 잰 값 중 5초~2시간만 남긴다(자리 비움 등 이상값 제외)."""
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return {}
    return {key: n} if 5 <= n <= 7200 else {}


def _date_ok(v: str) -> bool:
    try:
        dt.date.fromisoformat(v)
        return bool(re.fullmatch(r"\d{4}-\d\d-\d\d", v))
    except (TypeError, ValueError):
        return False


def incremental_vacuum(con, pages: int = 20000):
    """파기·사진 정리로 빈 쪽을 파일 크기에서 조금씩 돌려준다(auto_vacuum=INCREMENTAL인 DB만; 아니면 아무 일 없음). 트랜잭션 밖에서."""
    if con.in_transaction or is_readonly(con):
        return
    try:
        if con.execute("PRAGMA auto_vacuum").fetchone()[0] == 2:
            con.execute(f"PRAGMA incremental_vacuum({int(pages)})")
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ 통계·보고서 (개인정보 없이 집계)
def _kst_date(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(KST).date().isoformat()
    except ValueError:
        return iso[:10]


def _kst_range(start: str, end: str) -> tuple[str, str]:
    """한국시간 날짜 start~end(포함) → UTC 시각 [a, b)."""
    a = dt.datetime.fromisoformat(start).replace(tzinfo=KST).astimezone(dt.timezone.utc)
    b = (dt.datetime.fromisoformat(end) + dt.timedelta(days=1)).replace(tzinfo=KST).astimezone(dt.timezone.utc)
    return a.strftime("%Y-%m-%dT%H:%M:%SZ"), b.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ 같은 요청 다시 보내기 막기 (client_req_id)
IDEM_HOURS = 24


def idempotent(con, user: str, endpoint: str, key, fn):
    """화면이 보낸 client_req_id(요청마다 새 값)가 24시간 안에 이미 처리된 것이면 그때의 응답을 그대로 돌려준다(두 번 눌러도 한 건).
    처리와 응답 기록은 한 트랜잭션(처리 중 오류면 기록도 남지 않음)."""
    key = str(key or "").strip()[:80]
    if not key:
        return fn()
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=IDEM_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with tx(con):
        prev = one(con, "SELECT response_json FROM idem WHERE user=? AND endpoint=? AND key=? AND at>=?", (user or "-", endpoint, key, since))
        if prev:
            return json.loads(prev["response_json"])
        res = fn()
        con.execute("INSERT OR REPLACE INTO idem (key, user, endpoint, response_json, at) VALUES (?,?,?,?,?)",
                    (key, user or "-", endpoint, json.dumps(res, ensure_ascii=False, default=str), now()))
    return res


def purge_idem(con) -> int:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=IDEM_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with tx(con):
        return con.execute("DELETE FROM idem WHERE at < ?", (since,)).rowcount
