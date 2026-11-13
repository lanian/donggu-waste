"""반영된 현장 결과의 확정 수거량과 실제 수거일별 증가분(주민 정보 없음)."""
from __future__ import annotations

import json
import datetime as dt

from dgpc import core as C
from dgpc.derive import FINAL


def event_order(events: list[dict]):
    """가져온 순서 안에서 같은 기기·세대의 늦은 낮은 순번을 먼저 한 번 재생한다."""
    streams, cursors = {}, {}
    for index, event in enumerate(events):
        key = (event["device_id"], event.get('gen') or "")
        streams.setdefault(key, []).append((event["device_seq"], index, event))
        cursors[key] = 0
    for stream in streams.values():
        stream.sort(key=lambda entry: entry[:2])
    for index, event in enumerate(events):
        key = (event["device_id"], event.get('gen') or "")
        stream = streams[key]
        while cursors[key] < len(stream) and stream[cursors[key]][:2] <= (event["device_seq"], index):
            entry = stream[cursors[key]]
            cursors[key] += 1
            yield entry[2]


def _price(value):
    if isinstance(value, bool):
        return None
    try:
        price = int(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return price if 0 <= price <= 10**9 else None


def event_time(event):
    """기기 시각은 가져온 시점 +10분 안인지 고정 검증한다. 시간이 흘러도 수거일이 바뀌지 않는다."""
    def parse(value):
        if not isinstance(value, str):
            return None
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo is not None and parsed.year >= 2020 else None
        except (ValueError, OverflowError):
            return None
    received = parse(event.get("imported_at"))
    if received is None:
        return None
    device = parse(event.get("device_ts"))
    try:
        good = device is not None and device <= received + dt.timedelta(minutes=10)
    except OverflowError:
        good = False
    return (device if good else received).strftime("%Y-%m-%dT%H:%M:%SZ")


def trace_pure(items: list[dict], events: list[dict], links: list[dict] | None = None) -> dict:
    """누적 ITEM_RESULT를 ATTEMPT에서 확정해 수거일별 구간으로 바꾼다.

    구간의 start/end는 품목 안에서의 0부터 시작하는 수거 순서다. 감소 정정은
    마지막 구간부터 줄이며, 다음 증가분은 새 수거일에 남긴다. 수량만큼 배열을 만들지 않는다.
    """
    by = {i["line_id"]: {"line_id": i["line_id"], "kind": "RECEIVED", "name": i["name"], "spec": i.get("spec") or "",
                         "unit": i.get("unit") or "EA", "unit_label": i.get("unit_label") or "개",
                         "fee_rate_id": i.get("fee_rate_id"), "price": _price(i.get("unit_price")) if i.get("price_status") == "CONFIRMED" else None,
                         "qty_collected": 0} for i in items}
    pending = {line: 0 for line in by}
    segments = {line: [] for line in by}
    retirements, source_orders = [], {}
    for ordinal, event in enumerate(event_order(events)):
        try:
            body = json.loads(event["body_json"])
        except (ValueError, TypeError):
            continue
        if not isinstance(body, dict):
            continue
        kind, line = event["kind"], body.get("line_id")
        if kind == "FIELD_ITEM_ADD" and isinstance(line, str) and line and line not in by:
            by[line] = {"line_id": line, "kind": "FIELD", "name": str(body.get("name") or "현장 추가 품목")[:60],
                        "spec": str(body.get("spec") or "")[:60], "unit": str(body.get("unit") or "EA")[:10],
                        "unit_label": str(body.get("unit_label") or "개")[:10], "fee_rate_id": body.get("fee_rate_id"),
                        "price": _price(body.get("unit_price")) if body.get("price_status") == "CONFIRMED" else None, "qty_collected": 0}
            pending[line], segments[line] = 0, []
        elif kind == "ITEM_RESULT" and isinstance(line, str) and line in by:
            pending[line] = min(C._int_clamp(body.get("qty_confirmed")), C._int_clamp(body.get("qty_collected")))
        elif kind == "ATTEMPT" and isinstance(body.get("outcome"), str) and body["outcome"] in FINAL:
            at = event_time(event)
            source_orders[event["event_id"]] = ordinal
            for lid, metadata in by.items():
                before, after = metadata["qty_collected"], pending[lid]
                if after > before:
                    segments[lid].append({"line_id": lid, "start": before, "end": after, "qty": after - before,
                                          "at": at, "date": C._kst_date(at), "source_event": event["event_id"], "order": ordinal})
                elif after < before:
                    retirements.append({"line_id": lid, "start": after, "end": before, "order": ordinal,
                                        "source_event": event["event_id"], "at": at})
                    while segments[lid] and segments[lid][-1]["start"] >= after:
                        segments[lid].pop()
                    if segments[lid] and segments[lid][-1]["end"] > after:
                        last = segments[lid][-1]
                        last["end"], last["qty"] = after, after - last["start"]
                metadata["qty_collected"] = after
    for link in links or []:
        line = by.get(link["line_id"])
        if line and line["price"] is None:
            line["price"] = _price(link["unit_price"])
            line["fee_rate_id"] = link.get("fee_rate_id")
    flat = sorted((s for group in segments.values() for s in group), key=lambda s: (s["at"] or "9999", s["order"], s["line_id"], s["start"]))
    return {"lines": list(by.values()), "segments": flat, "total": sum(line["qty_collected"] for line in by.values()),
            "retirements": retirements, "source_orders": source_orders}


def read_traces(con, reqs: list[dict]) -> dict:
    """필요한 품목·적용된 수거 기록·단가 연결을 묶어서 읽는다. 접수의 주민 정보는 읽지 않는다."""
    out = {}
    for chunk in C._chunks(reqs, 500):
        ids = [r["id"] for r in chunk]
        marks = ",".join("?" for _ in ids)
        items, events, links = ({rid: [] for rid in ids} for _ in range(3))
        for item in C.rows(con, f"SELECT * FROM request_item WHERE request_id IN ({marks}) ORDER BY rowid", ids):
            items[item.pop("request_id")].append(item)
        for event in C.rows(con, f"""SELECT e.request_id,e.event_id,e.kind,e.body_json,e.device_ts,e.device_id,e.gen,e.device_seq,i.imported_at
                FROM result_event e JOIN result_import i ON i.id=e.import_id
                WHERE e.request_id IN ({marks}) AND e.apply_status='APPLIED' AND e.kind IN ('FIELD_ITEM_ADD','ITEM_RESULT','ATTEMPT')
                ORDER BY i.imported_at,i.rowid,e.device_seq,e.rowid""", ids):
            events[event.pop("request_id")].append(event)
        for link in C.rows(con, f"SELECT request_id,line_id,unit_price,fee_rate_id FROM item_link WHERE request_id IN ({marks}) AND void_reason IS NULL ORDER BY at", ids):
            links[link.pop("request_id")].append(link)
        for rid in ids:
            out[rid] = trace_pure(items[rid], events[rid], links[rid])
    return out
