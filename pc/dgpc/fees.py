"""수수료표: 원문 표(data/fee_table.json) + 승인된 개정(fee_revision) · 품목 부피 보정 · 자주 쓴 품목.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid

from dgpc import settings as SR
from dgpc.core import ACTOR, ROOT, UserError, audit, get_setting, now, one, rows, set_setting, today_kst, tx

FEE_TABLE = json.loads((ROOT / "data" / "fee_table.json").read_text(encoding="utf8"))
FEE_ROWS = {r["id"]: r for r in FEE_TABLE["rows"]}


def fee_usage(con, limit: int = 10) -> list[str]:
    """접수에 많이 쓰인 품목(PC 화면의 자주 쓰는 품목)."""
    return [r["fee_rate_id"] for r in rows(con, """SELECT fee_rate_id, COUNT(*) AS n FROM request_item WHERE fee_rate_id IS NOT NULL AND removed=0
        GROUP BY fee_rate_id ORDER BY n DESC LIMIT ?""", (limit,))]


def volume_overrides(con) -> dict:
    return get_setting(con, "volume_overrides")


def save_volume_override(con, fee_rate_id: str, m3: float | None):
    if fee_rate_id not in FEE_ROWS:
        raise UserError("수수료표에 없는 품목입니다.")
    v = volume_overrides(con)
    if m3 in (None, ""):
        v.pop(fee_rate_id, None)
    else:
        m = float(m3)
        if not 0 < m <= 20:
            raise UserError("부피는 0보다 크고 20m³ 이하로 적어 주세요.")
        v[fee_rate_id] = m
    with tx(con):
        set_setting(con, "volume_overrides", v)
        audit(con, "volume", fee_rate_id, "OVERRIDE", {"m3": m3})


# ------------------------------------------------------------------ 운영 수수료표 (원문 표 + 승인된 개정)
# 원문 변환본(data/fee_table.json)은 고치지 않는다. 담당자가 확정한 단가·시행일·확인자는 개정(fee_revision)으로 쌓고,
# 다른 담당자가 승인한 뒤 시행일부터 적용한다. 이미 접수된 품목의 단가·버전은 바꾸지 않는다(원본 보존).
def _active_revision(con, today: str | None = None):
    t = today or today_kst()
    return one(con, """SELECT * FROM fee_revision WHERE status='APPROVED' AND base_version=? AND (effective_from IS NULL OR effective_from<=?)
        ORDER BY effective_from DESC, rev_no DESC LIMIT 1""", (FEE_TABLE["version_id"], t))


def fee_state(con, today: str | None = None) -> dict:
    rev = _active_revision(con, today) if con is not None else None
    if not rev:
        return {"version_id": FEE_TABLE["version_id"], "base_version": FEE_TABLE["version_id"], "rev_no": 0, "effective_from": FEE_TABLE["effective_from"],
                "confirmed_by": None, "approved_by": None, "overrides": {}}
    return {"version_id": f'{FEE_TABLE["version_id"]}-r{rev["rev_no"]}', "base_version": FEE_TABLE["version_id"], "rev_no": rev["rev_no"],
            "effective_from": rev["effective_from"], "confirmed_by": rev["confirmed_by"], "approved_by": rev["approved_by"],
            "overrides": json.loads(rev["overrides_json"])}


def fee_rows(con, today: str | None = None) -> dict:
    ov = fee_state(con, today)["overrides"]
    if not ov:
        return FEE_ROWS
    out = dict(FEE_ROWS)
    for rid, o in ov.items():
        if rid in out:
            out[rid] = {**out[rid], "price": int(o["price"]), "status": "CONFIRMED", "note": o.get("reason", ""), "overridden": True}
    return out


def fee_table_for_phone(con) -> dict:
    stt = fee_state(con)
    return {"version_id": stt["version_id"], "base_version": stt["base_version"], "label": FEE_TABLE["label"],
            "effective_from": stt["effective_from"], "overrides": {k: int(v["price"]) for k, v in stt["overrides"].items()}}


def list_fee_revisions(con) -> list[dict]:
    out = rows(con, "SELECT * FROM fee_revision ORDER BY rev_no DESC")
    act = _active_revision(con)
    for r in out:
        r["overrides"] = json.loads(r.pop("overrides_json"))
        r["active"] = bool(act and act["id"] == r["id"])
        r["version_id"] = f'{r["base_version"]}-r{r["rev_no"]}'
    return out


def review_rows() -> list[dict]:
    return [r for r in FEE_TABLE["rows"] if r["status"] != "CONFIRMED"]


def save_fee_draft(con, data: dict) -> str:
    """개정안 작성: 시행일·확인자·행별 확정 단가(사유 필수). 같은 사람이 승인할 수 없다."""
    eff = (data.get("effective_from") or "").strip()
    try:
        dt.date.fromisoformat(eff)
    except ValueError:
        raise UserError("시행일(적용 시작일)을 날짜로 적어 주세요 (예: 2027-01-01).")
    later = one(con, """SELECT rev_no, effective_from FROM fee_revision WHERE status='APPROVED' AND base_version=? AND effective_from>?
        ORDER BY effective_from DESC LIMIT 1""", (FEE_TABLE["version_id"], eff))
    if later:  # 시행일이 뒤섞이면 나중 개정이 앞선 개정의 바뀐 단가를 모른 채 적용된다
        raise UserError(f"이미 승인된 더 나중 시행 개정(r{later['rev_no']}, {later['effective_from']} 시행)이 있습니다. 그보다 앞선 날짜의 개정은 만들 수 없습니다. "
                        "필요하면 두 내용을 합친 새 개정을 그 날짜 이후로 만드세요.")
    who = (data.get("confirmed_by") or "").strip()
    if not who:
        raise UserError("원문을 대조한 확인자 이름을 적어 주세요.")
    ov = {}
    for rid, o in (data.get("overrides") or {}).items():
        if rid not in FEE_ROWS:
            raise UserError("수수료표에 없는 행입니다.")
        try:
            price = int(str(o.get("price")).replace(",", ""))
        except (TypeError, ValueError):
            raise UserError(f"{FEE_ROWS[rid]['name']} {FEE_ROWS[rid]['spec']}: 단가를 숫자로 적어 주세요.")
        if price <= 0:
            raise UserError("단가는 0원보다 커야 합니다. 모르는 단가는 확정하지 말고 확인 대기로 두세요.")
        reason = (o.get("reason") or "").strip()
        if not reason:
            raise UserError(f"{FEE_ROWS[rid]['name']} {FEE_ROWS[rid]['spec']}: 확정 근거(사유)를 적어 주세요.")
        ov[rid] = {"price": price, "reason": reason[:200]}
    # 이전 승인본의 확정값은 이어받는다(명시적으로 빼지 않는 한)
    prev = one(con, """SELECT overrides_json FROM fee_revision WHERE status='APPROVED' AND base_version=? AND (effective_from IS NULL OR effective_from<=?)
        ORDER BY effective_from DESC, rev_no DESC LIMIT 1""", (FEE_TABLE["version_id"], eff))  # 그 시행일에 적용 중일 승인본
    merged = {**(json.loads(prev["overrides_json"]) if prev else {}), **ov}
    for rid in data.get("remove") or []:
        merged.pop(rid, None)
    with tx(con):
        con.execute("DELETE FROM fee_revision WHERE status='DRAFT'")  # 작성 중인 안은 하나만
        n = (one(con, "SELECT MAX(rev_no) AS n FROM fee_revision")["n"] or 0) + 1
        rid_ = str(uuid.uuid4())
        con.execute("""INSERT INTO fee_revision (id, rev_no, base_version, status, effective_from, confirmed_by, note, overrides_json, created_by, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (rid_, n, FEE_TABLE["version_id"], "DRAFT", eff, who, (data.get("note") or "")[:500],
                                              json.dumps(merged, ensure_ascii=False), ACTOR.get(), now()))
        audit(con, "fee", rid_, "DRAFT", {"rev_no": n, "effective_from": eff, "rows": len(merged)})
    return rid_


