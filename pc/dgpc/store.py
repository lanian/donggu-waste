"""PC 로컬 저장소: SQLCipher로 DB 파일 전체를 암호화한다.

DB 키 우선순위
1. 환경변수 DG_DB_KEY (자동 테스트·서비스 실행용)
2. 운영체제 키 저장소 (Windows 자격 증명 관리자 / macOS 키체인 / Linux Secret Service) - keyring
3. 시작할 때 관리자가 입력하는 DB 비밀번호

SQLCipher 모듈이 없으면 실행을 멈춘다. (DG_ALLOW_PLAIN_DB=1 은 개발용으로만 평문 DB를 허용)
PC 개인키(서명·키 합의)는 파일이 아니라 이 암호화 DB 안에 보관한다.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import sys
from pathlib import Path

try:
    import sqlcipher3 as dbapi  # type: ignore
    ENCRYPTED = True
except ImportError:  # pragma: no cover
    import sqlite3 as dbapi  # type: ignore
    ENCRYPTED = False

KEYRING_SERVICE = "dongu-bulky-waste-pc"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS pc_key (name TEXT PRIMARY KEY, pem BLOB NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS device (
  id TEXT PRIMARY KEY, kex_pub BLOB NOT NULL, sign_pub BLOB NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ACTIVE','LOST','RETIRED')),
  registered_at TEXT NOT NULL, label TEXT);
CREATE TABLE IF NOT EXISTS crew (
  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, device_id TEXT REFERENCES device(id),
  capacity_m3 REAL NOT NULL DEFAULT 8, shift_start TEXT NOT NULL DEFAULT '09:00', shift_end TEXT NOT NULL DEFAULT '17:00',
  active INTEGER NOT NULL DEFAULT 1);

CREATE TABLE IF NOT EXISTS request (
  id TEXT PRIMARY KEY, receipt_no TEXT NOT NULL UNIQUE, req_ver INTEGER NOT NULL DEFAULT 1,
  name TEXT NOT NULL, phone TEXT NOT NULL CHECK (phone GLOB '0[0-9]*'), address TEXT NOT NULL,
  spot_note TEXT NOT NULL DEFAULT '', memo TEXT NOT NULL DEFAULT '',
  lat REAL, lon REAL, geo_source TEXT,
  planned_date TEXT, appt_start TEXT, appt_end TEXT,
  must_today INTEGER NOT NULL DEFAULT 0, urgent INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'RECEIVED', cancel_reason TEXT,
  received_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_request_phone ON request(phone);
CREATE INDEX IF NOT EXISTS ix_request_plan ON request(planned_date, status);
CREATE TABLE IF NOT EXISTS request_version (
  request_id TEXT NOT NULL REFERENCES request(id), req_ver INTEGER NOT NULL,
  snapshot_json TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (request_id, req_ver));
CREATE TABLE IF NOT EXISTS request_item (
  line_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES request(id),
  fee_rate_id TEXT, fee_version_id TEXT, name TEXT NOT NULL, spec TEXT NOT NULL DEFAULT '',
  unit TEXT NOT NULL, unit_label TEXT NOT NULL, qty INTEGER NOT NULL CHECK (qty > 0),
  unit_price INTEGER, amount INTEGER, price_status TEXT NOT NULL, removed INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_item_req ON request_item(request_id);

CREATE TABLE IF NOT EXISTS assignment_batch (
  id TEXT PRIMARY KEY, crew_id TEXT NOT NULL REFERENCES crew(id), work_date TEXT NOT NULL, assign_ver INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ACTIVE','SUPERSEDED')), route_json TEXT, created_at TEXT NOT NULL,
  UNIQUE (crew_id, work_date, assign_ver));
CREATE TABLE IF NOT EXISTS assignment_stop (
  batch_id TEXT NOT NULL REFERENCES assignment_batch(id), request_id TEXT NOT NULL REFERENCES request(id),
  seq INTEGER NOT NULL, eta TEXT, PRIMARY KEY (batch_id, request_id));
-- 한 접수는 활성 배정 1개만 (두 팀이 오프라인으로 같은 건을 처리하지 않게)
CREATE TABLE IF NOT EXISTS active_assignment (request_id TEXT PRIMARY KEY REFERENCES request(id), batch_id TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS transfer_bundle (
  id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES device(id), batch_id TEXT,
  work_date TEXT NOT NULL, assign_ver INTEGER NOT NULL, chunk_count INTEGER NOT NULL,
  envelope_sha256 TEXT NOT NULL, receipt_code TEXT NOT NULL, frames_json TEXT,
  displayed_at TEXT, receipt_confirmed_at TEXT);
CREATE TABLE IF NOT EXISTS result_import (
  id TEXT PRIMARY KEY, channel TEXT NOT NULL CHECK (channel IN ('MAIL','QR','USB')),
  device_id TEXT NOT NULL, package_sha256 TEXT NOT NULL UNIQUE,
  imported_at TEXT NOT NULL, summary_json TEXT, release_code TEXT);
CREATE TABLE IF NOT EXISTS result_event (
  event_id TEXT PRIMARY KEY, import_id TEXT NOT NULL REFERENCES result_import(id),
  device_id TEXT NOT NULL, device_seq INTEGER NOT NULL,
  request_id TEXT NOT NULL, assign_ver INTEGER NOT NULL, base_req_ver INTEGER, kind TEXT NOT NULL,
  body_json TEXT NOT NULL, apply_status TEXT NOT NULL CHECK (apply_status IN ('APPLIED','CONFLICT','REJECTED')),
  conflict_reason TEXT, resolved_reason TEXT, resolved_at TEXT, device_ts TEXT,
  UNIQUE (device_id, device_seq));
CREATE INDEX IF NOT EXISTS ix_event_req ON result_event(request_id);
CREATE TABLE IF NOT EXISTS photo (
  photo_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, phase TEXT, sha256 TEXT NOT NULL, bytes INTEGER,
  device_id TEXT NOT NULL, data BLOB, recovery_status TEXT NOT NULL DEFAULT 'EXPECTED', recovered_at TEXT);
CREATE TABLE IF NOT EXISTS photo_import (
  id TEXT PRIMARY KEY, package_sha256 TEXT NOT NULL UNIQUE, device_id TEXT NOT NULL, imported_at TEXT NOT NULL,
  summary_json TEXT, release_code TEXT);
CREATE TABLE IF NOT EXISTS qr_test (
  id TEXT PRIMARY KEY, device_id TEXT NOT NULL, params_json TEXT NOT NULL, frames_json TEXT NOT NULL, bytes INTEGER NOT NULL,
  check_code TEXT NOT NULL, created_at TEXT NOT NULL, elapsed_s REAL, reads INTEGER, verified INTEGER, note TEXT, recorded_at TEXT);
CREATE TABLE IF NOT EXISTS payment (
  id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES request(id), kind TEXT NOT NULL CHECK (kind IN ('PAY','REFUND')),
  amount INTEGER NOT NULL CHECK (amount > 0), method TEXT NOT NULL, paid_on TEXT NOT NULL, note TEXT,
  recorded_at TEXT NOT NULL, void_reason TEXT, voided_at TEXT);
CREATE INDEX IF NOT EXISTS ix_payment_req ON payment(request_id);
CREATE TABLE IF NOT EXISTS unload_log (
  id TEXT PRIMARY KEY, crew_id TEXT NOT NULL REFERENCES crew(id), work_date TEXT NOT NULL, at_time TEXT, place TEXT NOT NULL,
  volume_m3 REAL, weight_kg REAL, note TEXT, recorded_at TEXT NOT NULL, void_reason TEXT);
CREATE TABLE IF NOT EXISTS item_link (
  id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES request(id), line_id TEXT NOT NULL, fee_rate_id TEXT NOT NULL, fee_version_id TEXT NOT NULL,
  unit_price INTEGER NOT NULL CHECK (unit_price > 0), reason TEXT NOT NULL, confirmed_by TEXT NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL,
  void_reason TEXT, voided_at TEXT);
CREATE INDEX IF NOT EXISTS ix_item_link_req ON item_link(request_id);
CREATE TABLE IF NOT EXISTS addr_point (
  sigungu TEXT NOT NULL DEFAULT '', road TEXT NOT NULL, main INTEGER NOT NULL, sub INTEGER NOT NULL DEFAULT 0, lat REAL NOT NULL, lon REAL NOT NULL,
  PRIMARY KEY (road, main, sub, sigungu));
CREATE TABLE IF NOT EXISTS fee_revision (
  id TEXT PRIMARY KEY, rev_no INTEGER NOT NULL, base_version TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('DRAFT','APPROVED','REJECTED')),
  effective_from TEXT, confirmed_by TEXT, note TEXT, overrides_json TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL, created_at TEXT NOT NULL, approved_by TEXT, approved_at TEXT, reject_reason TEXT);
CREATE TABLE IF NOT EXISTS app_user (
  id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('ADMIN','INTAKE','FIELD','LEAD')), pw_salt TEXT NOT NULL, pw_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1, must_change INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, last_login_at TEXT, crew_id TEXT);
-- (구) 날짜별 마감 1행. v0.10부터 마감 기록은 day_close_log 에 회차별로 쌓는다(덮어쓰지 않음). 이 표는 이전 DB 이전용으로만 읽는다.
CREATE TABLE IF NOT EXISTS day_close (work_date TEXT PRIMARY KEY, closed_at TEXT NOT NULL, summary_json TEXT NOT NULL, note TEXT);
-- 마감 기록 회차: 담당자 기록(RECORDED) → 팀장 확인(APPROVED). 다시 마감하면 새 회차가 생긴다.
CREATE TABLE IF NOT EXISTS day_close_log (
  id TEXT PRIMARY KEY, work_date TEXT NOT NULL, seq INTEGER NOT NULL, closed_at TEXT NOT NULL, closed_by TEXT, summary_json TEXT NOT NULL,
  note TEXT, carried_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'RECORDED' CHECK (status IN ('RECORDED','APPROVED')),
  approved_by TEXT, approved_at TEXT, approve_note TEXT, UNIQUE (work_date, seq));
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL, entity_id TEXT NOT NULL, action TEXT NOT NULL,
  detail TEXT, at TEXT NOT NULL);
-- 행정동: 기본 담당 팀, 수거 요일(0=월 … 6=일, 쉼표), 일일 한도(건수·m³), 주소 자동 판별 낱말(법정동·도로명 등)
CREATE TABLE IF NOT EXISTS dong (
  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1, crew_id TEXT,
  weekdays TEXT NOT NULL DEFAULT '', daily_limit INTEGER, daily_limit_m3 REAL, keywords_json TEXT NOT NULL DEFAULT '[]',
  sort INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
-- 휴무일·공휴일 (관리자가 직접 넣음. 실제 공휴일을 미리 넣어 두지 않는다)
CREATE TABLE IF NOT EXISTS holiday (day TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
-- 민원·통화 기록 (접수별)
CREATE TABLE IF NOT EXISTS contact_log (
  id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES request(id), at TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
  actor TEXT, recorded_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_contact_req ON contact_log(request_id);
-- 개인정보 파기 대장 (보유기간 지난 접수의 이름·연락처·주소·메모를 가명 처리한 기록)
CREATE TABLE IF NOT EXISTS pii_purge_log (
  id TEXT PRIMARY KEY, at TEXT NOT NULL, count INTEGER NOT NULL, actor TEXT, criteria_json TEXT NOT NULL, trigger TEXT NOT NULL DEFAULT 'MANUAL');
-- 개인정보 접속기록 (조회·목록·출력·내보내기). 해시 사슬로 위·변조 확인.
CREATE TABLE IF NOT EXISTS access_log (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', count INTEGER,
  filter TEXT NOT NULL DEFAULT '', client TEXT NOT NULL DEFAULT 'local', prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_access_at ON access_log(at);
-- 접속기록 월 점검 기록
CREATE TABLE IF NOT EXISTS access_check (
  id TEXT PRIMARY KEY, month TEXT NOT NULL, at TEXT NOT NULL, actor TEXT NOT NULL, result_json TEXT NOT NULL, note TEXT);
-- 환불 절차: 신청 → 결재(팀장·관리자, 신청자 아님) → 지급 기록. 계좌번호는 저장하지 않는다(확인 여부·끝 4자리만).
CREATE TABLE IF NOT EXISTS refund_case (
  id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES request(id), cause TEXT NOT NULL, amount INTEGER NOT NULL CHECK (amount > 0),
  status TEXT NOT NULL CHECK (status IN ('DRAFT','REQUESTED','APPROVED','REJECTED','PAID','VOID')), auto INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, requested_by TEXT, requested_at TEXT, note TEXT,
  approved_by TEXT, approved_at TEXT, approve_note TEXT,
  paid_on TEXT, pay_method TEXT, account_checked INTEGER NOT NULL DEFAULT 0, account_last4 TEXT, paid_by TEXT, paid_at TEXT, payment_id TEXT, void_reason TEXT);
CREATE INDEX IF NOT EXISTS ix_refund_req ON refund_case(request_id);
-- 세외수입 대사 파일 가져오기 기록
CREATE TABLE IF NOT EXISTS recon_import (
  id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, at TEXT NOT NULL, actor TEXT, rows INTEGER NOT NULL, matched INTEGER NOT NULL, summary_json TEXT NOT NULL);
"""


