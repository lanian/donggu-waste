"""순수 계산: 품목 라인 현재 상태(derive_pure)·수수료(계산 금액 → 감면 → 부과)·수납 상태, 접수 요약 열 갱신.

derive_pure·fee_summary·payment_core 는 DB 없이 같은 입력이면 같은 답을 낸다. 휴대폰(web/fees.js)과 같은 규칙이며
data/derive_vectors.json 의 공통 시험 벡터로 양쪽을 함께 확인한다.
"""
from __future__ import annotations

import json

from dgpc import settings as SR
from dgpc.core import on_dirty, _chunks, _int_clamp, chosung_key, get_setting, one, rows
from dgpc.fees import FEE_ROWS

FINAL = {"DONE", "PARTIAL", "NOT_COLLECTED", "ON_HOLD"}


# ------------------------------------------------------------------ 현장 결과로 현재 상태 계산
def _events_for(con, rid):
    return rows(con, """SELECT e.* FROM result_event e JOIN result_import i ON i.id=e.import_id
        WHERE e.request_id=? AND e.apply_status='APPLIED' ORDER BY i.imported_at, i.rowid, e.device_seq, e.rowid""", (rid,))


def prefetch(con, ids, what=("items", "events", "links", "payments", "partial", "active")) -> dict:
    """여러 접수의 품목·적용된 현장 기록·유사 품목 적용·수납·일부 수거 환불·현재 배정 팀을 묶어서 읽는다(접수마다 질의하지 않게)."""
    ids = list(dict.fromkeys(i for i in ids if i))
    out = {k: {i: [] for i in ids} for k in ("items", "events", "links", "payments")}
    out["partial"] = {i: 0 for i in ids}
    out["active"] = {i: None for i in ids}
    for ch in _chunks(ids):
        q = ",".join("?" * len(ch))
        if "items" in what:
            for r in rows(con, f"SELECT * FROM request_item WHERE request_id IN ({q}) AND removed=0", ch):
                out["items"][r["request_id"]].append(r)
        if "events" in what:
            for r in rows(con, f"""SELECT e.* FROM result_event e JOIN result_import i ON i.id=e.import_id
                    WHERE e.request_id IN ({q}) AND e.apply_status='APPLIED' ORDER BY i.imported_at, i.rowid, e.device_seq, e.rowid""", ch):
                out["events"][r.pop("request_id")].append(r)
        if "links" in what:
            for r in rows(con, f"SELECT * FROM item_link WHERE request_id IN ({q}) AND void_reason IS NULL ORDER BY at", ch):
                out["links"][r["request_id"]].append(r)
        if "payments" in what:
            for r in rows(con, f"SELECT * FROM payment WHERE request_id IN ({q}) ORDER BY recorded_at", ch):
                out["payments"][r["request_id"]].append(r)
        if "partial" in what:
            for r in rows(con, f"""SELECT request_id, COALESCE(SUM(amount),0) AS n FROM refund_case WHERE request_id IN ({q}) AND status='PAID' AND cause='PARTIAL'
                    GROUP BY request_id""", ch):
                out["partial"][r["request_id"]] = r["n"] or 0
        if "active" in what:
            for r in rows(con, f"SELECT a.request_id, b.crew_id FROM active_assignment a JOIN assignment_batch b ON b.id=a.batch_id WHERE a.request_id IN ({q})", ch):
                out["active"][r["request_id"]] = r["crew_id"]
    return out


def _price(v):
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def derive(con, req: dict, pre: dict | None = None) -> dict:
    """휴대폰 앱(fees.js deriveStop)과 같은 규칙. 행별 잔량, 상쇄 없음, 금액은 현장 확인 수량 기준.
    pre: prefetch() 결과(있으면 DB를 다시 읽지 않음)."""
    rid = req["id"]
    if pre is not None:
        items = req.get("items") or pre["items"].get(rid, [])
        events, links = pre["events"].get(rid, []), pre["links"].get(rid, [])
    else:
        items = req.get("items") or rows(con, "SELECT * FROM request_item WHERE request_id=? AND removed=0", (rid,))
        events = _events_for(con, rid)
        links = rows(con, "SELECT * FROM item_link WHERE request_id=? AND void_reason IS NULL ORDER BY at", (rid,))
    return derive_pure(items, events, links)


