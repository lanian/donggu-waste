"""수거 방문 순서 추천 (OR-Tools 차량 경로 + 시간창).

- 이동시간 행렬: 설정에 로컬 OSRM 주소(http://127.0.0.1:포트)가 있으면 실제 도로 시간,
  없으면 직선거리 × 1.4 ÷ 시속 20km 로 추정하고 결과에 "직선거리 추정(도로 아님)"을 표시한다.
- 최적화 엔진에는 좌표·작업시간·적재 추정치·시간창·우선순위만 넘긴다. 이름·연락처는 넘기지 않는다.
- 오늘 반드시 포함(must_today)은 빠질 수 없는 지점, 나머지는 우선순위가 높을수록 빠지기 어렵게(벌점) 한다.
- "먼저 방문"(오전 등)은 벌점이 아니라 시간창으로만 표현한다.
- 반입지 좌표가 설정되어 있으면, 적재가 차면 반입지에 들러 비우고(하차) 이어서 수거하는 경로를 만든다(차량당 최대 2회).
- 결과는 추천안이다. 담당자가 순서를 바꾸고 확정한다.
"""
from __future__ import annotations

import json
import math
import urllib.request

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

# 품목별 적재 부피 추정치(m³/단위). 근거 있는 실측값이 생기면 바꾼다(추정치와 보정값을 구분해 관리).
VOLUME_HINTS = [("장롱", 1.2), ("침대", 1.5), ("응접세트", 1.2), ("냉장고", 1.0), ("소파", 1.2), ("식탁", 0.8), ("서랍장", 0.6),
                ("거실장", 0.8), ("장식장", 1.0), ("피아노", 2.0), ("세탁기", 0.6), ("에어컨", 0.6), ("책상", 0.6), ("매트리스", 0.8)]
DEFAULT_VOLUME = 0.3


def volume_of(item: dict, overrides: dict | None = None) -> float:
    """품목 적재 부피(m³). 담당자가 입력한 보정값이 있으면 그것을, 없으면 이름 기준 추정치를 쓴다."""
    o = (overrides or {}).get(item.get("fee_rate_id") or "")
    if o:
        return o * int(item.get("qty") or 1)
    text = f"{item.get('name', '')} {item.get('spec', '')}"
    v = next((vol for key, vol in VOLUME_HINTS if key in text), DEFAULT_VOLUME)
    return v * int(item.get("qty") or 1)


SAME_PLACE_KM = 0.025  # 25m 안의 접수는 한 번에 방문(접수·수수료·완료는 건별 유지)


def hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def to_min(s: str | None, default: int) -> int:
    if not s:
        return default
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def haversine_km(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(d))


def matrix(points: list[tuple[float, float]], osrm_url: str = "") -> tuple[list[list[int]], str]:
    """분 단위 이동시간 행렬과 출처('OSRM' 또는 'ESTIMATE')."""
    if osrm_url:
        coords = ";".join(f"{lon},{lat}" for lat, lon in points)
        url = osrm_url.rstrip("/") + f"/table/v1/driving/{coords}?annotations=duration"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:  # 이 PC 안의 OSRM에만 요청 (설정에서 127.0.0.1만 허용)
                data = json.load(r)
            if data.get("code") == "Ok":
                return [[int(math.ceil((d or 0) / 60)) for d in row] for row in data["durations"]], "OSRM"
        except Exception:
            pass
    return [[0 if i == j else int(math.ceil(haversine_km(a, b) * 1.4 / 20 * 60)) for j, b in enumerate(points)]
            for i, a in enumerate(points)], "ESTIMATE"