def state_dir() -> Path:
    return Path(os.environ.get("DG_STATE", Path(__file__).resolve().parent.parent / "state"))


def _keyring_get() -> str | None:
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, "db-key")
    except Exception:
        return None


def _keyring_set(value: str) -> bool:
    try:
        import keyring
        kr = keyring.get_keyring()
        # Windows 자격 증명 관리자 기본값(ENTERPRISE)은 도메인 로밍 프로필로 키가 복제될 수 있다 → 이 PC에만(LOCAL_MACHINE = 2)
        if type(kr).__name__ == "WinVaultKeyring" and hasattr(kr, "persist"):
            try:
                kr.persist = 2
            except Exception:  # noqa: BLE001
                pass
        kr.set_password(KEYRING_SERVICE, "db-key", value)
        return keyring.get_password(KEYRING_SERVICE, "db-key") == value
    except Exception:
        return False


# 콘솔에서 입력받은 DB 비밀번호(키 저장소·DG_DB_KEY가 없을 때). DB가 실제로 열린 뒤에만 이 프로세스 메모리에 기억해
# 조회용 연결·백그라운드 작업·예약 작업이 비밀번호를 다시 묻지 않게 한다(디스크에는 남기지 않음).
_PROMPTED_KEY: str | None = None


def db_key(create: bool = False) -> str:
    k = os.environ.get("DG_DB_KEY") or _keyring_get()
    if k:
        return k
    if _PROMPTED_KEY:
        return _PROMPTED_KEY
    if create:
        k = secrets.token_urlsafe(32)
        if _keyring_set(k):
            print("DB 키를 운영체제 키 저장소에 보관했습니다.")
            return k
        print("운영체제 키 저장소를 쓸 수 없습니다. DB 비밀번호를 정해 주세요 (분실 시 복구 불가).")
        while True:
            a = getpass.getpass("새 DB 비밀번호(12자 이상): ")
            if len(a) >= 12 and a == getpass.getpass("한 번 더: "):
                return a
            print("12자 이상, 두 번 같게 입력해 주세요.")
    return getpass.getpass("DB 비밀번호: ")