def derive_pure(items: list[dict], events: list[dict], links: list[dict]) -> dict:
    """DB 없이 계산(미리 읽은 품목·현장 기록·유사 품목 적용). 휴대폰이 보낸 이상한 수량·단가가 있어도 멈추지 않는다(0~9999 정수로)."""
    if events and all('device_id' in e and 'device_seq' in e for e in events):
        from dgpc.collection import event_order
        events = list(event_order(events))
    lines = [{"line_id": i["line_id"], "kind": "RECEIVED", "name": i["name"], "spec": i["spec"], "unit": i["unit"],
              "unit_label": i["unit_label"], "price": i["unit_price"] if i["price_status"] == "CONFIRMED" else None,
              "qty_received": i["qty"], "amount_received": i["amount"], "qty_confirmed": i["qty"], "qty_collected": 0, "reason": "",
              "fee_rate_id": i["fee_rate_id"]} for i in items]
    by = {l["line_id"]: l for l in lines}
    contact, outcome, outcome_ts, loc, n = "NOT_CONTACTED", None, None, None, 0
    memos = []
    # 휴대폰 v0.10 기록: 도착·스티커 확인·추가 금액 고지·문자/길안내(횟수만)·작업자·부재 증거
    fx = {"arrive": None, "stickers": {}, "fee_notices": [], "sms": {}, "nav": 0, "nav_apps": {}, "workers": [], "no_photo_reason": None, "other_kinds": {}}
    known = {"CONTACT", "FIELD_ITEM_ADD", "ITEM_RESULT", "ATTEMPT", "LOCATION_FIX", "FIELD_MEMO", "PHOTO_META", "REVISIT_APPT", "DIAL_INTENT", "UNLOAD",
             "ARRIVE", "STICKER_CHECK", "FEE_NOTICE", "SMS_INTENT", "NAV_INTENT", "FIELD_INTAKE", "CORRECTION", "CANCEL_REQUEST"}
    for e in events:
        try:
            b = json.loads(e["body_json"]) or {}
        except (ValueError, TypeError):
            b = {}
        if not isinstance(b, dict):
            b = {}
        k = e["kind"]
        w = b.get("worker")
        if isinstance(w, str) and w.strip() and w.strip()[:10] not in fx["workers"]:
            fx["workers"].append(w.strip()[:10])
        if k == "ARRIVE":
            fx["arrive"] = {"at": e["device_ts"], "acc": b.get("acc"), "has_gps": b.get("lat") is not None, "worker": w}
        elif k == "STICKER_CHECK" and b.get("line_id"):
            fx["stickers"][b["line_id"]] = {"result": b.get("result"), "number": str(b.get("number") or "")[:30], "decision": b.get("decision"), "at": e["device_ts"]}
        elif k == "FEE_NOTICE":
            fx["fee_notices"].append({"at": e["device_ts"], "received": b.get("received"), "total": b.get("total"), "extra": b.get("extra"),
                                      "pending": b.get("pending"), "response": b.get("response"), "lines": len(b.get("lines") or []), "worker": w})
        elif k == "SMS_INTENT":
            t = b.get("template") or "OTHER"
            fx["sms"][t] = fx["sms"].get(t, 0) + 1
        elif k == "NAV_INTENT":
            fx["nav"] += 1
            a_ = str(b.get("app") or "기타")[:20]
            fx["nav_apps"][a_] = fx["nav_apps"].get(a_, 0) + 1
        elif k == "ATTEMPT" and b.get("no_photo_reason"):
            fx["no_photo_reason"] = b.get("no_photo_reason")
        elif k not in known:
            fx["other_kinds"][k] = fx["other_kinds"].get(k, 0) + 1
        if k == "CONTACT":
            contact = b.get("result", contact)
        elif k == "FIELD_ITEM_ADD" and b.get("line_id") and b.get("line_id") not in by:
            l = {"line_id": b["line_id"], "kind": "FIELD", "name": b.get("name"), "spec": b.get("spec", ""), "unit": b.get("unit"),
                 "unit_label": b.get("unit_label"), "price": _price(b.get("unit_price")) if b.get("price_status") == "CONFIRMED" else None,
                 "qty_received": 0, "amount_received": 0, "qty_confirmed": _int_clamp(b.get("qty")), "qty_collected": 0, "reason": "",
                 "fee_rate_id": b.get("fee_rate_id")}
            lines.append(l); by[l["line_id"]] = l; n += 1
        elif k == "ITEM_RESULT" and b.get("line_id") in by:
            qc = _int_clamp(b.get("qty_confirmed", 0))
            by[b["line_id"]].update(qty_confirmed=qc, qty_collected=min(qc, _int_clamp(b.get("qty_collected", 0))), reason=str(b.get("reason") or ""))
            n += 1
        elif k == "ATTEMPT":
            outcome, outcome_ts = b.get("outcome"), e.get("device_ts"); n += 1
        elif k == "LOCATION_FIX":
            loc = {"lat": b.get("lat"), "lon": b.get("lon"), "source": b.get("source")}
        elif k == "FIELD_MEMO":
            memos.append(str(b.get("text") or "")[:300])
    # 표에 없던 품목을 담당자가 유사 품목 단가로 확정한 기록(근거·확인자)을 덧씌운다. 원래 행은 그대로 둔다.
    for lk in links:
        l = by.get(lk["line_id"])
        if l and l["price"] is None:
            row = FEE_ROWS.get(lk["fee_rate_id"]) or {}
            l["price"] = lk["unit_price"]
            l["link"] = {"id": lk["id"], "fee_rate_id": lk["fee_rate_id"], "name": row.get("name"), "spec": row.get("spec"), "reason": lk["reason"],
                         "confirmed_by": lk["confirmed_by"], "at": lk["at"], "fee_version_id": lk["fee_version_id"]}
    for l in lines:
        l["remaining"] = max(0, l["qty_confirmed"] - l["qty_collected"])
    received = sum(l["amount_received"] or 0 for l in lines if l["kind"] == "RECEIVED")
    pending = [l for l in lines if l["price"] is None and l["qty_confirmed"] > 0]
    subtotal = sum(l["price"] * l["qty_confirmed"] for l in lines if l["price"] is not None)
    qty_by_unit: dict[str, int] = {}
    for l in lines:
        qty_by_unit[l["unit_label"]] = qty_by_unit.get(l["unit_label"], 0) + l["qty_confirmed"]
    return {"lines": lines, "contact": contact, "outcome": outcome, "outcome_ts": outcome_ts, "field_loc": loc, "has_field_records": n > 0, "memos": memos, "field": fx,
            "fee": {"received": received, "confirmed_subtotal": subtotal, "diff": subtotal - received,
                    "field_added": sum(l["price"] * l["qty_confirmed"] for l in lines if l["kind"] == "FIELD" and l["price"] is not None),
                    "pending_count": len(pending), "final": None if pending else subtotal, "qty_by_unit": qty_by_unit},
            "remaining_total": sum(l["remaining"] for l in lines)}


