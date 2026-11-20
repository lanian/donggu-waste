"""수거 방문 순서 추천 (OR-Tools 차량 경로 + 시간창).

- 이동시간 행렬(설정 "이동시간 계산 방식"):
  · 직선거리 추정(기본): 직선거리 × 1.4 ÷ 시속 20km, 결과에 "직선거리 추정(도로 아님)"을 표시한다.
  · 로컬 OSRM: 이 PC 안 OSRM(http://127.0.0.1:포트)의 실제 도로 시간. 경로가 없다고 응답한 방향은 금지한다.
  · 카카오모빌리티: 다중 목적지 길찾기 API로 출발지 하나씩 행을 채운다(좌표만 보냄). 받은 시간은 이 PC에 저장하지 않고
    노선을 계산할 때마다 새로 묻는다. 시간 초과·한도·반경(10km) 밖·실패한 쌍은 그 쌍만 직선거리 추정으로
    채우고 화면에 "도로 시간: 카카오모빌리티 (일부 직선 추정 N쌍)"처럼 몇 쌍을 추정했는지 보인다.
- 최적화 엔진에는 좌표·작업시간·적재 추정치·시간창·우선순위만 넘긴다. 이름·연락처는 넘기지 않는다.
- 오늘 반드시 포함(must_today)은 빠질 수 없는 지점, 나머지는 우선순위가 높을수록 빠지기 어렵게(벌점) 한다.
- 가까운 접수라도 한 팀이 이동·작업·복귀를 시간 안에 끝낼 수 있을 때만 묶는다. 필수와 일반 접수는 나눠 계산한다.
- "먼저 방문"(오전 등)은 벌점이 아니라 시간창으로만 표현한다.
- 반입지 좌표가 설정되어 있으면, 적재가 차면 반입지에 들러 비우고(하차) 이어서 수거하는 경로를 만든다(차량당 최대 2회).
- 필수 방문, 전체 배정 건수, 이동 비용 순으로 비교한다. 긴급도·대기일·적재 선호는 동률을 가르는 비용이다.
- 직접 넘겨 준 팀 선호(crew_pref)는 이동 분에 벌점을 더한다. 자동 배정의 담당팀 제한(crew_required)은 별도 제약이다.
- 품목별 적재 기준(data/item_handling.json, PC·휴대폰 공용)으로 접수마다 부피·무게·작업 시간·취급 표시를 낸다.
  차량에 최대 적재 중량(kg)을 넣으면 무게도 제약으로 쓴다(반입지에서 부피·무게 모두 비움).
- "먼저·바닥" 품목(장롱·침대·대형 냉장고 등)은 적재함이 40% 넘게 찬 뒤 싣지 않도록 약한 벌점을 둔다(설정에서 끌 수 있음).
- 반입지 받는 시간·쉬는(점심) 시간 창을 설정하면 그 안에서 하차하고, 그 창 안에 쉬는 시간을 넣는다.
- 오늘 필수가 모두 들어가지 않으면 전체를 실패로 내지 않고, 넣지 못한 필수 건을 사유와 함께 따로 알린다.
- 결과는 추천안이다. 담당자가 순서를 바꾸고 확정한다.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from dgpc import external as X, osrm
from dgpc.core import UserError
from dgpc.paths import resource_root

# 품목별 적재 기준(1단위당 부피 m³·무게 kg·작업 분·취급 표시): data/item_handling.json 을 PC·휴대폰이 함께 쓴다.
# 추정치다. 담당자가 실측한 부피 보정값(fee_rate_id 별)이 있으면 부피는 그것을 쓴다.
HANDLING = json.loads((resource_root() / "data" / "item_handling.json").read_text(encoding="utf8"))
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
    q = item.get("qty")
    if q is None or q == "":
        return 1
    try:
        return max(0, int(q))
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
SAME_PLACE_TRAVEL_MIN = 1  # 1분을 넘는 도로 우회는 독립 방문으로 계산한다.


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


def _osrm_matrix(points, url):
    size = len(points)
    if size <= 1 or not url:
        return estimate_matrix(points), 0, 0, None
    radiuses = ';'.join(['200'] * size)
    path = f'/table/v1/driving/{osrm.coordinate_path(points)}'
    data, code = osrm.fetch_road(url, path, f'annotations=duration&radiuses={radiuses}', timeout=10)
    if code == 'NoTable':
        # OSRM Table의 NoTable은 요청한 지점 사이에 경로가 없다는 응답이다.
        matrix = [[0 if a == b else None for b in points] for a in points]
        return matrix, 0, sum(row.count(None) for row in matrix), code
    durations = data.get('durations')
    if (code != 'Ok' or not isinstance(durations, list) or len(durations) != size
            or any(not isinstance(row, list) or len(row) != size for row in durations)):
        return estimate_matrix(points), 0, 0, code
    matrix, road, unreachable = [], 0, 0
    for i, row in enumerate(durations):
        line = []
        for j, seconds in enumerate(row):
            if points[i] == points[j]:
                line.append(0)
            elif seconds is None:
                line.append(None)
                unreachable += 1
            elif type(seconds) in (float, int) and 0 <= seconds < 10**9:
                line.append(math.ceil(seconds / 60))
                road += 1
            else:
                line.append(_estimate_min(points[i], points[j]))
        matrix.append(line)
    return matrix, road, unreachable, code


def matrix(points: list[tuple[float, float]], osrm_url: str = '') -> tuple[list[list[int | None]], str]:
    """분 단위 이동시간 행렬과 기존 호출자가 쓰는 출처."""
    mat, source, _ = build_matrix(points, osrm_url)
    return mat, source


def _lonlat(point):
    return (isinstance(point, list) and len(point) == 2 and all(type(v) in (int, float) for v in point)
            and -180 <= point[0] <= 180 and -90 <= point[1] <= 90)


def route_points(points):
    """브라우저가 이미 표시하는 WGS84 지점. 서버 주소나 임의 URL은 받지 않는다."""
    if not isinstance(points, list) or not 2 <= len(points) <= 500 or not all(_lonlat(p) for p in points):
        raise UserError('도로 경로에 사용할 지도 좌표는 2~500개여야 합니다.')
    return [(lat, lon) for lon, lat in points]


def road_path(points, url):
    """현재 방문 순서 그대로 그릴 도로 형상과 km. 배정과는 별도의 조회 결과."""
    coords = osrm.coordinate_path(points)
    radiuses = ';'.join(['200'] * len(points))
    path = f'/route/v1/driving/{coords}'
    query = f'overview=simplified&geometries=geojson&steps=false&radiuses={radiuses}'
    data, code = osrm.fetch_road(url, path, query, timeout=4)
    routes = data.get('routes')
    if code == 'Ok' and isinstance(routes, list) and routes and isinstance(routes[0], dict):
        route = routes[0]
        distance, geometry = route.get('distance'), route.get('geometry')
        if isinstance(geometry, dict) and geometry.get('type') == 'LineString' and type(distance) in (int, float) and 0 <= distance < 10**9:
            line = geometry.get('coordinates')
            if isinstance(line, list) and 2 <= len(line) <= 100_000 and all(_lonlat(p) for p in line):
                return {'source': 'OSRM', 'toll_excluded': True, 'distance_km': round(distance / 1000, 2),
                        'geometry': {'type': 'LineString', 'coordinates': line}}
    reasons = ('NoRoute', 'NoSegment', 'TooBig', osrm.TOLL_EXCLUSION_UNAVAILABLE)
    return {'source': 'ESTIMATE', 'reason': code if code in reasons else 'UNAVAILABLE'}


# ------------------------------------------------------------------ 카카오모빌리티 이동시간 (보관 없음 + 쌍별 직선 추정 대체)
def _ck(p) -> str:
    return f"{p[0]:.5f},{p[1]:.5f}"


def kakao_matrix(points: list[tuple[float, float]], key: str, *, max_calls: int = 300,
                 deadline_s: float = 30.0, min_interval_s: float = 0.05, timeout_s: float = 5.0) -> tuple[list[list[int]], dict]:
    """N×N 분 단위 행렬. 출발지마다 다중 목적지 API(30곳씩, 반경 10km 안)로 한 행씩 채우고, 반경 밖은 1:1 길찾기로 묻는다.
    받은 시간은 이 계산 안에서만 쓰고 어디에도 저장하지 않는다(다음 계산은 다시 묻는다). 같은 좌표(소수 5자리)는 한 번만 묻는다.
    호출 수·전체 시간 한도를 넘거나 인증 실패·연결 실패가 나면 더 묻지 않고, 못 구한 쌍은 직선거리 추정으로 채운다.
    돌려주는 info: api_calls, road_pairs, fallback_pairs, error"""
    uniq: list[str] = []
    pos: dict[str, tuple[float, float]] = {}
    for p in points:
        k = _ck(p)
        if k not in pos:
            pos[k] = (round(p[0], 5), round(p[1], 5))
            uniq.append(k)
    need = [(a, b) for a in uniq for b in uniq if a != b]
    secs: dict[tuple[str, str], int] = {}
    info = {"api_calls": 0, "road_pairs": 0, "fallback_pairs": 0, "error": ""}
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
                    secs[(a, idx[n])] = sec
        for b in far:
            sec = call(X.kakao_direction, key, pos[a], pos[b])
            if sec is not None:
                secs[(a, b)] = sec
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
OSRM_STATUS = {
    'STARTING': '동봉 도로 자료 준비 중',
    'MISSING': '연결 주소와 동봉 도로 자료 없음',
    'INVALID_PACK': '동봉 도로 자료 또는 실행 파일 확인 필요',
    'FAILED': '동봉 OSRM을 시작하지 못함',
    'PACK_LIMIT': f'동봉 OSRM의 한 번 조회 한도({osrm.MAX_PACK_TABLE_POINTS}개 좌표) 초과',
    'TooBig': '로컬 OSRM 한 번에 조회할 수 있는 지점 수 초과',
    'NoSegment': '도로 자료 범위 또는 도로와의 거리 확인 필요',
    'NoTable': '지점 사이에 도로 연결 없음',
    'Ok': '도로에서 이동시간을 구하지 못한 지점 쌍이 있음',
    osrm.TOLL_EXCLUSION_UNAVAILABLE: '로컬 OSRM이 유료도로 제외를 지원하지 않음',
}


def build_matrix(points, osrm_url: str = "", method: str | None = None, kakao_key: str = "", osrm_status: str = '') -> tuple[list[list[int | None]], str, dict]:
    """(분 단위 이동시간, 출처, 정보)를 반환한다. None인 칸은 그 방향의 도로 연결 없음이다."""
    m = method or ("OSRM" if osrm_url else "ESTIMATE")
    if m == "KAKAO" and kakao_key:
        mat, info = kakao_matrix(points, kakao_key)
        if info["road_pairs"] == 0 and info["fallback_pairs"] > 0:
            return mat, "ESTIMATE", {**info, "requested": "KAKAO"}
        return mat, "KAKAO", info
    counts = {}
    for p in points:
        counts[tuple(p)] = counts.get(tuple(p), 0) + 1
    pairs = len(points)**2 - sum(n*n for n in counts.values())
    if m == "OSRM":
        if osrm_status == 'READY' and len(points) > osrm.MAX_PACK_TABLE_POINTS:
            return estimate_matrix(points), 'ESTIMATE', {'requested': 'OSRM', 'road_pairs': 0, 'unreachable_pairs': 0,
                                                         'fallback_pairs': pairs, 'status': 'PACK_LIMIT'}
        mat, road, unreachable, code = _osrm_matrix(points, osrm_url)
        status = code if isinstance(code, str) and code in OSRM_STATUS else osrm_status
        return mat, 'OSRM' if road or unreachable else 'ESTIMATE', {
            'requested': 'OSRM', 'road_pairs': road, 'unreachable_pairs': unreachable,
            'fallback_pairs': pairs - road - unreachable, 'status': status,
        }
    info = {'road_pairs': 0, 'fallback_pairs': pairs}
    if m == "KAKAO":
        info.update(requested='KAKAO', error='카카오모빌리티 REST 키가 없음')
    return estimate_matrix(points), 'ESTIMATE', info


def matrix_note(source: str | None, info: dict | None) -> str:
    info = info or {}
    n = info.get("fallback_pairs") or 0
    if source == "KAKAO":
        return "도로 시간: 카카오모빌리티 · 유료도로 제외" + (f" (일부 직선 추정 {n}쌍)" if n else "")
    if source == "OSRM":
        road_note = '(로컬 OSRM, 유료도로 제외)'
        if blocked := info.get('unreachable_pairs'):
            if info.get('road_pairs'):
                label = '도로·직선 혼합' if n else '도로 이동시간'
            else:
                label = '도로 이동시간 없음' if n else '이동 가능한 도로 연결 없음'
            return f"{label}{road_note} · 도로 연결 불가 {blocked}쌍(방향별)" + (f" · 직선 대체 {n}쌍" if n else "")
        return f"도로·직선 혼합{road_note} · 직선 대체 {n}쌍" if n else f"전체 실제 도로 이동시간{road_note}"
    if info.get("requested") == "KAKAO":
        return "직선거리 추정(도로 아님) — 카카오모빌리티로 구하지 못함" + (f": {info['error']}" if info.get("error") else "")
    if info.get("requested") == "OSRM":
        reason = OSRM_STATUS.get(info.get('status'), '로컬 OSRM에서 이동시간을 구하지 못함')
        return f"전체 직선거리 추정(도로 아님) · 직선 대체 {n}쌍 · {reason}"
    return f"전체 직선거리 추정(도로 아님) · 추정 {n}쌍"


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


COST = 10            # 하차 시간과 적재 벌점 비교 단위
BASE_FIRST_SHARE = 0.4  # "먼저·바닥" 품목을 실으러 갈 때 적재함이 40% 넘으면 동률 비용을 더한다.


def _break_window(lunch: dict | None):
    """점심(쉬는) 시간: start~end 사이 어딘가에 minutes 분. 설정이 없거나 잘못되면 None."""
    if not lunch or not lunch.get("start") or not lunch.get("end"):
        return None
    lo, hi, d = to_min(lunch["start"], 0), to_min(lunch["end"], 0), int(lunch.get("minutes") or 0)
    if d <= 0 or hi - lo < d:
        return None
    return lo, hi, d


MAX_TIME_LIMIT_S = 60  # 노선 계산 한 번의 최대 시간(초). 요청 스레드·CPU를 오래 붙잡지 않게


def clamp_time_limit(v, default: int = 5) -> int:
    """화면·API에서 온 계산 시간(초)을 1~MAX_TIME_LIMIT_S 정수로. 숫자가 아니면 기본값."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_TIME_LIMIT_S, n))


