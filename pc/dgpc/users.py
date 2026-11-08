"""사용자·역할·로그인(scrypt)·비밀번호 규칙·화면 설정(테마·글자 크기).
"""
from __future__ import annotations

import hashlib as _hl
import hmac as _hmac
import json
import re
import secrets as _secrets
import uuid

from dgpc.core import UserError, audit, get_setting, now, one, rows, tx

ROLES = {"ADMIN": "운영 관리자", "INTAKE": "접수 담당자", "FIELD": "수거팀", "LEAD": "팀장(검토자)", "DONG": "동 담당자"}


def _pw_hash(pw: str, salt: bytes) -> str:
    return _hl.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1).hex()


def _check_pw_rules(pw: str):
    if len(pw or "") < 10:
        raise UserError("비밀번호는 10자 이상으로 정해 주세요.")


def users_exist(con) -> bool:
    return bool(one(con, "SELECT 1 AS x FROM app_user LIMIT 1"))


def migrate_legacy_login(con):
    """v0.4 이하의 단일 업무 비밀번호를 관리자 계정(admin)으로 옮긴다."""
    old = get_setting(con, "login")
    if old and not users_exist(con):
        with tx(con):
            con.execute("INSERT INTO app_user (id, username, display_name, role, pw_salt, pw_hash, created_at) VALUES (?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), "admin", "관리자", "ADMIN", old["salt"], old["hash"], now()))
            con.execute("DELETE FROM meta WHERE key='login'")


def create_user(con, username: str, display_name: str, role: str, password: str, must_change: bool = False, crew_id: str | None = None,
                dong_ids: list[str] | None = None) -> str:
    username = (username or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{3,30}", username):
        raise UserError("아이디는 영문 소문자·숫자·.-_ 3~30자로 정해 주세요.")
    if role not in ROLES:
        raise UserError("역할을 골라 주세요.")
    if not (display_name or "").strip():
        raise UserError("이름을 적어 주세요.")
    _check_pw_rules(password)
    if one(con, "SELECT 1 AS x FROM app_user WHERE username=?", (username,)):
        raise UserError("이미 있는 아이디입니다.")
    if crew_id and not one(con, "SELECT 1 AS x FROM crew WHERE id=?", (crew_id,)):
        raise UserError("없는 수거팀입니다.")
    dongs = _validated_dongs(con, dong_ids, required=True) if role == "DONG" else []
    salt = _secrets.token_bytes(16)
    uid = str(uuid.uuid4())
    with tx(con):
        con.execute("INSERT INTO app_user (id, username, display_name, role, pw_salt, pw_hash, must_change, created_at, crew_id) VALUES (?,?,?,?,?,?,?,?,?)",
                    (uid, username, display_name.strip(), role, salt.hex(), _pw_hash(password, salt), 1 if must_change else 0, now(),
                     crew_id if role == "FIELD" else None))
        con.executemany("INSERT INTO app_user_dong (user_id,dong_id) VALUES (?,?)", [(uid, did) for did in dongs])
        audit(con, "user", uid, "CREATE", {"username": username, "role": role, **({"dong_ids": dongs} if role == "DONG" else {})})
    return uid


def _login_user(u: dict) -> dict:
    return {"id": u["id"], "username": u["username"], "display_name": u["display_name"], "role": u["role"], "must_change": bool(u["must_change"]),
            "crew_id": u["crew_id"]}


def _login_credential(u: dict) -> str:
    return _hl.sha256((u["pw_salt"] + u["pw_hash"]).encode("ascii")).hexdigest()


def verify_login(con, username: str, password: str, *, evidence: dict | None = None) -> dict | None:
    """비밀번호 확인만(쓰기 없음 — 서버는 DB 잠금 밖·조회 전용 연결에서 scrypt를 돌린다)."""
    u = one(con, "SELECT * FROM app_user WHERE username=? AND active=1", ((username or "").strip().lower(),))
    if not u:
        _pw_hash(password or "", b"0" * 16)  # 아이디 유무에 따른 응답 시간 차이를 줄인다
        return None
    if not _hmac.compare_digest(_pw_hash(password or "", bytes.fromhex(u["pw_salt"])), u["pw_hash"]):
        return None
    if evidence is not None:  # 서버 내부에서만 전달한다. 사용자 정보·세션·API에는 넣지 않는다.
        evidence["credential"] = _login_credential(u)
    return _login_user(u)


def current_login(con, uid: str, credential: str) -> dict | None:
    """비싼 암호 검증 뒤 잠금 안에서 현재 계정·자격을 확인하고 최신 접근 범위만 돌려준다."""
    u = one(con, "SELECT * FROM app_user WHERE id=? AND active=1", (uid,))
    if not u or not _hmac.compare_digest(_login_credential(u), credential):
        return None
    return _login_user(u)


def check_login(con, username: str, password: str) -> dict | None:
    u = verify_login(con, username, password)
    if not u:
        return None
    con.execute("UPDATE app_user SET last_login_at=? WHERE id=?", (now(), u["id"]))
    u = one(con, "SELECT * FROM app_user WHERE id=?", (u["id"],))
    return _login_user(u)


THEMES = ("auto", "light", "dark")
FONT_SIZES = (14, 15, 16, 17)  # v0.12.16: 기본 15(보통). 예전에 고른 13은 14로


def user_prefs(con, uid: str) -> dict:
    r = one(con, "SELECT prefs_json FROM app_user WHERE id=?", (uid,))
    p = json.loads(r["prefs_json"]) if r and r["prefs_json"] else {}
    return {"theme": p.get("theme") if p.get("theme") in THEMES else "auto", "font": p.get("font") if p.get("font") in FONT_SIZES else (14 if p.get("font") == 13 else 15)}


def save_prefs(con, uid: str, data: dict) -> dict:
    """사용자별 화면 설정(테마: 자동·밝게·어둡게, 글자 크기 13~16px). 서버(DB)에 저장해 어느 PC에서나 같게."""
    cur = user_prefs(con, uid)
    if "theme" in data:
        if data["theme"] not in THEMES:
            raise UserError("테마 값이 올바르지 않습니다.")
        cur["theme"] = data["theme"]
    if "font" in data:
        try:
            f = int(data["font"])
        except (TypeError, ValueError):
            f = 0
        if f not in FONT_SIZES:
            raise UserError("글자 크기 값이 올바르지 않습니다.")
        cur["font"] = f
    with tx(con):
        con.execute("UPDATE app_user SET prefs_json=? WHERE id=?", (json.dumps(cur), uid))
    return cur


def list_users(con) -> list[dict]:
    out = rows(con, """SELECT u.id, u.username, u.display_name, u.role, u.active, u.must_change, u.created_at, u.last_login_at, u.crew_id, c.name AS crew_name
        FROM app_user u LEFT JOIN crew c ON c.id=u.crew_id ORDER BY u.created_at""")
    mappings = {}
    for link in rows(con, "SELECT user_id,dong_id FROM app_user_dong ORDER BY dong_id"):
        mappings.setdefault(link["user_id"], []).append(link["dong_id"])
    for u in out:
        u["dong_ids"] = mappings.get(u["id"], [])
    return out


def set_user_active(con, uid: str, active: bool, me: str):
    if uid == me and not active:
        raise UserError("자기 계정은 중지할 수 없습니다.")
    u = one(con, "SELECT role FROM app_user WHERE id=?", (uid,))
    if not u:
        raise UserError("사용자를 찾을 수 없습니다.")
    if not active and u["role"] == "ADMIN" and one(con, "SELECT COUNT(*) AS n FROM app_user WHERE role='ADMIN' AND active=1")["n"] <= 1:
        raise UserError("마지막 운영 관리자는 중지할 수 없습니다.")
    if active and u["role"] == "DONG" and not user_dong_ids(con, uid):
        raise UserError("동 담당자는 한 개 이상의 담당 동을 지정한 뒤 사용하세요.")
    with tx(con):
        con.execute("UPDATE app_user SET active=? WHERE id=?", (1 if active else 0, uid))
        audit(con, "user", uid, "ACTIVE" if active else "DEACTIVATE", None)


def set_password(con, uid: str, password: str, must_change: bool = False, old: str | None = None):
    _check_pw_rules(password)
    u = one(con, "SELECT * FROM app_user WHERE id=?", (uid,))
    if not u:
        raise UserError("사용자를 찾을 수 없습니다.")
    if old is not None and not _hmac.compare_digest(_pw_hash(old, bytes.fromhex(u["pw_salt"])), u["pw_hash"]):
        raise UserError("현재 비밀번호가 맞지 않습니다.")
    salt = _secrets.token_bytes(16)
    with tx(con):
        con.execute("UPDATE app_user SET pw_salt=?, pw_hash=?, must_change=? WHERE id=?", (salt.hex(), _pw_hash(password, salt), 1 if must_change else 0, uid))
        audit(con, "user", uid, "PASSWORD", {"reset_by_admin": must_change})



# ------------------------------------------------------------------ 수거팀 계정의 접근 범위 (자기 팀 배정만)
def set_user_crew(con, uid: str, crew_id: str | None):
    u = one(con, "SELECT role FROM app_user WHERE id=?", (uid,))
    if not u:
        raise UserError("사용자를 찾을 수 없습니다.")
    if u["role"] != "FIELD":
        raise UserError("수거팀 계정만 팀을 연결합니다.")
    if crew_id and not one(con, "SELECT 1 AS x FROM crew WHERE id=?", (crew_id,)):
        raise UserError("없는 수거팀입니다.")
    with tx(con):
        con.execute("UPDATE app_user SET crew_id=? WHERE id=?", (crew_id or None, uid))
        audit(con, "user", uid, "CREW", {"crew_id": crew_id})


def user_dong_ids(con, uid: str) -> list[str]:
    return [r["dong_id"] for r in rows(con, "SELECT dong_id FROM app_user_dong WHERE user_id=? ORDER BY dong_id", (uid,))]


def _validated_dongs(con, dong_ids, *, required: bool) -> list[str]:
    if dong_ids is None:
        dong_ids = []
    if not isinstance(dong_ids, list) or any(not isinstance(did, str) for did in dong_ids):
        raise UserError("담당 동을 목록에서 골라 주세요.")
    ids = sorted(set(dong_ids))
    if required and not ids:
        raise UserError("동 담당자는 한 개 이상의 담당 동을 지정하세요.")
    for did in ids:
        if not one(con, "SELECT 1 AS x FROM dong WHERE id=?", (did,)):
            raise UserError("등록된 행정동을 골라 주세요.")
    return ids


def set_user_dongs(con, uid: str, dong_ids):
    u = one(con, "SELECT role,active FROM app_user WHERE id=?", (uid,))
    if not u or u["role"] != "DONG":
        raise UserError("동 담당자 계정의 담당 동만 변경할 수 있습니다.")
    ids = _validated_dongs(con, dong_ids, required=bool(u["active"]))
    with tx(con):
        old = user_dong_ids(con, uid)
        con.execute("DELETE FROM app_user_dong WHERE user_id=?", (uid,))
        con.executemany("INSERT INTO app_user_dong (user_id,dong_id) VALUES (?,?)", [(uid, did) for did in ids])
        audit(con, "user", uid, "DONG_SCOPE", {"from_dong_ids": old, "dong_ids": ids})