def refresh_request_summary(con, rids) -> int:
    """접수 요약 열(fee_final·levied·paid_total·pay_status·active_crew_id·chosung)을 derive()·수납 기록으로 다시 계산해 저장.
    쓰기 경로마다 트랜잭션 끝에서 자동으로 불린다(store 임시 트리거 → tx → flush_dirty)."""
    if isinstance(rids, str):
        rids = [rids]
    rids = [r for r in dict.fromkeys(rids) if r]
    if not rids:
        return 0
    n = 0
    for ch in _chunks(rids):
        reqs = rows(con, f"SELECT * FROM request WHERE id IN ({','.join('?' * len(ch))})", ch)
        pre = prefetch(con, [r["id"] for r in reqs])
        for r in reqs:
            final = derive(con, r, pre)["fee"]["final"]
            ps = payment_core(r, final, pre["payments"][r["id"]], pre["partial"][r["id"]])
            con.execute("UPDATE request SET fee_final=?, levied=?, paid_total=?, pay_status=?, active_crew_id=?, chosung=? WHERE id=?",
                        (final, ps["levied"], ps["paid"], ps["status"], pre["active"][r["id"]], chosung_key(r["name"], r["address"]), r["id"]))
            n += 1
    return n


ROUND_UNITS = (1, 10)  # 감면액 끝수 처리 단위(원): 1 = 원 미만 버림(기본, 이전과 같음), 10 = 10원 미만 버림