class _Connection(dbapi.Connection):
    """업무 DB 연결. 조회 전용·요약 열 추적 여부를 연결 객체에 직접 표시한다.
    (예전에는 id(con) 집합으로 기억했는데, 버린 조회 전용 연결의 메모리 주소를 새 쓰기 연결이 다시 받으면
    쓰기 연결이 조회 전용으로 오인돼 기록이 막혔다.)"""
    dg_readonly = False
    dg_dirty_tracked = False


def is_readonly(con) -> bool:
    return getattr(con, "dg_readonly", False)


def is_dirty_tracked(con) -> bool:
    return getattr(con, "dg_dirty_tracked", False)


def connect(create: bool = False, readonly: bool = False):
    """업무 DB 열기. readonly=True: 화면 조회 전용 연결(쓰기 금지 PRAGMA query_only, 이전 단계 실행 안 함)."""
    if not ENCRYPTED and os.environ.get("DG_ALLOW_PLAIN_DB") != "1":
        sys.exit("SQLCipher 모듈(sqlcipher3)이 없어 실행을 멈춥니다. 'pip install -r pc/requirements.txt' 후 다시 실행하세요.")
    d = state_dir()
    path = d / "pc.db"
    if not path.exists() and not create:
        sys.exit("PC 자료가 없습니다. 먼저 'python pc_tool.py init'을 실행하세요.")
    d.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    con = dbapi.connect(str(path), check_same_thread=False, isolation_level=None, factory=_Connection)
    if ENCRYPTED:
        global _PROMPTED_KEY
        key = db_key(create=is_new)
        con.execute("PRAGMA key = " + _quote(key))
        try:
            con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except Exception:
            sys.exit("DB를 열 수 없습니다. DB 키(비밀번호)가 맞는지 확인하세요.")
        if not (os.environ.get("DG_DB_KEY") or _keyring_get()):
            _PROMPTED_KEY = key  # 맞는 것으로 확인된 입력값만 기억
    con.execute("PRAGMA busy_timeout = 10000")
    if readonly:
        con.execute("PRAGMA query_only = ON")
        con.dg_readonly = True
        return con
    con.execute("PRAGMA foreign_keys = ON")
    if is_new:  # 새 DB: 사진·개인정보 파기 뒤 빈 공간을 조금씩 돌려줄 수 있게(PRAGMA incremental_vacuum)
        con.execute("PRAGMA auto_vacuum = INCREMENTAL")
    con.execute("PRAGMA journal_mode = WAL")
    run_migrations(con, path=None if is_new else path)
    install_dirty_tracking(con)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return con


# ------------------------------------------------------------------ 이전 단계 (PRAGMA user_version)
# 단계마다 BEGIN IMMEDIATE 한 트랜잭션(구조 변경 + 기존 자료 채우기)으로 실행한다. 중간에 멈추면 그 단계 전체가 되돌려지고
# 다음에 열 때 그 단계부터 다시 한다. DB가 이 프로그램보다 새 버전이면 열지 않는다(옛 프로그램으로 새 DB를 망치지 않게).
_TEST_FAIL_STEP: int | None = None  # 시험용: 이 단계의 끝(기록 직전)에서 일부러 실패


