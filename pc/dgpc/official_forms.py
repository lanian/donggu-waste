"""동별 실제 수거분 월보·일일 수불대장. 원자료와 번호 배정은 출력할 때 바꾸지 않는다."""
from __future__ import annotations

import calendar
from itertools import groupby
import re

from dgpc import core as C, label_auto as LA
from dgpc.collection import read_traces
from dgpc.fees import FEE_ROWS


def number_ranges(numbers):
    """앞 0과 30자리 번호를 보존하며 실제로 이어지는 같은 자릿수만 묶는다."""
    ordered = sorted(set(numbers), key=lambda value: (int(value), len(value), value))
    out = []
    if not ordered:
        return ""
    first = last = ordered[0]
    for number in ordered[1:]:
        if len(number) == len(last) and int(number) == int(last) + 1:
            last = number
        else:
            out.append(first if first == last else first + "-" + last)
            first = last = number
    out.append(first if first == last else first + "-" + last)
    return ", ".join(out)


def _dong(con, dong_id):
    if not isinstance(dong_id, str) or not dong_id:
        raise C.UserError("보고서를 작성할 행정동을 골라 주세요.")
    dong = C.one(con, "SELECT id,name FROM dong WHERE id=?", (dong_id,))
    if not dong:
        raise C.UserError("보고서를 작성할 행정동을 찾지 못했습니다.")
    return dong


