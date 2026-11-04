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
- 품목별 적재 기준(data/item_handling.json, PC·휴대폰 공용)으로 접수마다 부피·무게·작업 시간·취급 표시를 낸다.
  차량에 최대 적재 중량(kg)을 넣으면 무게도 제약으로 쓴다(반입지에서 부피·무게 모두 비움).
- "먼저·바닥" 품목(장롱·침대·대형 냉장고 등)은 적재함이 40% 넘게 찬 뒤 싣지 않도록 약한 벌점을 둔다(설정에서 끌 수 있음).
- 반입지 받는 시간·쉬는(점심) 시간 창을 설정하면 그 안에서 하차하고, 그 창 안에 쉬는 시간을 넣는다.
- 오늘 필수가 모두 들어가지 않으면 전체를 실패로 내지 않고, 넣지 못한 필수 건을 사유와 함께 따로 알린다.
- 결과는 추천안이다. 담당자가 순서를 바꾸고 확정한다.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import time
import urllib.request
from pathlib import Path

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

# 품목별 적재 기준(1단위당 부피 m³·무게 kg·작업 분·취급 표시): data/item_handling.json 을 PC·휴대폰이 함께 쓴다.
# 추정치다. 담당자가 실측한 부피 보정값(fee_rate_id 별)이 있으면 부피는 그것을 쓴다.
HANDLING = json.loads((Path(__file__).resolve().parent.parent.parent / "data" / "item_handling.json").read_text(encoding="utf8"))
TAG_LABEL = {k: v["label"] for k, v in HANDLING["tags"].items()}
DEFAULT_VOLUME = HANDLING["default"][0]
TAG_ORDER = ("EQUIP", "TWO", "BASE", "UPRIGHT", "DRAIN", "FRAGILE")


def handling_of(item: dict) -> dict:
    """품목 1단위의 적재 기준. 이름에 낱말이 들어 있고(규격 조건이 있으면 규격이나 이름에 그 말이 있으면) 처음 맞는 규칙."""
    name, spec = str(item.get("name") or ""), str(item.get("spec") or "")
    for key, sk, m3, kg, mins, tags in HANDLING["rules"]:
        if key in name and (not sk or sk in spec or sk in name):
            return {"m3": m3, "kg": kg, "min": mins, "tags": list(tags), "rule": key + (f" | {sk}" if sk else "")}
    m3, kg, mins, tags = HANDLING["default"]
    return {"m3": m3, "kg": kg, "min": mins, "tags": list(tags), "rule": ""}


def _qty(item: dict) -> int:
    try:
        return max(0, int(item.get("qty") or 1))
    except (TypeError, ValueError):
        return 1


def volume_of(item: dict, overrides: dict | None = None) -> float:
    """품목 적재 부피(m³). 담당자가 입력한 보정값이 있으면 그것을, 없으면 적재 기준표의 추정치를 쓴다."""
    o = (overrides or {}).get(item.get("fee_rate_id") or "")
    if o:
        return o * _qty(item)
    return handling_of(item)["m3"] * _qty(item)


def weight_of(item: dict) -> float:
    return handling_of(item)["kg"] * _qty(item)


def tags_of(items: list[dict]) -> list[str]:
    """여러 품목의 취급 표시(중복 없이, 정해진 순서)."""
    got = {t for i in items if _qty(i) for t in handling_of(i)["tags"]}
    if "EQUIP" in got:
        got.discard("TWO")
    return [t for t in TAG_ORDER if t in got]


def work_min(items: list[dict], base: int = 5, per_item: int = 3) -> int:
    """한 접수의 현장 작업 시간(분): 지점 기본 + 품목별 기준 분(품목당 설정값 3분 기준으로 비례)."""
    units = sum(handling_of(i)["min"] * _qty(i) for i in items)
    return int(base + round(units * per_item / 3))


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
        # 초당 호출 수 제한. Windows의 sleep은 타이머 해상도 때문에 조금 일찍 깰 수 있어 간격이 찰 때까지 다시 잔다.
        while (wait := min_interval_s - (time.monotonic() - last)) > 0:
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


