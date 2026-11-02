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
import os
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
CREATE TABLE IF NOT EXISTS day_close (work_date TEXT PRIMARY KEY, closed_at TEXT NOT NULL, summary_json TEXT NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL, entity_id TEXT NOT NULL, action TEXT NOT NULL,
  detail TEXT, at TEXT NOT NULL);
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
        keyring.set_password(KEYRING_SERVICE, "db-key", value)
        return keyring.get_password(KEYRING_SERVICE, "db-key") == value
    except Exception:
        return False


def db_key(create: bool = False) -> str:
    k = os.environ.get("DG_DB_KEY") or _keyring_get()
    if k:
        return k
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


def connect(create: bool = False):
    if not ENCRYPTED and os.environ.get("DG_ALLOW_PLAIN_DB") != "1":
        sys.exit("SQLCipher 모듈(sqlcipher3)이 없어 실행을 멈춥니다. 'pip install sqlcipher3-binary' 후 다시 실행하세요.")
    d = state_dir()
    path = d / "pc.db"
    if not path.exists() and not create:
        sys.exit("PC 자료가 없습니다. 먼저 'python pc_tool.py init'을 실행하세요.")
    d.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    con = dbapi.connect(str(path), check_same_thread=False, isolation_level=None)
    if ENCRYPTED:
        key = db_key(create=is_new)
        con.execute("PRAGMA key = " + _quote(key))
        try:
            con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except Exception:
            sys.exit("DB를 열 수 없습니다. DB 키(비밀번호)가 맞는지 확인하세요.")
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return con


def _quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"
