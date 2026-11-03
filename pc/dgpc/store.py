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
  role TEXT NOT NULL CHECK (role IN ('ADMIN','INTAKE','FIELD')), pw_salt TEXT NOT NULL, pw_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1, must_change INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, last_login_at TEXT);
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
    cols = {r[1] for r in con.execute("PRAGMA table_info(audit_log)").fetchall()}
    if "actor" not in cols:  # 이전 버전 DB에 작업자 열 추가
        con.execute("ALTER TABLE audit_log ADD COLUMN actor TEXT")
    if "line_id" not in {r[1] for r in con.execute("PRAGMA table_info(photo)").fetchall()}:  # 사진을 품목 행에 묶기(선택)
        con.execute("ALTER TABLE photo ADD COLUMN line_id TEXT")
    if "crew_id" not in {r[1] for r in con.execute("PRAGMA table_info(app_user)").fetchall()}:  # 수거팀 계정 ↔ 팀
        con.execute("ALTER TABLE app_user ADD COLUMN crew_id TEXT")
    if "purged_at" not in {r[1] for r in con.execute("PRAGMA table_info(photo)").fetchall()}:
        con.execute("ALTER TABLE photo ADD COLUMN purged_at TEXT")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return con


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
    if ENCRYPTED:
        con.execute(f"ATTACH DATABASE {_quote(str(dest))} AS bk KEY {_quote(passphrase)}")
        try:
            con.execute("SELECT sqlcipher_export('bk')")
        finally:
            con.execute("DETACH DATABASE bk")
    else:  # 개발용 평문 모드
        out = dbapi.connect(str(dest))
        con.backup(out)
        out.close()
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return dest


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
        kept = d / f"pc.db.before-restore-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
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
