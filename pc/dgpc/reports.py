"""보고: 고른 접수 CSV·인쇄, 통계·통계 CSV, 시범 운영 비교 보고.
"""
from __future__ import annotations

import calendar
import datetime as dt
import io
import json
import re

from dgpc import privacy as _pv, finance
from dgpc.core import (
    LABELS, KST, UserError, _chunks, _date_ok, _kst_date, _kst_range, audit, get_setting, one, rows, safe_csv_writer, set_setting, today_kst, tx,
)
from dgpc.derive import FINAL, derive, payment_core, prefetch
from dgpc.fees import FEE_ROWS
from dgpc.intake import CHANNELS, _ids

EXPORT_COLS = ["접수번호", "부과번호", "접수일", "수거 예정일", "완료일", "행정동", "접수 경로", "상태", "상태 코드", "품목(현장 확인 수량)", "수거 수량 합계", "남은 수량 합계",
               "계산 금액(원)", "감면 사유", "감면율(%)", "감면액(원)", "부과 금액(원)", "납부 기록(원)", "수납 상태", "수납 상태 코드", "수수료표 버전", "수거팀"]
STATUS_LABEL = LABELS["status"]  # data/labels.json (PC 화면·휴대폰과 같은 표)
PAY_LABEL = {"NONE": "해당 없음", "AMOUNT_PENDING": "금액 확인 대기", "PAID_AMOUNT_PENDING": "납부 기록 있음·금액 미확정", "UNPAID": "미납", "UNDERPAID": "부족",
             "PAID": "납부 확인", "OVERPAID_REVIEW": "과납 확인 필요", "REFUND_NEEDED": "환불 필요", "OVERREFUNDED_REVIEW": "환불 초과 확인 필요", "EXEMPT": "전액 감면"}


def _export_rows(con, reqs: list[dict]):
    """CSV 행(묶어서 미리 읽음 — 접수마다 질의하지 않음). 만들면서 하나씩 돌려준다."""
    for ch in _chunks([r for r in reqs], 500):
        ids = [r["id"] for r in ch]
        pre = prefetch(con, ids, ("items", "events", "links", "payments", "partial"))
        crew_of = _last_crew_names(con, ids)
        ver = {}
        for x in rows(con, f"SELECT request_id, fee_version_id FROM request_item WHERE request_id IN ({','.join('?' * len(ids))}) AND fee_version_id IS NOT NULL", ids):
            ver.setdefault(x["request_id"], x["fee_version_id"])
        for r in ch:
            st = derive(con, r, pre)
            ps = payment_core(r, st["fee"]["final"], pre["payments"][r["id"]], pre["partial"][r["id"]])
            yield r, _export_line(r, st, ps, ver.get(r["id"]), crew_of.get(r["id"]))


def _export_line(r: dict, st: dict, ps: dict, ver: str | None, crew: str | None) -> list:
    blank = lambda v: "" if v is None else v
    return [r["receipt_no"], r.get("levy_no") or "", _kst_date(r["received_at"]), r["planned_date"], _kst_date(r.get("completed_at")) or "", r["dong"] or "", r["channel"] or "",
            STATUS_LABEL.get(r["status"], r["status"]), r["status"],
            "; ".join(f'{l["name"]} {l["spec"]} {l["qty_confirmed"]}{l["unit_label"]}' for l in st["lines"]),
            sum(l["qty_collected"] for l in st["lines"]), sum(l["remaining"] for l in st["lines"]),
            blank(ps["calculated"]), ps.get("reduction_label") or "", ps["reduction_rate"] or "", blank(ps["reduction"]) if ps["reduction_rate"] else "", blank(ps["levied"]),
            ps["paid"], PAY_LABEL.get(ps["status"], ps["status"]), ps["status"], ver or "", crew or ""]


def _export_row(con, r: dict) -> list:
    return next(_export_rows(con, [r]))[1]


