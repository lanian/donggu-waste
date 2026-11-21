"""행정동별 실물 배출표를 확정 수거 단위에 한 번만 배정한다."""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
import heapq
from itertools import accumulate
import re

from dgpc import core as C
from dgpc.collection import read_traces


_ISSUE_PAGE_SIZE = 50


def _requests(con, request_ids=None, dong_id=None):
    where, args = [], []
    if request_ids is not None:
        ids = list(dict.fromkeys(request_ids))
        if not ids:
            return []
        import json
        where.append("r.id IN (SELECT value FROM json_each(?))")
        args.append(json.dumps(ids))
    else:
        where.append("(EXISTS (SELECT 1 FROM result_event e WHERE e.request_id=r.id AND e.apply_status='APPLIED' AND e.kind='ATTEMPT') OR EXISTS (SELECT 1 FROM numbered_label_allocation a WHERE a.request_id=r.id))")
    if dong_id:
        # 범위 조회/재배정은 현재 담당 동의 수거만 대상으로 한다. 이전 동의 배정 이력이
        # 있어도 다른 동으로 옮긴 접수의 부족분을 이 작업에서 배정하면 안 된다.
        where.append("r.dong=(SELECT name FROM dong WHERE id=?)")
        args.append(dong_id)
    return C.rows(con, "SELECT r.id,r.receipt_no,r.dong FROM request r WHERE " + " AND ".join(where), args)


def _source(con, request_ids=None, dong_id=None):
    reqs = _requests(con, request_ids, dong_id)
    traces = read_traces(con, reqs)
    dongs = {d["name"]: d["id"] for d in C.rows(con, "SELECT id,name FROM dong")}
    segments, by_line = [], {}
    for req in reqs:
        trace = traces[req["id"]]
        kinds = {line["line_id"]: line["kind"] for line in trace["lines"]}
        for segment in trace["segments"]:
            entry = {**segment, "request_id": req["id"], "receipt_no": req["receipt_no"], "dong_id": dongs.get(req["dong"]),
                     "dong_name": req["dong"] or "", "kind": kinds[segment["line_id"]]}
            segments.append(entry)
            by_line.setdefault((req["id"], segment["line_id"]), []).append(entry)
    segments.sort(key=lambda s: (s["at"] or "9999", s["receipt_no"], s["order"], s["line_id"], s["start"]))
    return reqs, traces, segments, by_line


def allocation_rows(con, ids):
    out = []
    for chunk in C._chunks(ids):
        out.extend(C.rows(con, f"""SELECT a.*,l.status,l.stock_dong_id,l.distributed_at,l.received_at,l.number,l.used_at,l.voided_at AS label_voided_at
            FROM numbered_label_allocation a JOIN numbered_label l ON l.number_key=a.number_key
            WHERE a.request_id IN ({','.join('?' for _ in chunk)})""", chunk))
    return out


def _in_day(label):
    days = [C._kst_date(label.get(key)) for key in ("received_at", "distributed_at")]
    return max((day for day in days if day and C._date_ok(day)), default="9999-12-31")


def _slot(by_line, rid, line, unit):
    return next((s for s in by_line.get((rid, line), []) if s["start"] <= unit < s["end"]), None)


def _matches(allocation, segment):
    return (allocation["status"] == "USED" and allocation["dong_id"] == segment["dong_id"]
            and allocation["stock_dong_id"] == segment["dong_id"] and allocation["collection_at"] == segment["at"]
            and allocation["source_event"] == segment["source_event"] and bool(segment["date"]) and _in_day(allocation) <= segment["date"])


def _retired(allocation, trace):
    source_order = trace["source_orders"].get(allocation["source_event"], -1)
    return any(r["line_id"] == allocation["line_id"] and r["start"] <= allocation["unit_index"] < r["end"] and r["order"] > source_order
               for r in trace["retirements"])


def _counts(segments, active, by_line, *, by_segment=None):
    required = sum(s["qty"] for s in segments)
    assigned = manual_excess = conflicts = 0
    for (rid, line, unit), allocation in active.items():
        segment = _slot(by_line, rid, line, unit)
        if segment is None:
            if not allocation["automatic"] and allocation["status"] == "USED":
                manual_excess += 1
            continue
        if _matches(allocation, segment):
            assigned += 1
            category = "assigned"
        else:
            conflicts += 1
            category = "conflicts"
        if by_segment is not None:
            by_segment[(rid, line, segment["start"])][category] += 1
    return {"required": required, "assigned": assigned, "pending": required - assigned,
            "manual_excess": manual_excess, "conflicts": conflicts}