def plan(requests: list[dict], crews: list[dict], depot: dict, *, osrm_url: str = "", service_base: int = 5,
         service_per_item: int = 3, time_limit_s: int = 5, today: str = "", volume_overrides: dict | None = None,
         unload_site: dict | None = None, unload_min: int = 20, max_unloads: int = 2, end_site: dict | None = None) -> dict:
    """requests: id, lat, lon, items, appt_start/end, must_today, urgent, priority, waiting_days
    crews: id, name, capacity_m3, shift_start, shift_end"""
    unassigned = [{"request_id": r["id"], "reason": "배출 위치 좌표가 없음 (위치 지정 필요)"} for r in requests if r.get("lat") is None]
    reqs = [r for r in requests if r.get("lat") is not None]
    if not crews:
        return {"routes": [], "unassigned": unassigned + [{"request_id": r["id"], "reason": "선택한 팀이 없음"} for r in reqs],
                "matrix_source": None}
    if not reqs:
        return {"routes": [{"crew_id": c["id"], "crew_name": c["name"], "stops": [], "end": None} for c in crews],
                "unassigned": unassigned, "matrix_source": None}
    starts = [to_min(c.get("shift_start"), 540) for c in crews]
    ends = [to_min(c.get("shift_end"), 1020) for c in crews]
    # 같은 장소(25m 이내)이고 방문 시간창이 겹치는 접수는 한 지점으로 묶어 연속 방문한다
    groups: list[dict] = []
    for r in reqs:
        lo, hi = to_min(r.get("appt_start"), min(starts)), to_min(r.get("appt_end"), max(ends))
        svc = service_base + service_per_item * sum(int(i.get("qty") or 1) for i in r.get("items", []))
        vol = sum(volume_of(i, volume_overrides) for i in r.get("items", []))
        g = next((g for g in groups if haversine_km((g["lat"], g["lon"]), (r["lat"], r["lon"])) <= SAME_PLACE_KM
                  and max(g["lo"], lo) < min(g["hi"], hi)), None)
        if g:
            g["members"].append((r, svc)); g["lo"], g["hi"] = max(g["lo"], lo), min(g["hi"], hi)
            g["service"] += svc; g["volume"] += vol; g["must"] = g["must"] or bool(r.get("must_today"))
        else:
            groups.append({"lat": r["lat"], "lon": r["lon"], "members": [(r, svc)], "lo": lo, "hi": hi, "service": svc, "volume": vol,
                           "must": bool(r.get("must_today"))})
    points = [(depot["lat"], depot["lon"])] + [(g["lat"], g["lon"]) for g in groups]
    scale = 100  # 부피를 정수로 (0.01m³ 단위)
    caps = [int(float(c.get("capacity_m3", 8)) * scale) for c in crews]
    use_unload = bool(unload_site and unload_site.get("lat") is not None and unload_site.get("lon") is not None and max_unloads > 0)
    n_groups = len(groups)
    unload_nodes = list(range(1 + n_groups, 1 + n_groups + (len(crews) * max_unloads if use_unload else 0)))
    if use_unload:
        points += [(float(unload_site["lat"]), float(unload_site["lon"]))] * len(unload_nodes)
    use_end = bool(end_site and end_site.get("lat") is not None and end_site.get("lon") is not None)
    end_node = len(points) if use_end else 0
    if use_end:  # 일을 마치고 돌아갈 곳이 출발지와 다를 때 (예: 반입지에서 마감 후 차고지 아닌 곳)
        points.append((float(end_site["lat"]), float(end_site["lon"])))
    tmat, source = matrix(points, osrm_url)
    service = [0] + [g["service"] for g in groups] + [int(unload_min)] * len(unload_nodes) + ([0] if use_end else [])
    demand = [0] + [int(round(g["volume"] * scale)) for g in groups] + [-max(caps)] * len(unload_nodes) + ([0] if use_end else [])

    n, v = len(points), len(crews)
    mgr = pywrapcp.RoutingIndexManager(n, v, [0] * v, [end_node] * v)
    model = pywrapcp.RoutingModel(mgr)

    def transit(i, j):
        a, b = mgr.IndexToNode(i), mgr.IndexToNode(j)
        return tmat[a][b] + service[a]
    tcb = model.RegisterTransitCallback(transit)
    model.SetArcCostEvaluatorOfAllVehicles(tcb)
    model.AddDimension(tcb, 600, 24 * 60, False, "Time")
    tdim = model.GetDimensionOrDie("Time")
    for k in range(v):
        tdim.CumulVar(model.Start(k)).SetRange(starts[k], ends[k])
        tdim.CumulVar(model.End(k)).SetRange(starts[k], ends[k])
    for node, g in enumerate(groups, start=1):
        tdim.CumulVar(mgr.NodeToIndex(node)).SetRange(g["lo"], max(g["lo"], g["hi"] - service[node]))
    dcb = model.RegisterUnaryTransitCallback(lambda i: demand[mgr.IndexToNode(i)])
    # 반입지에서는 적재량을 0으로 되돌릴 수 있게 여유(slack)를 둔다. 일반 지점은 여유 0.
    model.AddDimensionWithVehicleCapacity(dcb, max(caps) if use_unload else 0, caps, True, "Load")
    ldim = model.GetDimensionOrDie("Load")
    if use_unload:
        for node in range(1, 1 + n_groups):
            ldim.SlackVar(mgr.NodeToIndex(node)).SetValue(0)
        for node in unload_nodes:
            idx = mgr.NodeToIndex(node)
            model.AddDisjunction([idx], 0)  # 필요할 때만 들른다
            ldim.CumulVar(idx).SetMin(0)
    for node, g in enumerate(groups, start=1):
        if not g["must"]:
            penalty = sum(10_000 + 3_000 * int(r.get("priority") or 0) + 300 * int(r.get("waiting_days") or 0) + (20_000 if r.get("urgent") else 0)
                          for r, _ in g["members"])
            model.AddDisjunction([mgr.NodeToIndex(node)], penalty)
    for k in range(v):
        model.AddVariableMinimizedByFinalizer(tdim.CumulVar(model.End(k)))

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = (routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION if use_unload else routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC)
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.seconds = time_limit_s
    sol = model.SolveWithParameters(params)
    if not sol:
        must = [r for r in reqs if r.get("must_today")]
        return {"routes": [], "matrix_source": source, "infeasible": True,
                "unassigned": unassigned + [{"request_id": r["id"], "reason": "해를 찾지 못함: 오늘 필수 포함 건이 근무시간·적재·방문 시간 안에 모두 들어가지 않음"
                                             if must else "해를 찾지 못함"} for r in reqs]}
    routes, visited = [], set()
    unload_set = set(unload_nodes)
    for k, c in enumerate(crews):
        idx, stops, load, unloads = model.Start(k), [], 0, 0
        while not model.IsEnd(idx):
            node = mgr.IndexToNode(idx)
            if node in unload_set:
                if stops and load > 0:  # 실제로 실은 뒤에만 하차로 표시
                    t = sol.Min(tdim.CumulVar(idx))
                    stops[-1]["unload_after"] = {"eta": hhmm(t), "label": unload_site.get("label") or "반입지", "volume_m3": round(load / scale, 2), "minutes": int(unload_min)}
                    unloads += 1
                load = 0
            elif node:
                g = groups[node - 1]
                load += demand[node]
                t = sol.Min(tdim.CumulVar(idx))
                for r, svc in g["members"]:
                    stops.append({"request_id": r["id"], "eta": hhmm(t), "service_min": svc, "load_m3": round(load / scale, 2),
                                  "same_place": len(g["members"])})
                    visited.add(r["id"])
                    t += svc
            idx = sol.Value(model.NextVar(idx))
        routes.append({"crew_id": c["id"], "crew_name": c["name"], "stops": stops, "end": hhmm(sol.Min(tdim.CumulVar(idx))),
                       "capacity_m3": c.get("capacity_m3", 8), "unloads": unloads, "end_load_m3": round(load / scale, 2)})
    for r in reqs:
        if r["id"] not in visited:
            vol = sum(volume_of(i, volume_overrides) for i in r.get("items", []))
            cap_max = max(float(c.get("capacity_m3", 8)) for c in crews)
            reason = ("적재 추정치(%.1fm³)가 차량 1대 용량(%.1fm³)보다 큼" % (vol, cap_max) if vol > cap_max
                      else "적재 추정치(%.1fm³)가 남은 차량 용량을 넘음" % vol if vol > min(float(c.get("capacity_m3", 8)) for c in crews) / 2 and not use_unload
                      else "근무시간·방문 약속 시간 안에 들어가지 않음 (우선순위가 낮아 제외)")
            unassigned.append({"request_id": r["id"], "reason": reason})
    return {"routes": routes, "unassigned": unassigned, "matrix_source": source,
            "unload_site": unload_site if use_unload else None, "end_site": end_site if use_end else None,
            "matrix_note": "실제 도로 이동시간(로컬 OSRM)" if source == "OSRM" else "직선거리 추정(도로 아님) — 로컬 OSRM을 연결하면 실제 도로 시간으로 계산"}