def export_requests(con, ids, include_pii: bool = False) -> bytes:
    """고른 접수 CSV(엑셀용 BOM). 기본은 통계 CSV와 같이 이름·연락처·주소 없이. 개인정보 포함은 접수 담당자·관리자만(서버가 막음),
    화면에서 확인을 받고, 건마다 내보낸 기록을 남긴다."""
    ids = _ids(con, ids)
    buf = io.StringIO()
    w = safe_csv_writer(buf)
    w.writerow(EXPORT_COLS + (["이름", "연락처", "배출 주소", "상세 위치"] if include_pii else []))
    with tx(con):
        reqs = []
        for ch in _chunks(ids):
            got = {r["id"]: r for r in rows(con, f"SELECT * FROM request WHERE id IN ({','.join('?' * len(ch))})", ch)}
            reqs += [got[i] for i in ch if i in got]
        for r, line in _export_rows(con, reqs):
            w.writerow(line + ([r["name"], r["phone"], r["address"], r["spot_note"]] if include_pii else []))
            audit(con, "request", r["id"], "EXPORT", {"pii": bool(include_pii)})
        if include_pii:
            _pv.record_access(con, "EXPORT", "", len(ids), "고른 접수 CSV(개인정보 포함)")
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def print_requests(con, ids) -> dict:
    """고른 접수 목록 인쇄용 자료(이름·연락처·주소 포함 → 파쇄 안내, 건마다 출력 기록)."""
    ids = _ids(con, ids)
    out = []
    with tx(con):
        for rid in ids:
            r = one(con, "SELECT * FROM request WHERE id=?", (rid,))
            r["items"] = rows(con, "SELECT name, spec, qty, unit_label FROM request_item WHERE request_id=? AND removed=0", (rid,))
            r["assigned"] = one(con, """SELECT c.name, b.work_date FROM active_assignment a JOIN assignment_batch b ON b.id=a.batch_id
                JOIN crew c ON c.id=b.crew_id WHERE a.request_id=?""", (rid,))
            out.append(r)
            audit(con, "request", rid, "PRINT", None)
        _pv.record_access(con, "PRINT", ids[0] if len(ids) == 1 else "", len(ids), "고른 접수 목록 인쇄")
    out.sort(key=lambda r: (r["planned_date"] or "9999", r["dong"] or "", r["receipt_no"]))
    return {"rows": out, "printed_at": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")}


STATS_BASIS = {"planned": "수거 예정일", "received": "접수일", "completed": "완료일"}


def _basis_date(r: dict, basis: str) -> str | None:
    if basis == "received":
        return _kst_date(r["received_at"])
    if basis == "completed":
        return _kst_date(r.get("completed_at")) if r["status"] in FINAL else (_kst_date(r.get("cancelled_at")) if r["status"] == "CANCELLED" else None)
    return r["planned_date"]


def _basis_rows(con, start: str, end: str, basis: str, cols: str = "*") -> list[dict]:
    """집계 기준 날짜가 기간 안인 접수(SQL 범위로, 색인 사용). 결과는 _basis_date로 한 번 더 확인한다."""
    if basis == "received":
        a, b = _kst_range(start, end)
        out = rows(con, f"SELECT {cols} FROM request WHERE received_at >= ? AND received_at < ?", (a, b))
    elif basis == "completed":
        a, b = _kst_range(start, end)
        out = rows(con, f"""SELECT {cols} FROM request WHERE (status IN ('DONE','PARTIAL','NOT_COLLECTED','ON_HOLD') AND completed_at >= ? AND completed_at < ?)
            OR (status='CANCELLED' AND cancelled_at >= ? AND cancelled_at < ?)""", (a, b, a, b))
    else:
        out = rows(con, f"SELECT {cols} FROM request WHERE planned_date BETWEEN ? AND ?", (start, end))
    keep = []
    for r in out:
        bd = _basis_date(r, basis)
        if bd and start <= bd <= end:
            r["_bd"] = bd
            keep.append(r)
    return keep


def _last_crew_names(con, ids) -> dict:
    """접수별 마지막(가장 최근 배정) 팀 이름 — 묶어서 한 번에."""
    out = {}
    for ch in _chunks(ids):
        for r in rows(con, f"""SELECT s.request_id, c.name, b.created_at FROM assignment_stop s JOIN assignment_batch b ON b.id=s.batch_id JOIN crew c ON c.id=b.crew_id
                WHERE s.request_id IN ({','.join('?' * len(ch))}) ORDER BY b.created_at""", ch):
            out[r["request_id"]] = r["name"]
    return out


def _check_period(start: str, end: str, basis: str = "planned"):
    for d in (start, end):
        if not _date_ok(d or ""):
            raise UserError("기간을 날짜로 골라 주세요.")
    if start > end:
        raise UserError("시작일이 끝일보다 늦습니다.")
    if basis not in STATS_BASIS:
        raise UserError("집계 기준을 골라 주세요.")


def stats(con, start: str, end: str, basis: str = "planned") -> dict:
    """기간 집계(기준: 수거 예정일·접수일·완료일). 이름·연락처·주소는 쓰지 않는다. 기간은 SQL 범위로 고르고 품목·기록은 묶어서 읽는다."""
    _check_period(start, end, basis)
    fin = finance
    reqs = _basis_rows(con, start, end, basis)
    live_ids = [r["id"] for r in reqs if r["status"] != "CANCELLED"]
    pre = prefetch(con, live_ids, ("items", "events", "links", "payments", "partial"))
    crew_of = _last_crew_names(con, live_ids)
    by_day: dict[str, dict] = {}
    def day(d):
        return by_day.setdefault(d, {"date": d, "received": 0, "planned": 0, "done": 0, "partial": 0, "not_collected": 0, "cancelled": 0,
                                     "calculated": 0, "levied": 0, "paid": 0, "unload_m3": 0.0, "unload_kg": 0.0})
    for r in _basis_rows(con, start, end, "received", "id, received_at, status, planned_date, completed_at, cancelled_at"):
        day(r["_bd"])["received"] += 1
    totals = {"requests": len(reqs), "by_status": {}, "calculated": 0, "reduction": 0, "levied": 0, "paid": 0, "amount_pending": 0, "unpaid": 0,
              "field_added_lines": 0, "lead_days_avg": None, "reduction_count": 0}
    items: dict[str, dict] = {}
    cats: dict[str, dict] = {}
    reds: dict[str, dict] = {}
    months: dict[str, dict] = {}
    leads = []
    crews: dict[str, dict] = {}
    dongs: dict[str, dict] = {}
    channels: dict[str, dict] = {}
    for r in reqs:
        dd = day(r["_bd"])
        mm = months.setdefault(r["_bd"][:7], {"month": r["_bd"][:7], "count": 0, "done": 0, "revisit": 0, "cancelled": 0, "calculated": 0, "reduction": 0, "levied": 0, "paid": 0})
        mm["count"] += 1
        dd["planned"] += 1
        dg = dongs.setdefault(r["dong"] or "", {"dong": r["dong"] or "", "planned": 0, "done": 0, "revisit": 0, "cancelled": 0, "levied": 0, "paid": 0})
        ch = channels.setdefault(r["channel"] or "전화", {"channel": r["channel"] or "전화", "planned": 0, "done": 0, "cancelled": 0})
        dg["planned"] += 1; ch["planned"] += 1
        dg["done"] += r["status"] == "DONE"; ch["done"] += r["status"] == "DONE"; mm["done"] += r["status"] == "DONE"
        rv = r["status"] in ("PARTIAL", "NOT_COLLECTED", "ON_HOLD")
        dg["revisit"] += rv; mm["revisit"] += rv
        dg["cancelled"] += r["status"] == "CANCELLED"; ch["cancelled"] += r["status"] == "CANCELLED"; mm["cancelled"] += r["status"] == "CANCELLED"
        totals["by_status"][r["status"]] = totals["by_status"].get(r["status"], 0) + 1
        key = {"DONE": "done", "PARTIAL": "partial", "NOT_COLLECTED": "not_collected", "CANCELLED": "cancelled"}.get(r["status"])
        if key:
            dd[key] += 1
        if r["status"] == "CANCELLED":
            continue
        st = derive(con, r, pre)
        if r["status"] in ("DONE", "PARTIAL", "NOT_COLLECTED", "ON_HOLD"):
            ps = payment_core(r, st["fee"]["final"], pre["payments"][r["id"]], pre["partial"][r["id"]])
            if ps["calculated"] is None:
                totals["amount_pending"] += 1
            else:
                totals["calculated"] += ps["calculated"]; dd["calculated"] += ps["calculated"]; mm["calculated"] += ps["calculated"]
                totals["levied"] += ps["levied"]; dd["levied"] += ps["levied"]; mm["levied"] += ps["levied"]; dg["levied"] += ps["levied"]
                if ps["reduction_rate"]:
                    totals["reduction"] += ps["reduction"]; mm["reduction"] += ps["reduction"]
                    rd = reds.setdefault(r["reduction_label"] or r["reduction_code"], {"label": r["reduction_label"] or r["reduction_code"], "rate": ps["reduction_rate"], "count": 0, "amount": 0})
                    rd["count"] += 1; rd["amount"] += ps["reduction"]
            totals["paid"] += ps["paid"]; dd["paid"] += ps["paid"]; mm["paid"] += ps["paid"]; dg["paid"] += ps["paid"]
            if ps["status"] in ("UNPAID", "UNDERPAID"):
                totals["unpaid"] += 1
        if r.get("reduction_code"):
            totals["reduction_count"] += 1
        for l in st["lines"]:
            if l["kind"] == "FIELD":
                totals["field_added_lines"] += 1
            k = f'{l["name"]}|{l["spec"]}'
            it = items.setdefault(k, {"name": l["name"], "spec": l["spec"], "unit_label": l["unit_label"], "requested": 0, "collected": 0})
            it["requested"] += l["qty_confirmed"]; it["collected"] += l["qty_collected"]
            cat = (FEE_ROWS.get(l.get("fee_rate_id") or "") or {}).get("category") or "표에 없는 품목"
            c_ = cats.setdefault(cat, {"category": cat, "lines": 0, "requested": 0, "collected": 0, "amount": 0})
            c_["lines"] += 1; c_["requested"] += l["qty_confirmed"]; c_["collected"] += l["qty_collected"]
            if l["price"] is not None:
                c_["amount"] += l["price"] * l["qty_confirmed"]
        rd_ = _kst_date(r["received_at"])
        if rd_ and r["status"] == "DONE" and r["planned_date"]:
            leads.append((dt.date.fromisoformat(r["planned_date"]) - dt.date.fromisoformat(rd_)).days)
        a = {"name": crew_of[r["id"]]} if r["id"] in crew_of else None
        if a:
            c = crews.setdefault(a["name"], {"crew": a["name"], "assigned": 0, "done": 0, "partial_or_not": 0})
            c["assigned"] += 1
            c["done"] += r["status"] == "DONE"
            c["partial_or_not"] += r["status"] in ("PARTIAL", "NOT_COLLECTED", "ON_HOLD")
    for u in rows(con, "SELECT * FROM unload_log WHERE work_date BETWEEN ? AND ? AND void_reason IS NULL", (start, end)):
        dd = day(u["work_date"])
        dd["unload_m3"] = round(dd["unload_m3"] + (u["volume_m3"] or 0), 2)
        dd["unload_kg"] = round(dd["unload_kg"] + (u["weight_kg"] or 0), 1)
    if leads:
        totals["lead_days_avg"] = round(sum(leads) / len(leads), 1)
    days = [by_day[k] for k in sorted(by_day)]
    totals["unload_m3"] = round(sum(d["unload_m3"] for d in days), 2)
    totals["unload_kg"] = round(sum(d["unload_kg"] for d in days), 1)
    totals["received"] = sum(d["received"] for d in days)
    rev = fin.revenue(con, start, end, "month")
    return {"start": start, "end": end, "basis": basis, "basis_label": STATS_BASIS[basis], "totals": totals, "days": days,
            "items": sorted(items.values(), key=lambda x: (-x["requested"], x["name"])), "crews": sorted(crews.values(), key=lambda c: c["crew"]),
            "dongs": sorted(dongs.values(), key=lambda x: (x["dong"] == "", x["dong"])),
            "channels": sorted(channels.values(), key=lambda x: CHANNELS.index(x["channel"]) if x["channel"] in CHANNELS else 99),
            "categories": sorted(cats.values(), key=lambda x: -x["requested"]), "reductions": sorted(reds.values(), key=lambda x: -x["amount"]),
            "months": [months[k] for k in sorted(months)], "revenue": {"methods": rev["methods"], "totals": rev["totals"]}}


def stats_csv(con, start: str, end: str, kind: str = "requests", basis: str = "planned") -> bytes:
    """엑셀에서 열리는 CSV(UTF-8 BOM). 이름·연락처·주소·메모는 넣지 않는다."""
    buf = io.StringIO()
    w = safe_csv_writer(buf)
    if kind == "days":
        s = stats(con, start, end, basis)
        w.writerow(["날짜", "접수", "수거 예정", "수거 완료", "일부 수거", "미수거", "취소", "계산 금액(원)", "부과 금액(원)", "납부 기록(원)", "하차 부피(m3)", "하차 중량(kg)"])
        for d in s["days"]:
            w.writerow([d["date"], d["received"], d["planned"], d["done"], d["partial"], d["not_collected"], d["cancelled"], d["calculated"], d["levied"], d["paid"], d["unload_m3"], d["unload_kg"]])
    elif kind in ("dongs", "items", "categories", "months", "reductions", "channels"):
        s = stats(con, start, end, basis)
        spec = {"dongs": (["행정동", "건수", "수거 완료", "재방문·보류", "취소", "부과 금액(원)", "납부 기록(원)"], lambda x: [x["dong"] or "(동 미지정)", x["planned"], x["done"], x["revisit"], x["cancelled"], x["levied"], x["paid"]]),
                "items": (["품목", "규격", "단위", "확인 수량", "수거 수량"], lambda x: [x["name"], x["spec"], x["unit_label"], x["requested"], x["collected"]]),
                "categories": (["품목 분류", "품목 행", "확인 수량", "수거 수량", "계산 금액(원)"], lambda x: [x["category"], x["lines"], x["requested"], x["collected"], x["amount"]]),
                "months": (["월", "건수", "수거 완료", "재방문·보류", "취소", "계산 금액(원)", "감면액(원)", "부과 금액(원)", "납부 기록(원)"],
                           lambda x: [x["month"], x["count"], x["done"], x["revisit"], x["cancelled"], x["calculated"], x["reduction"], x["levied"], x["paid"]]),
                "reductions": (["감면 사유", "감면율(%)", "건수", "감면액(원)"], lambda x: [x["label"], x["rate"], x["count"], x["amount"]]),
                "channels": (["접수 경로", "건수", "수거 완료", "취소"], lambda x: [x["channel"], x["planned"], x["done"], x["cancelled"]])}[kind]
        w.writerow(spec[0])
        for x in s[kind]:
            w.writerow(spec[1](x))
    else:
        _check_period(start, end, basis)
        w.writerow(EXPORT_COLS)
        for _r, line in _export_rows(con, sorted(_basis_rows(con, start, end, basis), key=lambda r: (r["_bd"], r["receipt_no"]))):
            w.writerow(line)
    audit(con, "report", kind, "EXPORT", {"start": start, "end": end, "basis": basis})
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


# ------------------------------------------------------------------ 일일 작업일지·월보 (현재 원자료 조회, 개인정보 없는 집계)
def _operational_report(con, start: str, end: str, basis: str, kind: str, period: str) -> dict:
    _check_period(start, end, basis)
    try:
        _kst_range(start, end)
    except OverflowError:
        raise UserError("보고서 기간을 지원하는 날짜로 골라 주세요.")
    result = stats(con, start, end, basis)
    unloads = one(con, """SELECT COUNT(*) AS count,
        COALESCE(SUM(volume_m3 IS NOT NULL),0) AS volume_records,
        COALESCE(SUM(weight_kg IS NOT NULL),0) AS weight_records
        FROM unload_log WHERE work_date BETWEEN ? AND ? AND void_reason IS NULL""", (start, end))
    result.update(kind=kind, generated_at=dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M"), unload_summary=unloads)
    result["work_date" if kind == "daily" else "month"] = period
    result["source_note"] = (
        f"접수·품목·부과 금액은 {STATS_BASIS[basis]} 기준으로 고른 접수의 현재 상태를 집계합니다. "
        "수거 수량과 접수별 납부 기록은 현재 확인된 누적 값입니다. "
        "완료일은 마지막 최종 결과일이며 취소 건은 취소일을 사용합니다. "
        "별도로 표시한 기간 내 접수 건수는 접수일, 하차 기록은 작업일, 세입은 납부일이 보고 기간에 속하는 기록을 집계합니다. "
        "수거팀은 접수의 마지막 배정팀입니다. "
        "무효 하차·납부 기록은 제외합니다. 예정일 변경·추가 결과·수정에 따라 다시 조회한 값이 달라질 수 있습니다."
    )
    return result


def daily_report(con, work_date: str, basis: str = "planned") -> dict:
    """일일 작업일지용 현재 집계. 조회만 하며 마감·이월·저장된 보고서를 만들지 않는다."""
    return _operational_report(con, work_date, work_date, basis, "daily", work_date)


def monthly_report(con, month: str, basis: str = "planned") -> dict:
    """YYYY-MM 한 달 전체의 현재 집계. 기존 통계의 날짜·상태·금액 의미를 유지한다."""
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-\d\d", month) or not _date_ok(month + "-01"):
        raise UserError("보고할 월을 YYYY-MM 형식으로 골라 주세요.")
    year, month_number = map(int, month.split("-"))
    end = f"{month}-{calendar.monthrange(year, month_number)[1]:02d}"
    return _operational_report(con, month + "-01", end, basis, "monthly", month)


def _operational_report_csv(report: dict) -> bytes:
    """조회한 집계의 전체 섹션을 내보낸다. 접수별 상세·주민 정보·자유 메모를 넣지 않는다."""
    buf = io.StringIO()
    w = safe_csv_writer(buf)
    w.writerow(["일일 작업일지" if report["kind"] == "daily" else "대형폐기물 수거 월보"])
    w.writerow(["기간", report["start"], report["end"]])
    w.writerow(["집계 기준", report["basis_label"]])
    w.writerow(["생성 시각(한국시간)", report["generated_at"]])
    w.writerow(["집계 안내", report["source_note"]])

    def section(title, headers, values):
        w.writerow([])
        w.writerow([title])
        w.writerow(headers)
        w.writerows(values)

    t = report["totals"]
    summary = [("선택 기준 접수 건수", "requests"), ("기간 내 접수 건수(접수일)", "received"),
               ("계산 금액(원)", "calculated"), ("감면액(원)", "reduction"), ("부과 금액(원)", "levied"),
               ("접수별 누적 납부 기록(원)", "paid"), ("금액 확인 대기(건)", "amount_pending"),
               ("미납·부족(건)", "unpaid"), ("현장 추가 품목 행", "field_added_lines"),
               ("접수일부터 예정일까지 평균 일수", "lead_days_avg"), ("감면 접수(건)", "reduction_count"),
               ("하차 부피(m3)", "unload_m3"), ("하차 중량(kg)", "unload_kg")]
    section("요약", ["항목", "값"], ([label, "" if t[key] is None else t[key]] for label, key in summary))
    section("상태별", ["현재 상태", "상태 코드", "건수"],
            ([STATUS_LABEL.get(status, status), status, count] for status, count in sorted(t["by_status"].items())))
    section("날짜별", ["날짜", "접수일 기준 접수", "선택 기준 건수", "수거 완료", "일부 수거", "미수거", "취소",
                       "계산 금액(원)", "부과 금액(원)", "접수별 누적 납부 기록(원)", "하차 부피(m3)", "하차 중량(kg)"],
            ([x[key] for key in ("date", "received", "planned", "done", "partial", "not_collected", "cancelled", "calculated", "levied", "paid", "unload_m3", "unload_kg")] for x in report["days"]))
    section("행정동별", ["행정동", "선택 기준 건수", "수거 완료", "재방문·보류", "취소", "부과 금액(원)", "접수별 누적 납부 기록(원)"],
            ([x["dong"] or "(동 미지정)", x["planned"], x["done"], x["revisit"], x["cancelled"], x["levied"], x["paid"]] for x in report["dongs"]))
    section("품목별", ["품목", "규격", "단위", "확인 수량", "누적 수거 수량"],
            ([x[key] for key in ("name", "spec", "unit_label", "requested", "collected")] for x in report["items"]))
    section("품목 분류별", ["분류", "품목 행", "확인 수량", "누적 수거 수량", "계산 금액(원)"],
            ([x[key] for key in ("category", "lines", "requested", "collected", "amount")] for x in report["categories"]))
    section("수거팀별", ["수거팀", "배정 건수", "수거 완료", "재방문·보류"],
            ([x[key] for key in ("crew", "assigned", "done", "partial_or_not")] for x in report["crews"]))
    section("접수 경로별", ["접수 경로", "선택 기준 건수", "수거 완료", "취소"],
            ([x[key] for key in ("channel", "planned", "done", "cancelled")] for x in report["channels"]))
    section("감면별", ["감면 사유", "감면율(%)", "건수", "감면액(원)"],
            ([x[key] for key in ("label", "rate", "count", "amount")] for x in report["reductions"]))
    section("월별", ["월", "선택 기준 건수", "수거 완료", "재방문·보류", "취소", "계산 금액(원)", "감면액(원)", "부과 금액(원)", "접수별 누적 납부 기록(원)"],
            ([x[key] for key in ("month", "count", "done", "revisit", "cancelled", "calculated", "reduction", "levied", "paid")] for x in report["months"]))
    u = report["unload_summary"]
    section("하차 기록", ["하차 횟수", "부피 입력 횟수", "중량 입력 횟수", "하차 부피(m3)", "하차 중량(kg)"],
            [[u["count"], u["volume_records"], u["weight_records"], t["unload_m3"], t["unload_kg"]]])
    revenue_fields = ("pay_n", "pay", "refund_n", "refund", "net")
    section("세입(납부일 기준)", ["납부 방법", "납부 건수", "납부액(원)", "환불 건수", "환불액(원)", "순수입(원)"],
            ([x["method"], *(x[key] for key in revenue_fields)] for x in report["revenue"]["methods"]))
    w.writerow(["합계", *(report["revenue"]["totals"][key] for key in revenue_fields)])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def daily_report_csv(con, work_date: str, basis: str = "planned") -> bytes:
    """일일 작업일지의 전체 집계 CSV(UTF-8 BOM, 원자료 변경 없음)."""
    return _operational_report_csv(daily_report(con, work_date, basis))


def monthly_report_csv(con, month: str, basis: str = "planned") -> bytes:
    """월보의 전체 집계 CSV(UTF-8 BOM, 원자료 변경 없음)."""
    return _operational_report_csv(monthly_report(con, month, basis))


# ------------------------------------------------------------------ 시범 운영 비교 (기존 방식 수치 ↔ 이 시스템으로 운영한 기간)
PILOT_BASE = {  # 기존 방식에서 담당자가 직접 적는 수치(같은 길이의 기간 권장)
    "requests": "접수 건수", "missed": "누락 건수(약속한 날 처리 못 하고 넘어간 건)", "revisit": "재방문·미수거 건수",
    "complaints": "독촉·불만 건수", "lead_days": "평균 처리 일수(접수 → 수거)", "intake_min": "접수 1건 처리 시간(분)", "close_min": "하루 마감 시간(분)"}
PILOT_MISS_KINDS = ("미배정", "결과 없음", "최종 결과 없음")


def pilot_settings(con) -> dict:
    p = get_setting(con, "pilot") or {}
    return {"start": p.get("start") or "", "end": p.get("end") or "", "crew_id": p.get("crew_id") or "", "base": p.get("base") or {},
            "base_period": p.get("base_period") or "", "note": p.get("note") or ""}


def save_pilot(con, data: dict) -> dict:
    """시범 운영 기간·팀과 기존 방식 수치 저장(개인정보 없음)."""
    out = pilot_settings(con)
    for k in ("start", "end"):
        v = (data.get(k) or "").strip()
        if v and not re.fullmatch(r"\d{4}-\d\d-\d\d", v):
            raise UserError("시범 운영 기간을 날짜로 골라 주세요.")
        out[k] = v
    if bool(out["start"]) != bool(out["end"]):
        raise UserError("시범 운영 기간은 시작일과 끝일을 함께 정해 주세요.")
    if out["start"] and out["start"] > out["end"]:
        raise UserError("시작일이 끝일보다 늦습니다.")
    cid = (data.get("crew_id") or "").strip()
    if cid and not one(con, "SELECT 1 AS x FROM crew WHERE id=?", (cid,)):
        raise UserError("없는 수거팀입니다.")
    out["crew_id"] = cid
    base = {}
    for k in PILOT_BASE:
        v = (data.get("base") or {}).get(k)
        if v in (None, ""):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise UserError(f"{PILOT_BASE[k]}는 숫자로 적어 주세요.")
        if f < 0:
            raise UserError(f"{PILOT_BASE[k]}는 0 이상이어야 합니다.")
        base[k] = round(f, 2)
    out["base"] = base
    out["base_period"] = (data.get("base_period") or "").strip()[:60]
    out["note"] = (data.get("note") or "").strip()[:300]
    with tx(con):
        set_setting(con, "pilot", out)
        audit(con, "settings", "pilot", "SAVE", {"start": out["start"], "end": out["end"], "crew": bool(cid), "base_keys": sorted(base)})
    return out


def pilot_report(con, start: str | None = None, end: str | None = None, crew_id: str | None = None) -> dict:
    """시범 운영 기간의 지표를 이 시스템 기록으로 계산하고, 담당자가 적은 기존 방식 수치와 나란히 둔다.
    건수 지표는 기간 길이·물량이 달라도 비교되게 100건당으로 바꾼다. 이름·연락처·주소는 쓰지 않는다."""
    cfg = pilot_settings(con)
    start, end = start or cfg["start"], end or cfg["end"]
    crew_id = cfg["crew_id"] if crew_id is None else crew_id
    if not (start and end):
        raise UserError("시범 운영 기간(시작·끝)을 먼저 정해 주세요.")
    if start > end:
        raise UserError("시작일이 끝일보다 늦습니다.")
    scope = None
    if crew_id:  # 그 팀에 배정됐거나 그 팀 휴대폰에서 받은 접수만
        dev = (one(con, "SELECT device_id FROM crew WHERE id=?", (crew_id,)) or {}).get("device_id")
        scope = {r["request_id"] for r in rows(con, "SELECT s.request_id FROM assignment_stop s JOIN assignment_batch b ON b.id=s.batch_id WHERE b.crew_id=?", (crew_id,))}
        if dev:
            scope |= {r["id"] for r in rows(con, "SELECT id FROM request WHERE origin_device=?", (dev,))}
    def in_scope(rid):
        return scope is None or rid in scope
    today = today_kst()
    reqs = [r for r in _basis_rows(con, start, end, "received", "id, status, received_at, completed_at, cancelled_at, planned_date, fee_final")
            if r["status"] != "CANCELLED" and in_scope(r["id"])]
    n = len(reqs)
    ids = {r["id"] for r in reqs}
    # 누락: 마감 때 '미배정·결과 없음·최종 결과 없음'으로 이월된 건 + 예정일이 지났는데 아직 결과가 없는 건(중복 없이)
    missed = set()
    for a in rows(con, "SELECT entity_id, detail FROM audit_log WHERE entity='request' AND action='CARRY' AND at >= ?", (_kst_range(start, start)[0],)):
        dj = json.loads(a["detail"] or "{}")
        if a["entity_id"] in ids and dj.get("kind") in PILOT_MISS_KINDS:
            missed.add(a["entity_id"])
    for r in reqs:
        if r["planned_date"] and r["planned_date"] < today and r["status"] in ("RECEIVED", "ASSIGNED", "IN_PROGRESS"):
            missed.add(r["id"])
    revisit = sum(r["status"] in ("PARTIAL", "NOT_COLLECTED", "ON_HOLD") for r in reqs)
    done = [r for r in reqs if r["status"] == "DONE"]
    leads = []
    for r in done:
        a, b = _kst_date(r["received_at"]), _kst_date(r["completed_at"])
        if a and b:
            leads.append((dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days)
    complaints = 0
    if ids:
        for ch in _chunks(list(ids)):
            complaints += con.execute(f"SELECT COUNT(*) FROM contact_log WHERE kind IN ('독촉','불만') AND request_id IN ({','.join('?' * len(ch))})", ch).fetchone()[0]
    isecs = []
    for a in rows(con, "SELECT entity_id, detail FROM audit_log WHERE entity='request' AND action='CREATE' AND at >= ? AND detail LIKE '%intake_secs%'",
                  (_kst_range(start, start)[0],)):
        if a["entity_id"] in ids:
            v = json.loads(a["detail"] or "{}").get("intake_secs")
            if v:
                isecs.append(v)
    csecs = []
    for a in rows(con, "SELECT entity_id, detail FROM audit_log WHERE entity='day' AND action='CLOSE'"):
        if start <= (a["entity_id"] or "") <= end:
            v = json.loads(a["detail"] or "{}").get("close_secs")
            if v:
                csecs.append(v)
    # 당일 결과 회수: 최종 결과가 있는 건 중 그 결과가 수거 예정일 당일에 PC로 들어온 비율
    sameday = total_final = 0
    fin_ids = [r["id"] for r in reqs if r["status"] in FINAL]
    last_by: dict[str, dict] = {}
    for ch in _chunks(fin_ids):
        # 마지막 수거 결과(ATTEMPT)를 휴대폰에 남긴 날 = 그 결과가 PC에 들어온 날이면 당일 회수(예정일이 재방문·이월로 바뀌어도 맞게)
        for x in rows(con, f"""SELECT e.request_id, e.device_ts AS d, i.imported_at AS t FROM result_event e JOIN result_import i ON i.id=e.import_id
                WHERE e.request_id IN ({','.join('?' * len(ch))}) AND e.kind='ATTEMPT' AND e.apply_status='APPLIED' ORDER BY e.device_ts""", ch):
            last_by[x["request_id"]] = x
    for last in last_by.values():
        if last["t"] and last["d"]:
            total_final += 1
            sameday += _kst_date(last["d"]) == _kst_date(last["t"])
    pending = sum(r["fee_final"] is None for r in reqs)  # 요약 열(fee_final) — 접수마다 다시 계산하지 않음
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None
    per100 = lambda x, base_n: round(x * 100 / base_n, 1) if base_n else None
    B = cfg["base"]
    rows_ = [
        {"key": "requests", "label": "접수 건수", "unit": "건", "base": B.get("requests"), "pilot": n, "better": None},
        {"key": "missed", "label": "누락(약속한 날 처리 못 함)", "unit": "100건당", "base": per100(B["missed"], B.get("requests")) if "missed" in B else None,
         "pilot": per100(len(missed), n), "better": "lower", "count": len(missed)},
        {"key": "revisit", "label": "재방문·미수거", "unit": "100건당", "base": per100(B["revisit"], B.get("requests")) if "revisit" in B else None,
         "pilot": per100(revisit, n), "better": "lower", "count": revisit},
        {"key": "complaints", "label": "독촉·불만 전화", "unit": "100건당", "base": per100(B["complaints"], B.get("requests")) if "complaints" in B else None,
         "pilot": per100(complaints, n), "better": "lower", "count": complaints},
        {"key": "lead_days", "label": "평균 처리 일수(접수 → 수거 완료)", "unit": "일", "base": B.get("lead_days"), "pilot": avg(leads), "better": "lower", "count": len(leads)},
        {"key": "intake_min", "label": "접수 1건 처리 시간", "unit": "분", "base": B.get("intake_min"),
         "pilot": round(sum(isecs) / len(isecs) / 60, 1) if isecs else None, "better": "lower", "count": len(isecs)},
        {"key": "close_min", "label": "하루 마감 시간", "unit": "분", "base": B.get("close_min"),
         "pilot": round(sum(csecs) / len(csecs) / 60, 1) if csecs else None, "better": "lower", "count": len(csecs)},
        {"key": "sameday", "label": "당일 결과 회수", "unit": "%", "base": None, "pilot": per100(sameday, total_final), "better": "higher", "count": total_final},
        {"key": "pending_fee", "label": "금액 확인 대기", "unit": "%", "base": None, "pilot": per100(pending, n), "better": "lower", "count": pending},
    ]
    for r in rows_:
        b, v = r["base"], r["pilot"]
        r["diff"] = round(v - b, 1) if b is not None and v is not None and r["better"] else None  # 접수 건수는 물량이라 차이를 판단하지 않음
        if r["diff"] is None or not r["better"] or r["diff"] == 0:
            r["verdict"] = "같음" if r["diff"] == 0 and r["better"] else None
        else:
            r["verdict"] = "좋아짐" if (r["diff"] < 0) == (r["better"] == "lower") else "나빠짐"
    crew_name = (one(con, "SELECT name FROM crew WHERE id=?", (crew_id,)) or {}).get("name") if crew_id else None
    return {"start": start, "end": end, "crew_id": crew_id or "", "crew": crew_name, "rows": rows_, "base_period": cfg["base_period"], "note": cfg["note"],
            "days": (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1, "base_keys": PILOT_BASE}


def pilot_csv(con, start=None, end=None, crew_id=None) -> bytes:
    r = pilot_report(con, start, end, crew_id)
    buf = io.StringIO()
    w = safe_csv_writer(buf)
    w.writerow(["시범 운영 비교", f"{r['start']}~{r['end']}", r["crew"] or "전체 팀", f"기존 방식: {r['base_period'] or '-'}"])
    w.writerow(["지표", "단위", "기존 방식", "시범 운영", "차이", "판단", "근거 건수"])
    for x in r["rows"]:
        w.writerow([x["label"], x["unit"], "" if x["base"] is None else x["base"], "" if x["pilot"] is None else x["pilot"],
                    "" if x["diff"] is None else x["diff"], x["verdict"] or "", x.get("count", "")])
    audit(con, "report", "pilot", "EXPORT", {"start": r["start"], "end": r["end"]})
    return ("\ufeff" + buf.getvalue()).encode("utf-8")
