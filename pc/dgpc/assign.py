"""배정: 노선 확정·부분 수정·배정 목록·휴대폰 목록 묶음(서명·암호화)·수신 확인·배차표·하차·적재 점검.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor

import dgproto as P
from dgpc import privacy as _pv, route_depots as DP
from dgpc.core import KST, StaleError, UserError, _chunks, _date_ok, _int_clamp, _num, audit, get_setting, now, one, require_current_crew, rows, today_kst, tx
from dgpc.derive import FINAL, derive, request_round_unit
from dgpc.devices import list_crews, pc_key
from dgpc.fees import FEE_ROWS, fee_table_for_phone, volume_overrides

# ------------------------------------------------------------------ 배정
def confirm_plan(con, work_date: str, plan: dict[str, list[dict]], adjust_reason: str = "", adjusted: bool = False, *, histories=None) -> list[dict]:
    """plan = {crew_id: [{request_id, eta}, ...]} 순서대로. 팀별로 새 배정 버전을 만든다."""
    crews = {c["id"]: c for c in list_crews(con)}
    for cid in plan:
        if cid not in crews:
            raise UserError("없는 팀이 포함되어 있습니다.")
    if adjusted and not (adjust_reason or "").strip():
        raise UserError("추천 순서·팀을 바꿨습니다. 조정 사유를 적어 주세요 (예: 골목 진입 불가, 주민 요청 시간).")
    seen = set()
    for cid, stops in plan.items():
        for s in stops:
            if s["request_id"] in seen:
                raise UserError("같은 접수가 두 팀에 들어 있습니다.")
            seen.add(s["request_id"])
    replaced = {r["id"] for r in rows(con, "SELECT id FROM assignment_batch WHERE work_date=? AND status='ACTIVE' AND crew_id IN (%s)"
                                       % ",".join("?" * len(plan)), [work_date, *plan]) } if plan else set()
    for rid in seen:
        req = one(con, "SELECT status FROM request WHERE id=?", (rid,))
        if not req:
            raise UserError("없는 접수가 포함되어 있습니다.")
        if req["status"] in ("CANCELLED", "DONE"):
            raise UserError("취소·완료된 접수는 배정할 수 없습니다.")
        act = one(con, "SELECT batch_id FROM active_assignment WHERE request_id=?", (rid,))
        if act and act["batch_id"] not in replaced:
            other = one(con, "SELECT b.work_date, c.name FROM assignment_batch b JOIN crew c ON c.id=b.crew_id WHERE b.id=?", (act["batch_id"],))
            raise UserError(f"이미 다른 배정({other['work_date']} {other['name']})에 들어 있는 접수가 있습니다. 먼저 그 배정에서 빼 주세요.")
    out = []
    with tx(con):
        for cid, stops in plan.items():
            out.append(_write_batch(con, cid, work_date, stops,
                                    {"adjusted": True, "reason": adjust_reason.strip()[:200]} if adjusted else None,
                                    history=(histories or {}).get(cid)))
        _release_dropped(con, replaced)
    return out


def _clean_stop(x: dict) -> dict:
    u = x.get("unload_after")
    item = {"request_id": x["request_id"], "eta": x.get("eta") or ""}
    if isinstance(u, dict) and u.get("label"):
        item["unload_after"] = {"eta": str(u.get("eta") or "")[:5], "label": str(u["label"])[:40],
                                "minutes": _int_clamp(u.get("minutes"), 0, 600), "volume_m3": _num(u.get("volume_m3")) or 0.0}
        if u.get("final") is True:
            item["unload_after"]["final"] = True
    return item


def _last_final_unload(stops):
    last = (stops[-1].get("unload_after") if stops else None) or {}
    return last if last.get("final") is True else None


def _route_record(raw):
    stops = json.loads(raw or "[]")
    if isinstance(stops, dict):  # 남은 방문 0건의 반입·복귀 노선은 배정 헤더에만 있다.
        return [], stops.get("route_history")
    return stops, stops[0].get("route_history") if stops and isinstance(stops[0], dict) else None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _stop_digest(stops):
    return _digest([_clean_stop(s) for s in stops])


def _waypoints(depot, stops, end, *, via_key):
    sites = [depot]
    for stop in stops:
        sites.append(stop)
        if stop.get(via_key):
            sites.append(stop[via_key])
    sites.append(end)
    if any(not s or s.get('lat') is None or s.get('lon') is None for s in sites):
        return []
    return [[round(float(s['lon']), 6), round(float(s['lat']), 6)] for s in sites]


def route_histories(result, requests, road_basis, endpoint, road_status):
    """계산 중 로컬 도로 형상과 시간 기준을 함께 캡처한다. 호출자는 최신 입력을 다시 검사한다."""
    from dgpc import routing
    locations = {r['id']: {'lat': r.get('lat'), 'lon': r.get('lon')} for r in requests}
    source = result.get('matrix_source')
    routes = [r for r in result['routes'] if r['stops'] or r.get('final_unload')]

    def capture(route):
        stops = route['stops']
        unload_site = result.get('unload_site')
        visits = [{**locations[s['request_id']], 'via': unload_site if s.get('unload_after') else None} for s in stops]
        depot = route.get('depot')
        end = result.get('end_site') or route.get('garage') or depot
        detached_unload = route.get('final_unload')
        if detached_unload:
            detached_unload = {**(unload_site or {}), **detached_unload, 'depot': end}
            visits.append(detached_unload)
        points = _waypoints(depot, visits, end, via_key='via')
        path = {'source': 'ESTIMATE', 'reason': road_status}
        if endpoint and len(points) >= 2:
            if len(points) > 500:
                path['reason'] = 'TooBig'
            else:
                path = routing.road_path(routing.route_points(points), endpoint)
        if points and path.get('source') != 'OSRM':
            path['distance_km'] = round(sum(routing.haversine_km((ay, ax), (by, bx))
                                           for (ax, ay), (bx, by) in zip(points, points[1:])), 2)
        return route['crew_id'], {
            'stop_digest': _stop_digest(stops),
            'waypoint_digest': _digest([[s['request_id'] for s in stops], points]),
            'depot': depot, 'end': end, 'unload_site': unload_site, 'path': path,
            'road_basis': road_basis, 'matrix_source': source,
            'matrix_note': result.get('matrix_note', ''), 'start': route.get('start'), 'end_at': route.get('end'),
            'estimated_legs': route.get('estimated_legs', []),
            **({'final_unload': detached_unload} if detached_unload else {}),
            **({'initial_load': route['initial_load']} if 'initial_load' in route else {}),
        }

    if not routes:
        return {}
    with ThreadPoolExecutor(max_workers=min(4, len(routes))) as executor:
        histories = dict(executor.map(capture, routes))
    blockers = []
    for route in routes:
        cid = route['crew_id']
        path = histories[cid]['path']
        route['road_summary'] = {k: v for k, v in path.items() if k != 'geometry'}
        if path.get('reason') == 'NoRoute':
            blockers.append({'crew_id': cid, 'crew_name': route['crew_name'], 'message': NO_ROAD_CONNECTION})
    if blockers:
        result['route_blockers'] = blockers
    return histories


NO_ROAD_CONNECTION = '방문·경유·복귀 순서에 도로로 연결되지 않는 구간이 있습니다. 위치 또는 순서를 확인한 뒤 다시 계산하세요.'


def matching_histories(plan, histories):
    """서명된 계산안의 방문 순서·시간과 해당 경로 상태를 확정 전에 검증한다."""
    if histories is None:
        raise StaleError('확정에 필요한 도로 확인 정보가 없습니다. 노선을 다시 계산하세요.')
    if any(h['path'].get('reason') == 'NoRoute' for h in histories.values()):
        raise StaleError(NO_ROAD_CONNECTION, code='ROUTE_CONNECTION_BLOCKED')
    trips = {cid for cid, stops in plan.items() if stops or cid in histories}
    if trips != set(histories) or any(_stop_digest(plan[cid]) != h['stop_digest'] for cid, h in histories.items()):
        raise StaleError('계산한 방문 순서·도착 시각·하차 일정과 다릅니다. 노선을 다시 계산하세요.')
    return histories


def history_for_view(history, stops, waypoints, current_road):
    if not history:
        return None
    result = {k: history[k] for k in ('matrix_source', 'matrix_note', 'start', 'end_at')}
    basis = history['road_basis']
    result['road_revision'] = basis['revision']
    if not waypoints or history.get('waypoint_digest') != _digest([[s['request_id'] for s in stops], waypoints]):
        return {**result, 'state': 'VISITS_CHANGED'}
    result['estimated_legs'] = history.get('estimated_legs', [])
    if basis != current_road:
        state = 'ROAD_CHANGED'
    elif basis['configured'] or (basis['method'] == 'OSRM' and basis['revision'] in (None, 'base') and not basis.get('fingerprint')):
        state = 'UNVERIFIED'
    else:
        state = 'CURRENT'
    return {**result, 'state': state, 'path': history.get('path', {})}


def _write_batch(con, cid: str, work_date: str, stops: list[dict], extra: dict | None = None, action: str = "CONFIRM", *, history=None) -> dict:
    """팀의 그날 배정 새 버전(assign_ver = 지난 최대 + 1)을 만들고 이전 버전은 SUPERSEDED. 트랜잭션은 호출하는 쪽."""
    crew_name = require_current_crew(con, cid)["name"]
    _check_fixed_crew(con, cid, stops)
    prev = one(con, "SELECT MAX(assign_ver) AS v FROM assignment_batch WHERE crew_id=? AND work_date=?", (cid, work_date))["v"] or 0
    con.execute("UPDATE assignment_batch SET status='SUPERSEDED' WHERE crew_id=? AND work_date=? AND status='ACTIVE'", (cid, work_date))
    bid = str(uuid.uuid4())
    clean = [_clean_stop(x) for x in stops]
    for stop in clean[:-1]:
        if unload := stop.get("unload_after"):
            unload.pop("final", None)
    site = history.get('unload_site') if history else None
    if unload := _last_final_unload(clean):
        site = site or get_setting(con, "unload_site")
        if site:
            unload.update(lat=site["lat"], lon=site["lon"])
        depot = history.get('end') if history else None
        if not depot:
            requests = []
            for chunk in _chunks([stop["request_id"] for stop in clean]):
                marks = ",".join("?" for _ in chunk)
                requests.extend(rows(con, f"SELECT id,dong FROM request WHERE id IN ({marks})", chunk))
            depot = _team_depot(con, requests, {"id": cid, "name": crew_name})
        if depot:
            unload["depot"] = depot
    route_record = clean
    if history:
        if clean:
            clean[0]['route_history'] = history
        elif history.get('final_unload'):
            route_record = {'route_history': history}
    con.execute("INSERT INTO assignment_batch (id, crew_id, work_date, assign_ver, status, route_json, created_at) VALUES (?,?,?,?,'ACTIVE',?,?)",
                (bid, cid, work_date, prev + 1, json.dumps(route_record, ensure_ascii=False), now()))
    for i, st in enumerate(clean, 1):
        con.execute("INSERT INTO assignment_stop VALUES (?,?,?,?)", (bid, st["request_id"], i, st["eta"] or None))
        con.execute("INSERT INTO active_assignment VALUES (?,?) ON CONFLICT(request_id) DO UPDATE SET batch_id=excluded.batch_id", (st["request_id"], bid))
        con.execute("UPDATE request SET status=CASE WHEN status IN ('RECEIVED','PARTIAL','NOT_COLLECTED','ON_HOLD') THEN 'ASSIGNED' ELSE status END, planned_date=?, updated_at=? WHERE id=?",
                    (work_date, now(), st["request_id"]))
    audit(con, "batch", bid, action, {"crew": crew_name, "date": work_date, "ver": prev + 1, "n": len(clean), **(extra or {})})
    return {"batch_id": bid, "crew_id": cid, "assign_ver": prev + 1, "count": len(clean)}


def _check_fixed_crew(con, cid, stops):
    if not stops:
        return
    from dgpc.area import list_dongs
    dongs = {d['name']: d for d in list_dongs(con) if d.get('depot')}
    if not dongs:
        return
    crew = one(con, 'SELECT active FROM crew WHERE id=?', (cid,))
    for chunk in _chunks([s['request_id'] for s in stops]):
        for req in rows(con, f"SELECT dong FROM request WHERE id IN ({','.join('?' for _ in chunk)})", chunk):
            dong = dongs.get(req['dong'])
            if dong and (dong['crew_id'] != cid or not crew or not crew['active']):
                raise UserError('차고지가 지정된 동은 해당 동의 사용 중인 담당 수거팀에 배정해 주세요.')


def _release_dropped(con, old_batch_ids):
    """이전 버전에만 있고 새 버전에서 빠진 접수는 활성 배정을 풀고 접수 상태로 되돌린다."""
    old_batch_ids = list(old_batch_ids)
    if not old_batch_ids:
        return
    for rid, in con.execute("SELECT request_id FROM active_assignment WHERE batch_id IN (%s)" % ",".join("?" * len(old_batch_ids)), old_batch_ids).fetchall():
        con.execute("DELETE FROM active_assignment WHERE request_id=?", (rid,))
        con.execute("UPDATE request SET status='RECEIVED', updated_at=? WHERE id=? AND status='ASSIGNED'", (now(), rid))


def current_assignment(con, work_date: str) -> list[dict]:
    """그날 확정된(ACTIVE) 배정: 팀별 갱신본·순서·예상 도착·하차 경유. 부분 수정 화면의 시작점."""
    out = rows(con, """SELECT b.id AS batch_id, b.crew_id, c.name AS crew_name, b.assign_ver, b.route_json, b.created_at FROM assignment_batch b
        JOIN crew c ON c.id=b.crew_id WHERE b.work_date=? AND b.status='ACTIVE' ORDER BY c.name""", (work_date,))
    for b in out:
        planned, history = _route_record(b.pop("route_json"))
        if history and history.get('final_unload'):
            b['final_unload'] = history['final_unload']
        route = {x["request_id"]: x for x in planned if isinstance(x, dict)}
        # 지금도 이 배정에 묶인 접수만: 다른 날·다른 팀으로 옮겨 간 접수(지난 배정 기록만 남은 것)는 부분 수정에서 다시 쓰지 않는다
        b["stops"] = rows(con, """SELECT s.seq, s.eta, r.id AS request_id, r.receipt_no, r.status, r.dong, r.lat, r.lon FROM assignment_stop s
            JOIN request r ON r.id=s.request_id
            JOIN active_assignment a ON a.request_id=s.request_id AND a.batch_id=s.batch_id
            WHERE s.batch_id=? ORDER BY s.seq""", (b["batch_id"],))
        for st in b["stops"]:
            if route.get(st["request_id"], {}).get("unload_after"):
                st["unload_after"] = route[st["request_id"]]["unload_after"]
        sent = one(con, "SELECT MAX(displayed_at) AS t FROM transfer_bundle WHERE batch_id=?", (b["batch_id"],))
        b["sent_at"] = sent["t"] if sent else None
    return out


ASSIGN_OPS = ("insert", "remove", "reorder", "move")


def prepare_edit(con, work_date: str, op: str, data: dict, *, review=False) -> dict:
    """현재 확정 배정으로부터 변경할 팀과 남은 방문 순서를 만든다."""
    if not _date_ok(work_date or ""):
        raise UserError("배정 날짜를 골라 주세요.")
    if op not in ASSIGN_OPS:
        raise UserError("배정 수정 종류가 올바르지 않습니다.")
    if review and any(key in data and not isinstance(data[key], str) for key in ("request_id", "crew_id", "to_crew_id")):
        raise UserError("변경할 접수와 수거팀을 확인하세요.")
    crews = {c["id"]: c for c in list_crews(con)}
    rid = data.get("request_id") or ""
    cur = {b["crew_id"]: b for b in current_assignment(con, work_date)}
    seen = data.get("assign_ver") or {}
    if isinstance(seen, dict):
        for cid, v in seen.items():
            have = cur.get(cid, {}).get("assign_ver", 0)
            if str(v) != str(have):
                raise StaleError("배정이 다른 곳에서 바뀌었습니다. 다시 열어 확인하세요", crew_id=cid, assign_ver=have)

    if review:
        expected = data.get("expected_order")
        if not isinstance(seen, dict) or not seen or not isinstance(expected, dict) or set(expected) != set(seen):
            raise UserError("변경할 팀의 갱신본과 현재 방문 순서를 확인하세요.")
        for cid, order in expected.items():
            if order != [s["request_id"] for s in cur.get(cid, {}).get("stops", [])]:
                raise StaleError("처리된 접수나 방문 순서가 바뀌었습니다. 현재 배정을 다시 불러와 주세요.", crew_id=cid)

    def stops_of(cid):
        return [{"request_id": x["request_id"], "eta": x["eta"] or "", **({"unload_after": x["unload_after"]} if x.get("unload_after") else {})}
                for x in cur.get(cid, {}).get("stops", [])]

    def where(r_):
        return next((cid for cid, b in cur.items() if any(x["request_id"] == r_ for x in b["stops"])), None)

    def place(lst, item, pos):
        try:
            pos = int(pos) if pos not in (None, "") else None
        except (TypeError, ValueError):
            raise UserError("넣을 위치는 숫자(1부터)입니다.")
        if pos is None or pos > len(lst):
            lst.append(item)
        else:
            lst.insert(max(0, pos - 1), item)

    changes: dict[str, list] = {}
    if op in ("insert", "move"):
        to = data.get("crew_id") if op == "insert" else data.get("to_crew_id")
        if to not in crews:
            raise UserError("넣을 팀을 골라 주세요.")
        req = one(con, "SELECT status,planned_date FROM request WHERE id=?", (rid,))
        if not req:
            raise UserError("접수를 찾을 수 없습니다.")
        if req["status"] in ("CANCELLED", "DONE"):
            raise UserError("취소·완료된 접수는 배정할 수 없습니다.")
        src = where(rid)
        if op == "insert":
            if review and req["planned_date"] != work_date:
                raise StaleError("접수의 수거 예정일이 바뀌었습니다. 현재 배정을 다시 불러와 주세요.")
            if src:
                raise UserError(f"이미 이날 {crews[src]['name']} 배정에 들어 있습니다. [다른 팀으로 옮기기]를 쓰세요.")
            act = one(con, """SELECT b.work_date, c.name FROM active_assignment a JOIN assignment_batch b ON b.id=a.batch_id JOIN crew c ON c.id=b.crew_id
                WHERE a.request_id=?""", (rid,))
            if act:
                raise UserError(f"이미 다른 배정({act['work_date']} {act['name']})에 들어 있는 접수입니다. 먼저 그 배정에서 빼 주세요.")
        else:
            if not src:
                raise UserError("이날 배정에 없는 접수입니다.")
            if src == to:
                raise UserError("같은 팀입니다. 순서 바꾸기를 쓰세요.")
            changes[src] = [x for x in stops_of(src) if x["request_id"] != rid]
        lst = changes.get(to) or stops_of(to)
        place(lst, {"request_id": rid, "eta": str(data.get("eta") or "")[:5]}, data.get("position"))
        changes[to] = lst
    elif op == "remove":
        src = where(rid)
        if not src:
            raise UserError("이날 배정에 없는 접수입니다.")
        changes[src] = [x for x in stops_of(src) if x["request_id"] != rid]
    else:  # reorder
        cid = data.get("crew_id")
        if cid not in cur or cid not in crews:
            raise UserError("순서를 바꿀 팀의 배정이 없습니다.")
        order = [x for x in (data.get("order") or []) if isinstance(x, str)]
        old = stops_of(cid)
        if sorted(order) != sorted(x["request_id"] for x in old) or len(set(order)) != len(order):
            raise UserError("순서 목록이 지금 배정과 다릅니다(빠지거나 더해진 접수). 다시 열어 확인하세요.")
        by = {x["request_id"]: x for x in old}
        if review and data.get('recalculate') is not True and order == [x["request_id"] for x in old]:
            raise UserError("방문 순서에 변경이 없습니다.")
        changes[cid] = [by[i] for i in order]
    if review and (set(seen) != set(changes) or any(not crews[cid]["active"] for cid in changes)):
        raise StaleError("변경 대상 팀이 달라졌습니다. 현재 배정을 다시 불러와 주세요.")
    return {"current": cur, "changes": changes, "crews": crews}


def apply_edit(con, work_date, op, data, prepared, plan, *, reviewed=None):
    """검토 또는 기존 부분 수정의 변경 팀만 새 갱신본으로 기록한다."""
    cur = prepared["current"]
    reason = str(data.get("reason") or "").strip()[:200]
    rid = data.get("request_id")
    old_ids = [cur[cid]["batch_id"] for cid in plan if cid in cur]
    detail = {"edit": op, **({"reason": reason} if reason else {}), **({"request": rid} if rid else {})}
    if reviewed:
        detail.update(reviewed=True, matrix_source=reviewed["matrix_source"])
    with tx(con):
        histories = matching_histories(plan, reviewed.get("histories")) if reviewed else {}
        out = [_write_batch(con, cid, work_date, stops, detail, action="EDIT", history=histories.get(cid))
               for cid, stops in plan.items()]
        _release_dropped(con, old_ids)
    return {"batches": out, "resend_needed": [b["crew_id"] for b in out]}


def edit_assignment(con, work_date: str, op: str, data: dict) -> dict:
    """기존 즉시 부분 수정 호출을 처리한다."""
    with tx(con):
        prepared = prepare_edit(con, work_date, op, data)
        return apply_edit(con, work_date, op, data, prepared, prepared["changes"])


def _map_depots(stops, crew, dongs, depot, end, *, saved_depot=None):
    if saved_depot:
        return {'depot': saved_depot, 'end': saved_depot, 'complete': True}
    start, complete = depot, True
    try:
        start = DP.resolve(stops, [crew], dongs, depot, end)['crew_depots'][crew['id']]
    except UserError:
        complete = False
    return {'depot': start, 'end': end or start, 'complete': complete}


def _team_depot(con, stops, crew):
    from dgpc.area import list_dongs
    route = _map_depots(stops, crew, list_dongs(con), get_setting(con, "depot"), None)
    return route["depot"] if route["complete"] else None


def _kept_visit(batch_id, active_batch, status):
    return status != 'CANCELLED' and (active_batch == batch_id or active_batch is None and status in FINAL)


def list_batches(con, work_date: str, *, route_map=False, current_road=None, for_send=False) -> list[dict]:
    archive_flag = ", c.archived_at IS NOT NULL AS crew_archived" if for_send else ""
    out = rows(con, f"""SELECT b.*, c.name AS crew_name, c.device_id{archive_flag} FROM assignment_batch b JOIN crew c ON c.id=b.crew_id
        WHERE b.work_date=? ORDER BY c.name, b.assign_ver DESC""", (work_date,))
    if route_map:
        from dgpc.area import list_dongs
        dongs = list_dongs(con)
        depot, end, unload = (get_setting(con, key) for key in ('depot', 'end_site', 'unload_site'))
    for b in out:
        show_route = route_map and b['status'] == 'ACTIVE'
        show_send = for_send and not route_map and b['status'] == 'ACTIVE'
        map_columns = ', a.batch_id AS active_batch' if show_route or show_send else ''
        if show_route:
            map_columns += ', r.dong'
        map_join = 'LEFT JOIN active_assignment a ON a.request_id=s.request_id' if show_route or show_send else ''
        b["stops"] = rows(con, f"""SELECT s.seq, s.eta, r.id AS request_id, r.receipt_no, r.name, r.address, r.status, r.updated_at, r.lat, r.lon{map_columns}
            FROM assignment_stop s JOIN request r ON r.id=s.request_id {map_join} WHERE s.batch_id=? ORDER BY s.seq""", (b["id"],))
        b["bundles"] = rows(con, "SELECT id, chunk_count, displayed_at, receipt_confirmed_at FROM transfer_bundle WHERE batch_id=? ORDER BY displayed_at DESC, rowid DESC", (b["id"],))
        if for_send and b["bundles"]:
            latest = b["bundles"][0]
            sent = one(con, """SELECT json_extract(detail, '$.stop_versions') AS versions FROM audit_log
                WHERE entity='bundle' AND entity_id=? AND action='DISPLAY' ORDER BY rowid DESC LIMIT 1""", (latest["id"],))
            latest["sent_stops"] = bool(json.loads(sent["versions"])) if sent and sent["versions"] is not None else None
        last = b["bundles"][0]["displayed_at"] if b["bundles"] else None
        b["resend_needed"] = bool(last and any(s["updated_at"] > last for s in b["stops"]))
        saved_route = b.pop("route_json", None)
        if show_send:
            b['stops'] = [s for s in b['stops'] if _kept_visit(b['id'], s['active_batch'], s['status'])]
            for stop in b['stops']:
                del stop['active_batch']
            if not b['stops']:
                planned, header = _route_record(saved_route)
                final = _detached_final(header, _last_final_unload(planned))
                if final:
                    b['final_unload'] = final
        elif not b['stops']:
            _, header = _route_record(saved_route)
            if header and header.get('final_unload'):
                b['final_unload'] = header['final_unload']
        if show_route:
            crew = {'id': b['crew_id'], 'name': b['crew_name']}
            planned, history = _route_record(saved_route)
            planned = [s for s in planned if isinstance(s, dict)]
            via = {s['request_id']: s['unload_after'] for s in planned if s.get('unload_after')}
            final = _last_final_unload(planned)
            stops = []
            map_stops = []
            for stop in b['stops']:
                dong, active_batch = stop.pop('dong'), stop.pop('active_batch')
                rid = stop['request_id']
                if (final or history) and not _kept_visit(b['id'], active_batch, stop['status']):
                    stop['map_skipped'] = True
                    if rid in via and stops:
                        via[stops[-1]['id']] = via[rid]
                    continue
                stops.append({'id': rid, 'dong': dong})
                map_stops.append(stop)
            complete = True
            for stop in map_stops:
                if visit := via.get(stop['request_id']):
                    if visit is final:
                        stop['final_unload'] = True
                    site = (DP.point(visit) if visit.get('lat') is not None and visit.get('lon') is not None
                            else history.get('unload_site') if history else unload)
                    if site:
                        stop['via'] = site
                    else:
                        complete = False
            b['map_route'] = _map_depots(stops, crew, dongs, depot, None if final else end,
                                         saved_depot=final.get('depot') if final else None)
            b['map_route']['complete'] &= complete
            if history:
                m = b['map_route']
                m.update(depot=history.get('depot'), end=history.get('end'))
                detached = _detached_final(history, final) if not map_stops else None
                if detached:
                    m['final_unload'] = detached
                points = _waypoints(m['depot'], [*map_stops, detached] if detached else map_stops, m['end'], via_key='via')
                m['complete'] = bool(points) and complete
                m['history'] = history_for_view(history, map_stops, points, current_road)
    return out


# ------------------------------------------------------------------ 목록 묶음 (QR)
def _carry_load(history):
    load = (history or {}).get('initial_load') or {}
    return load if load.get('volume_m3') or load.get('weight_kg') else None


def _detached_final(history, final=None):
    if not _carry_load(history):
        return None
    site = history.get('final_unload') or final
    if not site or not history.get('end'):
        return None
    return {**site, 'depot': history['end'], 'stop_count': 0}


def _read_daylist(con, batch_id: str):
    from dgpc.pc_collection import SOURCE as pc_collection_source

    b = one(con, "SELECT b.*, c.name AS crew_name, c.device_id FROM assignment_batch b JOIN crew c ON c.id=b.crew_id WHERE b.id=?", (batch_id,))
    if not b:
        raise UserError("배정을 찾을 수 없습니다.")
    if b["status"] != "ACTIVE":
        raise UserError("새 배정 버전이 있습니다. 최신 배정으로 전송하세요.")
    stops = []
    pc_completed = set()
    # 배정 확정 때 저장한 반입지 경유(하차) 위치: 그 지점을 마친 뒤 반입지에 들른다
    planned, history = _route_record(b["route_json"])
    if history and history.get('path', {}).get('reason') == 'NoRoute':
        raise UserError(NO_ROAD_CONNECTION)
    unload_by = {x["request_id"]: x["unload_after"] for x in planned if isinstance(x, dict) and x.get("unload_after")}
    us = history.get('unload_site') if history else get_setting(con, "unload_site")
    if us and us.get("lat") is not None:
        for u_ in unload_by.values():
            u_.setdefault("lat", us["lat"]); u_.setdefault("lon", us["lon"])
    # 지금 이 배정에 묶인 접수 + 이 배정에서 최종 결과가 나와 배정이 풀린 접수만 싣는다.
    # 일정 변경·다른 팀 배정으로 풀린 접수(지난 배정 기록만 남은 것)는 다시 보내지 않는다(옛 목록으로 엉뚱한 날 수거 방지).
    for s in rows(con, """SELECT s.request_id, s.seq, s.eta, a.batch_id AS active_batch FROM assignment_stop s
            LEFT JOIN active_assignment a ON a.request_id=s.request_id WHERE s.batch_id=? ORDER BY s.seq""", (batch_id,)):
        r = one(con, "SELECT * FROM request WHERE id=?", (s["request_id"],))
        if not _kept_visit(batch_id, s["active_batch"], r["status"]):
            if unload_by.get(r["id"]) and stops:  # 빠진 지점 뒤의 하차는 앞 지점 뒤로 옮긴다
                stops[-1]["unload_after"] = unload_by[r["id"]]
            continue
        st = derive(con, r)
        revisit = st["has_field_records"]
        items = []
        for l in st["lines"]:
            if revisit and l["remaining"] == 0:
                continue
            items.append({"line_id": l["line_id"], "line_kind": l["kind"], "fee_rate_id": l["fee_rate_id"], "name": l["name"], "spec": l["spec"],
                          "unit": l["unit"], "unit_label": l["unit_label"], "qty": l["qty_confirmed"], "qty_received": l["qty_received"],
                          "prior_collected": l["qty_collected"] if revisit else 0,
                          "unit_price": l["price"] if l["price"] is not None else None,
                          "amount": l["amount_received"] if l["kind"] == "RECEIVED" else 0,
                          "price_status": "CONFIRMED" if l["price"] is not None else "PENDING"})
        if unload_by.get(r["id"]):
            extra_u = {"unload_after": unload_by[r["id"]]}
        else:
            extra_u = {}
        # "dong": 행정동 이름(문자열). 휴대폰 목록 형식(dgproto)이 아직 모르면 압축할 때 빠진다.
        # "reduction": 감면율(%)·사유. 휴대폰 목록 압축 형식에는 감면율만 "rd"로 싣는다(사유 이름은 복지 정보라 휴대폰에 보내지 않음).
        # "levy_no": 부과번호 → "lv". 감면액 끝수 단위(접수에 저장된 값, 10원일 때만) → "ru"
        red_ = {"reduction": {"rate": int(r["reduction_rate"] or 0), "label": r["reduction_label"] or "",
                              **({"round_unit": 10} if request_round_unit(r) == 10 else {})}} if r.get("reduction_code") else {}
        pc = one(con, """SELECT json_extract(body_json,'$.pc_req_ver') AS version, json_extract(body_json,'$.outcome') AS outcome
            FROM result_event WHERE request_id=? AND device_id=? AND kind='ATTEMPT' AND apply_status='APPLIED'
            ORDER BY rowid DESC LIMIT 1""", (r['id'], pc_collection_source))
        pc_ver = pc['version'] if pc else 0
        if r['status'] == 'DONE' and pc and pc['outcome'] == 'DONE':
            pc_completed.add(r['id'])
        stops.append({**extra_u, **red_, **({'pc_collection_ver': pc_ver} if pc_ver else {}), "seq": s["seq"], "request_id": r["id"], "receipt_no": r["receipt_no"], "req_ver": r["req_ver"], "dong": r["dong"] or "", "levy_no": r.get("levy_no") or "",
                      "name": r["name"], "phone": r["phone"], "address": r["address"], "spot_note": r["spot_note"], "memo": r["memo"],
                      "lat": r["lat"], "lon": r["lon"], "eta": s["eta"] or (r["appt_start"] or ""),
                      "appt": [r["appt_start"], r["appt_end"]] if r["appt_start"] else None,
                      "revisit": revisit, "assign_ver": b["assign_ver"], "items": items})
    if final := _last_final_unload(stops):
        depot = final.get("depot")
        if not depot:  # 차고지 저장 이전에 계산한 최종 하차도 현재 유효한 장소는 복귀에 사용한다.
            visits = [{"id": s["request_id"], "dong": s["dong"]} for s in stops]
            depot = _team_depot(con, visits, {"id": b["crew_id"], "name": b["crew_name"]})
        if depot:
            final.update(depot=depot, stop_count=len(stops))
    payload = {"schema": "dg.daylist/1", "work_date": b["work_date"], "crew": b["crew_name"], "assign_ver": b["assign_ver"],
               "fee_table": fee_table_for_phone(con), "stops": stops}
    if not stops and (detached := _detached_final(history, _last_final_unload(planned))):
        payload['final_unload'] = detached
    if (payload.get('final_unload') or _last_final_unload(stops)) and (carry := _carry_load(history)):
        payload['carry_load'] = carry
    if b.get("device_id"):  # 휴대폰이 지워도 되는 것: PC가 이미 받은 기록 순번·회수한 사진 (dgproto가 서명해 싣는다)
        payload["rel"] = release_info(con, b["device_id"])
    return payload, b, pc_completed, history


def _phone_daylist(payload, pc_completed, history):
    """이전 휴대폰도 PC 완료 건을 다시 열지 않도록 전송 목록에서만 제외한다."""
    if not pc_completed:
        return payload
    stops = []
    for stop in payload['stops']:
        if stop['request_id'] in pc_completed:
            if stops and stop.get('unload_after'):
                stops[-1]['unload_after'] = stop['unload_after']
        else:
            stops.append(stop.copy())
    phone = {**payload, 'stops': stops}
    if (final := _last_final_unload(stops)) and final.get('depot'):
        stops[-1]['unload_after'] = {**final, 'stop_count': len(stops)}
    if not stops and (detached := _detached_final(history, _last_final_unload(payload['stops']))):
        phone['final_unload'] = detached
    return phone


def build_daylist(con, batch_id: str) -> tuple[dict, dict]:
    payload, batch, pc_completed, history = _read_daylist(con, batch_id)
    return _phone_daylist(payload, pc_completed, history), batch


REL_GENS = 4  # rel.g 에 싣는 최근 세대 수


def _contiguous_to(con, device_id: str, gen: str, start: int) -> int:
    """이 기기·세대에서 start부터 빠짐없이 받은 마지막 번호(start를 못 받았으면 start-1)."""
    if not con.execute("SELECT 1 FROM result_seq WHERE device_id=? AND gen=? AND seq=?", (device_id, gen, start)).fetchone():
        return start - 1
    return con.execute("""SELECT MIN(e.seq) FROM result_seq e WHERE e.device_id=? AND e.gen=? AND e.seq >= ?
        AND NOT EXISTS (SELECT 1 FROM result_seq x WHERE x.device_id=e.device_id AND x.gen=e.gen AND x.seq=e.seq+1)""",
                       (device_id, gen, start)).fetchone()[0]


def release_info(con, device_id: str) -> dict:
    """휴대폰에 알려 줄 '안전하게 받은 것'.
    seq = 세대 없는(이전 앱) 기록 1..seq를 PC가 빠짐없이 저장한(반영·충돌·거부·중복 모두) 마지막 순번,
    g   = 최근 세대별 [세대, 그 세대 첫 번호부터 빠짐없이 받은 마지막 번호] (첫 번호를 못 받은 세대는 싣지 않음),
    ph  = 이 기기에서 회수했지만 아직 휴대폰에 알리지 않은(수신 확인된 목록에 실리지 않은) 사진 ID."""
    # 빠짐없이 받은 마지막 번호: 첫 번호부터 이어진 구간의 끝(중간 번호가 빠졌으면 그 앞에서 멈춤 — 휴대폰은 이 번호까지 지운다)
    seq = _contiguous_to(con, device_id, "", 1)
    g = []
    # 최근 세대: 휴대폰은 새 세대를 늘 지금 번호 다음부터 시작하므로 첫 번호가 큰 세대가 더 최근이다
    for r in con.execute("SELECT gen, start_seq FROM device_gen WHERE device_id=? ORDER BY start_seq DESC, last_seen_at DESC LIMIT ?",
                         (device_id, REL_GENS)).fetchall():
        to = _contiguous_to(con, device_id, r[0], r[1])
        if to >= r[1]:
            g.append([r[0], to])
    ph = [r[0] for r in con.execute("""SELECT photo_id FROM photo WHERE device_id=? AND recovery_status='RECOVERED' AND rel_acked_at IS NULL
        ORDER BY recovered_at LIMIT 500""", (device_id,)).fetchall()]
    return {"seq": max(0, seq or 0), "ph": ph, **({"g": g} if g else {})}


def _transfer_device(con, device_id: str) -> dict:
    dev = one(con, "SELECT * FROM device WHERE id=?", (device_id,))
    if not dev or dev["status"] != "ACTIVE":
        raise UserError("휴대폰이 등록되어 있지 않거나 사용 중지 상태입니다.")
    if not dev["confirmed_at"]:
        raise UserError("지문 확인 전인 휴대폰입니다. 설정 → 기기에서 두 화면의 지문이 같은지 보고 [지문 같음 · 사용 시작]을 누르세요.")
    return dev


def _seal_daylist(payload: dict, device: dict, sign):
    bundle_id = uuid.uuid4().bytes
    head = P.Header(P.TYPE_DAYLIST, bundle_id, uuid.UUID(device["id"]).bytes, payload["assign_ver"], 1, P.key_id(P.pub_raw(sign)))
    env, code = P.seal(payload, head, P.pub_from_raw(device["kex_pub"]), sign)
    return bundle_id, env, code


def make_bundle(con, batch_id: str | None = None, payload: dict | None = None, device_id: str | None = None,
                chunk: int = 700) -> dict:
    stop_versions = {}
    if batch_id:
        payload, b = build_daylist(con, batch_id)
        stop_versions = {s["request_id"]: s["req_ver"] for s in payload["stops"]}
        device_id = b["device_id"]
        if not device_id:
            raise UserError("이 팀에 연결된 휴대폰이 없습니다. 설정에서 기기를 연결하세요.")
    rel = payload.get("rel")
    if payload.get("schema") == "dg.daylist/1":
        payload = P.compact_daylist(payload, FEE_ROWS)
    dev = _transfer_device(con, device_id)
    bundle_id, env, code = _seal_daylist(payload, dev, pc_key(con, "sign"))
    frames = P.split_frames(env, bundle_id, chunk)
    bid = str(uuid.UUID(bytes=bundle_id))
    with tx(con):
        con.execute("""INSERT INTO transfer_bundle (id, device_id, batch_id, work_date, assign_ver, chunk_count, envelope_sha256, receipt_code,
            frames_json, displayed_at, receipt_confirmed_at, rel_seq, rel_json) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?)""",
                    (bid, device_id, batch_id, payload["work_date"], payload["assign_ver"], len(frames),
                     hashlib.sha256(env).hexdigest(), code, json.dumps(frames), now(),
                     (rel or {}).get("seq"), json.dumps(rel) if rel else None))
        audit(con, "bundle", bid, "DISPLAY", {"batch": batch_id, "frames": len(frames),
                                             **({"stop_versions": stop_versions} if batch_id else {})})
    return {"bundle_id": bid, "frames": frames, "bytes": len(env), "count": len(payload.get("s") or payload.get("stops") or []),
            "work_date": payload["work_date"], "assign_ver": payload["assign_ver"]}


def confirm_receipt(con, bundle: str, code: str) -> str:
    r = rows(con, "SELECT id, receipt_code, rel_json FROM transfer_bundle WHERE id LIKE ?", (bundle + "%",))
    if len(r) != 1:
        raise UserError("전송 묶음을 찾을 수 없습니다.")
    c_ = re.sub(r"[\s-]", "", code or "").upper()
    if c_.startswith("R") and len(c_) == len(r[0]["receipt_code"].replace("-", "")) + 1:  # 휴대폰 화면의 앞 글자 'R-'(수신 코드)를 붙여 넣어도
        c_ = c_[1:]
    if c_ != r[0]["receipt_code"].replace("-", "").upper():
        raise UserError("수신 코드가 맞지 않습니다. 수신이 확인되지 않았습니다.")
    with tx(con):
        con.execute("UPDATE transfer_bundle SET receipt_confirmed_at=?, frames_json=NULL WHERE id=?", (now(), r[0]["id"]))  # 받은 것이 확인되면 QR 조각(암호문)은 더 둘 까닭이 없다
        rel = json.loads(r[0]["rel_json"] or "{}")
        for ch in _chunks(rel.get("ph") or []):  # 이 목록으로 휴대폰에 '회수 완료'를 알린 사진
            con.execute(f"UPDATE photo SET rel_acked_at=? WHERE rel_acked_at IS NULL AND photo_id IN ({','.join('?' * len(ch))})", [now(), *ch])
        audit(con, "bundle", r[0]["id"], "RECEIPT", None)
    return r[0]["id"]


# ------------------------------------------------------------------ 하차 기록·적재량 보정
def add_unload(con, data: dict) -> str:
    if not data.get("crew_id"):
        raise UserError("팀을 골라 주세요.")
    place = (data.get("place") or "").strip()
    if not place:
        raise UserError("하차 장소(반입지)를 적어 주세요.")
    vol = float(data["volume_m3"]) if data.get("volume_m3") not in (None, "") else None
    kg = float(data["weight_kg"]) if data.get("weight_kg") not in (None, "") else None
    if vol is None and kg is None:
        raise UserError("부피나 중량 중 하나는 적어 주세요.")
    uid = str(uuid.uuid4())
    with tx(con):
        require_current_crew(con, data["crew_id"])
        con.execute("INSERT INTO unload_log (id, crew_id, work_date, at_time, place, volume_m3, weight_kg, note, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (uid, data["crew_id"], data.get("work_date") or today_kst(), data.get("at_time") or None, place, vol, kg,
                     (data.get("note") or "").strip(), now()))
        audit(con, "unload", uid, "ADD", {"crew": data["crew_id"]})
    return uid


def list_unloads(con, work_date: str) -> list[dict]:
    return rows(con, """SELECT u.*, c.name AS crew_name FROM unload_log u JOIN crew c ON c.id=u.crew_id
        WHERE u.work_date=? AND u.void_reason IS NULL ORDER BY u.at_time""", (work_date,))


def load_check(con, start: str = "", end: str = "") -> dict:
    """적재 추정 ↔ 하차 기록(반입지 계근표·부피) 비교. 날마다: 그날 예정이던 수거 완료·일부 수거 건의 '수거한 수량'으로 낸 추정치와
    그날 하차 기록 합. 재방문 건은 전에 수거한 수량까지 들어가는 근사치다(보정값을 고칠지 판단하는 참고용). 개인정보 없음."""
    from dgpc import routing  # OR-Tools 를 불러오는 무거운 모듈: 쓸 때만
    if not end:
        end = today_kst()
    if not start:
        start = (dt.date.fromisoformat(end) - dt.timedelta(days=29)).isoformat()
    ov = volume_overrides(con)
    ul = rows(con, """SELECT work_date, SUM(COALESCE(volume_m3,0)) AS m3, SUM(COALESCE(weight_kg,0)) AS kg, COUNT(*) AS n,
        SUM(weight_kg IS NOT NULL) AS kn, SUM(volume_m3 IS NOT NULL) AS vn FROM unload_log
        WHERE void_reason IS NULL AND work_date BETWEEN ? AND ? GROUP BY work_date ORDER BY work_date""", (start, end))
    days = []
    for u in ul:
        em3 = ekg = 0.0
        for r in rows(con, "SELECT * FROM request WHERE planned_date=? AND status IN ('DONE','PARTIAL')", (u["work_date"],)):
            for l in derive(con, r)["lines"]:
                it = {"name": l["name"], "spec": l["spec"], "qty": l["qty_collected"], "fee_rate_id": l.get("fee_rate_id")}
                if l["qty_collected"]:
                    em3 += routing.volume_of(it, ov); ekg += routing.weight_of(it)
        days.append({"date": u["work_date"], "unloads": u["n"], "est_m3": round(em3, 1), "est_kg": int(ekg),
                     "rec_m3": round(u["m3"], 1) if u["vn"] else None, "rec_kg": int(u["kg"]) if u["kn"] else None})
    def ratio(rec, est):
        pairs = [(d[rec], d[est]) for d in days if d[rec] is not None and d[est]]
        return round(100 * sum(a for a, _ in pairs) / sum(b for _, b in pairs)) if pairs else None
    return {"start": start, "end": end, "days": days, "m3_pct": ratio("rec_m3", "est_m3"), "kg_pct": ratio("rec_kg", "est_kg"),
            "est_m3": round(sum(d["est_m3"] for d in days), 1), "est_kg": sum(d["est_kg"] for d in days),
            "rec_m3": round(sum(d["rec_m3"] or 0 for d in days), 1), "rec_kg": sum(d["rec_kg"] or 0 for d in days)}


def print_sheet_stamp(con, batch_id: str) -> list[dict]:
    """도로 조회 중 인쇄할 방문·순서·접수가 바뀌었는지 확인할 자료."""
    return rows(con, """SELECT s.seq, s.eta, r.id, r.req_ver, r.updated_at, r.status, a.batch_id AS active_batch
        FROM assignment_stop s JOIN request r ON r.id=s.request_id
        LEFT JOIN active_assignment a ON a.request_id=s.request_id
        WHERE s.batch_id=? ORDER BY s.seq""", (batch_id,))


def dispatch_sheet(con, batch_id: str, protected: bool = False, *, route_positions: bool = False, current_road=None) -> dict:
    """인쇄용 배차표. 보호 인쇄는 이 팀 휴대폰용 암호화 QR을 방문별로 만든다."""
    payload, b, pc_completed, history = _read_daylist(con, batch_id)
    stops = payload["stops"]
    detached = payload.get('final_unload')
    unload_frames = None
    if protected:
        if not b["device_id"]:
            raise UserError("이 팀에 연결된 휴대폰이 없습니다. 설정 → 수거팀에서 등록된 휴대폰을 연결하세요.")
        dev = _transfer_device(con, b["device_id"])
        sign = pc_key(con, "sign")
        phone = _phone_daylist(payload, pc_completed, history)
        compact = P.compact_daylist({**phone, "rel": None}, FEE_ROWS)
        print_stops = {stop['request_id']: stop for stop in stops}
        for stop in stops:
            stop['qr_frames'] = []
        for stop, row in zip(phone['stops'], compact["s"], strict=True):
            visit_schema = 'dg.visit/2' if compact['schema'] == 'dg.daylist/3' else 'dg.visit/1'
            visit = {**compact, "schema": visit_schema, "s": [row]}
            bundle_id, envelope, _ = _seal_daylist(visit, dev, sign)
            print_stops[stop['request_id']]["qr_frames"] = P.split_frames(envelope, bundle_id)
        if phone_final := phone.get('final_unload'):
            bundle_id, envelope, _ = _seal_daylist({**compact, 'schema': 'dg.unload/1'}, dev, sign)
            unload_frames = P.split_frames(envelope, bundle_id)
            if not detached:
                # 수거 QR 없이 최종 하차 QR만 필요한 경우 인쇄에서도 하차를 한 번만 표시한다.
                if _last_final_unload(stops):
                    del stops[-1]['unload_after']
                detached = phone_final
    with tx(con):
        detail = {"stops": len(stops), "protected": protected}
        if protected and route_positions:
            detail["route_positions"] = True
        audit(con, "batch", batch_id, "PRINT", detail)
        _pv.record_access(con, "PRINT", "", len(payload["stops"]), f"배차표 {b['work_date']} {b['crew_name']}")
    for st in payload["stops"]:
        st["status"] = one(con, "SELECT status FROM request WHERE id=?", (st["request_id"],))["status"]
    sheet = {"work_date": payload["work_date"], "crew": payload["crew"], "assign_ver": payload["assign_ver"], "fee_version": payload["fee_table"]["version_id"],
             "stops": stops, "printed_at": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")}
    if detached:
        sheet['final_unload'] = detached
        if unload_frames:
            sheet['unload_qr_frames'] = unload_frames
    if route_positions:
        from dgpc.area import list_dongs
        requests = [{'id': st['request_id'], 'dong': st['dong']} for st in stops]
        crew = {'id': b['crew_id'], 'name': b['crew_name']}
        final = _last_final_unload(stops)
        end = None if final else get_setting(con, 'end_site')
        sheet['map_route'] = _map_depots(requests, crew, list_dongs(con), get_setting(con, 'depot'), end,
                                         saved_depot=final.get('depot') if final else None)
        if history:
            m = sheet['map_route']
            m.update(depot=history.get('depot'), end=history.get('end'))
            points = _waypoints(m['depot'], [*stops, detached] if detached else stops, m['end'], via_key='unload_after')
            m['complete'] = bool(points)
            m['history'] = history_for_view(history, stops, points, current_road)
    return sheet
