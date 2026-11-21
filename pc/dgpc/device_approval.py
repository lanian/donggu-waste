"""새 수거폰 승인을 위한 관리자 계정별 오프라인 2단계 인증."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import re
import secrets
from time import time
from urllib.parse import quote, urlencode

from device_pairing import totp_number
from dgpc.core import UserError, audit, one, tx
from dgpc.devices import confirm_device
from dgpc.users import verify_login

ISSUER = "동구 수거"
SETUP_SECONDS = LOCK_SECONDS = 5 * 60
MAX_FAILURES = 5
_LOCKED = "인증을 여러 번 실패했습니다. 5분 뒤 다시 시도하세요."


class _BadProof(Exception):
    """계정에 실패 횟수를 기록할 잘못된 자격 증명."""


def _admin(con, uid):
    user = one(con, "SELECT username, pw_hash FROM app_user WHERE id=? AND active=1 AND role='ADMIN' AND must_change=0", (uid,))
    if not user:
        raise UserError("업무 비밀번호를 설정한 운영 관리자만 새 휴대폰을 승인할 수 있습니다.")
    return user


def status(con, uid):
    _admin(con, uid)
    row = one(con, "SELECT 1 AS configured FROM admin_device_factor WHERE user_id=? AND secret IS NOT NULL", (uid,))
    return {"configured": bool(row)}


@contextmanager
def _attempt(con, uid):
    # 실패 횟수는 커밋하고, 예상치 못한 오류는 기기 승인과 OTP 소비를 함께 되돌린다.
    if con.in_transaction:
        raise RuntimeError("기기 승인 인증은 독립된 쓰기 트랜잭션에서 시작해야 합니다.")
    invalid = None
    with tx(con):
        user = _admin(con, uid)
        now = int(time())
        con.execute("INSERT OR IGNORE INTO admin_device_factor(user_id) VALUES (?)", (uid,))
        factor = one(con, "SELECT * FROM admin_device_factor WHERE user_id=?", (uid,))
        try:
            yield user, factor, now
        except _BadProof as exc:
            failures = factor["failures"] + 1
            blocked_until = 0
            invalid = str(exc)
            if failures >= MAX_FAILURES:
                failures, blocked_until, invalid = 0, now + LOCK_SECONDS, _LOCKED
                audit(con, "user", uid, "DEVICE_APPROVAL_THROTTLED")
            con.execute("UPDATE admin_device_factor SET failures=?, blocked_until=? WHERE user_id=?", (failures, blocked_until, uid))
    if invalid is not None:
        raise UserError(invalid)


def _check_throttle(factor, now):
    if factor["blocked_until"] > now:
        raise UserError(_LOCKED)


def _password(con, user, password):
    if not isinstance(password, str) or not password or not verify_login(con, user["username"], password):
        raise _BadProof("현재 관리자 비밀번호를 확인하세요.")


def _owner_binding(user, owner):
    if not isinstance(owner, str) or not owner:
        raise UserError("관리자로 다시 로그인해 주세요.")
    # 비밀번호 변경·세션 변경 시 발급된 설정 QR의 확인도 중단한다.
    return hmac.digest(bytes.fromhex(user["pw_hash"]), owner.encode(), "sha256")


def _match_step(key, code, now, last_step):
    if isinstance(code, str) and re.fullmatch("[0-9]{6}", code):
        current = now // 30
        for slot in (current, current - 1, current + 1):
            if slot >= 0 and hmac.compare_digest(totp_number(key, slot), code):
                if slot <= last_step:
                    raise _BadProof("이미 사용한 관리자 인증번호입니다. 인증 앱에 다음 번호가 표시되면 다시 입력하세요.")
                return slot
    raise _BadProof("인증 앱에 표시된 관리자 인증번호 6자리를 확인하세요.")


def _recovery_hash(uid, code):
    return hashlib.sha256((uid + ":" + code).encode("ascii")).hexdigest()


def _rotation_value(code):
    value = code.strip().upper() if isinstance(code, str) else ""
    if not re.fullmatch(r"[0-9]{6}|[A-F0-9]{5}(?:-[A-F0-9]{5}){3}", value):
        raise _BadProof("현재 인증 앱의 6자리 번호 또는 사용하지 않은 복구 코드를 입력하세요.")
    return value


def _prove_rotation(con, factor, value, now):
    uid = factor["user_id"]
    if len(value) == 6:
        step = _match_step(factor["secret"], value, now, factor["last_step"])
        con.execute("UPDATE admin_device_factor SET last_step=? WHERE user_id=?", (step, uid))
        return
    hashes = json.loads(factor["recovery_hashes"])
    digest = _recovery_hash(uid, value)
    for saved in hashes:
        if hmac.compare_digest(digest, saved):
            hashes.remove(saved)
            con.execute("UPDATE admin_device_factor SET recovery_hashes=? WHERE user_id=?", (json.dumps(hashes), uid))
            audit(con, "user", uid, "DEVICE_APPROVAL_RECOVERY_USED")
            return
    raise _BadProof("사용하지 않은 관리자 복구 코드를 입력하세요.")


def start_setup(con, uid, owner, password, recovery_code=None):
    """현재 비밀번호를 확인하고 이 세션의 한시적인 OTP 등록 정보를 발급한다."""
    with _attempt(con, uid) as (user, factor, now):
        _check_throttle(factor, now)
        _password(con, user, password)
        binding = _owner_binding(user, owner)
        pending = factor["pending_until"] > now and hmac.compare_digest(factor["pending_owner"], binding)
        key = factor["pending_secret"] if pending else secrets.token_bytes(20)
        proof = None
        if factor["secret"] is not None:
            value = _rotation_value(recovery_code)
            proof = hmac.digest(key, binding + value.encode("ascii"), "sha256")
            # 같은 세션에서 응답을 잃어 원래 증명으로 QR을 다시 열 때 유효기간과 실패 예산을 그대로 둔다.
            if not pending or not hmac.compare_digest(factor["pending_proof"], proof):
                _prove_rotation(con, factor, value, now)
                con.execute("UPDATE admin_device_factor SET failures=0, blocked_until=0 WHERE user_id=?", (uid,))
        deadline = factor["pending_until"] if pending else now + SETUP_SECONDS
        con.execute("UPDATE admin_device_factor SET pending_secret=?, pending_owner=?, pending_proof=?, pending_until=? WHERE user_id=?",
                    (key, binding, proof, deadline, uid))
        secret = base64.b32encode(key).decode("ascii")
        label = quote(ISSUER, safe="") + ":" + quote(user["username"], safe="")
        params = urlencode({"secret": secret, "issuer": ISSUER, "algorithm": "SHA1", "digits": 6, "period": 30})
    return {"uri": f"otpauth://totp/{label}?{params}"}


def finish_setup(con, uid, owner, otp):
    """새 인증 앱이 실제로 코드를 생성한 뒤에만 키를 활성화하고 복구 코드를 한 번 반환한다."""
    with _attempt(con, uid) as (user, factor, now):
        _check_throttle(factor, now)
        binding = _owner_binding(user, owner)
        if (factor["pending_until"] <= now or not factor["pending_secret"]
                or not hmac.compare_digest(factor["pending_owner"], binding)):
            raise UserError("인증 앱 연결 정보가 만료되었습니다. 현재 세션에서 설정 QR을 다시 여세요.")
        step = _match_step(factor["pending_secret"], otp, now, -1)
        recovery_codes = []
        for _ in range(6):
            raw = secrets.token_hex(10).upper()
            recovery_codes.append("-".join(raw[i:i + 5] for i in range(0, 20, 5)))
        hashes = json.dumps([_recovery_hash(uid, code) for code in recovery_codes])
        con.execute("""UPDATE admin_device_factor SET secret=pending_secret, last_step=?, recovery_hashes=?,
                       pending_secret=NULL, pending_owner=NULL, pending_proof=NULL, pending_until=0, failures=0, blocked_until=0 WHERE user_id=?""",
                    (step, hashes, uid))
        audit(con, "user", uid, "DEVICE_APPROVAL_ROTATE" if factor["secret"] else "DEVICE_APPROVAL_ENROLL")
    return {"recovery_codes": recovery_codes}


def approve_device(con, uid, dev_id, password, otp):
    """재요청은 완료 상태를 반환하고 새 휴대폰으로 처음 확정할 때만 2단계 인증한다."""
    with _attempt(con, uid) as (user, factor, now):
        dev = one(con, "SELECT status, confirmed_at FROM device WHERE id=?", (dev_id,))
        if not dev or dev["status"] != "ACTIVE":
            raise UserError("확인할 기기가 없습니다.")
        if dev["confirmed_at"]:
            return {"ok": True}
        _check_throttle(factor, now)
        if not factor["secret"]:
            raise UserError("설정 → 기기에서 관리자 인증 앱을 먼저 연결하세요.")
        _password(con, user, password)
        step = _match_step(factor["secret"], otp, now, factor["last_step"])
        confirm_device(con, dev_id)
        con.execute("UPDATE admin_device_factor SET last_step=?, failures=0, blocked_until=0 WHERE user_id=?", (step, uid))
        audit(con, "user", uid, "DEVICE_APPROVAL_AUTHORIZE")
    return {"ok": True}