def day_summary(info: dict, crews: list[dict], use_unload: bool, max_unloads: int, brk, use_kg: bool) -> dict:
    """하루 물량 점검: 고른 접수의 적재·무게·작업시간 합과 고른 팀이 하루에 감당할 수 있는 양(하차 횟수 포함)."""
    trips = 1 + (max_unloads if use_unload else 0)
    m3 = sum(x["m3"] for x in info.values()); kg = sum(x["kg"] for x in info.values()); work = sum(x["svc"] for x in info.values())
    cap_m3 = sum(float(c.get("capacity_m3", 8)) for c in crews) * trips
    cap_kg = sum(float(c.get("capacity_kg") or 0) for c in crews) * trips if use_kg else 0
    shift = sum(max(0, to_min(c.get("shift_end"), 1020) - to_min(c.get("shift_start"), 540) - (brk[2] if brk else 0)) for c in crews)
    count = {t: sum(1 for x in info.values() if t in x["tags"]) for t in TAG_ORDER}
    warn = []
    if m3 > cap_m3:
        warn.append(f"적재 추정 합계 {m3:.1f}m³가 고른 팀이 하루에 실을 수 있는 양({cap_m3:.1f}m³{', 하차 포함' if use_unload else ''})보다 많습니다.")
    if cap_kg and kg > cap_kg:
        warn.append(f"무게 추정 합계 {kg:,.0f}kg가 고른 팀의 하루 최대 적재 중량({cap_kg:,.0f}kg)보다 많습니다.")
    if work > shift * 0.7:
        warn.append(f"현장 작업만 약 {work}분으로 근무시간 합({shift}분)의 70%를 넘어 이동시간이 모자랄 수 있습니다.")
    if count["EQUIP"]:
        warn.append(f"4인·장비가 필요한 품목이 있는 곳 {count['EQUIP']}곳: 그 팀의 인원·운반 장비를 확인하세요.")
    return {"requests": len(info), "m3": round(m3, 1), "kg": int(kg), "work_min": int(work), "cap_m3": round(cap_m3, 1),
            "cap_kg": int(cap_kg), "shift_min": int(shift), "trips_per_crew": trips, "tags": {k: v for k, v in count.items() if v}, "warnings": warn}


COST = 10            # 목적함수 단위: 0.1분 (적재 선호처럼 1분보다 작은 벌점을 쓰려고)
BASE_FIRST_SHARE = 0.4  # "먼저·바닥" 품목은 적재함이 40% 넘게 찬 뒤에 실으러 가면 벌점(1m³ 넘을 때마다 약 10분)
MUST_RELAX_PENALTY = 1_000_000


def _break_window(lunch: dict | None):
    """점심(쉬는) 시간: start~end 사이 어딘가에 minutes 분. 설정이 없거나 잘못되면 None."""
    if not lunch or not lunch.get("start") or not lunch.get("end"):
        return None
    lo, hi, d = to_min(lunch["start"], 0), to_min(lunch["end"], 0), int(lunch.get("minutes") or 0)
    if d <= 0 or hi - lo < d:
        return None
    return lo, hi, d


