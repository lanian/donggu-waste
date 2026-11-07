"""수수료 감면·부과번호·세입 일계표/월계표·대사(세외수입 납부 자료 맞추기)·환불 절차.

- 감면 사유·감면율은 설정의 목록(기본값은 '조례 확인 필요' 표시가 붙은 예시). 접수마다 사유·감면율·증빙 확인 방법·확인자만 남기고
  주민등록번호·수급자 증명번호 같은 식별번호는 저장하지 않는다.
- 금액: 계산 금액(현장 확인 수량 × 단가) → 감면액(계산 금액 × 감면율, 원 미만 버림) → 부과 금액. 수납 상태·통계·CSV는 부과 금액으로 비교한다.
- 부과번호: 연도-동코드-일련번호(service/store). 세외수입 시스템 연계 형식은 기관 확인이 필요하다.
- 환불: 신청(접수 담당자·관리자) → 결재(팀장·관리자, 신청자가 아닌 사람) → 지급 기록(지급일·방법, 계좌번호는 저장하지 않고 '계좌 확인함'과 끝 4자리만).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import uuid

from dgpc import core as C, derive as D, geo, payments as PAY, settings as SR
from dgpc.derive import fee_summary  # noqa: F401 — 감면 계산(순수)은 derive 로 옮김. FN.fee_summary 이름은 그대로

DEFAULT_REDUCTIONS = [
    {"code": "BASIC", "label": "기초생활수급자", "rate": 100, "note": "조례 확인 필요", "active": True},
    {"code": "NEAR_POOR", "label": "차상위계층", "rate": 50, "note": "조례 확인 필요", "active": True},
    {"code": "DISABLED", "label": "장애인", "rate": 50, "note": "조례 확인 필요", "active": True},
    {"code": "MERIT", "label": "국가유공자", "rate": 50, "note": "조례 확인 필요", "active": True},
]
PROOF_METHODS = ("서류 확인", "행정정보 공동이용", "기타")
REFUND_METHODS = ("계좌 이체", "현금", "카드 취소", "기타")
REFUND_STATUS = {"DRAFT": "자동 생성(신청 전)", "REQUESTED": "결재 대기", "APPROVED": "결재 완료(지급 전)", "REJECTED": "반려", "PAID": "지급 완료", "VOID": "해소·취소"}
REFUND_CAUSE = {"CANCELLED": "납부 뒤 취소", "OVERPAID": "과납", "PARTIAL": "일부 수거 차액", "MANUAL": "기타(직접 신청)"}
RECON_LABEL = {"MATCH": "일치", "AMOUNT_DIFF": "금액 다름", "UNMATCHED": "부과번호 없음", "DUPLICATE": "파일 안 중복", "ALREADY": "이미 반영",
               "AMOUNT_PENDING": "금액 미확정", "CANCELLED": "취소된 접수", "BAD": "형식 오류"}


# ------------------------------------------------------------------ 감면
def reductions(con) -> list[dict]:
    return C.get_setting(con, "reductions") or [dict(x) for x in DEFAULT_REDUCTIONS]


@SR.contribute("reductions")
def _settings_part(con) -> dict:
    return {"reductions": reductions(con), "reduction_proofs": list(PROOF_METHODS)}


def save_reductions(con, items: list[dict]):
    """감면 사유 목록 저장(호출하는 쪽이 트랜잭션). 이미 쓰인 코드는 지우지 말고 '사용 안 함'으로 둔다."""
    out, seen = [], set()
    for x in items or []:
        label = (x.get("label") or "").strip()[:30]
        if not label:
            continue
        code = (x.get("code") or "").strip().upper() or re.sub(r"[^A-Z0-9]", "", label.upper()) or f"R{len(out) + 1}"
        if not re.fullmatch(r"[A-Z0-9_]{1,20}", code):
            raise C.UserError("감면 코드는 영문 대문자·숫자 20자 이내로 적어 주세요.")
        if code in seen:
            raise C.UserError(f"같은 감면 코드가 두 번 있습니다({code}).")
        try:
            rate = int(x.get("rate"))
        except (TypeError, ValueError):
            raise C.UserError(f"{label}: 감면율을 숫자(%)로 적어 주세요.")
        if not 1 <= rate <= 100:
            raise C.UserError(f"{label}: 감면율은 1~100% 사이입니다.")
        seen.add(code)
        out.append({"code": code, "label": label, "rate": rate, "note": (x.get("note") or "").strip()[:60], "active": bool(x.get("active", True))})
    C.set_setting(con, "reductions", out)
    return out


def _check_reduction(con, code: str, proof: str, checker: str) -> dict:
    item = next((x for x in reductions(con) if x["code"] == code), None)
    if not item:
        raise C.UserError("감면 사유를 골라 주세요.")
    if not item.get("active", True):
        raise C.UserError("사용하지 않는 감면 사유입니다.")
    if proof not in PROOF_METHODS:
        raise C.UserError("증빙 확인 방법을 골라 주세요 (" + ", ".join(PROOF_METHODS) + ").")
    if not (checker or "").strip():
        raise C.UserError("증빙을 확인한 사람(확인자)을 적어 주세요.")
    if re.search(r"\d{6}\s*-?\s*\d{7}", checker):
        raise C.UserError("주민등록번호 같은 식별번호는 적지 마세요.")
    return item


def apply_reduction_fields(con, data: dict) -> dict | None:
    """접수 등록 자료의 감면 칸 → 저장할 값(없으면 None)."""
    code = (data.get("reduction_code") or "").strip()
    if not code:
        return None
    item = _check_reduction(con, code, data.get("reduction_proof") or "", data.get("reduction_checker") or "")
    return {"reduction_code": code, "reduction_rate": item["rate"], "reduction_label": item["label"], "reduction_proof": data.get("reduction_proof"),
            "reduction_checker": (data.get("reduction_checker") or "").strip()[:30], "reduction_at": C.now(),
            # 끝수 단위는 감면을 적용·바꿀 때의 설정값을 접수에 남긴다(나중에 설정을 바꿔도 이 접수의 부과 금액은 그대로)
            "reduction_round_unit": D.reduction_round_unit(con)}


def set_reduction(con, rid: str, data: dict) -> dict:
    r = C.one(con, "SELECT * FROM request WHERE id=?", (rid,))
    if not r:
        raise C.UserError("접수를 찾을 수 없습니다.")
    flag = C._after_close(con, rid)
    why = (data.get("reason") or "").strip()
    if flag and not why:
        raise C.UserError(f"마감한 날({flag['after_close']})의 접수입니다. 감면 변경 사유를 적어 주세요.")
    vals = apply_reduction_fields(con, data)
    if not vals and not r["reduction_code"]:
        raise C.UserError("감면 사유를 골라 주세요.")
    with C.tx(con):
        if data.get("req_ver") not in (None, ""):  # 화면이 연 버전과 같을 때만(다르면 409)
            C.check_req_ver(con, rid, data.get("req_ver"))
        if vals:
            con.execute("""UPDATE request SET reduction_code=?, reduction_rate=?, reduction_label=?, reduction_proof=?, reduction_checker=?, reduction_at=?,
                reduction_round_unit=? WHERE id=?""",
                        (*vals.values(), rid))
        else:
            con.execute("UPDATE request SET reduction_code=NULL, reduction_rate=NULL, reduction_label=NULL, reduction_proof=NULL, reduction_checker=NULL, reduction_at=?, reduction_round_unit=NULL WHERE id=?",
                        (C.now(), rid))
        con.execute("UPDATE request SET updated_at=? WHERE id=?", (C.now(), rid))  # 목록 전송 뒤 바뀌면 '재전송 필요'
        C.audit(con, "request", rid, "REDUCTION" if vals else "REDUCTION_CLEAR",
                {**({"code": vals["reduction_code"], "rate": vals["reduction_rate"], "proof": vals["reduction_proof"], "checker": vals["reduction_checker"],
                     "round_unit": vals["reduction_round_unit"]} if vals else {}),
                 **({"reason": why[:200]} if why else {}), **flag})
    return D.payment_status(con, rid)


# ------------------------------------------------------------------ 세입 일계표·월계표
def _period(d: str, unit: str) -> str:
    return d[:7] if unit == "month" else d


def revenue(con, start: str, end: str, unit: str = "day") -> dict:
    for d in (start, end):
        if not re.fullmatch(r"\d{4}-\d\d-\d\d", d or ""):
            raise C.UserError("기간을 날짜로 골라 주세요.")
    if start > end:
        raise C.UserError("시작일이 끝일보다 늦습니다.")
    unit = "month" if unit == "month" else "day"
    pays = C.rows(con, """SELECT p.*, r.dong, r.levy_no, r.receipt_no FROM payment p JOIN request r ON r.id=p.request_id
        WHERE p.void_reason IS NULL AND p.paid_on BETWEEN ? AND ? ORDER BY p.paid_on, p.recorded_at""", (start, end))
    by_method: dict[tuple, dict] = {}
    by_dong: dict[tuple, dict] = {}
    tot = {"pay_n": 0, "pay": 0, "refund_n": 0, "refund": 0}
    def add(dct, key, base, p):
        x = dct.setdefault(key, {**base, "pay_n": 0, "pay": 0, "refund_n": 0, "refund": 0})
        if p["kind"] == "PAY":
            x["pay_n"] += 1; x["pay"] += p["amount"]
        else:
            x["refund_n"] += 1; x["refund"] += p["amount"]
        x["net"] = x["pay"] - x["refund"]
    for p in pays:
        per = _period(p["paid_on"], unit)
        add(by_method, (per, p["method"]), {"period": per, "method": p["method"]}, p)
        add(by_dong, (per, p["dong"] or ""), {"period": per, "dong": p["dong"] or ""}, p)
        if p["kind"] == "PAY":
            tot["pay_n"] += 1; tot["pay"] += p["amount"]
        else:
            tot["refund_n"] += 1; tot["refund"] += p["amount"]
    tot["net"] = tot["pay"] - tot["refund"]
    methods: dict[str, dict] = {}
    for x in by_method.values():
        m = methods.setdefault(x["method"], {"method": x["method"], "pay_n": 0, "pay": 0, "refund_n": 0, "refund": 0})
        for k in ("pay_n", "pay", "refund_n", "refund"):
            m[k] += x[k]
        m["net"] = m["pay"] - m["refund"]
    return {"start": start, "end": end, "unit": unit, "by_method": sorted(by_method.values(), key=lambda x: (x["period"], x["method"])),
            "by_dong": sorted(by_dong.values(), key=lambda x: (x["period"], x["dong"] == "", x["dong"])),
            "methods": sorted(methods.values(), key=lambda x: -x["pay"]), "totals": tot, "count": len(pays)}


def revenue_csv(con, start: str, end: str, unit: str = "day", kind: str = "method") -> bytes:
    """세외수입 담당자용 CSV(엑셀 BOM). 이름·연락처·주소 없음."""
    buf = io.StringIO()
    w = C.safe_csv_writer(buf)
    per = "월" if unit == "month" else "납부일"
    if kind == "detail":
        w.writerow(["납부일", "부과번호", "접수번호", "행정동", "구분", "납부 방법", "금액(원)", "영수번호·거래번호", "기록 출처", "기록 시각"])
        for p in C.rows(con, """SELECT p.*, r.dong, r.levy_no, r.receipt_no FROM payment p JOIN request r ON r.id=p.request_id
                WHERE p.void_reason IS NULL AND p.paid_on BETWEEN ? AND ? ORDER BY p.paid_on, r.levy_no""", (start, end)):
            w.writerow([p["paid_on"], p["levy_no"] or "", p["receipt_no"], p["dong"] or "", "납부" if p["kind"] == "PAY" else "환불", p["method"],
                        p["amount"] if p["kind"] == "PAY" else -p["amount"], p["receipt_ref"] or "", p["source"] or "수기", p["recorded_at"]])
    else:
        rep = revenue(con, start, end, unit)
        key = "dong" if kind == "dong" else "method"
        w.writerow([per, "행정동" if key == "dong" else "납부 방법", "납부 건수", "납부액(원)", "환불 건수", "환불액(원)", "순수입(원)"])
        for x in rep["by_dong" if key == "dong" else "by_method"]:
            w.writerow([x["period"], (x[key] or "(동 미지정)") if key == "dong" else x[key], x["pay_n"], x["pay"], x["refund_n"], x["refund"], x["net"]])
        t = rep["totals"]
        w.writerow(["합계", "", t["pay_n"], t["pay"], t["refund_n"], t["refund"], t["net"]])
    with C.tx(con):
        C.audit(con, "report", "revenue", "EXPORT", {"start": start, "end": end, "unit": unit, "kind": kind})
    return ("﻿" + buf.getvalue()).encode("utf-8")


# ------------------------------------------------------------------ 대사 (세외수입 납부 자료 CSV: 부과번호·금액·납부일)
def recon_template() -> bytes:
    buf = io.StringIO()
    w = C.safe_csv_writer(buf)
    w.writerow(["부과번호", "금액", "납부일", "영수번호", "납부방법"])
    w.writerow(["2026-00-00001", "12000", "2026-11-03", "가상-0001", "가상계좌"])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _recon_method(method: str) -> str:
    """대사 파일의 납부방법 → 수납 기록의 방법. 빈 칸은 세외수입 기본인 가상계좌, 목록에 없는 글은 '기타'
    (가상계좌로 바꿔 적으면 방법별 세입 합계가 틀어진다. 원래 글은 수납 메모에 남긴다)."""
    if not method:
        return "가상계좌"
    return method if method in PAY.PAY_METHODS else "기타"


def parse_recon(con, raw: bytes) -> dict:
    text = geo.decode(raw)
    rd = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rd:
        raise C.UserError("빈 파일입니다.")
    head = [c.strip().replace(" ", "") for c in rd[0]]
    def col(*names):
        return next((head.index(n) for n in names if n in head), None)
    ci = {"levy": col("부과번호", "고지번호"), "amount": col("금액", "납부금액", "수납금액"), "date": col("납부일", "수납일", "납부일자"),
          "ref": col("영수번호", "거래번호", "수납번호"), "method": col("납부방법", "수납방법")}
    if ci["levy"] is None or ci["amount"] is None or ci["date"] is None:
        raise C.UserError("머리줄에 '부과번호, 금액, 납부일' 열이 있어야 합니다. [서식 내려받기]를 참고하세요.")
    if len(rd) > 5001:
        raise C.UserError("한 번에 5,000행까지 넣을 수 있습니다.")
    get = lambda r, k: (r[ci[k]].strip() if ci[k] is not None and ci[k] < len(r) else "")
    out, seen = [], {}
    for n, r in enumerate(rd[1:], start=2):
        levy, ref, method = get(r, "levy"), get(r, "ref")[:40], get(r, "method")[:20]
        date = get(r, "date").replace(".", "-").replace("/", "-")
        if re.fullmatch(r"\d{8}", date):
            date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        try:
            amount = int(re.sub(r"[,\s]|원$", "", get(r, "amount")))  # '15,000원'·'15 000'도 받는다
        except ValueError:
            amount = None
        row = {"line": n, "levy_no": levy, "amount": amount, "paid_on": date, "ref": ref, "method": method}
        if not levy or amount is None or amount <= 0 or not re.fullmatch(r"\d{4}-\d\d-\d\d", date):
            row.update(status="BAD", note="부과번호·금액·납부일 형식 확인")
            out.append(row); continue
        q = C.one(con, "SELECT * FROM request WHERE levy_no=?", (levy,))
        if not q:
            row.update(status="UNMATCHED", note="PC에 없는 부과번호")
            out.append(row); continue
        row.update(request_id=q["id"], receipt_no=q["receipt_no"], dong=q["dong"])
        if levy in seen:
            row.update(status="DUPLICATE", note=f"파일 {seen[levy]}행과 같은 부과번호")
            out.append(row); continue
        seen[levy] = n
        dup = C.one(con, """SELECT 1 AS x FROM payment WHERE request_id=? AND void_reason IS NULL AND kind='PAY' AND source='대사'
            AND ((receipt_ref IS NOT NULL AND receipt_ref!='' AND receipt_ref=?)
                 OR (amount=? AND paid_on=? AND (?='' OR receipt_ref IS NULL OR receipt_ref='')))""", (q["id"], ref, amount, date, ref))
        # 영수번호가 서로 다르면 금액·날짜가 같아도 다른 납부로 본다(같은 날 같은 금액을 두 번 낸 경우)
        ps = D.payment_status(con, q["id"])
        row.update(levied=ps["levied"], paid=ps["paid"])
        if dup:
            row.update(status="ALREADY", note="같은 납부를 이미 반영함")
        elif q["status"] == "CANCELLED":
            row.update(status="CANCELLED", note="취소된 접수의 납부 → 환불 검토")
        elif ps["levied"] is None:
            row.update(status="AMOUNT_PENDING", note="부과 금액 미확정(금액 확인 대기 품목)")
        elif ps["levied"] - ps["paid"] == amount:
            row.update(status="MATCH", note=f"납부방법 '{method}'은(는) 목록에 없어 '기타'로 기록" if _recon_method(method) == "기타" else "")
        else:
            row.update(status="AMOUNT_DIFF", note=f"남은 부과 금액 {ps['levied'] - ps['paid']:,}원 ≠ 납부 {amount:,}원")
        out.append(row)
    counts: dict[str, int] = {}
    for x in out:
        counts[x["status"]] = counts.get(x["status"], 0) + 1
    return {"rows": out, "counts": counts, "total": len(out)}


def import_recon(con, raw: bytes, preview: bool = True) -> dict:
    """미리보기(일치·불일치 목록) → 확인 뒤 일치 행만 납부(출처 '대사')로 기록. 같은 파일은 한 번만."""
    res = parse_recon(con, raw)
    sha = hashlib.sha256(raw).hexdigest()
    prev = C.one(con, "SELECT at, matched FROM recon_import WHERE sha256=?", (sha,))
    res["already_imported"] = prev
    if preview:
        return res
    if prev:
        raise C.UserError(f"이미 반영한 파일입니다({C._kst_date(prev['at'])}, {prev['matched']}건).")
    matched = [x for x in res["rows"] if x["status"] == "MATCH"]
    with C.tx(con):
        for x in matched:
            pid = str(uuid.uuid4())
            method = _recon_method(x["method"])
            note = f"대사 파일의 납부방법: {x['method']}" if method != x["method"] and x["method"] else ""
            con.execute("""INSERT INTO payment (id, request_id, kind, amount, method, paid_on, note, recorded_at, receipt_ref, source)
                VALUES (?,?,?,?,?,?,?,?,?,'대사')""", (pid, x["request_id"], "PAY", x["amount"], method, x["paid_on"], note, C.now(), x["ref"] or None))
            C.audit(con, "payment", pid, "PAY", {"request": x["request_id"], "amount": x["amount"], "method": method, "source": "대사"})
        iid = str(uuid.uuid4())
        con.execute("INSERT INTO recon_import (id, sha256, at, actor, rows, matched, summary_json) VALUES (?,?,?,?,?,?,?)",
                    (iid, sha, C.now(), C.ACTOR.get(), res["total"], len(matched), json.dumps(res["counts"], ensure_ascii=False)))
        C.audit(con, "revenue", iid, "RECON", {"rows": res["total"], "matched": len(matched), **res["counts"]})
    return {**res, "recorded": len(matched)}


def recon_history(con) -> list[dict]:
    out = C.rows(con, "SELECT at, actor, rows, matched, summary_json FROM recon_import ORDER BY at DESC LIMIT 50")
    for r in out:
        r["counts"] = json.loads(r.pop("summary_json"))
    return out


# ------------------------------------------------------------------ 환불
def _partial_due(con, r: dict, ps: dict) -> int:
    """일부 수거: 실제로 실은 수량 기준 부과 금액보다 더 낸 돈(재방문 없이 끝낼 때 돌려줄 차액)."""
    st = D.derive(con, r)
    if any(l["price"] is None and l["qty_collected"] for l in st["lines"]):
        return 0
    collected = sum(l["price"] * l["qty_collected"] for l in st["lines"] if l["price"] is not None)
    lev = fee_summary(r, collected)["levied"]
    return max(0, ps["paid"] - lev)


def sync_refunds(con, rids=None, progress=None) -> dict:
    """환불 필요(취소 뒤 납부 남음)·과납 건에 자동 환불 건(신청 전)을 만들고, 해소된 자동 건은 '해소'로 닫는다.
    rids: 바뀐 접수만(쓰기 경로의 트랜잭션 끝에서 자동). None = 전체 점검(야간 예약 작업·관리자 단추에서만)."""
    if C.is_readonly(con):
        return {"created": 0, "closed": 0}
    made = closed = 0
    if rids is None:
        rids = sorted({x["request_id"] for x in C.rows(con, "SELECT DISTINCT request_id FROM payment WHERE void_reason IS NULL")}
                      | {x["request_id"] for x in C.rows(con, "SELECT DISTINCT request_id FROM refund_case WHERE status IN ('DRAFT','REQUESTED','APPROVED')")})
    else:
        rids = [r for r in dict.fromkeys(rids) if r]
    if not rids:
        return {"created": 0, "closed": 0}
    with C.tx(con):
        for ch in C._chunks(rids):
            q = ",".join("?" * len(ch))
            open_by: dict[str, list] = {}
            for c in C.rows(con, f"SELECT * FROM refund_case WHERE status IN ('DRAFT','REQUESTED','APPROVED') AND request_id IN ({q})", ch):
                open_by.setdefault(c["request_id"], []).append(c)
            has_pay = {x["request_id"] for x in C.rows(con, f"SELECT DISTINCT request_id FROM payment WHERE request_id IN ({q})", ch)}
            todo = [r for r in ch if r in has_pay or r in open_by]
            if not todo:
                continue
            reqs = {r["id"]: r for r in C.rows(con, f"SELECT * FROM request WHERE id IN ({','.join('?' * len(todo))})", todo)}
            pre = D.prefetch(con, list(reqs), ("items", "events", "links", "payments", "partial"))
            for rid, r in reqs.items():
                ps = D.payment_core(r, D.derive(con, r, pre)["fee"]["final"], pre["payments"][rid], pre["partial"][rid])
                need, cause = 0, None
                if ps["status"] == "REFUND_NEEDED":
                    need, cause = ps["paid"], "CANCELLED"
                elif ps["status"] == "OVERPAID_REVIEW":
                    need, cause = ps["paid"] - ps["levied"], "OVERPAID"
                cases = open_by.get(rid, [])
                autos = [c for c in cases if c["auto"] and c["status"] == "DRAFT"]
                if need > 0 and not cases:
                    con.execute("INSERT INTO refund_case (id, request_id, cause, amount, status, auto, created_at) VALUES (?,?,?,?,'DRAFT',1,?)",
                                (str(uuid.uuid4()), rid, cause, need, C.now()))
                    made += 1
                elif need > 0:
                    for c in autos:
                        if c["amount"] != need or c["cause"] != cause:
                            con.execute("UPDATE refund_case SET amount=?, cause=? WHERE id=?", (need, cause, c["id"]))
                elif need <= 0:
                    for c in autos:
                        con.execute("UPDATE refund_case SET status='VOID', void_reason='환불 필요 해소(수납 기록 변경)' WHERE id=?", (c["id"],))
                        closed += 1
            if progress:
                progress(len(ch))
    return {"created": made, "closed": closed}


def partial_candidates(con) -> list[dict]:
    out = []
    for r in C.rows(con, "SELECT * FROM request WHERE status='PARTIAL' AND EXISTS (SELECT 1 FROM payment p WHERE p.request_id=request.id AND p.void_reason IS NULL)"):
        if C.one(con, "SELECT 1 AS x FROM refund_case WHERE request_id=? AND status IN ('DRAFT','REQUESTED','APPROVED')", (r["id"],)):
            continue
        ps = D.payment_status(con, r["id"])
        due = _partial_due(con, r, ps)
        if due > 0:
            out.append({"request_id": r["id"], "receipt_no": r["receipt_no"], "levy_no": r["levy_no"], "dong": r["dong"], "paid": ps["paid"], "levied": ps["levied"], "due": due})
    return out


def list_refunds(con, status: str = "") -> dict:
    sql = """SELECT f.*, r.receipt_no, r.levy_no, r.dong, r.status AS request_status FROM refund_case f JOIN request r ON r.id=f.request_id"""
    args: list = []
    if status == "OPEN":
        sql += " WHERE f.status IN ('DRAFT','REQUESTED','APPROVED')"
    elif status:
        sql += " WHERE f.status=?"; args.append(status)
    out = C.rows(con, sql + " ORDER BY f.created_at DESC LIMIT 1000", args)
    counts = {k: 0 for k in REFUND_STATUS}
    for c in C.rows(con, "SELECT status, COUNT(*) AS n FROM refund_case GROUP BY status"):
        counts[c["status"]] = c["n"]
    return {"rows": out, "counts": counts, "partial": partial_candidates(con)}


def request_refund(con, data: dict) -> str:
    """환불 신청: 자동 건(case_id)을 신청하거나, 접수에 새로 신청(일부 수거 차액·기타)."""
    note = (data.get("note") or "").strip()[:200]
    if data.get("case_id"):
        c = C.one(con, "SELECT * FROM refund_case WHERE id=?", (data["case_id"],))
        if not c or c["status"] != "DRAFT":
            raise C.UserError("신청할 환불 건이 없습니다.")
        amt = int(data.get("amount") or c["amount"])
        if not 0 < amt <= c["amount"]:
            raise C.UserError(f"환불 금액은 1원 이상 {c['amount']:,}원 이하입니다.")
        with C.tx(con):
            con.execute("UPDATE refund_case SET status='REQUESTED', amount=?, requested_by=?, requested_at=?, note=? WHERE id=?",
                        (amt, C.ACTOR.get(), C.now(), note, c["id"]))
            C.audit(con, "refund", c["id"], "REQUEST", {"request": c["request_id"], "amount": amt, "cause": c["cause"]})
        return c["id"]
    rid = data.get("request_id") or ""
    r = C.one(con, "SELECT * FROM request WHERE id=?", (rid,))
    if not r:
        raise C.UserError("접수를 찾을 수 없습니다.")
    cause = data.get("cause") or "MANUAL"
    if cause not in ("PARTIAL", "MANUAL"):
        raise C.UserError("환불 사유를 골라 주세요.")
    if C.one(con, "SELECT 1 AS x FROM refund_case WHERE request_id=? AND status IN ('DRAFT','REQUESTED','APPROVED')", (rid,)):
        raise C.UserError("이미 진행 중인 환불 건이 있습니다. 환불 목록에서 확인하세요.")
    ps = D.payment_status(con, rid)
    try:
        amt = int(data.get("amount") or 0)
    except (TypeError, ValueError):
        amt = 0
    if amt <= 0 or amt > ps["paid"]:
        raise C.UserError(f"환불 금액은 1원 이상, 납부 확인 금액({ps['paid']:,}원) 이하로 적어 주세요.")
    if cause == "MANUAL" and not note:
        raise C.UserError("환불 사유(메모)를 적어 주세요.")
    cid = str(uuid.uuid4())
    with C.tx(con):
        con.execute("""INSERT INTO refund_case (id, request_id, cause, amount, status, auto, created_at, requested_by, requested_at, note)
            VALUES (?,?,?,?,'REQUESTED',0,?,?,?,?)""", (cid, rid, cause, amt, C.now(), C.ACTOR.get(), C.now(), note))
        C.audit(con, "refund", cid, "REQUEST", {"request": rid, "amount": amt, "cause": cause})
    return cid


def approve_refund(con, cid: str, approve: bool, note: str = ""):
    c = C.one(con, "SELECT * FROM refund_case WHERE id=?", (cid,))
    if not c or c["status"] != "REQUESTED":
        raise C.UserError("결재할 환불 신청이 없습니다.")
    if c["requested_by"] == C.ACTOR.get():
        raise C.UserError("신청한 사람은 결재할 수 없습니다. 팀장(검토자)·관리자 중 다른 사람이 결재하세요.")
    if not approve and not (note or "").strip():
        raise C.UserError("반려 사유를 적어 주세요.")
    with C.tx(con):
        con.execute("UPDATE refund_case SET status=?, approved_by=?, approved_at=?, approve_note=? WHERE id=?",
                    ("APPROVED" if approve else "REJECTED", C.ACTOR.get(), C.now(), (note or "").strip()[:200], cid))
        C.audit(con, "refund", cid, "APPROVE" if approve else "REJECT", {"request": c["request_id"], "amount": c["amount"]})


def pay_refund(con, cid: str, data: dict) -> dict:
    """지급 기록: 지급일·방법. 계좌번호는 받지 않는다(계좌 확인 여부와 끝 4자리만)."""
    c = C.one(con, "SELECT * FROM refund_case WHERE id=?", (cid,))
    if not c or c["status"] != "APPROVED":
        raise C.UserError("결재가 끝난 환불 건만 지급 기록을 남길 수 있습니다.")
    paid_on = data.get("paid_on") or C.today_kst()
    method = data.get("method") or ""
    if not re.fullmatch(r"\d{4}-\d\d-\d\d", paid_on):
        raise C.UserError("지급일을 날짜로 골라 주세요.")
    if method not in REFUND_METHODS:
        raise C.UserError("지급 방법을 골라 주세요.")
    checked = bool(data.get("account_checked"))
    last4 = (data.get("account_last4") or "").strip()
    if last4 and not re.fullmatch(r"\d{4}", last4):
        raise C.UserError("계좌 끝 4자리만 숫자로 적어 주세요 (전체 계좌번호는 저장하지 않습니다).")
    if method == "계좌 이체" and not checked:
        raise C.UserError("계좌 이체는 예금주·계좌를 확인했는지 '계좌 확인함'을 표시해 주세요.")
    left = D.payment_status(con, c["request_id"])["paid"]
    if c["amount"] > left:  # 결재 뒤 원 납부가 정정됐을 수 있다: 남은 납부액보다 많이 돌려주지 않는다
        raise C.UserError(f"남은 납부 확인 금액({left:,}원)이 결재된 환불액({c['amount']:,}원)보다 적습니다(수납 기록이 바뀜). 이 환불 건을 취소하고 다시 신청하세요.")
    pid = str(uuid.uuid4())
    with C.tx(con):
        con.execute("""INSERT INTO payment (id, request_id, kind, amount, method, paid_on, note, recorded_at, source) VALUES (?,?,?,?,?,?,?,?,'환불 결재')""",
                    (pid, c["request_id"], "REFUND", c["amount"], method, paid_on, "환불 결재 지급", C.now()))
        con.execute("""UPDATE refund_case SET status='PAID', paid_on=?, pay_method=?, account_checked=?, account_last4=?, paid_by=?, paid_at=?, payment_id=? WHERE id=?""",
                    (paid_on, method, 1 if checked else 0, last4 or None, C.ACTOR.get(), C.now(), pid, cid))
        C.audit(con, "refund", cid, "PAY", {"request": c["request_id"], "amount": c["amount"], "method": method, "account_checked": checked})
    return D.payment_status(con, c["request_id"])


def cancel_refund(con, cid: str, reason: str):
    c = C.one(con, "SELECT * FROM refund_case WHERE id=?", (cid,))
    if not c or c["status"] not in ("DRAFT", "REQUESTED", "APPROVED"):
        raise C.UserError("취소할 환불 건이 없습니다.")
    if not (reason or "").strip():
        raise C.UserError("취소 사유를 적어 주세요.")
    with C.tx(con):
        con.execute("UPDATE refund_case SET status='VOID', void_reason=? WHERE id=?", (reason.strip()[:200], cid))
        C.audit(con, "refund", cid, "VOID", {"request": c["request_id"], "reason": reason.strip()[:200]})


C.on_dirty("refunds", sync_refunds)
