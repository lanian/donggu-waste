"""보고서·번호표의 기본 인쇄 본문. 기관 제출 서식은 집계와 별도로 조정한다."""
from html import escape


def _text(value):
    return escape("—" if value is None else str(value))


def _table(title, headers, rows):
    values = list(rows)
    head = "".join(f"<th>{_text(value)}</th>" for value in headers)
    body = "".join("<tr>" + "".join(f"<td>{_text(value)}</td>" for value in row) + "</tr>" for row in values)
    if not values:
        body = f'<tr><td colspan="{len(headers)}">기록 없음</td></tr>'
    return f"<h2>{_text(title)}</h2><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def report_body(report):
    title = "일일 작업일지" if report["kind"] == "daily" else "대형폐기물 수거 월보"
    t = report["totals"]
    body = (f'<section class="report"><h1>{title}</h1><p>기간 {_text(report["start"])} ~ {_text(report["end"])}'
            f' · 기준 {_text(report["basis_label"])} · 생성 {_text(report["generated_at"])} (한국시간)</p>'
            f'<p>{_text(report["source_note"])}</p>')
    summary = [("선택 기준 접수", t["requests"]), ("기간 내 접수(접수일)", t["received"]),
               ("계산 금액(원)", t["calculated"]), ("감면액(원)", t["reduction"]),
               ("부과 금액(원)", t["levied"]), ("접수별 누적 납부 기록(원)", t["paid"]),
               ("금액 확인 대기", t["amount_pending"]), ("미납·부족", t["unpaid"])]
    body += _table("요약", ("항목", "집계"), summary)
    from .reports import STATUS_LABEL
    body += _table("현재 상태", ("상태", "건수"), ((STATUS_LABEL.get(key, key), value) for key, value in t["by_status"].items()))
    body += _table("일자별", ("일자", "접수", "선택 기준 건수", "완료", "일부", "미수거", "취소"),
                   ([row.get(key) for key in ("date", "received", "planned", "done", "partial", "not_collected", "cancelled")] for row in report["days"]))
    body += _table("팀별", ("팀", "배정", "완료", "재방문·보류"),
                   ([row.get(key) for key in ("crew", "assigned", "done", "partial_or_not")] for row in report["crews"]))
    body += _table("행정동별", ("동", "건수", "완료", "재방문·보류", "취소", "부과 금액(원)"),
                   ([row.get(key) for key in ("dong", "planned", "done", "revisit", "cancelled", "levied")] for row in report["dongs"]))
    body += _table("품목별(현재 누적 수량)", ("품목", "규격", "단위", "확인 수량", "수거 수량"),
                   ([row.get(key) for key in ("name", "spec", "unit_label", "requested", "collected")] for row in report["items"]))
    body += _table("품목 분류별", ("분류", "행 수", "확인 수량", "수거 수량", "계산 금액(원)"),
                   ([row.get(key) for key in ("category", "lines", "requested", "collected", "amount")] for row in report["categories"]))
    body += _table("접수 경로별", ("접수 경로", "선택 기준 건수", "수거 완료", "취소"),
                   ([row.get(key) for key in ("channel", "planned", "done", "cancelled")] for row in report["channels"]))
    body += _table("감면별", ("감면 사유", "감면율(%)", "건수", "감면액(원)"),
                   ([row.get(key) for key in ("label", "rate", "count", "amount")] for row in report["reductions"]))
    u = report["unload_summary"]
    body += _table("하차(작업일 기준)", ("횟수", "부피 입력 횟수", "중량 입력 횟수", "부피(m³)", "중량(kg)"),
                   [(u["count"], u["volume_records"], u["weight_records"], t["unload_m3"], t["unload_kg"])])
    revenue_fields = ("pay_n", "pay", "refund_n", "refund", "net")
    revenue_rows = [[row["method"], *(row[key] for key in revenue_fields)] for row in report["revenue"]["methods"]]
    revenue_rows.append(["합계", *(report["revenue"]["totals"][key] for key in revenue_fields)])
    body += _table("세입(납부일 기준)", ("방법", "납부 건수", "납부액(원)", "환불 건수", "환불액(원)", "순수입(원)"), revenue_rows)
    body += '<p class="foot">개인정보 없는 기본 집계 서식 · 제출 서식은 추후 조정합니다.</p></section>'
    return title, body


def numbered_label_body(label, qr):
    statuses = {"AVAILABLE": "미배부", "DISTRIBUTED": "배부", "USED": "사용", "VOID": "무효"}
    number = _text(label["number"])
    status = _text(statuses[label["status"]])
    return (f'<div class="sheet"><div class="label numbered-label"><div class="lh"><b class="rno">{number}</b><span>{status}</span></div>'
            f'<p class="lday">배출표 번호 {number}</p><img class="lqr" src="{_text(qr)}" alt="배출표 번호 QR">'
            '<div class="lnote">번호 확인용 · 인쇄만으로 사용 처리되지 않습니다.</div></div></div>')
