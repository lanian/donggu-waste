"""수거 방문 순서 추천 (OR-Tools 차량 경로 + 시간창).

- 이동시간 행렬: 설정에 로컬 OSRM 주소(http://127.0.0.1:포트)가 있으면 실제 도로 시간,
  없으면 직선거리 × 1.4 ÷ 시속 20km 로 추정하고 결과에 "직선거리 추정(도로 아님)"을 표시한다.
- 최적화 엔진에는 좌표·작업시간·적재 추정치·시간창·우선순위만 넘긴다. 이름·연락처는 넘기지 않는다.
- 오늘 반드시 포함(must_today)은 빠질 수 없는 지점, 나머지는 우선순위가 높을수록 빠지기 어렵게(벌점) 한다.
- "먼저 방문"(오전 등)은 벌점이 아니라 시간창으로만 표현한다.
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


def volume_of(item: dict) -> float:
    text = f"{item.get('name', '')} {item.get('spec', '')}"
    v = next((vol for key, vol in VOLUME_HINTS if key in text), DEFAULT_VOLUME)
    return v * int(item.get("qty") or 1)


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
         service_per_item: int = 3, time_limit_s: int = 5, today: str = "") -> dict:
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
    points = [(depot["lat"], depot["lon"])] + [(r["lat"], r["lon"]) for r in reqs]
    tmat, source = matrix(points, osrm_url)
    service = [0] + [service_base + service_per_item * sum(int(i.get("qty") or 1) for i in r.get("items", [])) for r in reqs]
    scale = 100  # 부피를 정수로 (0.01m³ 단위)
    demand = [0] + [int(round(sum(volume_of(i) for i in r.get("items", [])) * scale)) for r in reqs]
    starts = [to_min(c.get("shift_start"), 540) for c in crews]
    ends = [to_min(c.get("shift_end"), 1020) for c in crews]

    n, v = len(points), len(crews)
    mgr = pywrapcp.RoutingIndexManager(n, v, 0)
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
    for node, r in enumerate(reqs, start=1):
        lo = to_min(r.get("appt_start"), min(starts))
        hi = to_min(r.get("appt_end"), max(ends))
        tdim.CumulVar(mgr.NodeToIndex(node)).SetRange(lo, max(lo, hi - service[node]))
    dcb = model.RegisterUnaryTransitCallback(lambda i: demand[mgr.IndexToNode(i)])
    model.AddDimensionWithVehicleCapacity(dcb, 0, [int(c.get("capacity_m3", 8) * scale) for c in crews], True, "Load")
    for node, r in enumerate(reqs, start=1):
        if not r.get("must_today"):
            penalty = 10_000 + 3_000 * int(r.get("priority") or 0) + 300 * int(r.get("waiting_days") or 0) + (20_000 if r.get("urgent") else 0)
            model.AddDisjunction([mgr.NodeToIndex(node)], penalty)
    for k in range(v):
        model.AddVariableMinimizedByFinalizer(tdim.CumulVar(model.End(k)))

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.seconds = time_limit_s
    sol = model.SolveWithParameters(params)
    if not sol:
        must = [r for r in reqs if r.get("must_today")]
        return {"routes": [], "matrix_source": source, "infeasible": True,
                "unassigned": unassigned + [{"request_id": r["id"], "reason": "해를 찾지 못함: 오늘 필수 포함 건이 근무시간·적재·방문 시간 안에 모두 들어가지 않음"
                                             if must else "해를 찾지 못함"} for r in reqs]}
    routes, visited = [], set()
    for k, c in enumerate(crews):
        idx, stops, load = model.Start(k), [], 0
        while not model.IsEnd(idx):
            node = mgr.IndexToNode(idx)
            if node:
                r = reqs[node - 1]
                load += demand[node]
                stops.append({"request_id": r["id"], "eta": hhmm(sol.Min(tdim.CumulVar(idx))), "service_min": service[node],
                              "load_m3": round(load / scale, 2)})
                visited.add(r["id"])
            idx = sol.Value(model.NextVar(idx))
        routes.append({"crew_id": c["id"], "crew_name": c["name"], "stops": stops, "end": hhmm(sol.Min(tdim.CumulVar(idx))),
                       "capacity_m3": c.get("capacity_m3", 8)})
    for r in reqs:
        if r["id"] not in visited:
            vol = sum(volume_of(i) for i in r.get("items", []))
            reason = ("적재 추정치(%.1fm³)가 남은 차량 용량을 넘음" % vol if vol > min(c.get("capacity_m3", 8) for c in crews) / 2
                      else "근무시간·방문 약속 시간 안에 들어가지 않음 (우선순위가 낮아 제외)")
            unassigned.append({"request_id": r["id"], "reason": reason})
    return {"routes": routes, "unassigned": unassigned, "matrix_source": source,
            "matrix_note": "실제 도로 이동시간(로컬 OSRM)" if source == "OSRM" else "직선거리 추정(도로 아님) — 로컬 OSRM을 연결하면 실제 도로 시간으로 계산"}
