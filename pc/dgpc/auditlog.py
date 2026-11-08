"""변경 이력 화면: 조건 조회·쪽 나누기·CSV(개인정보 칸 가림).
"""
from __future__ import annotations

import datetime as dt
import io
import json
import re

from dgpc.core import KST, UserError, audit, one, rows, safe_csv_writer, tx

AUDIT_ENTITIES = {"request": "접수", "payment": "수납", "batch": "배정", "bundle": "목록 전송", "import": "결과 가져오기", "event": "충돌 처리",
                  "photo": "사진", "photo_import": "사진 가져오기", "day": "마감", "device": "휴대폰", "crew": "수거팀", "user": "사용자",
                  "backup": "백업", "fee": "수수료표", "address": "주소 자료", "report": "보고서·내보내기", "unload": "하차", "volume": "적재 보정",
                  "dong": "행정동", "holiday": "휴무일", "settings": "설정", "contact": "민원·통화", "request_import": "접수 일괄 등록",
                  "privacy": "개인정보 파기", "access": "접속기록", "refund": "환불", "revenue": "세입·대사", "numbered_label": "숫자 배출표"}
_AUDIT_HIDE = ("name", "phone", "address", "memo", "location_note", "sha256", "content", "spot_note")


def _audit_where(entity="", date="", actor="", date_from="", date_to="", action="") -> tuple[str, list]:
    sql, args = " WHERE 1=1", []
    if entity:
        sql += " AND entity=?"; args.append(entity)
    if action:
        sql += " AND action=?"; args.append(action)
    if date:
        date_from = date_to = date
    for v, op in ((date_from, ">="), (date_to, "<")):  # 한국시간 날짜 → UTC 범위
        if not v:
            continue
        if not re.fullmatch(r"\d{4}-\d\d-\d\d", v):
            raise UserError("기간을 날짜로 골라 주세요.")
        d0 = dt.datetime.fromisoformat(v).replace(tzinfo=KST).astimezone(dt.timezone.utc)
        if op == "<":
            d0 += dt.timedelta(days=1)
        sql += f" AND at {op} ?"; args.append(d0.strftime("%Y-%m-%dT%H:%M:%SZ"))
    if actor:
        sql += " AND actor=?"; args.append(actor)
    return sql, args


def list_audit(con, entity: str = "", date: str = "", actor: str = "", limit: int = 300, date_from: str = "", date_to: str = "",
               action: str = "", offset: int = 0) -> list[dict]:
    w, args = _audit_where(entity, date, actor, date_from, date_to, action)
    out = rows(con, "SELECT id, entity, entity_id, action, detail, at, actor FROM audit_log" + w + " ORDER BY id DESC LIMIT ? OFFSET ?",
               args + [int(limit), int(offset)])
    return [_with_note(con, a) for a in out]


def _with_note(con, a: dict) -> dict:
    """따로 둔 사유 글을 이력 내용에 다시 붙여 보여 준다(파기된 글은 '(파기)')."""
    if a.get("detail") and '"note_sha"' in a["detail"]:
        n = one(con, "SELECT note FROM audit_note WHERE audit_id=?", (a["id"],))
        try:
            d = json.loads(a["detail"])
            d.pop("note_sha", None)
            d["reason"] = (n["note"] if n and n["note"] is not None else "(파기)")
            a = {**a, "detail": json.dumps(d, ensure_ascii=False)}
        except ValueError:
            pass
    return a


def audit_page(con, f: dict) -> dict:
    """변경 이력 조회(쪽 나눔·전체 건수). f: entity, action, actor, date_from, date_to, page, size."""
    w, args = _audit_where(f.get("entity", ""), f.get("date", ""), f.get("actor", ""), f.get("date_from", ""), f.get("date_to", ""), f.get("action", ""))
    total = one(con, "SELECT COUNT(*) AS n FROM audit_log" + w, args)["n"]
    size = max(1, min(1000, int(f.get("size") or 100)))
    pages = max(1, -(-total // size))
    page = max(1, min(pages, int(f.get("page") or 1)))
    return {"rows": list_audit(con, limit=size, offset=(page - 1) * size, entity=f.get("entity", ""), date=f.get("date", ""), actor=f.get("actor", ""),
                               date_from=f.get("date_from", ""), date_to=f.get("date_to", ""), action=f.get("action", "")),
            "total": total, "page": page, "pages": pages, "size": size}


def _audit_detail_text(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        d = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(d, dict):
        return str(d)[:200]
    return "; ".join(f"{k}={json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v}" for k, v in d.items() if k not in _AUDIT_HIDE)[:500]


def audit_csv(con, f: dict) -> bytes:
    """거른 변경 이력 CSV(엑셀용 BOM). 이력 내용에는 원래 이름·연락처·주소가 없고, 혹시 있을 만한 칸도 빼고 내보낸다."""
    w, args = _audit_where(f.get("entity", ""), f.get("date", ""), f.get("actor", ""), f.get("date_from", ""), f.get("date_to", ""), f.get("action", ""))
    buf = io.StringIO()
    wr = safe_csv_writer(buf)
    wr.writerow(["번호", "시각(한국시간)", "작업자", "대상", "작업", "대상 번호", "내용"])
    n = 0
    for a in rows(con, "SELECT id, entity, entity_id, action, detail, at, actor FROM audit_log" + w + " ORDER BY id DESC LIMIT 100000", args):
        a = _with_note(con, a)
        t = dt.datetime.fromisoformat(a["at"].replace("Z", "+00:00")).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
        wr.writerow([a["id"], t, a["actor"] or "", AUDIT_ENTITIES.get(a["entity"], a["entity"]), a["action"], (a["entity_id"] or "")[:8], _audit_detail_text(a["detail"])])
        n += 1
    with tx(con):
        audit(con, "report", "audit", "EXPORT", {"rows": n, **{k: f[k] for k in ("entity", "actor", "date_from", "date_to") if f.get(k)}})
    return ("\ufeff" + buf.getvalue()).encode("utf-8")