def _schema_statements(sql: str) -> list[str]:
    body = "\n".join(ln for ln in sql.splitlines() if not ln.strip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def _m1_legacy(con):
    """v0.12까지의 구조(기존 이전 함수 모음, 여러 번 실행해도 같음)."""
    for st in _schema_statements(SCHEMA):
        con.execute(st)
    cols = {r[1] for r in con.execute("PRAGMA table_info(audit_log)").fetchall()}
    if "actor" not in cols:  # 이전 버전 DB에 작업자 열 추가
        con.execute("ALTER TABLE audit_log ADD COLUMN actor TEXT")
    if "line_id" not in _cols(con, "photo"):  # 사진을 품목 행에 묶기(선택)
        con.execute("ALTER TABLE photo ADD COLUMN line_id TEXT")
    if "crew_id" not in _cols(con, "app_user"):  # 수거팀 계정 ↔ 팀
        con.execute("ALTER TABLE app_user ADD COLUMN crew_id TEXT")
    if "purged_at" not in _cols(con, "photo"):
        con.execute("ALTER TABLE photo ADD COLUMN purged_at TEXT")
    migrate(con)


# v0.13 색인: 실제 조회문 기준 (주석의 함수에서 쓰는 WHERE·JOIN)
V13_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_photo_req ON photo(request_id)",                                   # get_request·day_summary·purge_photos
    "CREATE INDEX IF NOT EXISTS ix_stop_req ON assignment_stop(request_id)",                           # request_in_crew·_export_row·stats 팀 이름
    "CREATE INDEX IF NOT EXISTS ix_active_batch ON active_assignment(batch_id)",                       # confirm_plan 옛 배정 풀기
    "CREATE INDEX IF NOT EXISTS ix_batch_date ON assignment_batch(work_date, status)",                 # day_summary·list_batches·pending
    "CREATE INDEX IF NOT EXISTS ix_audit_eai ON audit_log(entity, action, id)",                        # list_audit(대상·작업 거름)·pilot_report CARRY/CREATE
    "CREATE INDEX IF NOT EXISTS ix_audit_eid ON audit_log(entity_id)",                                 # 접수별 이력
    "CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_log(at)",                                         # 기간 거름
    "CREATE INDEX IF NOT EXISTS ix_audit_after_close ON audit_log(after_close) WHERE after_close IS NOT NULL",  # _post_close_edits
    "CREATE INDEX IF NOT EXISTS ix_payment_paid_on ON payment(paid_on) WHERE void_reason IS NULL",     # revenue·revenue_csv (void_reason IS NULL AND paid_on BETWEEN)
    "CREATE INDEX IF NOT EXISTS ix_request_received ON request(received_at)",                          # 접수일 기준 조회·통계
    "CREATE INDEX IF NOT EXISTS ix_request_completed ON request(completed_at)",                        # 완료일 기준 통계
    "CREATE INDEX IF NOT EXISTS ix_request_cancelled ON request(cancelled_at) WHERE cancelled_at IS NOT NULL",  # 완료일 기준(취소일)·마감 환불
    "CREATE INDEX IF NOT EXISTS ix_request_status_plan ON request(status, planned_date)",              # 빠른 조회 OVERDUE·pending·carry
    "CREATE INDEX IF NOT EXISTS ix_request_pay ON request(pay_status)",                                # 수납 상태 거름
    "CREATE INDEX IF NOT EXISTS ix_request_origin ON request(origin_device) WHERE origin_device IS NOT NULL",  # field_intakes
    "CREATE INDEX IF NOT EXISTS ix_event_import ON result_event(import_id)",                           # 가져오기별 충돌 일괄 처리·최근 가져오기
    "CREATE INDEX IF NOT EXISTS ix_event_dev_seq ON result_event(device_id, device_seq)",              # release_info(기기별 받은 순번)
    "CREATE INDEX IF NOT EXISTS ix_event_conflict ON result_event(apply_status) WHERE apply_status='CONFLICT'",  # 충돌함
    "CREATE INDEX IF NOT EXISTS ix_bundle_batch ON transfer_bundle(batch_id)",                         # list_batches·_cancel
    "CREATE INDEX IF NOT EXISTS ix_bundle_date ON transfer_bundle(work_date)",                         # day_summary 미확인 전송
    "CREATE INDEX IF NOT EXISTS ix_bundle_dev ON transfer_bundle(device_id, displayed_at)",            # 결과 미회수 팀·release 확인
    "CREATE INDEX IF NOT EXISTS ix_unload_date ON unload_log(work_date)",                              # list_unloads·load_check·stats
    "CREATE INDEX IF NOT EXISTS ix_access_actor ON access_log(actor, at)",                             # 접속기록 사용자 거름
    "CREATE INDEX IF NOT EXISTS ix_access_action ON access_log(action, at)",                           # 접속기록 작업 거름
    "CREATE INDEX IF NOT EXISTS ix_import_dev ON result_import(device_id, imported_at)",               # 결과 미회수 팀·최근 가져오기
    "CREATE INDEX IF NOT EXISTS ix_close_date ON day_close_log(work_date, seq)",                       # close_info
    "CREATE INDEX IF NOT EXISTS ix_refund_status ON refund_case(status)",                              # 환불 결재 대기
    "CREATE INDEX IF NOT EXISTS ix_idem_at ON idem(at)",                                               # 하루 지난 재전송 기록 정리
]


def _m2_v13(con):
    """v0.13: 접수 요약 열(목록·통계를 SQL로), 변경 이력 '마감 후 수정' 열, 결과 기록 순번 재사용 허용(휴대폰 복원),
    재전송 방지(idem), 휴대폰 확인 코드 추적, 색인. 요약 열은 기존 자료로 채운다."""
    for col, decl in (("fee_final", "INTEGER"), ("levied", "INTEGER"), ("paid_total", "INTEGER NOT NULL DEFAULT 0"), ("pay_status", "TEXT"),
                      ("active_crew_id", "TEXT"), ("chosung", "TEXT")):
        if col not in _cols(con, "request"):
            con.execute(f"ALTER TABLE request ADD COLUMN {col} {decl}")
    if "after_close" not in _cols(con, "audit_log"):
        con.execute("ALTER TABLE audit_log ADD COLUMN after_close TEXT")
        con.execute("UPDATE audit_log SET after_close=json_extract(detail, '$.after_close') WHERE detail LIKE '%after_close%' AND json_valid(detail)")
    # 결과 기록: (기기, 순번) 고유 제약을 뺀다(휴대폰을 백업에서 되살리면 순번이 다시 쓰임). 같은 기록은 event_id로만 막는다.
    sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='result_event'").fetchone()[0]
    if "UNIQUE (device_id, device_seq)" in sql or "warning" not in _cols(con, "result_event"):
        cols = ("event_id, import_id, device_id, device_seq, request_id, assign_ver, base_req_ver, kind, body_json, apply_status, "
                "conflict_reason, resolved_reason, resolved_at, device_ts")
        con.execute("""CREATE TABLE result_event_v13 (
          event_id TEXT PRIMARY KEY, import_id TEXT NOT NULL REFERENCES result_import(id),
          device_id TEXT NOT NULL, device_seq INTEGER NOT NULL,
          request_id TEXT NOT NULL, assign_ver INTEGER NOT NULL, base_req_ver INTEGER, kind TEXT NOT NULL,
          body_json TEXT NOT NULL, apply_status TEXT NOT NULL CHECK (apply_status IN ('APPLIED','CONFLICT','REJECTED')),
          conflict_reason TEXT, resolved_reason TEXT, resolved_at TEXT, device_ts TEXT, warning TEXT)""")
        con.execute(f"INSERT INTO result_event_v13 ({cols}) SELECT {cols} FROM result_event")
        con.execute("DROP TABLE result_event")
        con.execute("ALTER TABLE result_event_v13 RENAME TO result_event")
        con.execute("CREATE INDEX IF NOT EXISTS ix_event_req ON result_event(request_id)")
    for table, col, decl in (("photo", "rel_acked_at", "TEXT"), ("transfer_bundle", "rel_seq", "INTEGER"), ("transfer_bundle", "rel_json", "TEXT"),
                             ("result_import", "phone_confirmed_at", "TEXT"), ("photo_import", "phone_confirmed_at", "TEXT")):
        if col not in _cols(con, table):
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    con.execute("""CREATE TABLE IF NOT EXISTS idem (key TEXT NOT NULL, user TEXT NOT NULL, endpoint TEXT NOT NULL, response_json TEXT NOT NULL,
        at TEXT NOT NULL, PRIMARY KEY (user, endpoint, key))""")
    for st in V13_INDEXES:
        con.execute(st)
    from dgpc import derive as _D  # 요약 열 채우기(접수 전체, 묶음으로). store 는 core 아래 계층이라 여기서만 늦게 불러온다
    ids = [r[0] for r in con.execute("SELECT id FROM request").fetchall()]
    for i in range(0, len(ids), 500):
        _D.refresh_request_summary(con, ids[i:i + 500])