def _net(gross, req):
    rate = int(req.get("reduction_rate") or 0) if req.get("reduction_code") else 0
    unit = req.get("reduction_round_unit")
    unit = unit if unit in (1, 10) else 1
    return gross - (gross * rate // 100) // unit * unit


def _stock_rows(con, dong_id):
    return C.rows(con, """SELECT l.*,a.collection_at,a.voided_at AS allocation_voided_at,a.automatic,
        a.line_id AS allocation_line,a.unit_index,a.source_event
        FROM numbered_label l LEFT JOIN numbered_label_allocation a ON a.number_key=l.number_key
        WHERE l.stock_dong_id=?""", (dong_id,))


def _empty_row(day, kind, item, *, at="", **extra):
    return {"date": day, "at": at, "kind": kind, "receipt_no": "", "request_id": "", "line_id": "", "address": "", "name": "", "phone": "",
            "category": "", "item": item, "spec": "", "unit_label": "개", "price": None, "qty": 0, "fee": 0,
            "received": 0, "issued": 0, "numbers": "", "note": "", **extra}


def _data(con, dong_id, start, end, kind, period):
    dong = _dong(con, dong_id)
    if not C._date_ok(start) or not C._date_ok(end) or start > end:
        raise C.UserError("보고서 기간을 날짜로 골라 주세요.")
    # 수거일은 접수일·완료 요약열과 다르므로 해당 동의 확정 결과를 읽어 실제 날짜로 고른다.
    reqs = C.rows(con, """SELECT id,receipt_no,name,phone,address,dong,reduction_code,reduction_rate,reduction_label,reduction_round_unit
        FROM request r WHERE dong=? AND EXISTS(SELECT 1 FROM result_event e WHERE e.request_id=r.id AND e.apply_status='APPLIED' AND e.kind='ATTEMPT')""", (dong["name"],))
    traces = read_traces(con, reqs)
    labels = _stock_rows(con, dong_id)
    allocations = LA.allocation_rows(con, [r["id"] for r in reqs])
    active = {(a["request_id"], a["line_id"], a["unit_index"]): a for a in allocations if not a["voided_at"]}
    by_line = {}
    for (rid, line, unit), allocation in active.items():
        by_line.setdefault((rid, line), []).append(allocation)
    rows, matched, request_ids = [], set(), set()
    qty = fee = pending_labels = pending_prices = 0
    unknown_dates = 0
    for req in reqs:
        trace = traces[req["id"]]
        metadata = {line["line_id"]: line for line in trace["lines"]}
        gross = 0
        unknown_gross = False
        for segment in trace["segments"]:
            line = metadata[segment["line_id"]]
            if line["price"] is None:
                unknown_gross = True
                amount = None
            elif unknown_gross:
                amount = None
            else:
                after = gross + line["price"] * segment["qty"]
                amount = _net(after, req) - _net(gross, req)
                gross = after
            if not segment["date"]:
                unknown_dates += 1
                continue
            if not start <= segment["date"] <= end:
                continue
            slot = {**segment, "request_id": req["id"], "dong_id": dong_id}
            assigned = [a for a in by_line.get((req["id"], segment["line_id"]), [])
                        if segment["start"] <= a["unit_index"] < segment["end"] and LA._matches(a, slot)]
            matched.update(a["number_key"] for a in assigned)
            numbers = number_ranges([a["number"] for a in assigned])
            missing = segment["qty"] - len(assigned)
            pending_labels += missing
            if amount is None:
                pending_prices += 1
            else:
                fee += amount
            qty += segment["qty"]
            request_ids.add(req["id"])
            notes = []
            if req["reduction_code"]:
                notes.append(f'{req["reduction_label"] or "감면"} {int(req["reduction_rate"] or 0)}%')
            if line["kind"] == "FIELD":
                notes.append("현장 추가")
            if missing:
                notes.append(f"배출표 {missing}장 미배정")
            if amount is None:
                notes.append("단가 확인 대기")
            category = (FEE_ROWS.get(line["fee_rate_id"]) or {}).get("category") or "기타"
            rows.append(_empty_row(segment["date"], "COLLECTION", line["name"], at=segment["at"], receipt_no=req["receipt_no"], request_id=req["id"],
                                   line_id=line["line_id"], address=req["address"], name=req["name"], phone=req["phone"], category=category,
                                   spec=line["spec"], unit_label=line["unit_label"], price=line["price"], qty=segment["qty"], fee=amount,
                                   issued=len(assigned), numbers=numbers, note=" · ".join(notes)))
    incoming, outgoing = [], []
    for label in labels:
        in_day = LA._in_day(label)
        incoming.append((in_day, label))
        out_at = label["collection_at"] or label["used_at"] or label["voided_at"]
        out_day = C._kst_date(out_at)
        if out_day and label["status"] in ("USED", "VOID"):
            outgoing.append((out_day, label))
    opening = sum(day < start for day, _ in incoming) - sum(day < start for day, _ in outgoing)
    for day, grouped in groupby(sorted(incoming, key=lambda entry: entry[0]), key=lambda entry: entry[0]):
        batch = [label for _, label in grouped]
        if start <= day <= end:
            numbers = number_ranges([label["number"] for label in batch])
            rows.append(_empty_row(day, "RECEIVE", "배출표 수령", received=len(batch), note=numbers))
    adjustments = {}
    for day, label in outgoing:
        if not start <= day <= end or label["number_key"] in matched:
            continue
        kind = "VOID" if label["status"] == "VOID" else "MANUAL"
        adjustments.setdefault((day, kind, bool(label["automatic"])), []).append(label)
    for (day, row_kind, automatic), batch in adjustments.items():
        text = "배출표 수량 정정 무효" if row_kind == "VOID" and automatic else "배출표 무효" if row_kind == "VOID" else "배출표 수동 사용(수거기록 미연결)"
        rows.append(_empty_row(day, row_kind, text, issued=len(batch), numbers=number_ranges([label["number"] for label in batch])))
    priority = {"RECEIVE": 0, "COLLECTION": 1, "VOID": 2, "MANUAL": 3}
    rows.sort(key=lambda row: (row["date"], priority[row["kind"]], row["at"], row["receipt_no"], row["line_id"]))
    balance = opening
    for row in rows:
        balance += row["received"] - row["issued"]
        row["balance"] = balance
    received = sum(row["received"] for row in rows)
    issued = sum(row["issued"] for row in rows)
    issues = []
    if pending_labels:
        issues.append(f"배출표 {pending_labels:,}장이 미배정입니다. 해당 동의 재고와 수령일을 확인해 주세요.")
    if pending_prices:
        issues.append(f"수거 품목 {pending_prices:,}행의 단가가 확인 대기입니다.")
    if unknown_dates:
        issues.append(f"수거 기록 {unknown_dates:,}건의 처리 시각을 확인할 수 없습니다.")
    if opening < 0 or any(row["balance"] < 0 for row in rows):
        issues.append("수불 잔량이 음수입니다. 기존 번호의 입고·배부·사용일을 확인해 주세요.")
    receipt_groups = []
    for day, grouped in groupby(sorted(incoming, key=lambda entry: (entry[0], int(entry[1]["number_key"]))), key=lambda entry: entry[0]):
        if day <= end:
            batch = [label for _, label in grouped]
            receipt_groups.append({"date": day, "numbers": [label["number"] for label in batch], "qty": len(batch)})
    return {"kind": kind, "period": period, "start": start, "end": end, "dong_id": dong_id, "dong": dong["name"], "rows": rows,
            "request_ids": sorted(request_ids), "receipts": receipt_groups, "ready": not issues, "issues": issues,
            "totals": {"qty": qty, "fee": fee, "opening": opening, "received": received, "issued": issued, "closing": balance,
                       "requests": len(request_ids), "pending_labels": pending_labels, "pending_prices": pending_prices},
            "source_note": "실제 수거일·수거 수량 기준입니다. 수거분 수수료는 저장된 단가와 감면 기준으로 계산하며 실제 수납 기록과 별도입니다."}


def monthly_data(con, dong_id, month):
    if not isinstance(month, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month) or not C._date_ok(month + "-01"):
        raise C.UserError("월보를 작성할 월을 YYYY-MM 형식으로 골라 주세요.")
    year, number = map(int, month.split("-"))
    return _data(con, dong_id, month + "-01", f"{month}-{calendar.monthrange(year, number)[1]:02d}", "monthly", month)


def daily_data(con, dong_id, day):
    if not isinstance(day, str) or not C._date_ok(day):
        raise C.UserError("일일 대장을 작성할 날짜를 골라 주세요.")
    return _data(con, dong_id, day, day, "daily", day)


def preview(con, dong_id, day):
    daily = daily_data(con, dong_id, day)
    monthly = monthly_data(con, dong_id, day[:7])
    return {"dong_id": dong_id, "dong": daily["dong"], "date": day, "month": day[:7],
            "daily": {**daily["totals"], "ready": daily["ready"], "issues": daily["issues"]},
            "monthly": {**monthly["totals"], "ready": monthly["ready"], "issues": monthly["issues"]}, "source_note": daily["source_note"]}


def _ready(data):
    if not data["ready"]:
        raise C.UserError(" ".join(data["issues"]))


def xlsx_file(data):
    _ready(data)
    from dgpc.form_files import monthly_xlsx
    return monthly_xlsx(data)


def odt_file(data):
    _ready(data)
    from dgpc.form_files import daily_odt
    return daily_odt(data)