def plan(requests: list[dict], crews: list[dict], depot: dict, *, osrm_url: str = "", service_base: int = 5,
         service_per_item: int = 3, time_limit_s: int = 5, today: str = "", volume_overrides: dict | None = None,
         unload_site: dict | None = None, unload_min: int = 20, max_unloads: int = 2, end_site: dict | None = None,
         crew_pref: dict | None = None, pref_penalty: int = 30, travel_method: str | None = None, kakao_key: str = "",
         unload_hours: dict | None = None, lunch: dict | None = None,
         base_first: bool = True, crew_depots: dict | None = None, crew_required: dict[str, str | None] | None = None,
         fixed_plan: dict | None = None, osrm_status: str = '', final_unload_return: bool = False) -> dict:
    """requests: id, lat, lon, items, appt_start/end, must_today, urgent, priority, waiting_days
    crews: id, name, capacity_m3, capacity_kg(0·없음 = 무게 안 따짐), shift_start, shift_end
    crew_pref: {request_id: 선호 팀 id} (행정동 기본 담당 팀, 약한 선호)
    crew_required: {request_id: 담당 팀 id}, 등록 동의 담당팀 미연결은 None (자동 추천에서 미배정)
    unload_hours: {"start","end"} 반입지가 받는 시간 / lunch: {"start","end","minutes"} 그 사이에 쉬는 시간
    final_unload_return: 종료지 대신 반입지에서 최종 하차한 뒤 팀별 출발 차고지로 복귀"""
    fixed_crews = {}
    excluded = []
    required_crew = crew_required or {}
    if fixed_plan is not None:
        by_id = {r['id']: r for r in requests}
        crew_ids = {c['id'] for c in crews}
        if not isinstance(fixed_plan, dict) or not fixed_plan:
            raise UserError('순서 재계산에 사용할 팀과 접수를 확인하세요.')
        for cid, ids in fixed_plan.items():
            if cid not in crew_ids or not isinstance(ids, list):
                raise UserError('선택한 수거팀과 접수 순서를 확인하세요.')
            for rid in ids:
                if not isinstance(rid, str) or rid not in by_id or rid in fixed_crews:
                    raise UserError('없는 접수 또는 중복 접수가 포함되어 있습니다. 목록을 다시 확인하세요.')
                if by_id[rid].get('lat') is None or by_id[rid].get('lon') is None:
                    raise UserError('위치 없는 접수는 먼저 배출 위치를 지정하세요.')
                if required_crew.get(rid) not in (None, cid):
                    raise UserError('이 동의 고정 담당 수거팀으로 배정하세요.')
                fixed_crews[rid] = cid
        if not fixed_crews:
            raise UserError('재계산할 접수를 하나 이상 배정하세요.')
        excluded = [{'request_id': r['id'], 'reason': '담당자가 이번 추천안에서 제외', 'must_today': bool(r.get('must_today'))} for r in requests if r['id'] not in fixed_crews]
        requests = [r for r in requests if r['id'] in fixed_crews]
    unassigned = excluded + [{"request_id": r["id"], "reason": "배출 위치 좌표가 없음 (위치 지정 필요)",
                              **({"must_today": True} if r.get("must_today") else {})} for r in requests if r.get("lat") is None]
    selected = {c['id'] for c in crews}
    unavailable = {}
    for r in requests:
        rid = r['id']
        if rid not in required_crew:
            continue
        team = required_crew[rid]
        if team is None and fixed_plan is None:
            unavailable[rid] = '이 동에 담당 수거팀이 연결되어 있지 않습니다. 행정동 설정에서 담당팀을 지정하세요.'
        elif team is not None and team not in selected:
            unavailable[rid] = '이 동의 담당 수거팀이 선택되지 않았거나 사용 중지 상태입니다.'
    unassigned.extend({'request_id': r['id'], 'reason': unavailable[r['id']],
                       **({'must_today': True} if r.get('must_today') else {})} for r in requests if r['id'] in unavailable and r.get('lat') is not None)
    reqs = [r for r in requests if r.get("lat") is not None and r['id'] not in unavailable]
    depots = {c['id']: dict((crew_depots or {}).get(c['id']) or depot) for c in crews}
    if not crews:
        return {"routes": [], "unassigned": unassigned + [{"request_id": r["id"], "reason": "선택한 팀이 없음"} for r in reqs],
                "matrix_source": None}
    if not reqs:
        return {"routes": [{"crew_id": c["id"], "crew_name": c["name"], "stops": [], "end": None, "depot": depots[c['id']]} for c in crews],
                "unassigned": unassigned, "matrix_source": None}
    starts = [to_min(c.get("shift_start"), 540) for c in crews]
    ends = [to_min(c.get("shift_end"), 1020) for c in crews]
    info = {}  # request_id → 부피·무게·작업분·취급 표시 (하루 물량 점검은 위치 없는 건까지 셈)
    for r in requests:
        its = r.get("items", [])
        info[r["id"]] = {"m3": sum(volume_of(i, volume_overrides) for i in its), "kg": sum(weight_of(i) for i in its),
                         "svc": work_min(its, service_base, service_per_item), "tags": tags_of(its)}
    min_cap = max(0.01, min(float(c.get("capacity_m3", 8)) for c in crews))
    kg_caps = [float(c.get("capacity_kg") or 0) for c in crews]
    min_kg = min(kg_caps) if all(kg_caps) else 0
    by_crew = {c['id']: c for c in crews}
    visits = []
    for r in reqs:
        lo, hi = to_min(r.get("appt_start"), min(starts)), to_min(r.get("appt_end"), max(ends))
        x = info[r["id"]]
        required = fixed_crews.get(r['id']) if fixed_plan is not None else required_crew.get(r['id'])
        visits.append({"point": (r["lat"], r["lon"]), "required": required, "members": [(r, x["svc"])], "lo": lo, "hi": hi,
                       "service": x["svc"], "volume": x["m3"], "kg": x["kg"], "must": bool(r.get("must_today")), "base": "BASE" in x["tags"]})
    invalid_ids = {v['members'][0][0]['id'] for v in visits if v['hi'] - v['lo'] < v['service']}
    if invalid_ids:
        unassigned += [{'request_id': r['id'], 'reason': '방문 약속 시간보다 작업 시간이 깁니다. 시간을 조정하세요.', 'must_today': bool(r.get('must_today'))} for r in reqs if r['id'] in invalid_ids]
        if fixed_plan is not None:
            return {'routes': [], 'infeasible': True, 'matrix_source': None, 'unassigned': unassigned}
        visits = [v for v in visits if v['members'][0][0]['id'] not in invalid_ids]
        reqs = [r for r in reqs if r['id'] not in invalid_ids]
        if not visits:
            return {'routes': [], 'matrix_source': None, 'unassigned': unassigned}

    scale = 100  # 부피를 정수로 (0.01m³ 단위)
    caps = [max(1, int(float(c.get("capacity_m3", 8)) * scale)) for c in crews]  # 0·음수 용량은 계산기를 멈추게 하므로 막는다
    use_kg = any(cap > 0 for cap in kg_caps)
    kcaps = [max(0, int(cap)) or 10 ** 6 for cap in kg_caps]
    has_unload_site = bool(unload_site and unload_site.get("lat") is not None and unload_site.get("lon") is not None)
    use_end = bool(end_site and end_site.get("lat") is not None and end_site.get("lon") is not None)
    use_unload = has_unload_site and max_unloads > 0
    final_unload = has_unload_site and final_unload_return and not use_end
    unload_duration = int(unload_min)
    uh = (to_min((unload_hours or {}).get("start"), 0), to_min((unload_hours or {}).get("end"), 24 * 60)) if unload_hours else None
    garage_points = [(depots[c['id']]['lat'], depots[c['id']]['lon']) for c in crews]
    unload_point = (float(unload_site['lat']), float(unload_site['lon'])) if use_unload or final_unload else None
    end_point = (float(end_site['lat']), float(end_site['lon'])) if use_end else None
    sites = [p for p in (unload_point, end_point) if p is not None]
    # 같은 좌표의 방문·하차 노드는 이동시간을 한 번만 조회한다.
    matrix_points = list(dict.fromkeys([garage_points[0], *(v['point'] for v in visits), *sites, *garage_points[1:]]))
    matrix_index = {p: i for i, p in enumerate(matrix_points)}
    travel_minutes, source, minfo = build_matrix(matrix_points, osrm_url, travel_method, kakao_key, osrm_status)
    matrix_info = {k: v for k, v in minfo.items() if k in ('api_calls', 'road_pairs', 'unreachable_pairs', 'fallback_pairs', 'requested', 'status')}
    has_unreachable = bool(matrix_info.get('unreachable_pairs'))
    # 공급자별 행렬 조회는 한 번이며, 아래 모든 탐색은 같은 마감 시각을 공유한다.
    deadline = time.monotonic() + clamp_time_limit(time_limit_s)

    def finish_unload(clock, to_site, to_garage):
        if to_site is None or to_garage is None:
            return None
        arrival = max(clock + to_site, uh[0] if uh else 0)
        unloaded = arrival + unload_duration
        return unloaded + to_garage if not uh or unloaded <= uh[1] else None

    def can_group(group, visit, group_cap, group_kg):
        if group['required'] != visit['required'] or group['must'] != visit['must']:
            return False
        if haversine_km(group['point'], visit['point']) > SAME_PLACE_KM:
            return False
        previous = group['members'][-1][0]
        a = matrix_index[(previous['lat'], previous['lon'])]
        b = matrix_index[visit['point']]
        if any(minutes is None or minutes > SAME_PLACE_TRAVEL_MIN for minutes in (travel_minutes[a][b], travel_minutes[b][a])):
            return False
        weight = group['kg'] + visit['kg']
        if group['volume'] + visit['volume'] > group_cap or (group_kg and weight > group_kg):
            return False
        lo, hi = max(group['lo'], visit['lo']), min(group['hi'], visit['hi'])
        duration = group['service'] + visit['service']
        node = matrix_index[group['point']]
        # 계산은 묶음 대표 좌표를 쓰지만 실제 다음 이동은 묶음의 마지막 주소에서 출발한다.
        if has_unreachable and any((x is None) != (y is None) for x, y in zip(travel_minutes[node], travel_minutes[b])):
            return False
        # 한 팀이 차고지에서 나와 묶음 작업과 복귀를 모두 끝낼 수 있는 경우에만 묶는다.
        for k, crew in enumerate(crews):
            if group['required'] not in (None, crew['id']) or (kg_caps[k] > 0 and weight > kg_caps[k]):
                continue
            departure = matrix_index[garage_points[k]]
            return_to = matrix_index[end_point] if use_end else departure
            outgoing = travel_minutes[departure][node]
            if outgoing is None:
                continue
            worked = max(lo, starts[k] + outgoing) + duration
            if worked > hi:
                continue
            if final_unload:
                site = matrix_index[unload_point]
                finish = finish_unload(worked, travel_minutes[node][site], travel_minutes[site][return_to])
            else:
                returning = travel_minutes[node][return_to]
                finish = worked + returning if returning is not None else None
            if finish is not None and finish <= ends[k]:
                return True
        return False

    # 자동 추천만 가까운 접수를 묶는다. 필수 건은 일반 접수의 배정 가능 여부와 분리한다.
    groups = []
    for visit in visits:
        vehicle = by_crew.get(visit['required'])
        group_cap = max(0.01, float(vehicle.get('capacity_m3', 8))) if vehicle else min_cap
        group_kg = float(vehicle.get('capacity_kg') or 0) if vehicle else min_kg
        group = next((g for g in groups if can_group(g, visit, group_cap, group_kg)), None) if fixed_plan is None else None
        if group is None:
            groups.append({**visit, 'members': visit['members'].copy()})
            continue
        group['members'].extend(visit['members'])
        group['lo'], group['hi'] = max(group['lo'], visit['lo']), min(group['hi'], visit['hi'])
        group['service'] += visit['service']
        group['volume'] += visit['volume']
        group['kg'] += visit['kg']
        group['base'] = group['base'] or visit['base']

    def nodes_for(groups):
        points = [garage_points[0], *(g['point'] for g in groups)]
        n_groups = len(groups)
        mid_count = len(crews) * max_unloads if use_unload else 0
        n_unloads = mid_count + (len(crews) if final_unload else 0)
        first_unload = 1 + n_groups
        unload_nodes = list(range(first_unload, first_unload + mid_count))
        final_nodes = list(range(first_unload + mid_count, first_unload + n_unloads))
        points.extend([unload_point] * n_unloads)
        end_node = len(points)
        if use_end:
            points.append(end_point)
        tail = [0] if use_end else []
        service = [0] + [g['service'] for g in groups] + [unload_duration] * n_unloads + tail
        demand = [0] + [round(g['volume'] * scale) for g in groups] + [-max(caps)] * n_unloads + tail
        kdemand = [0] + [round(g['kg']) for g in groups] + [-max(kcaps)] * n_unloads + tail
        depot_nodes = {points[0]: 0}
        start_nodes = []
        for coords in garage_points:
            if coords not in depot_nodes:
                depot_nodes[coords] = len(points)
                points.append(coords)
                service.append(0); demand.append(0); kdemand.append(0)
            start_nodes.append(depot_nodes[coords])
        end_nodes = [end_node] * len(crews) if use_end else start_nodes
        indices = [matrix_index[p] for p in points]
        tmat = [[travel_minutes[i][j] for j in indices] for i in indices]
        return points, unload_nodes, final_nodes, service, demand, kdemand, start_nodes, end_nodes, tmat

    brk = _break_window(lunch)
    n_requests = len(reqs)
    must_count = sum(v['must'] for v in visits)
    soft_scores = {r['id']: 3_000 * int(r.get('priority') or 0) + 300 * int(r.get('waiting_days') or 0)
                   + (20_000 if r.get('urgent') else 0) for r in reqs}
    soft_min = min(soft_scores.values())
    soft_scores = {rid: score - soft_min for rid, score in soft_scores.items()}
    unload_cost = COST * unload_duration if use_unload else 0
    base_cost_limit = sum(v['base'] for v in visits) * max(caps) if base_first else 0
    soft_limit = sum(soft_scores.values()) + len(crews) * max_unloads * unload_cost + base_cost_limit
    drive_limit = sum(max(0, end - start) for start, end in zip(starts, ends))
    # 이 한도보다 큰 벌점은 어떤 이동시간 차이보다 크므로 순위를 유지하며 줄일 수 있다.
    pref_minutes = min(max(0, int(pref_penalty)), drive_limit + 1) if len(crews) > 1 and crew_pref else 0
    route_cost_limit = drive_limit + n_requests * pref_minutes
    # 하위 비용의 합보다 상위 단위가 크게 한다. 두 묶음 모델에 같은 가중치를 쓴다.
    drop_units = n_requests + (n_requests + 1) * must_count
    max_soft = max(1, (1 << 60) // ((drop_units + 1) * (route_cost_limit + 1)) - 1)
    # 큰 업무 점수는 동률 비용만 축소해 OR-Tools의 64비트 비용 범위를 확보한다.
    divisor = max(1, (soft_limit + max_soft - 1) // max_soft)
    route_weight = soft_limit // divisor + 1
    drop_weight = (route_cost_limit + 1) * route_weight
    must_weight = (n_requests + 1) * drop_weight

    def set_limit(params, until):
        remaining_ms = int((until - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            return False
        params.time_limit.FromMilliseconds(remaining_ms)
        return True

    def search(model, params, until, seed=None):
        if not set_limit(params, until):
            return None
        if seed is not None:
            return model.SolveFromAssignmentWithParameters(seed, params)
        return model.SolveWithParameters(params)

    def solve(groups, nodes, must_hard, until):
        points, unload_nodes, final_nodes, service, demand, kdemand, start_nodes, end_nodes, tmat = nodes
        n_groups, unload_set = len(groups), set(unload_nodes)
        n, v = len(points), len(crews)
        mgr = pywrapcp.RoutingIndexManager(n, v, start_nodes, end_nodes)
        model = pywrapcp.RoutingModel(mgr)
        index_nodes = [mgr.IndexToNode(idx) for idx in range(mgr.GetNumberOfIndices())]
        empty_ends = {model.Start(k): model.End(k) for k in range(v)}
        final_indices = [mgr.NodeToIndex(node) for node in final_nodes]
        final_ends = {idx: model.End(k) for k, idx in enumerate(final_indices)}
        final_returns = {idx: tmat[node][end_nodes[k]] for k, (idx, node) in enumerate(zip(final_indices, final_nodes))}
        idle_steps = {model.Start(k): idx for k, idx in enumerate(final_indices)}
        # 빈 팀도 수거지를 독립적으로 삽입할 수 있게 종료 전 노드를 둔다.
        # 수거지가 없는 차량에 대해서는 이 노드의 가상 시각과 가상 거리를 결과에 쓰지 않는다.
        clock_limit = 24 * 60 + (max((leg or 0) for leg in final_returns.values()) + unload_duration if final_indices else 0)
        if has_unreachable:
            # 같은 좌표의 종료지라도 차량마다 인덱스가 다르다. 빈 차량과 미배정 노드는 살린다.
            for idx in range(model.Size()):
                row = tmat[index_nodes[idx]]
                model.NextVar(idx).RemoveValues([nxt for nxt, node in enumerate(index_nodes)
                                                 if row[node] is None and nxt != idx and nxt != empty_ends.get(idx)
                                                 and nxt != idle_steps.get(idx) and nxt != final_ends.get(idx)])

        def drive(i, j):
            if empty_ends.get(i) == j or idle_steps.get(i) == j:
                return 0
            if final_ends.get(i) == j:
                return final_returns[i] or 0
            minutes = tmat[index_nodes[i]][index_nodes[j]]
            # 검색 중의 콜백은 이미 제외한 간선에서도 정수를 반환해야 한다.
            return minutes if minutes is not None else clock_limit + 1

        def transit(i, j):
            return drive(i, j) + service[index_nodes[i]]
        tcb = model.RegisterTransitCallback(transit)
        # 행정동 기본 담당 팀: 지점(묶음)마다 구성원 다수의 선호 팀 → 다른 팀이 들어오면 벌점
        crew_idx = {c["id"]: k for k, c in enumerate(crews)}
        node_pref: list[int | None] = [None] * n
        for node, g in enumerate(groups, start=1):
            if g['required'] is not None:
                model.VehicleVar(mgr.NodeToIndex(node)).SetValues([-1, crew_idx[g['required']]])
            prefs = [crew_idx[(crew_pref or {}).get(r["id"])] for r, _ in g["members"] if (crew_pref or {}).get(r["id"]) in crew_idx]
            if prefs:
                node_pref[node] = max(set(prefs), key=prefs.count)
        keep = []  # 콜백이 가비지 컬렉션되지 않게
        use_pref = bool(pref_minutes) and any(x is not None for x in node_pref)
        for k in range(v):
            def cost(i, j, k=k):
                a, b = index_nodes[i], index_nodes[j]
                if final_ends.get(i) == j:
                    return 0
                # 하차지→차고지 비용은 마지막 수거지에서 들어올 때만 낸다.
                tail = (final_returns.get(j) or 0) if idle_steps.get(i) != j else 0
                minutes = min(drive(i, j) + tail, drive_limit + 1)
                if use_pref and node_pref[b] not in (None, k):
                    minutes += pref_minutes
                side_cost = unload_cost if a in unload_set else 0
                return minutes * route_weight + side_cost // divisor
            keep.append(cost)
            model.SetArcCostEvaluatorOfVehicle(model.RegisterTransitCallback(cost), k)
        model.AddDimension(tcb, 600, clock_limit, False, "Time")
        tdim = model.GetDimensionOrDie("Time")
        if fixed_plan is not None:
            model.AddConstantDimension(1, len(points) + 1, True, 'VisitOrder')
            rank = model.GetDimensionOrDie('VisitOrder')
            indices = {g['members'][0][0]['id']: mgr.NodeToIndex(node) for node, g in enumerate(groups, start=1)}
            for ids in fixed_plan.values():
                for first, second in zip(ids, ids[1:]):
                    model.solver().Add(rank.CumulVar(indices[first]) < rank.CumulVar(indices[second]))
        for k in range(v):
            tdim.CumulVar(model.Start(k)).SetRange(starts[k], ends[k])
            tdim.CumulVar(model.End(k)).SetRange(starts[k], clock_limit if final_indices else ends[k])
        solver = model.solver()
        last_unload_start = uh[1] - unload_duration if uh else None
        for k, idx in enumerate(final_indices):
            model.VehicleVar(idx).SetValue(k)
            model.NextVar(idx).SetValue(model.End(k))
            active = solver.IsDifferentCstVar(model.NextVar(model.Start(k)), idx)
            if final_returns[idx] is None or (uh and last_unload_start < uh[0]):
                active.SetValue(0)
            solver.Add(tdim.CumulVar(model.End(k)) <= ends[k] + clock_limit * (1 - active))
            if uh and last_unload_start >= uh[0]:
                solver.Add(tdim.CumulVar(idx) >= uh[0] * active)
                solver.Add(tdim.CumulVar(idx) <= last_unload_start + clock_limit * (1 - active))
        for node, g in enumerate(groups, start=1):
            tdim.CumulVar(mgr.NodeToIndex(node)).SetRange(g["lo"], max(g["lo"], g["hi"] - service[node]))
        if uh:  # 반입지가 받는 시간 안에 도착·하차를 마치도록
            for node in unload_nodes:
                if uh[1] - uh[0] < unload_duration:
                    model.ActiveVar(mgr.NodeToIndex(node)).SetValue(0)
                else:
                    tdim.CumulVar(mgr.NodeToIndex(node)).SetRange(uh[0], uh[1] - unload_duration)
        breaks = []
        if brk:
            visit = [0] * model.Size()
            for idx in range(model.Size()):
                visit[idx] = service[mgr.IndexToNode(idx)]
            for k in range(v):
                first = max(brk[0], starts[k])
                last = min(brk[1], ends[k]) - brk[2]
                if first <= last:
                    iv = model.solver().FixedDurationIntervalVar(first, last, brk[2], False, f"lunch{k}")
                    tdim.SetBreakIntervalsOfVehicle([iv], k, visit)
                    breaks.append((k, iv))
        dims = [("Load", demand, caps, max(caps))]
        if use_kg:
            dims.append(("Weight", kdemand, kcaps, max(kcaps)))
        for name, dem, cp, mx in dims:
            cb = model.RegisterUnaryTransitCallback(lambda i, dem=dem: dem[mgr.IndexToNode(i)])
            keep.append(cb)
            # 반입지에서는 적재량을 0으로 되돌릴 수 있게 여유(slack)를 둔다. 일반 지점은 여유 0.
            model.AddDimensionWithVehicleCapacity(cb, mx if use_unload or final_unload else 0, cp, True, name)
            dim = model.GetDimensionOrDie(name)
            if use_unload or final_unload:
                for node in range(1, 1 + n_groups):
                    dim.SlackVar(mgr.NodeToIndex(node)).SetValue(0)
                for k in range(v):
                    dim.SlackVar(model.Start(k)).SetValue(0)
                for node in unload_nodes + final_nodes:
                    idx = mgr.NodeToIndex(node)
                    model.solver().Add(dim.CumulVar(idx) + dim.SlackVar(idx) == mx)
        ldim = model.GetDimensionOrDie("Load")
        if use_unload:
            for i, node in enumerate(unload_nodes):
                idx = mgr.NodeToIndex(node)
                model.AddDisjunction([idx], 0)  # 필요할 때만 들른다
                # 반입지 노드는 팀마다 max_unloads 개씩: 한 팀이 다른 팀 몫까지 써서 '차량당 최대 N회'를 넘지 않게.
                # (-1 = 들르지 않음. SetAllowedVehiclesForIndex 는 OR-Tools 9.15 파이썬 바인딩에서 목록을 받지 못해 VehicleVar 로 건다)
                model.VehicleVar(idx).SetValues([-1, i // max_unloads])
                if final_indices:
                    model.NextVar(idx).RemoveValues(final_indices)
                # 실제로 실은 물량이 있을 때만 반입지에 들를 수 있다.
                load_before = ldim.CumulVar(idx)
                if use_kg:
                    load_before += model.GetDimensionOrDie('Weight').CumulVar(idx)
                model.solver().Add(load_before >= model.ActiveVar(idx))
        if base_first and divisor == 1:  # 이동 비용이 같으면 해당 품목을 적재함이 비어 있을 때 싣기를 선호
            ub = int(min(caps) * BASE_FIRST_SHARE)
            for node, g in enumerate(groups, start=1):
                if g["base"]:
                    ldim.SetCumulVarSoftUpperBound(mgr.NodeToIndex(node), ub, 1)
        for node, g in enumerate(groups, start=1):
            if fixed_plan is not None:
                continue  # 수동으로 넣은 접수를 적재·시간 때문에 조용히 누락하지 않는다.
            if g["must"] and must_hard:
                continue
            weight = drop_weight + (must_weight if g['must'] else 0)
            penalty = len(g['members']) * weight + sum(soft_scores[r['id']] // divisor for r, _ in g['members'])
            model.AddDisjunction([mgr.NodeToIndex(node)], penalty)
        for k in range(v):
            model.AddVariableMinimizedByFinalizer(tdim.CumulVar(model.End(k)))
        params = pywrapcp.DefaultRoutingSearchParameters()
        if has_unreachable and fixed_plan is None:
            # 단독 왕복이 막혀도 여러 방문을 이어 운행할 수 있다. 간선을 먼저 잇는 방식으로 시작한다.
            params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.LOCAL_CHEAPEST_ARC
        elif use_unload or final_unload or brk:
            params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
        else:
            params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        result = (mgr, model, tdim, breaks, params)
        if fixed_plan is not None and (use_unload or final_unload):
            # 고정 순서·용량 조건에서 일반 삽입 휴리스틱은 필수 하차를 먼저 넣지 못할 수 있다.
            # 가능한 방문·하차 순서를 시작 해로 주고 같은 시간·운영 제약으로 검사한다.
            node_for = {g["members"][0][0]["id"]: node for node, g in enumerate(groups, start=1)}
            seed = []
            for k, crew in enumerate(crews):
                path, load, kg, count = [], 0, 0, 0
                for rid in fixed_plan.get(crew["id"], []):
                    node = node_for[rid]
                    if load + demand[node] > caps[k] or kg + kdemand[node] > kcaps[k]:
                        if not unload_nodes or count >= max_unloads or demand[node] > caps[k] or kdemand[node] > kcaps[k]:
                            break
                        path.append(unload_nodes[k * max_unloads + count])
                        load, kg, count = 0, 0, count + 1
                    path.append(node)
                    load += demand[node]
                    kg += kdemand[node]
                else:
                    route = path + ([final_nodes[k]] if final_nodes else [])
                    if not path or all(tmat[a][b] is not None for a, b in zip([start_nodes[k], *route], [*route, end_nodes[k]])):
                        seed.append(route)
            if len(seed) == len(crews) and set_limit(params, until):
                model.CloseModelWithParameters(params)
                start = model.ReadAssignmentFromRoutes(seed, True)
                if start:
                    solution = search(model, params, until, start)
                    if solution:
                        return solution, *result
        return search(model, params, until), *result

    def assigned_count(answer, candidates):
        assignment, manager, model, *_ = answer
        return sum(len(candidates[node - 1]['members']) for node in range(1, len(candidates) + 1)
                   if (idx := manager.NodeToIndex(node)) != assignment.Value(model.NextVar(idx)))

    candidates = groups
    grouped_nodes = nodes_for(candidates)
    compare_split = len(candidates) < len(visits)
    chosen_nodes = grouped_nodes
    split_nodes = None
    can_relax = must_count and fixed_plan is None
    for must_hard in ((True, False) if can_relax else (True,)):
        now = time.monotonic()
        if not must_hard and now >= deadline:
            break
        # 필수 전부를 넣지 못하면 일부를 살릴 시간을 남긴다. 묶음 비교도 같은 예산을 나눠 사용한다.
        round_end = now + (deadline - now) * 2 / 3 if can_relax and must_hard else deadline
        first_end = (now + round_end) / 2 if compare_split else round_end
        chosen = solve(candidates, grouped_nodes, must_hard, first_end)
        if compare_split and (not chosen[0] or assigned_count(chosen, candidates) < n_requests) and time.monotonic() < round_end:
            if split_nodes is None:
                split_nodes = nodes_for(visits)
            split = solve(visits, split_nodes, must_hard, deadline if chosen[0] else round_end)
            if split[0] and (not chosen[0] or split[0].ObjectiveValue() < chosen[0].ObjectiveValue()):
                chosen, chosen_nodes, groups = split, split_nodes, visits
        if chosen[0]:
            break

    sol, mgr, model, tdim, breaks, params = chosen
    if sol and (compare_split or can_relax and must_hard):
        refined = search(model, params, deadline, sol)
        if refined and refined.ObjectiveValue() <= sol.ObjectiveValue():
            sol = refined
    points, unload_nodes, final_nodes, service, demand, kdemand, start_nodes, end_nodes, tmat = chosen_nodes
    n_groups, unload_set, final_set = len(groups), set(unload_nodes), set(final_nodes)
    if not sol:
        issues = []
        if fixed_plan is not None:
            by_request = {g["members"][0][0]["id"]: (node, g) for node, g in enumerate(groups, start=1)}
            for k, crew in enumerate(crews):
                clock, prev, volume, weight, unloads = starts[k], start_nodes[k], 0, 0, 0
                ids = fixed_plan.get(crew["id"], [])
                for rid in ids:
                    node, group = by_request[rid]
                    too_big = demand[node] > caps[k] or kdemand[node] > kcaps[k]
                    exceeds = volume + demand[node] > caps[k] or weight + kdemand[node] > kcaps[k]
                    reason = None
                    if too_big:
                        reason = "이 접수의 부피 또는 중량이 차량 적재한도를 넘습니다."
                    elif exceeds:
                        if not unload_nodes or unloads >= max_unloads:
                            reason = "누적 적재한도를 넘습니다. 반입지 설정과 하차 횟수를 확인하세요."
                        else:
                            site = unload_nodes[0]
                            leg = tmat[prev][site]
                            if leg is None:
                                issues.append({"request_id": rid, "reason": "하차가 필요한 지점에서 반입지까지 도로 연결 불가"})
                                break
                            arrive = max(clock + leg, uh[0] if uh else 0)
                            if uh and arrive + unload_min > uh[1]:
                                reason = "적재한도 전에 필요한 하차를 반입지 운영시간 안에 마칠 수 없습니다."
                            else:
                                clock, prev, volume, weight = arrive + unload_min, site, 0, 0
                                unloads += 1
                    if reason:
                        issues.append({"request_id": rid, "reason": reason})
                    leg = tmat[prev][node]
                    if leg is None:
                        origin = '반입지' if prev in unload_set else '출발지' if prev == start_nodes[k] else '이전 방문지'
                        issues.append({"request_id": rid, "reason": f"현재 순서에서 {origin}부터 이 방문지까지 도로 연결 불가"})
                        break
                    clock = max(clock + leg, group["lo"]) + service[node]
                    prev, volume, weight = node, volume + demand[node], weight + kdemand[node]
                    if clock > group["hi"]:
                        r = group["members"][0][0]
                        if r.get("appt_end") or r.get("appt_start"):
                            issues.append({"request_id": rid, "reason": "이 순서에서 방문 약속 시간 안에 작업을 마칠 수 없습니다."})
                else:
                    if ids:
                        if final_nodes:
                            site = final_nodes[k]
                            to_site, to_garage = tmat[prev][site], tmat[site][end_nodes[k]]
                            finish = finish_unload(clock, to_site, to_garage)
                            if to_site is None:
                                reason = "마지막 방문지에서 반입지까지 도로 연결 불가"
                            elif to_garage is None:
                                reason = "반입지에서 차고지까지 도로 연결 불가"
                            elif finish is None:
                                reason = "최종 하차를 반입지 운영시간 안에 마칠 수 없습니다."
                            else:
                                reason = "최종 하차와 차고지 복귀가 팀 근무 종료 시각을 넘습니다." if finish > ends[k] else None
                        else:
                            leg = tmat[prev][end_nodes[k]]
                            destination = '종료지' if use_end else '차고지'
                            reason = (f"마지막 방문지에서 {destination}까지 도로 연결 불가" if leg is None
                                      else "마지막 방문과 복귀가 팀 근무 종료 시각을 넘습니다." if clock + leg > ends[k] else None)
                        if reason:
                            issues.append({"request_id": ids[-1], "reason": reason})
        default_issue = ("도로 연결·근무시간·쉬는 시간·반입지 조건을 함께 만족하는 노선을 찾지 못했습니다."
                         if has_unreachable else "해를 찾지 못함: 근무시간·쉬는 시간·반입지 시간을 확인하세요")
        return {"routes": [], "matrix_source": source, "infeasible": True, "matrix_note": matrix_note(source, minfo), "matrix_info": matrix_info,
                "unassigned": unassigned + (issues or [{"request_id": r["id"], "reason": default_issue} for r in reqs])}
    routes, visited = [], set()
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
        start_min = sol.Min(tdim.CumulVar(idx))
        drive_min, distance_km, work_minutes = 0, 0.0, 0
        idle = final_nodes and mgr.IndexToNode(sol.Value(model.NextVar(idx))) == final_nodes[k]
        if idle:
            idx = model.End(k)
            start_min = starts[k]
        while not model.IsEnd(idx):
            node = mgr.IndexToNode(idx)
            if node in unload_set or node in final_set:
                if stops:
                    t = sol.Min(tdim.CumulVar(idx))
                    unload_after = {"eta": hhmm(t), "label": unload_site.get("label") or "반입지",
                                    "volume_m3": round(load / scale, 2), "minutes": unload_duration}
                    if node in final_set:
                        unload_after["final"] = True
                    stops[-1]["unload_after"] = unload_after
                    stops[-1]["unload_kg"] = int(kg)
                    unloads += 1
                load, kg = 0, 0
            elif 1 <= node <= n_groups:
                g = groups[node - 1]
                work_minutes += g['service']
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
            next_idx = sol.Value(model.NextVar(idx))
            next_node = mgr.IndexToNode(next_idx)
            if not (idx == model.Start(k) and next_idx == model.End(k)):
                drive_min += tmat[node][next_node]
                distance_km += haversine_km(points[node], points[next_node])
            idx = next_idx
        lunch_at = hhmm(lunch_by[k]) if k in lunch_by and not idle else None
        finish = start_min if idle else sol.Min(tdim.CumulVar(idx))
        routes.append({"crew_id": c["id"], "crew_name": c["name"], "stops": stops, "end": hhmm(finish), "depot": depots[c['id']],
                       'start': hhmm(start_min), 'drive_min': drive_min, 'work_min': work_minutes,
                       'duration_min': finish - start_min, 'distance_km': round(distance_km, 2),
                       "capacity_m3": c.get("capacity_m3", 8), "capacity_kg": int(float(c.get("capacity_kg") or 0)), "unloads": unloads,
                       "end_load_m3": round(load / scale, 2), "end_load_kg": int(kg),
                       **({"lunch": {"start": lunch_at, "minutes": brk[2]}} if lunch_at else {})})

    road_reachable = set()
    if has_unreachable and len(visited) < len(reqs):
        def reachable(origin, candidates, reverse=False):
            remaining, frontier = set(candidates), [origin]
            while remaining and frontier:
                node = frontier.pop()
                neighbors = {other for other in remaining if (tmat[other][node] if reverse else tmat[node][other]) is not None}
                remaining.difference_update(neighbors)
                frontier.extend(neighbors)
            return candidates - remaining

        # 시간·적재를 빼고도 해당 팀이 들어가거나 나올 수 없는 방문을 찾는다.
        for k, crew in enumerate(crews):
            eligible = {node for node, g in enumerate(groups, 1) if g['required'] in (None, crew['id'])}
            if use_unload:
                eligible.add(unload_nodes[0])
            terminal = end_nodes[k]
            if final_nodes:
                terminal = final_nodes[k]
                if tmat[terminal][end_nodes[k]] is None:
                    continue
            between = reachable(start_nodes[k], eligible) & reachable(terminal, eligible, reverse=True)
            road_reachable.update(r['id'] for node in between if node <= n_groups for r, _ in groups[node - 1]['members'])

    cap_max = max(float(c.get("capacity_m3", 8)) for c in crews)
    kcap_max = max(float(c.get("capacity_kg") or 0) for c in crews) if use_kg and all(float(c.get("capacity_kg") or 0) > 0 for c in crews) else 0
    for r in reqs:
        if r["id"] not in visited:
            vol, wkg = info[r["id"]]["m3"], info[r["id"]]["kg"]
            vehicle = by_crew.get(required_crew.get(r['id']))
            max_m3 = float(vehicle.get('capacity_m3', 8)) if vehicle else cap_max
            max_kg = float(vehicle.get('capacity_kg') or 0) if vehicle else kcap_max
            disconnected = has_unreachable and r['id'] not in road_reachable
            reason = ("적재 추정치(%.1fm³)가 차량 1대 용량(%.1fm³)보다 큼" % (vol, max_m3) if vol > max_m3
                      else "무게 추정치(%dkg)가 차량 최대 적재 중량(%dkg)보다 큼" % (wkg, max_kg) if max_kg and wkg > max_kg
                      else ("도로 연결 불가: 마지막 수거 뒤 반입지 하차와 차고지 복귀를 마칠 수 없음" if final_nodes else
                            "도로 연결 불가: 가능한 수거팀의 출발지에서 방문한 뒤 종료지까지 운행할 수 없음") if disconnected
                      else "적재 추정치(%.1fm³)가 남은 차량 용량을 넘음" % vol if vol > min(float(c.get("capacity_m3", 8)) for c in crews) / 2 and not use_unload
                      else "도로 연결·근무시간·방문 약속 등 배정 조건을 함께 만족하는 노선을 찾지 못함" if has_unreachable
                      else "근무시간·방문 약속 시간 안에 들어가지 않음 (우선순위가 낮아 제외)")
            if r.get("must_today"):
                reason = "오늘 필수였지만 넣지 못함: " + reason.replace(" (우선순위가 낮아 제외)", "")
                if not disconnected:
                    reason += " — 팀을 더하거나 다른 건을 미루세요"
            unassigned.append({"request_id": r["id"], "reason": reason, **({"must_today": True} if r.get("must_today") else {})})
    relaxed = not must_hard and any(v['must'] and v['members'][0][0]['id'] not in visited for v in visits)
    return {"routes": routes, "unassigned": unassigned, "matrix_source": source, "must_relaxed": relaxed,
            "summary": day_summary(info, crews, use_unload, max_unloads, brk, use_kg),
            "unload_site": unload_site if use_unload or final_unload else None, "end_site": end_site if use_end else None,
            **({"final_unload_return": True} if final_unload else {}),
            "uses_weight": use_kg, "tag_labels": TAG_LABEL,
            "matrix_info": matrix_info,
            "matrix_note": matrix_note(source, minfo)}