def _unmatched_manual(con, reqs):
    count = 0
    for chunk in C._chunks([r["id"] for r in reqs]):
        count += con.execute(f"""SELECT COUNT(*) FROM numbered_label l LEFT JOIN numbered_label_allocation a ON a.number_key=l.number_key
            WHERE l.status='USED' AND a.number_key IS NULL AND l.request_id IN ({','.join('?' for _ in chunk)})""", chunk).fetchone()[0]
    return count


def _insert(con, label, segment, unit, automatic):
    at = C.now()
    con.execute("""INSERT INTO numbered_label_allocation
        (number_key,request_id,line_id,unit_index,dong_id,dong_name,collection_at,source_event,automatic,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (label["number_key"], segment["request_id"], segment["line_id"], unit, segment["dong_id"], segment["dong_name"],
         segment["at"], segment["source_event"], int(automatic), at, at))
    return {**label, "request_id": segment["request_id"], "line_id": segment["line_id"], "unit_index": unit,
            "dong_id": segment["dong_id"], "dong_name": segment["dong_name"], "collection_at": segment["at"],
            "source_event": segment["source_event"], "automatic": int(automatic), "voided_at": None}


def sync(con, request_ids=None, *, dong_id=None):
    """쓰기 트랜잭션에서 재고·배정을 함께 맞춘다. 부족해도 수거 결과는 반영한다."""
    from dgpc import numbered_labels as NL
    NL._writable(con)
    changes = {"new_assigned": 0, "adopted": 0, "voided": 0, "updated": 0}
    with C.tx(con):
        reqs, traces, segments, by_line = _source(con, request_ids, dong_id)
        active = {}
        for allocation in allocation_rows(con, [r["id"] for r in reqs]):
            if allocation["voided_at"]:
                continue
            key = (allocation["request_id"], allocation["line_id"], allocation["unit_index"])
            segment = _slot(by_line, *key)
            retired = _retired(allocation, traces[allocation["request_id"]])
            if allocation["status"] == "VOID" or ((not segment or retired) and allocation["automatic"]):
                at = C.now()
                if allocation["status"] != "VOID":
                    label = NL._label(con, allocation["number_key"])
                    con.execute("UPDATE numbered_label SET status='VOID',voided_at=?,updated_at=? WHERE number_key=?", (at, at, label["number_key"]))
                    NL._record(con, {**label, "status": "VOID"}, "VOID", label["status"], at,
                               "확정 수거 수량 감소로 초과 자동 배정 번호 무효", extra={"automatic": True}, event_line_id=allocation["line_id"])
                    changes["voided"] += 1
                con.execute("UPDATE numbered_label_allocation SET voided_at=?,updated_at=? WHERE number_key=?",
                            (allocation["label_voided_at"] or at, at, allocation["number_key"]))
                continue
            if segment and not retired and segment["date"] and allocation["dong_id"] == segment["dong_id"] == allocation["stock_dong_id"] and _in_day(allocation) <= segment["date"]:
                if (allocation["collection_at"], allocation["source_event"]) != (segment["at"], segment["source_event"]):
                    C.audit(con, "label_allocation", allocation["number_key"], "RECONCILE", {"request": allocation["request_id"], "line": allocation["line_id"],
                            "from_at": allocation["collection_at"], "to_at": segment["at"], "source_event": segment["source_event"]})
                    con.execute("UPDATE numbered_label_allocation SET collection_at=?,source_event=?,updated_at=? WHERE number_key=?",
                                (segment["at"], segment["source_event"], C.now(), allocation["number_key"]))
                    if allocation["automatic"]:
                        con.execute("UPDATE numbered_label SET used_at=?,updated_at=? WHERE number_key=?", (segment["at"], C.now(), allocation["number_key"]))
                    allocation.update(collection_at=segment["at"], source_event=segment["source_event"])
                    changes["updated"] += 1
            active[key] = allocation
        # 이미 수동 연결한 번호도 수거 1단위에 한 번만 대응시킨다. 원래 line_id/사용일시는 바꾸지 않는다.
        for chunk in C._chunks([r["id"] for r in reqs]):
            labels = C.rows(con, f"""SELECT l.* FROM numbered_label l LEFT JOIN numbered_label_allocation a ON a.number_key=l.number_key
                WHERE l.request_id IN ({','.join('?' for _ in chunk)}) AND l.status='USED' AND a.number_key IS NULL
                ORDER BY l.used_at,length(l.number_key),l.number_key""", chunk)
            for label in labels:
                for segment in segments:
                    if segment["request_id"] != label["request_id"] or (label["line_id"] and label["line_id"] != segment["line_id"]):
                        continue
                    if not segment["dong_id"] or not segment["at"]:
                        continue
                    unit = next((i for i in range(segment["start"], segment["end"]) if (segment["request_id"], segment["line_id"], i) not in active), None)
                    if unit is None:
                        continue
                    allocation = _insert(con, label, segment, unit, False)
                    active[(segment["request_id"], segment["line_id"], unit)] = allocation
                    C.audit(con, "label_allocation", label["number_key"], "LINK_MANUAL", {"request": segment["request_id"], "line": segment["line_id"], "unit": unit})
                    changes["adopted"] += 1
                    break
        pools = {}
        for pool_id in {s["dong_id"] for s in segments if s["dong_id"]}:
            stock = C.rows(con, "SELECT * FROM numbered_label WHERE stock_dong_id=? AND status='DISTRIBUTED'", (pool_id,))
            stock.sort(key=lambda label: (_in_day(label), len(label["number_key"]), label["number_key"]))
            pools[pool_id] = {"stock": stock, "cursor": 0, "ready": []}
        for segment in segments:
            if not segment["dong_id"] or not segment["date"]:
                continue
            pool = pools[segment["dong_id"]]
            while pool["cursor"] < len(pool["stock"]) and _in_day(pool["stock"][pool["cursor"]]) <= segment["date"]:
                label = pool["stock"][pool["cursor"]]
                heapq.heappush(pool["ready"], (len(label["number_key"]), label["number_key"], label))
                pool["cursor"] += 1
            for unit in range(segment["start"], segment["end"]):
                key = (segment["request_id"], segment["line_id"], unit)
                if key in active:
                    continue
                if not pool["ready"]:
                    break
                _, _, label = heapq.heappop(pool["ready"])
                at = C.now()
                base_line = segment["line_id"] if segment["kind"] == "RECEIVED" else None
                con.execute("UPDATE numbered_label SET status='USED',request_id=?,line_id=?,used_at=?,updated_at=? WHERE number_key=?",
                            (segment["request_id"], base_line, segment["at"], at, label["number_key"]))
                used = {**label, "status": "USED", "request_id": segment["request_id"], "line_id": base_line, "used_at": segment["at"]}
                NL._record(con, used, "USE", label["status"], segment["at"], extra={"automatic": True, "unit": unit,
                           "stock_dong_id": segment["dong_id"], "source_event": segment["source_event"]}, event_line_id=segment["line_id"])
                active[key] = _insert(con, used, segment, unit, True)
                changes["new_assigned"] += 1
        counts = _counts(segments, active, by_line)
        counts["manual_excess"] += _unmatched_manual(con, reqs)
        return {**changes, **counts}


def _stock_timeline(con, dong_id):
    """배정 가능한 실물 번호를 동과 실제 수령·배부일별 누계로 읽는다."""
    by_dong = defaultdict(Counter)
    for stock in C.rows(con, """SELECT stock_dong_id,received_at,distributed_at,COUNT(*) AS qty
            FROM numbered_label WHERE status='DISTRIBUTED' AND stock_dong_id IS NOT NULL
            AND (? IS NULL OR stock_dong_id=?) GROUP BY stock_dong_id,received_at,distributed_at""", (dong_id, dong_id)):
        by_dong[stock["stock_dong_id"]][_in_day(stock)] += stock["qty"]
    timeline = {}
    for dong, dated in by_dong.items():
        days = sorted(dated)
        timeline[dong] = days, [0, *accumulate(dated[day] for day in days)]
    return timeline


def _issue_page(con, segments, coverage, page, dong_id):
    timeline = _stock_timeline(con, dong_id)
    grouped, claimed, shortages = {}, Counter(), Counter()
    # sync와 같은 수거순으로 남은 재고만 대조한다. 조회는 번호를 예약하거나 소모하지 않는다.
    for s in segments:
        counts = coverage.get((s["request_id"], s["line_id"], s["start"]), {})
        pending = s["qty"] - counts.get("assigned", 0)
        if not pending:
            continue
        day, dong = s["date"], s["dong_id"]
        key = (s["request_id"], day)
        if key not in grouped:
            grouped[key] = {"request_id": s["request_id"], "receipt_no": s["receipt_no"], "dong_name": s["dong_name"],
                            "date": day, "pending": 0, "reasons": set()}
        row = grouped[key]
        row["pending"] += pending
        reasons = row["reasons"]
        conflicts = counts.get("conflicts", 0)
        if conflicts:
            reasons.add("CONFLICT")
        if not dong:
            reasons.add("MISSING_DONG")
        if not day:
            reasons.add("MISSING_DATE")
        unlinked = pending - conflicts
        if not day or not dong or not unlinked:
            continue
        days, cumulative = timeline.get(dong, ([], [0]))
        eligible = cumulative[bisect_right(days, day)]
        available = min(unlinked, eligible - claimed[dong])
        if available:
            reasons.add("READY")
            claimed[dong] += available
        shortage = unlinked - available
        if shortage:
            shortages[key] += shortage
            later = cumulative[-1] - eligible
            if later:
                reasons.add("LATE_STOCK")
            if shortages[key] > later:
                reasons.add("NO_STOCK")
    rows = sorted(grouped.values(), key=lambda r: (r["date"] or "9999", r["receipt_no"], r["request_id"]))
    pages = max(1, (len(rows) + _ISSUE_PAGE_SIZE - 1) // _ISSUE_PAGE_SIZE)
    page = min(page, pages)
    selected = rows[(page - 1) * _ISSUE_PAGE_SIZE:page * _ISSUE_PAGE_SIZE]
    for row in selected:
        row["reasons"] = sorted(row["reasons"])
    return {"rows": selected, "total": len(rows), "page": page, "pages": pages}


def summary(con, dong_id=None, *, issues_page=None):
    """번호를 바꾸지 않고 확정 수량·배정·부족과 현재 동별 재고를 조회한다."""
    if issues_page is not None and not re.fullmatch(r"[1-9][0-9]{0,8}", str(issues_page)):
        raise C.UserError("미배정 목록의 쪽 번호를 확인해 주세요.")
    # 읽기 저장점은 입고·재배정의 중간 결과가 집계와 상세에 섞이지 않게 한다.
    # 이미 열린 호출자 트랜잭션은 이 저장점을 닫은 뒤에도 그대로 유지한다.
    con.execute("SAVEPOINT label_summary")
    try:
        return _summary(con, dong_id, issues_page)
    finally:
        con.execute("RELEASE label_summary")


def _summary(con, dong_id, issues_page):
    reqs, _, segments, by_line = _source(con, dong_id=dong_id)
    active = {(a["request_id"], a["line_id"], a["unit_index"]): a for a in allocation_rows(con, [r["id"] for r in reqs]) if not a["voided_at"]}
    stocks = C.rows(con, """SELECT d.id,d.name,d.sort,
        SUM(CASE WHEN l.status='DISTRIBUTED' THEN 1 ELSE 0 END) AS available,
        SUM(CASE WHEN l.status='USED' THEN 1 ELSE 0 END) AS used,
        SUM(CASE WHEN l.status='VOID' THEN 1 ELSE 0 END) AS voided
        FROM dong d LEFT JOIN numbered_label l ON l.stock_dong_id=d.id
        WHERE (? IS NULL OR d.id=?) GROUP BY d.id ORDER BY d.sort,d.name""", (dong_id, dong_id))
    coverage = defaultdict(Counter) if issues_page is not None else None
    counts = _counts(segments, active, by_line, by_segment=coverage)
    counts["manual_excess"] += _unmatched_manual(con, reqs)
    if coverage is not None:
        counts["issues"] = (_issue_page(con, segments, coverage, int(issues_page), dong_id) if counts["pending"]
                            else {"rows": [], "total": 0, "page": 1, "pages": 1})
    return {**counts, "dongs": stocks}
