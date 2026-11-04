"""수납: 납부·환불 기록과 취소(마감한 날은 사유 필수).
"""
from __future__ import annotations

import re
import uuid

from dgpc import settings as SR
from dgpc.core import UserError, _after_close, _date_ok, audit, now, one, today_kst, tx
from dgpc.derive import payment_status

# ------------------------------------------------------------------ 수납 (담당자 확인 기록) — 계산 금액·납부 금액·상태를 분리
PAY_METHODS = ("현장 카드", "현장 현금", "가상계좌", "무통장 입금", "스티커 구매", "기타")
LEGACY_PAY_METHODS = ("현장 납부",)  # 이전 버전 기록·시험용 (화면에는 내지 않음)
REFUND_PAY_METHODS = ("계좌 이체", "현금", "카드 취소", "기타")


def _won(v, what: str = "금액") -> int:
    """화면에서 받은 금액(숫자·'12,000'·'12000원') → 정수. 숫자가 아니면 업무 오류."""
    if isinstance(v, bool):
        raise UserError(f"{what}을 숫자로 적어 주세요.")
    if isinstance(v, (int, float)):
        if isinstance(v, float) and not v.is_integer():
            raise UserError(f"{what}은 원 단위 정수로 적어 주세요.")
        return int(v)
    t = re.sub(r"[,\s]|원$", "", str(v or ""))
    if not re.fullmatch(r"-?\d{1,12}", t):
        raise UserError(f"{what}을 숫자로 적어 주세요.")
    return int(t)


def add_payment(con, rid: str, data: dict) -> dict:
    if not one(con, "SELECT 1 AS x FROM request WHERE id=?", (rid,)):
        raise UserError("접수를 찾을 수 없습니다.")
    flag = _after_close(con, rid)
    why = (data.get("reason") or "").strip()
    if flag and not why:
        raise UserError(f"마감한 날({flag['after_close']})의 접수입니다. 수납 기록 사유를 적어 주세요.")
    kind = data.get("kind", "PAY")
    amount = _won(data.get("amount") or 0)
    method = data.get("method") or ""
    paid_on = (data.get("paid_on") or today_kst()).strip() if isinstance(data.get("paid_on") or "", str) else ""
    if kind == "REFUND":  # 환불은 결재선(신청 → 다른 사람 결재 → 지급 기록)으로만
        raise UserError("환불은 [환불] 화면에서 신청·결재한 뒤 지급 기록으로 남깁니다. 여기서는 납부만 기록합니다.")
    if kind != "PAY" or amount <= 0:
        raise UserError("금액을 1원 이상으로 적어 주세요.")
    if method not in PAY_METHODS + LEGACY_PAY_METHODS + (REFUND_PAY_METHODS if kind == "REFUND" else ()):
        raise UserError("납부 방법을 골라 주세요.")
    ref = str(data.get("receipt_ref") or "").strip()[:40] or None
    if not _date_ok(paid_on):
        raise UserError("날짜 형식이 올바르지 않습니다.")
    pid = str(uuid.uuid4())
    with tx(con):
        con.execute("INSERT INTO payment (id, request_id, kind, amount, method, paid_on, note, recorded_at, receipt_ref, source) VALUES (?,?,?,?,?,?,?,?,?,'수기')",
                    (pid, rid, kind, amount, method, paid_on, (data.get("note") or "").strip()[:100], now(), ref))
        audit(con, "payment", pid, kind, {"request": rid, "amount": amount, "method": method, **({"ref": ref} if ref else {}), **({"reason": why[:200]} if why else {}), **flag})
    return payment_status(con, rid)


def void_payment(con, pid: str, reason: str) -> dict:
    p = one(con, "SELECT * FROM payment WHERE id=?", (pid,))
    if not p or p["void_reason"]:
        raise UserError("정정할 수납 기록이 없습니다.")
    if not (reason or "").strip():
        raise UserError("정정 사유를 적어 주세요.")
    with tx(con):
        con.execute("UPDATE payment SET void_reason=?, voided_at=? WHERE id=?", (reason.strip(), now(), pid))
        audit(con, "payment", pid, "VOID", {"request": p["request_id"], "reason": reason.strip(), **_after_close(con, p["request_id"])})
    return payment_status(con, p["request_id"])


@SR.contribute("pay_methods")
def _settings_part(con) -> dict:
    return {"pay_methods": list(PAY_METHODS), "refund_methods": list(REFUND_PAY_METHODS)}