def approve_fee_revision(con, rev_id: str, approve: bool = True, reason: str = ""):
    r = one(con, "SELECT * FROM fee_revision WHERE id=?", (rev_id,))
    if not r or r["status"] != "DRAFT":
        raise UserError("승인할 개정안이 없습니다.")
    if r["created_by"] == ACTOR.get():
        raise UserError("작성한 사람은 승인할 수 없습니다. 다른 담당자가 원문과 대조해 승인해 주세요.")
    if not approve and not reason.strip():
        raise UserError("반려 사유를 적어 주세요.")
    if approve and one(con, "SELECT 1 AS x FROM fee_revision WHERE status='APPROVED' AND base_version=? AND effective_from>?", (r["base_version"], r["effective_from"])):
        raise UserError("이 개정안보다 나중 시행 개정이 이미 승인되어 있어 승인할 수 없습니다. 반려하고 그 날짜 이후로 다시 만드세요.")
    with tx(con):
        if approve:
            con.execute("UPDATE fee_revision SET status='APPROVED', approved_by=?, approved_at=? WHERE id=?", (ACTOR.get(), now(), rev_id))
        else:
            con.execute("UPDATE fee_revision SET status='REJECTED', approved_by=?, approved_at=?, reject_reason=? WHERE id=?", (ACTOR.get(), now(), reason.strip()[:200], rev_id))
        audit(con, "fee", rev_id, "APPROVE" if approve else "REJECT", {"rev_no": r["rev_no"]})


@SR.contribute("fee")
def _settings_part(con) -> dict:
    fs = fee_state(con)
    return {"fee_version": fs["version_id"], "fee_label": FEE_TABLE["label"], "fee_checked_on": FEE_TABLE["source_checked_on"],
            "fee_effective_from": fs["effective_from"]}