def _m3_round_unit(con):
    """v0.13: 감면액 끝수 단위를 접수마다 저장(감면을 적용·바꿀 때의 설정값). 설정을 나중에 바꿔도 이미 감면한 접수의 부과 금액은 그대로.
    기존 감면 접수는 지금 설정값으로 채운다(지금까지 그 값으로 계산·표시해 왔으므로 금액이 바뀌지 않음)."""
    if "reduction_round_unit" not in _cols(con, "request"):
        con.execute("ALTER TABLE request ADD COLUMN reduction_round_unit INTEGER")
    row = con.execute("SELECT value FROM meta WHERE key='reduction_round_unit'").fetchone()
    try:
        unit = json.loads(row[0]) if row else 1
    except (TypeError, ValueError):
        unit = 1
    unit = unit if unit in (1, 10) else 1
    con.execute("UPDATE request SET reduction_round_unit=? WHERE reduction_code IS NOT NULL AND reduction_round_unit IS NULL", (unit,))


def _m4_device_gen(con):
    """v0.13: 휴대폰 기록 '세대'(gen). 휴대폰은 백업에서 복원할 때마다 새 세대를 만들고 기록마다 세대를 적는다.
    PC는 기기·세대별로 받은 순번을 따로 세어(결과 반영 정보 rel.g), 복원 뒤 다시 쓰인 번호를 이미 받은 것으로 착각하지 않는다.
    - result_event.gen: 기록의 세대(이전 휴대폰 기록은 NULL)
    - device_gen: 기기·세대별 첫 번호(휴대폰이 결과 파일 머리에 알려 줌, 없으면 받은 가장 작은 번호)·마지막으로 받은 시각
    - result_seq: 기기·세대별로 받은 번호(중복 event_id로 건너뛴 것·충돌함에 넣은 것 포함). 이전 기록은 세대 ''로 채운다."""
    if "gen" not in _cols(con, "result_event"):
        con.execute("ALTER TABLE result_event ADD COLUMN gen TEXT")
    con.execute("""CREATE TABLE IF NOT EXISTS device_gen (device_id TEXT NOT NULL, gen TEXT NOT NULL, start_seq INTEGER NOT NULL,
        first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, PRIMARY KEY (device_id, gen))""")
    con.execute("""CREATE TABLE IF NOT EXISTS result_seq (device_id TEXT NOT NULL, gen TEXT NOT NULL DEFAULT '', seq INTEGER NOT NULL,
        PRIMARY KEY (device_id, gen, seq)) WITHOUT ROWID""")
    con.execute("""INSERT OR IGNORE INTO result_seq (device_id, gen, seq) SELECT device_id, COALESCE(gen, ''), device_seq FROM result_event
        WHERE device_seq >= 1""")


ADDR_POINT_COLS = ("sigungu", "road", "main", "sub", "lat", "lon", "dong_h", "dong_b", "bname", "bname_cho", "jibun_main", "jibun_sub")


def _m5_addr_codes(con):
    """5단계: 주소 위치 자료에 행안부 식별 번호를 둔다(행안부 검색 결과의 좌표를 좌표 API 없이 이 PC 자료로 찾기).
    - underground(지하여부 0/1): 같은 도로명·건물번호라도 지상·지하를 따로 둔다(기본 키에 넣으려고 표를 새로 만들어 옮김)
    - road_cd(도로명코드 12자리)·bjd_cd(법정동코드 10자리)·entr_sn(출입구일련번호): 위치정보요약DB를 다시 가져오면 채워진다
    기존 자료는 지상(0)·식별 번호 없음으로 옮긴다(좌표·동·건물명·지번 그대로)."""
    if "underground" in _cols(con, "addr_point"):
        return
    con.execute("""CREATE TABLE addr_point_v5 (
      sigungu TEXT NOT NULL DEFAULT '', road TEXT NOT NULL, main INTEGER NOT NULL, sub INTEGER NOT NULL DEFAULT 0, lat REAL NOT NULL, lon REAL NOT NULL,
      dong_h TEXT, dong_b TEXT, bname TEXT, bname_cho TEXT, jibun_main INTEGER, jibun_sub INTEGER,
      underground INTEGER NOT NULL DEFAULT 0, road_cd TEXT, bjd_cd TEXT, entr_sn INTEGER,
      PRIMARY KEY (road, main, sub, sigungu, underground))""")
    have = [c for c in ADDR_POINT_COLS if c in _cols(con, "addr_point")]
    con.execute(f"INSERT INTO addr_point_v5 ({', '.join(have)}) SELECT {', '.join(have)} FROM addr_point")
    con.execute("DROP TABLE addr_point")
    con.execute("ALTER TABLE addr_point_v5 RENAME TO addr_point")
    con.execute("CREATE INDEX IF NOT EXISTS ix_addr_point_rcd ON addr_point(road_cd, main, sub)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_addr_point_bjd ON addr_point(bjd_cd, jibun_main, jibun_sub)")


