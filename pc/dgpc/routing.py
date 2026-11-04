"""수거 방문 순서 추천 (OR-Tools 차량 경로 + 시간창).

- 이동시간 행렬(설정 "이동시간 계산 방식"):
  · 직선거리 추정(기본): 직선거리 × 1.4 ÷ 시속 20km, 결과에 "직선거리 추정(도로 아님)"을 표시한다.
  · 로컬 OSRM: 이 PC 안 OSRM(http://127.0.0.1:포트)의 실제 도로 시간.
  · 카카오모빌리티: 다중 목적지 길찾기 API로 출발지 하나씩 행을 채운다(좌표만 보냄). 결과는 DB(travel_cache)에
    좌표 소수 5자리 쌍으로 30일 보관해 다시 묻지 않는다. 시간 초과·한도·반경(10km) 밖·실패한 쌍은 그 쌍만 직선거리 추정으로
    채우고 화면에 "도로 시간: 카카오모빌리티 (일부 직선 추정 N쌍)"처럼 몇 쌍을 추정했는지 보인다.
- 최적화 엔진에는 좌표·작업시간·적재 추정치·시간창·우선순위만 넘긴다. 이름·연락처는 넘기지 않는다.
- 오늘 반드시 포함(must_today)은 빠질 수 없는 지점, 나머지는 우선순위가 높을수록 빠지기 어렵게(벌점) 한다.
- "먼저 방문"(오전 등)은 벌점이 아니라 시간창으로만 표현한다.
- 반입지 좌표가 설정되어 있으면, 적재가 차면 반입지에 들러 비우고(하차) 이어서 수거하는 경로를 만든다(차량당 최대 2회).
- 행정동의 기본 담당 팀(crew_pref)이 있으면 다른 팀이 그 지점에 갈 때 이동 비용에 벌점(pref_penalty 분)을 더한다(약한 선호).
  팀을 막지는 않으므로 담당 팀이 꽉 차면 다른 팀이 가져갈 수 있다.
- 결과는 추천안이다. 담당자가 순서를 바꾸고 확정한다.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import time
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


def _estimate_min(a, b) -> int:
    return int(math.ceil(haversine_km(a, b) * 1.4 / 20 * 60))


def estimate_matrix(points) -> list[list[int]]:
    return [[0 if i == j else _estimate_min(a, b) for j, b in enumerate(points)] for i, a in enumerate(points)]


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
    return estimate_matrix(points), "ESTIMATE"


# ------------------------------------------------------------------ 카카오모빌리티 이동시간 (캐시 + 쌍별 직선 추정 대체)
CACHE_DAYS = 30


def _ck(p) -> str:
    return f"{p[0]:.5f},{p[1]:.5f}"


class TravelCache:
    """좌표 쌍(소수 5자리) → 자동차 소요 시간(초). 이 PC의 암호화 DB(travel_cache 표)에 30일 보관."""

    def __init__(self, con, days: int = CACHE_DAYS):
        self.con, self.days = con, days

    def get_many(self, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
        if not pairs:
            return {}
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=self.days)).isoformat(timespec="seconds")
        want = set(pairs)
        origins = sorted({a for a, _ in pairs})
        out = {}
        for i in range(0, len(origins), 200):
            chunk = origins[i:i + 200]
            q = "SELECT origin, dest, seconds FROM travel_cache WHERE fetched_at >= ? AND origin IN (%s)" % ",".join("?" * len(chunk))
            for o, d, sec in self.con.execute(q, [since, *chunk]).fetchall():
                if (o, d) in want:
                    out[(o, d)] = int(sec)
        return out

    def put_many(self, vals: dict[tuple[str, str], int]):
        if not vals:
            return
        at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=self.days)).isoformat(timespec="seconds")
        own = not self.con.in_transaction
        if own:
            self.con.execute("BEGIN IMMEDIATE")
        try:
            self.con.executemany("INSERT INTO travel_cache (origin, dest, seconds, fetched_at) VALUES (?,?,?,?) "
                                 "ON CONFLICT(origin, dest) DO UPDATE SET seconds=excluded.seconds, fetched_at=excluded.fetched_at",
                                 [(o, d, int(s), at) for (o, d), s in vals.items()])
            self.con.execute("DELETE FROM travel_cache WHERE fetched_at < ?", (since,))  # 30일 지난 것 정리
            if own:
                self.con.execute("COMMIT")
        except Exception:
            if own:
                self.con.execute("ROLLBACK")
            raise


def kakao_matrix(points: list[tuple[float, float]], key: str, cache: TravelCache | None = None, *, max_calls: int = 300,
                 deadline_s: float = 30.0, min_interval_s: float = 0.05, timeout_s: float = 5.0) -> tuple[list[list[int]], dict]:
    """N×N 분 단위 행렬. 출발지마다 다중 목적지 API(30곳씩, 반경 10km 안)로 한 행씩 채우고, 반경 밖은 1:1 길찾기로 묻는다.
    캐시에 있는 쌍은 묻지 않는다. 호출 수·전체 시간 한도를 넘거나 인증 실패·연결 실패가 나면 더 묻지 않고,
    못 구한 쌍은 직선거리 추정으로 채운다. 돌려주는 info: api_calls, cache_hits, road_pairs, fallback_pairs, error"""
    from dgpc import external as X
    uniq: list[str] = []
    pos: dict[str, tuple[float, float]] = {}
    for p in points:
        k = _ck(p)
        if k not in pos:
            pos[k] = (round(p[0], 5), round(p[1], 5))
            uniq.append(k)
    need = [(a, b) for a in uniq for b in uniq if a != b]
    secs: dict[tuple[str, str], int] = dict(cache.get_many(need)) if cache else {}
    info = {"api_calls": 0, "cache_hits": len(secs), "road_pairs": 0, "fallback_pairs": 0, "error": ""}
    fresh: dict[tuple[str, str], int] = {}
    t0, last, stop = time.monotonic(), 0.0, False

    def call(fn, *args):
        nonlocal last, stop
        if stop:
            return None
        if info["api_calls"] >= max_calls or time.monotonic() - t0 > deadline_s:
            stop = True
            info["error"] = info["error"] or "호출 한도·시간 한도에 닿아 나머지는 직선거리로 추정"
            return None
        wait = min_interval_s - (time.monotonic() - last)
        if wait > 0:  # 초당 호출 수 제한
            time.sleep(wait)
        last = time.monotonic()
        info["api_calls"] += 1
        try:
            return fn(*args, timeout=timeout_s)
        except X.ExternalError as e:
            info["error"] = str(e)
            if e.code in ("AUTH", "NETWORK", "TIMEOUT", "HTTP_429") or e.code.startswith("HTTP_5"):
                stop = True  # 같은 실패가 반복될 것이므로 더 묻지 않는다
            return None

    for a in uniq:
        if stop:
            break
        todo = [b for b in uniq if b != a and (a, b) not in secs]
        near = [b for b in todo if haversine_km(pos[a], pos[b]) * 1000 <= X.KAKAO_RADIUS_M * 0.98]
        far = [b for b in todo if b not in near]
        for i in range(0, len(near), X.KAKAO_MAX_DEST):
            chunk = near[i:i + X.KAKAO_MAX_DEST]
            idx = {str(n): b for n, b in enumerate(chunk)}
            got = call(X.kakao_destinations, key, pos[a], [(n, *pos[b]) for n, b in idx.items()])
            for n, sec in (got or {}).items():
                if n in idx:
                    secs[(a, idx[n])] = fresh[(a, idx[n])] = sec
        for b in far:
            sec = call(X.kakao_direction, key, pos[a], pos[b])
            if sec is not None:
                secs[(a, b)] = fresh[(a, b)] = sec
    if cache and fresh:
        try:
            cache.put_many(fresh)
        except Exception:
            pass
    info["road_pairs"] = sum(1 for p in need if p in secs)
    info["fallback_pairs"] = len(need) - info["road_pairs"]
    mat = []
    for i, pa in enumerate(points):
        row = []
        for j, pb in enumerate(points):
            ka, kb = _ck(pa), _ck(pb)
            if i == j or ka == kb:
                row.append(0)
            elif (ka, kb) in secs:
                row.append(int(math.ceil(secs[(ka, kb)] / 60)))
            else:
                row.append(_estimate_min(pa, pb))
        mat.append(row)
    return mat, info


METHODS = {"ESTIMATE": "직선거리 추정", "OSRM": "로컬 OSRM", "KAKAO": "카카오모빌리티 API"}


def build_matrix(points, osrm_url: str = "", method: str | None = None, kakao_key: str = "", cache: TravelCache | None = None) -> tuple[list[list[int]], str, dict]:
    """설정의 이동시간 계산 방식대로 행렬을 만든다. 돌려주는 값: (행렬, 출처 'KAKAO'|'OSRM'|'ESTIMATE', 정보)"""
    m = method or ("OSRM" if osrm_url else "ESTIMATE")
    if m == "KAKAO" and kakao_key:
        mat, info = kakao_matrix(points, kakao_key, cache)
        if info["road_pairs"] == 0 and info["fallback_pairs"] > 0:
            return mat, "ESTIMATE", {**info, "requested": "KAKAO"}
        return mat, "KAKAO", info
    if m == "KAKAO":
        return estimate_matrix(points), "ESTIMATE", {"requested": "KAKAO", "error": "카카오모빌리티 REST 키가 없음"}
    mat, src = matrix(points, osrm_url if m == "OSRM" else "")
    return mat, src, ({"requested": "OSRM"} if m == "OSRM" and src != "OSRM" else {})


def matrix_note(source: str | None, info: dict | None) -> str:
    info = info or {}
    if source == "KAKAO":
        n = info.get("fallback_pairs") or 0
        return "도로 시간: 카카오모빌리티" + (f" (일부 직선 추정 {n}쌍)" if n else "")
    if source == "OSRM":
        return "실제 도로 이동시간(로컬 OSRM)"
    if info.get("requested") == "KAKAO":
        return "직선거리 추정(도로 아님) — 카카오모빌리티로 구하지 못함" + (f": {info['error']}" if info.get("error") else "")
    if info.get("requested") == "OSRM":
        return "직선거리 추정(도로 아님) — 로컬 OSRM에 연결하지 못함"
    return "직선거리 추정(도로 아님) — 로컬 OSRM이나 카카오모빌리티를 연결하면 실제 도로 시간으로 계산"


def plan(requests: list[dict], crews: list[dict], depot: dict, *, osrm_url: str = "", service_base: int = 5,
         service_per_item: int = 3, time_limit_s: int = 5, today: str = "", volume_overrides: dict | None = None,
         unload_site: dict | None = None, unload_min: int = 20, max_unloads: int = 2, end_site: dict | None = None,
         crew_pref: dict | None = None, pref_penalty: int = 30, travel_method: str | None = None, kakao_key: str = "",
         travel_cache: TravelCache | None = None) -> dict:
    """requests: id, lat, lon, items, appt_start/end, must_today, urgent, priority, waiting_days
    crews: id, name, capacity_m3, shift_start, shift_end
    crew_pref: {request_id: 선호 팀 id} (행정동 기본 담당 팀, 약한 선호)"""
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
    tmat, source, minfo = build_matrix(points, osrm_url, travel_method, kakao_key, travel_cache)
    service = [0] + [g["service"] for g in groups] + [int(unload_min)] * len(unload_nodes) + ([0] if use_end else [])
    demand = [0] + [int(round(g["volume"] * scale)) for g in groups] + [-max(caps)] * len(unload_nodes) + ([0] if use_end else [])

    n, v = len(points), len(crews)
    mgr = pywrapcp.RoutingIndexManager(n, v, [0] * v, [end_node] * v)
    model = pywrapcp.RoutingModel(mgr)

    def transit(i, j):
        a, b = mgr.IndexToNode(i), mgr.IndexToNode(j)
        return tmat[a][b] + service[a]
    tcb = model.RegisterTransitCallback(transit)
    # 행정동 기본 담당 팀: 지점(묶음)마다 구성원 다수의 선호 팀 → 다른 팀이 들어오면 벌점
    crew_idx = {c["id"]: k for k, c in enumerate(crews)}
    node_pref: list[int | None] = [None] * n
    for node, g in enumerate(groups, start=1):
        prefs = [crew_idx[(crew_pref or {}).get(r["id"])] for r, _ in g["members"] if (crew_pref or {}).get(r["id"]) in crew_idx]
        if prefs:
            node_pref[node] = max(set(prefs), key=prefs.count)
    keep = []  # 콜백이 가비지 컬렉션되지 않게
    if any(x is not None for x in node_pref) and pref_penalty > 0 and v > 1:
        for k in range(v):
            def cost(i, j, k=k):
                a, b = mgr.IndexToNode(i), mgr.IndexToNode(j)
                return tmat[a][b] + service[a] + (pref_penalty if node_pref[b] is not None and node_pref[b] != k else 0)
            keep.append(cost)
            model.SetArcCostEvaluatorOfVehicle(model.RegisterTransitCallback(cost), k)
    else:
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
        return {"routes": [], "matrix_source": source, "infeasible": True, "matrix_note": matrix_note(source, minfo),
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
                    pc = (crew_pref or {}).get(r["id"])
                    stops.append({"request_id": r["id"], "eta": hhmm(t), "service_min": svc, "load_m3": round(load / scale, 2),
                                  "same_place": len(g["members"]), **({"pref_crew_id": pc, "off_pref": pc != c["id"]} if pc else {})})
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
            "matrix_info": {k: v for k, v in minfo.items() if k in ("api_calls", "cache_hits", "road_pairs", "fallback_pairs", "requested")},
            "matrix_note": matrix_note(source, minfo)}