def reduction_round_unit(con) -> int:
    """지금 설정값. 감면을 적용·바꿀 때 접수에 저장하는 값이며, 금액 계산에는 접수에 저장된 값(request_round_unit)을 쓴다."""
    u = get_setting(con, "reduction_round_unit")
    return u if u in ROUND_UNITS else 1


def request_round_unit(r: dict) -> int:
    """접수에 저장된 감면액 끝수 단위(감면을 적용·바꿀 때의 설정값). 없으면 1원(감면 없음·옛 자료)."""
    u = r.get("reduction_round_unit")
    return u if u in ROUND_UNITS else 1


def fee_summary(r: dict, final: int | None, unit: int | None = None) -> dict:
    """계산 금액 → 감면액 → 부과 금액. 감면액은 unit(1원·10원) 미만 버림. unit 을 주지 않으면 접수에 저장된 단위
    (r["reduction_round_unit"], 감면을 적용할 때의 설정값 — 나중에 설정을 바꿔도 이미 감면한 접수는 그대로)."""
    rate = int(r.get("reduction_rate") or 0) if r.get("reduction_code") else 0
    unit = request_round_unit(r) if unit is None else unit
    unit = unit if unit in ROUND_UNITS else 1
    if final is None:
        return {"calculated": None, "reduction_rate": rate, "reduction": None, "levied": None, "reduction_label": r.get("reduction_label"),
                "reduction_round_unit": unit}
    red = (final * rate // 100) // unit * unit
    return {"calculated": final, "reduction_rate": rate, "reduction": red, "levied": final - red, "reduction_label": r.get("reduction_label") if rate else None,
            "reduction_round_unit": unit}


def payment_core(r: dict, final, recs: list[dict], partial_adj: int = 0, unit: int | None = None) -> dict:
    """DB 없이 수납 상태 계산(계산 금액 → 감면 → 부과 금액, 납부 기록, 일부 수거 환불 지급액)."""
    fs = fee_summary(r, final, unit)
    owed = fs["levied"]
    live = [x for x in recs if not x["void_reason"]]
    paid = sum(x["amount"] for x in live if x["kind"] == "PAY") - sum(x["amount"] for x in live if x["kind"] == "REFUND")
    # 결재를 거쳐 지급한 '일부 수거 차액' 환불은 부과 금액을 그만큼 줄인 것으로 본다(차액을 돌려준 뒤 '부족'으로 남지 않게)
    adj = partial_adj or 0
    if owed is not None and adj:
        owed = max(0, owed - adj)
        fs = {**fs, "partial_refund": adj, "levied_after_partial": owed}
    if r["status"] == "CANCELLED" and paid == 0:
        status = "NONE"
    elif r["status"] == "CANCELLED":  # 납부 뒤 취소: 돌려줄 돈이 남아 있음 (마감 수납 확인·'환불 필요' 목록)
        status = "REFUND_NEEDED" if paid > 0 else "OVERREFUNDED_REVIEW"
    elif owed is None:
        status = "AMOUNT_PENDING" if paid == 0 else "PAID_AMOUNT_PENDING"
    elif owed == 0 and paid == 0:
        status = "EXEMPT"  # 전액 감면
    elif paid == 0:
        status = "UNPAID"
    elif paid < owed:
        status = "UNDERPAID"
    elif paid == owed:
        status = "PAID"
    else:
        status = "OVERPAID_REVIEW"
    return {**fs, "paid": paid, "status": status, "records": recs, "levy_no": r.get("levy_no")}


def payment_status(con, rid: str, pre: dict | None = None, r: dict | None = None) -> dict:
    r = r or one(con, "SELECT * FROM request WHERE id=?", (rid,))
    if pre is None:
        pre = prefetch(con, [rid], ("items", "events", "links", "payments", "partial"))
    final = derive(con, r, pre)["fee"]["final"]
    return payment_core(r, final, pre["payments"].get(rid, []), pre["partial"].get(rid, 0))


on_dirty("summary", refresh_request_summary)


@SR.contribute("reduction_round")
def _settings_part(con) -> dict:
    return {"reduction_round_unit": reduction_round_unit(con), "reduction_round_units": list(ROUND_UNITS)}