def _m6_drop_travel_cache(con):
    """6단계: 카카오모빌리티 이동시간을 이 PC에 보관하지 않는다(노선 계산마다 새로 묻는다). 남아 있던 좌표 쌍·시간을 지우고 표도 없앤다."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='travel_cache'").fetchone():
        con.execute("DELETE FROM travel_cache")
        con.execute("DROP TABLE travel_cache")


MIGRATIONS = [(1, "v0.12 이하 구조", _m1_legacy), (2, "v0.13 요약 열·색인·재전송 방지", _m2_v13), (3, "감면액 끝수 단위 접수별 저장", _m3_round_unit),
              (4, "휴대폰 기록 세대(복원 구분)", _m4_device_gen), (5, "주소 위치 자료 행안부 식별 번호(도로명코드·법정동코드·지하여부)", _m5_addr_codes),
              (6, "카카오 이동시간 보관 중단(travel_cache 삭제)", _m6_drop_travel_cache)]
LATEST = MIGRATIONS[-1][0]


def pre_migration_backup(con, path: Path, from_ver: int) -> Path | None:
    """이전 단계 실행 전 DB 파일 복사(같은 DB 키로 암호화된 그대로). state/pre-migration/ 에 둔다."""
    import datetime as _dt
    import shutil
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:  # noqa: BLE001
        pass
    d = path.parent / "pre-migration"
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"pc-v{from_ver}-to-v{LATEST}-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    n = 1
    while dest.exists():
        dest = d / f"pc-v{from_ver}-to-v{LATEST}-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{n}.db"; n += 1
    shutil.copy2(path, dest)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return dest


# 사전 사본 보관: 개인정보가 든 DB 사본이므로 오래 두지 않는다. 가장 새 사본 1개는 늘 남기고(업데이트 직후 되돌리기용),
# 나머지는 '새것부터 3개 안' 그리고 '30일 이내'인 것만 남긴다(둘 중 하나라도 벗어나면 지움). 매일 예약 작업에서 돈다.
PRE_MIGRATION_KEEP = 3
PRE_MIGRATION_DAYS = 30
_PRE_RE = re.compile(r"^pc-v\d+-to-v\d+-(\d{8}-\d{6})(?:-\d+)?\.db$")


def pre_migration_dir(d: Path | None = None) -> Path:
    return (d or state_dir()) / "pre-migration"


def _dated_copies(folder: Path, pattern: re.Pattern) -> list[tuple[float, Path]]:
    """이름에 만든 시각이 든 DB 사본 목록 (만든 시각(초), 경로), 새것부터. 시각은 파일 이름(만든 때, 이 PC 시각) —
    copy2·rename 은 원본 DB의 수정 시각을 그대로 두므로 파일 시각은 쓰지 않음."""
    import datetime as _dt
    out = []
    if not folder.is_dir():
        return out
    for f in folder.iterdir():
        m = pattern.match(f.name)
        if not m or not f.is_file():
            continue
        try:
            t = _dt.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").timestamp()
        except ValueError:
            t = f.stat().st_mtime
        out.append((t, f))
    return sorted(out, key=lambda x: (x[0], x[1].name), reverse=True)


def _pre_copies(d: Path | None = None) -> list[tuple[float, Path]]:
    return _dated_copies(pre_migration_dir(d), _PRE_RE)


def _apply_copy_rule(copies: list[tuple[float, Path]], now: float | None) -> list[str]:
    """사본 보관 규칙: 가장 새 것은 늘 남기고, 나머지는 새것부터 PRE_MIGRATION_KEEP 개 안·PRE_MIGRATION_DAYS 일 이내만. 지운 이름 목록.
    SQLite 곁 파일(-wal·-shm)도 함께 지운다."""
    import time as _time
    now = _time.time() if now is None else now
    gone = []
    for i, (t, f) in enumerate(copies):
        if i == 0:
            continue
        if i >= PRE_MIGRATION_KEEP or now - t > PRE_MIGRATION_DAYS * 86400:
            try:
                f.unlink()
                gone.append(f.name)
            except OSError:
                continue
            for ext in ("-wal", "-shm"):
                try:
                    Path(str(f) + ext).unlink()
                except OSError:
                    pass
    return gone


def pre_migration_info(d: Path | None = None) -> dict:
    """진단용: 사전 사본 개수·크기·가장 오래된/새 사본 이름."""
    cs = _pre_copies(d)
    return {"count": len(cs), "bytes": sum(f.stat().st_size for _t, f in cs), "newest": cs[0][1].name if cs else None,
            "oldest": cs[-1][1].name if cs else None, "keep": PRE_MIGRATION_KEEP, "days": PRE_MIGRATION_DAYS}


def pre_migration_retention(d: Path | None = None, now: float | None = None) -> list[str]:
    """보관 규칙을 벗어난 사전 사본을 지운다(가장 새 것은 늘 남김). 지운 파일 이름 목록."""
    return _apply_copy_rule(_pre_copies(d), now)


# 복원 전 DB 사본(state/pc.db.before-restore-*): 업데이트 사전 사본과 같은 개인정보 든 DB 사본이므로 같은 규칙으로 정리한다.
_RESTORE_RE = re.compile(r"^pc\.db\.before-restore-(\d{8}-\d{6})(?:-\d+)?$")


def restore_copy_retention(d: Path | None = None, now: float | None = None) -> list[str]:
    """보관 규칙을 벗어난 복원 전 사본(곁 파일 포함)을 지운다(가장 새 것은 늘 남김). 지운 파일 이름 목록."""
    return _apply_copy_rule(_dated_copies(d or state_dir(), _RESTORE_RE), now)


def run_migrations(con, path: Path | None = None) -> list[int]:
    """남은 단계를 차례로 실행. path가 있으면(기존 DB) 실행 전 DB 파일을 복사해 둔다. 실행한 단계 번호 목록."""
    cur = con.execute("PRAGMA user_version").fetchone()[0]
    if cur > LATEST:
        raise SystemExit(f"DB 구조 버전({cur})이 이 프로그램({LATEST})보다 새것입니다. 최신 PC 프로그램으로 여세요(자료 보호를 위해 멈춤).")
    todo = [m for m in MIGRATIONS if m[0] > cur]
    if not todo:
        return []
    if path is not None and path.exists() and con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]:
        pre_migration_backup(con, path, cur)
    done = []
    for n, _label, fn in todo:
        con.execute("BEGIN IMMEDIATE")
        try:
            fn(con)
            if _TEST_FAIL_STEP == n:
                raise RuntimeError(f"시험용 실패(단계 {n})")
            con.execute(f"PRAGMA user_version = {int(n)}")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        done.append(n)
    return done


# 접수 요약 열(수수료·수납 상태·현재 배정 팀·초성)을 바꿀 수 있는 쓰기를 모으는 임시(연결별) 트리거.
# 트랜잭션 끝(service.tx)에서 모인 접수만 다시 계산한다(겹친 ID는 그때 하나로; 고유 제약을 두지 않음 — UPSERT 안 트리거와 충돌하지 않게). 요약 열 자체를 바꾸는 갱신은 다시 모으지 않는다.
_DIRTY_SQL = [
    "CREATE TEMP TABLE IF NOT EXISTS dirty_req (id TEXT)",
    "CREATE TEMP TRIGGER IF NOT EXISTS dg_req_ins AFTER INSERT ON main.request BEGIN INSERT INTO dirty_req VALUES (NEW.id); END",
    "CREATE TEMP TRIGGER IF NOT EXISTS dg_req_upd AFTER UPDATE OF name, address, status, reduction_code, reduction_rate, reduction_round_unit ON main.request "
    "BEGIN INSERT INTO dirty_req VALUES (NEW.id); END",
]
for _t, _ops, _col in (("request_item", ("INSERT", "UPDATE", "DELETE"), "request_id"), ("item_link", ("INSERT", "UPDATE"), "request_id"),
                       ("payment", ("INSERT", "UPDATE"), "request_id"), ("active_assignment", ("INSERT", "UPDATE", "DELETE"), "request_id"),
                       ("result_event", ("INSERT", "UPDATE"), "request_id")):
    for _op in _ops:
        _row = "OLD" if _op == "DELETE" else "NEW"
        _DIRTY_SQL.append(f"CREATE TEMP TRIGGER IF NOT EXISTS dg_{_t}_{_op.lower()} AFTER {_op} ON main.{_t} WHEN {_row}.{_col} != '' "
                          f"BEGIN INSERT INTO dirty_req VALUES ({_row}.{_col}); END")
    if "UPDATE" in _ops and _t == "active_assignment":
        _DIRTY_SQL.append("CREATE TEMP TRIGGER IF NOT EXISTS dg_active_assignment_upd_old AFTER UPDATE ON main.active_assignment "
                          "BEGIN INSERT INTO dirty_req VALUES (OLD.request_id); END")
_DIRTY_SQL.append("CREATE TEMP TRIGGER IF NOT EXISTS dg_refund_paid AFTER UPDATE OF status ON main.refund_case WHEN NEW.status='PAID' OR OLD.status='PAID' "
                  "BEGIN INSERT INTO dirty_req VALUES (NEW.request_id); END")


def install_dirty_tracking(con):
    for st in _DIRTY_SQL:
        con.execute(st)
    con.dg_dirty_tracked = True


def _cols(con, table) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}


def migrate(con):
    """v0.10 이전 DB를 올린다(행정동·접수 경로·취소 코드·주소 자료의 동·팀장 역할·마감 회차). 여러 번 실행해도 같다."""
    for col, decl in (("dong", "TEXT"), ("channel", "TEXT NOT NULL DEFAULT '전화'"), ("cancel_code", "TEXT"), ("cancelled_at", "TEXT")):
        if col not in _cols(con, "request"):
            con.execute(f"ALTER TABLE request ADD COLUMN {col} {decl}")
    con.execute("CREATE INDEX IF NOT EXISTS ix_request_dong ON request(dong)")
    for col in ("dong_h", "dong_b"):  # 주소 자료의 관할 행정동·법정동(파일에 있으면)
        if col not in _cols(con, "addr_point"):
            con.execute(f"ALTER TABLE addr_point ADD COLUMN {col} TEXT")
    sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='app_user'").fetchone()[0]
    if "'LEAD'" not in sql:  # 역할 목록(CHECK)에 팀장(LEAD) 추가: 표를 새로 만들어 옮긴다
        cols = "id, username, display_name, role, pw_salt, pw_hash, active, must_change, created_at, last_login_at, crew_id"
        con.execute("SAVEPOINT app_user_v10")
        try:
            con.execute("""CREATE TABLE app_user_v10 (
              id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
              role TEXT NOT NULL CHECK (role IN ('ADMIN','INTAKE','FIELD','LEAD')), pw_salt TEXT NOT NULL, pw_hash TEXT NOT NULL,
              active INTEGER NOT NULL DEFAULT 1, must_change INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, last_login_at TEXT, crew_id TEXT)""")
            con.execute(f"INSERT INTO app_user_v10 ({cols}) SELECT {cols} FROM app_user")
            con.execute("DROP TABLE app_user")
            con.execute("ALTER TABLE app_user_v10 RENAME TO app_user")
            con.execute("RELEASE app_user_v10")
        except BaseException:
            con.execute("ROLLBACK TO app_user_v10")
            con.execute("RELEASE app_user_v10")
            raise
    migrate_v11(con)
    migrate_v12(con)
    migrate_v12_load(con)
    migrate_v12_note(con)
    migrate_v12_device(con)
    if not con.execute("SELECT 1 FROM day_close_log LIMIT 1").fetchone():  # 이전 마감 기록(날짜별 1행) → 1회차
        for wd, at, sj, note in con.execute("SELECT work_date, closed_at, summary_json, note FROM day_close").fetchall():
            con.execute("INSERT INTO day_close_log (id, work_date, seq, closed_at, closed_by, summary_json, note) VALUES (?,?,1,?,NULL,?,?)",
                        (secrets.token_hex(16), wd, at, sj, note))


def migrate_v11(con):
    """v0.11: 개인정보 파기·접속기록 해시 사슬·감면·부과번호·수납 출처·환불·완료일·주소 자료 지번/건물명·사진 촬영 정보. 여러 번 실행해도 같다."""
    for col, decl in (("reduction_code", "TEXT"), ("reduction_rate", "INTEGER"), ("reduction_label", "TEXT"), ("reduction_proof", "TEXT"),
                      ("reduction_checker", "TEXT"), ("reduction_at", "TEXT"), ("levy_no", "TEXT"), ("completed_at", "TEXT"), ("pii_purged_at", "TEXT")):
        if col not in _cols(con, "request"):
            con.execute(f"ALTER TABLE request ADD COLUMN {col} {decl}")
            if col == "completed_at":  # 이전 자료: 수거 완료·최종 결과 건은 마지막 변경 시각을 완료일로
                con.execute("UPDATE request SET completed_at=updated_at WHERE status IN ('DONE','PARTIAL','NOT_COLLECTED','ON_HOLD')")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_request_levy ON request(levy_no) WHERE levy_no IS NOT NULL")
    for col, decl in (("receipt_ref", "TEXT"), ("source", "TEXT NOT NULL DEFAULT '수기'")):
        if col not in _cols(con, "payment"):
            con.execute(f"ALTER TABLE payment ADD COLUMN {col} {decl}")
    for col, decl in (("prev_hash", "TEXT"), ("hash", "TEXT"), ("legacy", "INTEGER NOT NULL DEFAULT 0")):
        if col not in _cols(con, "audit_log"):
            con.execute(f"ALTER TABLE audit_log ADD COLUMN {col} {decl}")
            if col == "legacy":  # 해시 사슬 이전에 쌓인 기록은 '이전 기록'으로 표시(검사 대상 아님)
                con.execute("UPDATE audit_log SET legacy=1 WHERE hash IS NULL")
    for col, decl in (("bname", "TEXT"), ("bname_cho", "TEXT"), ("jibun_main", "INTEGER"), ("jibun_sub", "INTEGER")):
        if col not in _cols(con, "addr_point"):
            con.execute(f"ALTER TABLE addr_point ADD COLUMN {col} {decl}")
    for col, decl in (("taken_at", "TEXT"), ("lat", "REAL"), ("lon", "REAL"), ("acc", "REAL")):
        if col not in _cols(con, "photo"):
            con.execute(f"ALTER TABLE photo ADD COLUMN {col} {decl}")
    if "code" not in _cols(con, "dong"):
        con.execute("ALTER TABLE dong ADD COLUMN code TEXT")
    if "prefs_json" not in _cols(con, "app_user"):  # 사용자별 화면 설정(테마·글자 크기)
        con.execute("ALTER TABLE app_user ADD COLUMN prefs_json TEXT")
    # 부과번호가 없는 접수에 접수일 순서로 부과번호를 붙인다
    todo = con.execute("""SELECT r.id, r.received_at, d.code FROM request r LEFT JOIN dong d ON d.name=r.dong
        WHERE r.levy_no IS NULL ORDER BY r.received_at, r.receipt_no""").fetchall()
    for rid, at, code in todo:
        con.execute("UPDATE request SET levy_no=? WHERE id=?", (next_levy_no(con, levy_year(at), code), rid))


def migrate_v12(con):
    """v0.12: 휴대폰(PC 연결 방식)에서 현장 접수한 건의 출처 기기와 사무실 확인 표시. 여러 번 실행해도 같다."""
    for col, decl in (("origin_device", "TEXT"), ("field_checked_at", "TEXT"), ("field_checked_by", "TEXT")):
        if col not in _cols(con, "request"):
            con.execute(f"ALTER TABLE request ADD COLUMN {col} {decl}")


def migrate_v12_device(con):
    """v0.12.17: 기기 등록 뒤 지문 대조를 확인해야 사용(confirmed_at). 이미 쓰던 기기는 확인된 것으로 둔다."""
    if "confirmed_at" not in _cols(con, "device"):
        con.execute("ALTER TABLE device ADD COLUMN confirmed_at TEXT")
        con.execute("UPDATE device SET confirmed_at=registered_at")


def migrate_v12_note(con):
    """v0.12.17: 변경 이력의 자유 글(사유)을 따로 보관 — 이름·번호가 적혀도 보유기간 파기 때 지울 수 있게. 해시 사슬에는 글의 해시만 들어감."""
    con.execute("CREATE TABLE IF NOT EXISTS audit_note (audit_id INTEGER PRIMARY KEY, request_id TEXT, note TEXT, note_sha TEXT NOT NULL)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_audit_note_req ON audit_note(request_id)")


def migrate_v12_load(con):
    """v0.12.15: 차량 최대 적재 중량(kg, 0 = 따지지 않음). 여러 번 실행해도 같다."""
    if "capacity_kg" not in _cols(con, "crew"):
        con.execute("ALTER TABLE crew ADD COLUMN capacity_kg REAL NOT NULL DEFAULT 0")


def levy_year(received_at: str | None) -> str:
    import datetime as _dt
    try:
        return (_dt.datetime.fromisoformat((received_at or "").replace("Z", "+00:00")) + _dt.timedelta(hours=9)).strftime("%Y")
    except ValueError:
        return _dt.date.today().strftime("%Y")


def next_levy_no(con, year: str, dong_code: str | None) -> str:
    """부과번호: 연도-동코드-일련번호(5자리). 동코드가 없으면 00. 연도·동코드마다 1부터."""
    code = (dong_code or "00").strip() or "00"
    prefix = f"{year}-{code}-"
    row = con.execute("SELECT MAX(CAST(substr(levy_no, ?) AS INTEGER)) FROM request WHERE levy_no LIKE ?", (len(prefix) + 1, prefix + "%")).fetchone()
    return f"{prefix}{(row[0] or 0) + 1:05d}"


def _quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


# ------------------------------------------------------------------ 백업·복원
BACKUP_MAGIC_TABLE = "dg_backup_info"


def backup_to(con, dest: Path, passphrase: str) -> Path:
    """현재 DB를 별도 백업 비밀번호로 암호화한 새 파일로 내보낸다(DB 키와 다른 비밀번호).

    백업 파일은 SQLCipher 형식이라 백업 비밀번호 없이는 열리지 않는다.
    """
    if len(passphrase or "") < 12:
        raise ValueError("백업 비밀번호는 12자 이상으로 정해 주세요.")
    dest = Path(dest)
    if dest.exists():
        raise ValueError("같은 이름의 백업 파일이 이미 있습니다.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    con.execute("PRAGMA wal_checkpoint(FULL)")
    # 임시 이름으로 다 쓴 뒤에만 정식 이름으로 바꾼다: 도중에 실패한 반쪽 파일이 백업 목록·보관 정리에 끼지 않게
    part = part_path(dest)
    try:
        if ENCRYPTED:
            con.execute(f"ATTACH DATABASE {_quote(str(part))} AS bk KEY {_quote(passphrase)}")
            try:
                con.execute("SELECT sqlcipher_export('bk')")
            finally:
                con.execute("DETACH DATABASE bk")
        else:  # 개발용 평문 모드
            out = dbapi.connect(str(part))
            con.backup(out)
            out.close()
        try:
            os.chmod(part, 0o600)
        except OSError:
            pass
        os.replace(part, dest)
    finally:
        discard(part)
    return dest


def part_path(dest: Path) -> Path:
    """쓰는 중인 파일의 임시 이름(같은 폴더, 점으로 시작 → 백업 목록·보관 정리의 이름 형식에 걸리지 않음)."""
    dest = Path(dest)
    return dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.part")


def discard(p: Path):
    """임시 파일과 SQLite 곁 파일(-wal·-shm·-journal)을 조용히 지운다."""
    for f in (Path(p), *(Path(str(p) + ext) for ext in ("-wal", "-shm", "-journal"))):
        try:
            f.unlink()
        except OSError:  # 없거나(정상) 지울 수 없음(다음 정리 때 다시)
            pass


def open_backup(src: Path, passphrase: str):
    """백업 파일을 읽기용으로 열고 무결성을 확인한다."""
    src = Path(src)
    if not src.exists():
        raise ValueError("백업 파일이 없습니다.")
    bcon = dbapi.connect(str(src), check_same_thread=False, isolation_level=None)
    if ENCRYPTED:
        bcon.execute("PRAGMA key = " + _quote(passphrase))
    try:
        ok = bcon.execute("PRAGMA integrity_check").fetchone()[0]
        n = bcon.execute("SELECT count(*) FROM request").fetchone()[0]
    except Exception:
        bcon.close()
        raise ValueError("백업 파일을 열 수 없습니다. 백업 비밀번호를 확인하세요.")
    if ok != "ok":
        bcon.close()
        raise ValueError("백업 파일이 손상되었습니다.")
    return bcon, n


def restore_from(src: Path, passphrase: str) -> dict:
    """백업 파일을 현재 DB 키로 다시 암호화해 업무 DB로 되돌린다. 기존 DB는 지우지 않고 옆에 보관한다.

    PC 업무 화면(serve)을 끈 상태에서만 실행한다.
    """
    import datetime as _dt
    bcon, n = open_backup(src, passphrase)
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    cur = d / "pc.db"
    tmp = d / "pc.db.restoring"
    for f in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        if f.exists():
            f.unlink()
    key = db_key(create=not cur.exists()) if ENCRYPTED else None
    if ENCRYPTED:
        bcon.execute(f"ATTACH DATABASE {_quote(str(tmp))} AS cur KEY {_quote(key)}")
        bcon.execute("SELECT sqlcipher_export('cur')")
        bcon.execute("DETACH DATABASE cur")
    else:
        out = dbapi.connect(str(tmp))
        bcon.backup(out)
        out.close()
    bcon.close()
    kept = None
    if cur.exists():
        stamp = _dt.datetime.now().strftime('%Y%m%d-%H%M%S')
        kept, k = d / f"pc.db.before-restore-{stamp}", 1
        while kept.exists():  # 같은 초에 두 번 복원(보관 정리는 '-n' 이름도 같은 사본으로 본다)
            kept, k = d / f"pc.db.before-restore-{stamp}-{k}", k + 1
        cur.rename(kept)
        for ext in ("-wal", "-shm"):
            side = Path(str(cur) + ext)
            if side.exists():
                side.rename(Path(str(kept) + ext))
    tmp.rename(cur)
    try:
        os.chmod(cur, 0o600)
    except OSError:
        pass
    return {"requests": n, "kept": str(kept) if kept else None}