def plan(requests: list[dict], crews: list[dict], depot: dict, *, osrm_url: str = "", service_base: int = 5,
         service_per_item: int = 3, time_limit_s: int = 5, today: str = "", volume_overrides: dict | None = None,
         unload_site: dict | None = None, unload_min: int = 20, max_unloads: int = 2, end_site: dict | None = None,
         crew_pref: dict | None = None, pref_penalty: int = 30, travel_method: str | None = None, kakao_key: str = "",
         travel_cache: TravelCache | None = None, unload_hours: dict | None = None, lunch: dict | None = None,
         base_first: bool = True) -> dict:
    """requests: id, lat, lon, items, appt_start/end, must_today, urgent, priority, waiting_days
    crews: id, name, capacity_m3, capacity_kg(0·없음 = 무게 안 따짐), shift_start, shift_end
    crew_pref: {request_id: 선호 팀 id} (행정동 기본 담당 팀, 약한 선호)
    unload_hours: {"start","end"} 반입지가 받는 시간 / lunch: {"start","end","minutes"} 그 사이에 쉬는 시간"""
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
    info = {}  # request_id → 부피·무게·작업분·취급 표시 (하루 물량 점검은 위치 없는 건까지 셈)
    for r in requests:
        its = r.get("items", [])
        info[r["id"]] = {"m3": sum(volume_of(i, volume_overrides) for i in its), "kg": sum(weight_of(i) for i in its),
                         "svc": work_min(its, service_base, service_per_item), "tags": tags_of(its)}
    # 같은 장소(25m 이내)이고 방문 시간창이 겹치는 접수는 한 지점으로 묶어 연속 방문한다
    groups: list[dict] = []
    for r in reqs:
        lo, hi = to_min(r.get("appt_start"), min(starts)), to_min(r.get("appt_end"), max(ends))
        x = info[r["id"]]
        g = next((g for g in groups if haversine_km((g["lat"], g["lon"]), (r["lat"], r["lon"])) <= SAME_PLACE_KM
                  and max(g["lo"], lo) < min(g["hi"], hi)), None)
        if g:
            g["members"].append((r, x["svc"])); g["lo"], g["hi"] = max(g["lo"], lo), min(g["hi"], hi)
            g["service"] += x["svc"]; g["volume"] += x["m3"]; g["kg"] += x["kg"]; g["must"] = g["must"] or bool(r.get("must_today"))
            g["base"] = g["base"] or "BASE" in x["tags"]
        else:
            groups.append({"lat": r["lat"], "lon": r["lon"], "members": [(r, x["svc"])], "lo": lo, "hi": hi, "service": x["svc"],
                           "volume": x["m3"], "kg": x["kg"], "must": bool(r.get("must_today")), "base": "BASE" in x["tags"]})
    points = [(depot["lat"], depot["lon"])] + [(g["lat"], g["lon"]) for g in groups]
    scale = 100  # 부피를 정수로 (0.01m³ 단위)
    caps = [int(float(c.get("capacity_m3", 8)) * scale) for c in crews]
    use_kg = any(float(c.get("capacity_kg") or 0) > 0 for c in crews)
    kcaps = [int(float(c.get("capacity_kg") or 0)) or 10 ** 6 for c in crews]
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
    tail = [0] if use_end else []
    service = [0] + [g["service"] for g in groups] + [int(unload_min)] * len(unload_nodes) + tail
    demand = [0] + [int(round(g["volume"] * scale)) for g in groups] + [-max(caps)] * len(unload_nodes) + tail
    kdemand = [0] + [int(round(g["kg"])) for g in groups] + [-max(kcaps)] * len(unload_nodes) + tail
    brk = _break_window(lunch)
    uh = (to_min((unload_hours or {}).get("start"), 0), to_min((unload_hours or {}).get("end"), 24 * 60)) if unload_hours else None

    def solve(must_hard: bool):
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
        use_pref = any(x is not None for x in node_pref) and pref_penalty > 0 and v > 1
        for k in range(v):
            def cost(i, j, k=k):
                a, b = mgr.IndexToNode(i), mgr.IndexToNode(j)
                return COST * (tmat[a][b] + service[a] + (pref_penalty if use_pref and node_pref[b] is not None and node_pref[b] != k else 0))
            keep.append(cost)
            model.SetArcCostEvaluatorOfVehicle(model.RegisterTransitCallback(cost), k)
        model.AddDimension(tcb, 600, 24 * 60, False, "Time")
        tdim = model.GetDimensionOrDie("Time")
        for k in range(v):
            tdim.CumulVar(model.Start(k)).SetRange(starts[k], ends[k])
            tdim.CumulVar(model.End(k)).SetRange(starts[k], ends[k])
        for node, g in enumerate(groups, start=1):
            tdim.CumulVar(mgr.NodeToIndex(node)).SetRange(g["lo"], max(g["lo"], g["hi"] - service[node]))
        if uh:  # 반입지가 받는 시간 안에 도착·하차를 마치도록
            for node in unload_nodes:
                tdim.CumulVar(mgr.NodeToIndex(node)).SetRange(uh[0], max(uh[0], uh[1] - int(unload_min)))
        breaks = []
        if brk:
            visit = [0] * model.Size()
            for idx in range(model.Size()):
                visit[idx] = service[mgr.IndexToNode(idx)]
            for k in range(v):
                if starts[k] < brk[1] and ends[k] > brk[0] + brk[2]:  # 근무시간에 쉬는 시간 창이 걸칠 때만
                    iv = model.solver().FixedDurationIntervalVar(max(brk[0], starts[k]), min(brk[1], ends[k]) - brk[2], brk[2], False, f"lunch{k}")
                    tdim.SetBreakIntervalsOfVehicle([iv], k, visit)
                    breaks.append((k, iv))
        dims = [("Load", demand, caps, max(caps))]
        if use_kg:
            dims.append(("Weight", kdemand, kcaps, max(kcaps)))
        for name, dem, cp, mx in dims:
            cb = model.RegisterUnaryTransitCallback(lambda i, dem=dem: dem[mgr.IndexToNode(i)])
            keep.append(cb)
            # 반입지에서는 적재량을 0으로 되돌릴 수 있게 여유(slack)를 둔다. 일반 지점은 여유 0.
            model.AddDimensionWithVehicleCapacity(cb, mx if use_unload else 0, cp, True, name)
            dim = model.GetDimensionOrDie(name)
            if use_unload:
                for node in range(1, 1 + n_groups):
                    dim.SlackVar(mgr.NodeToIndex(node)).SetValue(0)
                for node in unload_nodes:
                    dim.CumulVar(mgr.NodeToIndex(node)).SetMin(0)
        if use_unload:
            for node in unload_nodes:
                model.AddDisjunction([mgr.NodeToIndex(node)], 0)  # 필요할 때만 들른다
        ldim = model.GetDimensionOrDie("Load")
        if base_first:  # 크고 무거운 품목은 적재함이 비어 있을 때 싣는 쪽을 선호 (도착 시 적재량이 기준을 넘으면 벌점)
            ub = int(min(caps) * BASE_FIRST_SHARE)
            for node, g in enumerate(groups, start=1):
                if g["base"]:
                    ldim.SetCumulVarSoftUpperBound(mgr.NodeToIndex(node), ub, 1)
        for node, g in enumerate(groups, start=1):
            if g["must"] and must_hard:
                continue
            penalty = sum(10_000 + 3_000 * int(r.get("priority") or 0) + 300 * int(r.get("waiting_days") or 0) + (20_000 if r.get("urgent") else 0)
                          for r, _ in g["members"])
            if g["must"]:
                penalty = MUST_RELAX_PENALTY * len(g["members"])
            model.AddDisjunction([mgr.NodeToIndex(node)], COST * penalty)
        for k in range(v):
            model.AddVariableMinimizedByFinalizer(tdim.CumulVar(model.End(k)))
        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = (routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION if use_unload or brk
                                          else routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC)
        params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        params.time_limit.seconds = time_limit_s
        return model.SolveWithParameters(params), mgr, model, tdim, breaks

    has_must = any(g["must"] for g in groups)
    sol, mgr, model, tdim, breaks = solve(True)
    relaxed = False
    if not sol and has_must:  # 오늘 필수가 다 들어가지 않으면 전체를 버리지 않고, 필수도 빠질 수 있게(아주 큰 벌점) 다시 푼다
        sol, mgr, model, tdim, breaks = solve(False)
        relaxed = bool(sol)
    if not sol:
        return {"routes": [], "matrix_source": source, "infeasible": True, "matrix_note": matrix_note(source, minfo),
                "unassigned": unassigned + [{"request_id": r["id"], "reason": "해를 찾지 못함: 근무시간·쉬는 시간·반입지 시간을 확인하세요"} for r in reqs]}
    routes, visited = [], set()
    unload_set = set(unload_nodes)
    lunch_by = {}  # 쉬는 시간 시작(분): 해의 구간 변수에서 이름으로 찾는다
    if breaks:
        ivc = sol.IntervalVarContainer()
        for i in range(ivc.Size()):
            el = ivc.Element(i)
            nm = el.Var().Name()
            if nm.startswith("lunch"):
                lunch_by[int(nm[5:])] = el.StartValue()
    for k, c in enumerate(crews):
        idx, stops, load, kg, unloads = model.Start(k), [], 0, 0, 0
        while not model.IsEnd(idx):
            node = mgr.IndexToNode(idx)
            if node in unload_set:
                if stops and load > 0:  # 실제로 실은 뒤에만 하차로 표시
                    t = sol.Min(tdim.CumulVar(idx))
                    stops[-1]["unload_after"] = {"eta": hhmm(t), "label": unload_site.get("label") or "반입지", "volume_m3": round(load / scale, 2), "minutes": int(unload_min)}
                    stops[-1]["unload_kg"] = int(kg)
                    unloads += 1
                load, kg = 0, 0
            elif node:
                g = groups[node - 1]
                t = sol.Min(tdim.CumulVar(idx))
                for r, svc in g["members"]:
                    x = info[r["id"]]
                    load += int(round(x["m3"] * scale)); kg += x["kg"]
                    pc = (crew_pref or {}).get(r["id"])
                    stops.append({"request_id": r["id"], "eta": hhmm(t), "service_min": svc, "load_m3": round(load / scale, 2), "load_kg": int(kg),
                                  "m3": round(x["m3"], 2), "kg": int(x["kg"]), "tags": x["tags"],
                                  "same_place": len(g["members"]), **({"pref_crew_id": pc, "off_pref": pc != c["id"]} if pc else {})})
                    visited.add(r["id"])
                    t += svc
            idx = sol.Value(model.NextVar(idx))
        lunch_at = hhmm(lunch_by[k]) if k in lunch_by else None
        routes.append({"crew_id": c["id"], "crew_name": c["name"], "stops": stops, "end": hhmm(sol.Min(tdim.CumulVar(idx))),
                       "capacity_m3": c.get("capacity_m3", 8), "capacity_kg": int(float(c.get("capacity_kg") or 0)), "unloads": unloads,
                       "end_load_m3": round(load / scale, 2), "end_load_kg": int(kg),
                       **({"lunch": {"start": lunch_at, "minutes": brk[2]}} if lunch_at else {})})
    cap_max = max(float(c.get("capacity_m3", 8)) for c in crews)
    kcap_max = max(float(c.get("capacity_kg") or 0) for c in crews) if use_kg and all(float(c.get("capacity_kg") or 0) > 0 for c in crews) else 0
    for r in reqs:
        if r["id"] not in visited:
            vol, wkg = info[r["id"]]["m3"], info[r["id"]]["kg"]
            reason = ("적재 추정치(%.1fm³)가 차량 1대 용량(%.1fm³)보다 큼" % (vol, cap_max) if vol > cap_max
                      else "무게 추정치(%dkg)가 차량 최대 적재 중량(%dkg)보다 큼" % (wkg, kcap_max) if kcap_max and wkg > kcap_max
                      else "적재 추정치(%.1fm³)가 남은 차량 용량을 넘음" % vol if vol > min(float(c.get("capacity_m3", 8)) for c in crews) / 2 and not use_unload
                      else "근무시간·방문 약속 시간 안에 들어가지 않음 (우선순위가 낮아 제외)")
            if r.get("must_today"):
                reason = "오늘 필수였지만 넣지 못함: " + reason.replace(" (우선순위가 낮아 제외)", "") + " — 팀을 더하거나 다른 건을 미루세요"
            unassigned.append({"request_id": r["id"], "reason": reason, **({"must_today": True} if r.get("must_today") else {})})
    return {"routes": routes, "unassigned": unassigned, "matrix_source": source, "must_relaxed": relaxed,
            "summary": day_summary(info, crews, use_unload, max_unloads, brk, use_kg),
            "unload_site": unload_site if use_unload else None, "end_site": end_site if use_end else None,
            "uses_weight": use_kg, "tag_labels": TAG_LABEL,
            "matrix_info": {k: v for k, v in minfo.items() if k in ("api_calls", "cache_hits", "road_pairs", "fallback_pairs", "requested")},
            "matrix_note": matrix_note(source, minfo)}
