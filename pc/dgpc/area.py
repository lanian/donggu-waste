"""행정동 관리·휴무일·날짜별 수거 한도·예약 달력.

- 행정동(dong 표): 사용 여부, 기본 담당 수거팀(노선 계산의 약한 선호), 수거 요일, 일일 한도(건수·m³), 주소 자동 판별 낱말.
  동구 기본 행정동은 DB 이전 단계에서 누락분만 등록한다. 시험용 가상 동은 sample_data.seed_dongs 가 넣는다.
- 접수의 동은 이름(문자열)으로 request.dong 에 둔다(목록·CSV·휴대폰 목록에 그대로 쓰기 위해). 동 이름을 바꾸면 접수도 함께 바꾼다.
- 휴무일(holiday 표)과 쉬는 요일(설정)은 담당자가 정한다. 공휴일을 미리 넣지 않는다.
- 한도는 경고만 하고 막지 않는다(담당자가 판단).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid

from dgpc import addresses as AD, core as C, fees as F, settings as SR

WEEKDAYS = "월화수목금토일"  # date.weekday(): 월=0 … 일=6
DEFAULT_CLOSED_WEEKDAYS = [5, 6]  # 기본: 토·일 수거 안 함 (설정에서 바꿈)


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _date(s: str) -> dt.date:
    if not re.fullmatch(r"\d{4}-\d\d-\d\d", s or ""):
        raise C.UserError("날짜 형식이 올바르지 않습니다 (YYYY-MM-DD).")
    return dt.date.fromisoformat(s)


def weekdays_label(days: list[int]) -> str:
    return "·".join(WEEKDAYS[d] for d in sorted(days))


# ------------------------------------------------------------------ 한도·쉬는 요일 설정
def capacity_settings(con) -> dict:
    c = C.get_setting(con, "capacity") or {}
    return {"daily_count": c.get("daily_count"), "daily_m3": c.get("daily_m3"),
            "closed_weekdays": c.get("closed_weekdays", DEFAULT_CLOSED_WEEKDAYS),
            "prefer_dong_crew": c.get("prefer_dong_crew", True)}


def _opt_int(v, what, lo=0, hi=100000):
    if v in (None, ""):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise C.UserError(f"{what}은(는) 숫자로 적어 주세요.")
    if not lo <= n <= hi:
        raise C.UserError(f"{what}은(는) {lo}~{hi} 사이로 적어 주세요.")
    return n


def _opt_float(v, what, lo=0.0, hi=10000.0):
    if v in (None, ""):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise C.UserError(f"{what}은(는) 숫자로 적어 주세요.")
    if not lo <= n <= hi:
        raise C.UserError(f"{what}은(는) {lo:g}~{hi:g} 사이로 적어 주세요.")
    return n


def _weekday_list(v) -> list[int]:
    if isinstance(v, str):
        v = [x for x in re.split(r"[,\s]+", v) if x]
    out = sorted({int(x) for x in (v or [])})
    if any(not 0 <= x <= 6 for x in out):
        raise C.UserError("요일 값이 올바르지 않습니다.")
    return out


@SR.contribute("capacity")
def _settings_part(con) -> dict:
    return {"capacity": capacity_settings(con)}


def save_capacity(con, data: dict):
    """전체 일일 한도·쉬는 요일·동 담당 팀 선호를 저장한다(호출하는 쪽이 트랜잭션을 연다)."""
    cur = capacity_settings(con)
    if "daily_count" in data:
        cur["daily_count"] = _opt_int(data["daily_count"], "일일 한도(건수)", 1)
    if "daily_m3" in data:
        cur["daily_m3"] = _opt_float(data["daily_m3"], "일일 한도(m³)", 0.1)
    if "closed_weekdays" in data:
        cur["closed_weekdays"] = _weekday_list(data["closed_weekdays"])
        if len(cur["closed_weekdays"]) == 7:
            raise C.UserError("모든 요일을 쉬는 날로 정할 수 없습니다.")
    if "prefer_dong_crew" in data:
        cur["prefer_dong_crew"] = bool(data["prefer_dong_crew"])
    C.set_setting(con, "capacity", cur)
    return cur


# ------------------------------------------------------------------ 휴무일
def list_holidays(con, start: str = "", end: str = "") -> list[dict]:
    sql, args = "SELECT day, label FROM holiday WHERE 1=1", []
    if start:
        sql += " AND day>=?"; args.append(start)
    if end:
        sql += " AND day<=?"; args.append(end)
    return C.rows(con, sql + " ORDER BY day", args)


def add_holiday(con, day: str, label: str = ""):
    _date(day)
    label = (label or "").strip()[:30]
    with C.tx(con):
        con.execute("INSERT INTO holiday (day, label, created_at) VALUES (?,?,?) ON CONFLICT(day) DO UPDATE SET label=excluded.label", (day, label, C.now()))
        C.audit(con, "holiday", day, "SAVE", {"label": label})


def delete_holiday(con, day: str):
    if not C.one(con, "SELECT 1 AS x FROM holiday WHERE day=?", (day,)):
        raise C.UserError("등록된 휴무일이 아닙니다.")
    with C.tx(con):
        con.execute("DELETE FROM holiday WHERE day=?", (day,))
        C.audit(con, "holiday", day, "DELETE", None)


# ------------------------------------------------------------------ 행정동
def _dong_row(r: dict) -> dict:
    from dgpc.dong_centers import center_for
    r['center_depot'] = center_for(r['name'])
    r['depot'] = json.loads(r.pop('depot_json', None) or 'null')
    r["keywords"] = json.loads(r.pop("keywords_json") or "[]")
    r["weekdays"] = [int(x) for x in (r["weekdays"] or "").split(",") if x != ""]
    r["weekdays_label"] = weekdays_label(r["weekdays"]) if r["weekdays"] else "매일(쉬는 요일 제외)"
    r["active"] = bool(r["active"])
    return r


def list_dongs(con, active_only: bool = False) -> list[dict]:
    out = C.rows(con, "SELECT d.*, c.name AS crew_name FROM dong d LEFT JOIN crew c ON c.id=d.crew_id"
                 + (" WHERE d.active=1" if active_only else "") + " ORDER BY d.sort, d.name")
    return [_dong_row(r) for r in out]


def get_dong(con, name: str) -> dict | None:
    r = C.one(con, "SELECT d.*, c.name AS crew_name FROM dong d LEFT JOIN crew c ON c.id=d.crew_id WHERE d.name=?", ((name or "").strip(),))
    return _dong_row(r) if r else None


def reorder_dongs(con, ids, base_ids) -> list[dict]:
    """전체 동을 한 번에 정렬한다. 읽은 뒤 바뀐 목록·순서는 덮어쓰지 않는다."""
    for value in (ids, base_ids):
        if (not isinstance(value, list) or any(not isinstance(did, str) or not did for did in value)
                or len(value) != len(set(value))):
            raise C.UserError("동 순서를 목록에서 다시 확인해 주세요.")
    with C.tx(con):
        current = [d["id"] for d in list_dongs(con)]
        if set(ids) != set(current) or set(base_ids) != set(current):
            raise C.StaleError("행정동 목록이 변경되었습니다. 입력 내용을 확인한 뒤 목록을 다시 열어 주세요.")
        if ids == current:  # 응답을 잃은 같은 요청의 재시도도 이력을 중복 기록하지 않는다.
            return list_dongs(con)
        if base_ids != current:
            raise C.StaleError("다른 창에서 동 순서를 변경했습니다. 입력 내용을 확인한 뒤 목록을 다시 열어 주세요.")
        now = C.now()
        con.executemany("UPDATE dong SET sort=?, updated_at=? WHERE id=?", [(i, now, did) for i, did in enumerate(ids)])
        C.audit(con, "dong", "order", "REORDER", {"from": current, "to": ids})
        return list_dongs(con)


def save_dong(con, data: dict) -> str:
    """행정동 추가·수정. 이름을 바꾸면 그 동으로 접수된 건의 동 이름도 함께 바꾼다(이력 기록)."""
    did = data.get("id") or None
    name = (data.get("name") or "").strip()
    if not name or len(name) > 20:
        raise C.UserError("동 이름을 20자 이내로 적어 주세요.")
    crew = data.get("crew_id") or None
    if crew and not C.one(con, "SELECT 1 AS x FROM crew WHERE id=?", (crew,)):
        raise C.UserError("없는 수거팀입니다.")
    days = _weekday_list(data.get("weekdays") or [])
    kws = data.get("keywords") or []
    if isinstance(kws, str):
        kws = re.split(r"[,\n]+", kws)
    kws = list(dict.fromkeys(k.strip()[:30] for k in kws if k and k.strip()))[:50]
    limit = _opt_int(data.get("daily_limit"), "동 일일 한도(건수)", 1)
    limit_m3 = _opt_float(data.get("daily_limit_m3"), "동 일일 한도(m³)", 0.1)
    code = (data.get("code") or "").strip() if "code" in data else None
    if code and not re.fullmatch(r"[0-9A-Za-z]{1,4}", code):
        raise C.UserError("동 코드(부과번호용)는 숫자·영문 1~4자로 적어 주세요.")
    dup = C.one(con, "SELECT id FROM dong WHERE name=?", (name,))
    if dup and dup["id"] != did:
        raise C.UserError("같은 이름의 동이 이미 있습니다.")
    old = C.one(con, "SELECT * FROM dong WHERE id=?", (did,)) if did else None
    if did and not old:
        raise C.UserError("동을 찾을 수 없습니다.")
    did = did or str(uuid.uuid4())
    from dgpc import route_depots as DP
    depot = DP.point(data['depot'], name + ' 차고지') if 'depot' in data else json.loads((old or {}).get('depot_json') or 'null')
    if depot and not crew:
        raise C.UserError('동별 차고지를 사용하려면 기본 담당 수거팀을 지정해 주세요.')
    with C.tx(con):
        if depot and data.get('active', True):
            other = [d for d in list_dongs(con) if d['id'] != did and d['active'] and d['crew_id'] == crew and d.get('depot') and not DP.same(depot, d['depot'])]
            if other:
                raise C.UserError(f"{other[0]['name']}과 같은 수거팀에 서로 다른 차고지를 지정할 수 없습니다. 담당팀을 확인해 주세요.")
        if "sort" in data:
            order = int(data.get("sort") or 0)
        elif old:
            current = C.one(con, "SELECT sort FROM dong WHERE id=?", (did,))
            if not current:
                raise C.UserError("동을 찾을 수 없습니다.")
            order = current["sort"]
        else:
            order = C.one(con, "SELECT COALESCE(MAX(sort), -1) + 1 AS next FROM dong")["next"]
        con.execute("""INSERT INTO dong (id, name, active, crew_id, weekdays, daily_limit, daily_limit_m3, keywords_json, sort, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, active=excluded.active, crew_id=excluded.crew_id,
            weekdays=excluded.weekdays, daily_limit=excluded.daily_limit, daily_limit_m3=excluded.daily_limit_m3, keywords_json=excluded.keywords_json,
            sort=excluded.sort, updated_at=excluded.updated_at""",
                    (did, name, 1 if data.get("active", True) else 0, crew, ",".join(map(str, days)), limit, limit_m3,
                     json.dumps(kws, ensure_ascii=False), order, C.now()))
        if code is not None:
            con.execute("UPDATE dong SET code=? WHERE id=?", (code or None, did))
        if 'depot' in data:
            con.execute('UPDATE dong SET depot_json=? WHERE id=?', (json.dumps(depot, ensure_ascii=False) if depot else None, did))
        renamed = 0
        if old and old["name"] != name:
            renamed = con.execute("UPDATE request SET dong=? WHERE dong=?", (name, old["name"])).rowcount
        C.audit(con, "dong", did, "SAVE" if old else "CREATE",
                {"name": name, "active": bool(data.get("active", True)), "crew_id": crew, "weekdays": weekdays_label(days) if days else "",
                 "limit": limit, "limit_m3": limit_m3, "keywords": len(kws), "depot": depot,
                 **({"renamed_from": old["name"], "n": renamed} if renamed or (old and old["name"] != name) else {})})
    return did


def delete_dong(con, did: str):
    d = C.one(con, "SELECT * FROM dong WHERE id=?", (did,))
    if not d:
        raise C.UserError("동을 찾을 수 없습니다.")
    if C.one(con, "SELECT 1 AS x FROM app_user_dong WHERE dong_id=? LIMIT 1", (did,)):
        raise C.UserError("담당자에게 연결된 동은 지울 수 없습니다. 담당 동을 먼저 변경하거나 '사용' 표시를 끄세요.")
    n = C.one(con, "SELECT COUNT(*) AS n FROM request WHERE dong=?", (d["name"],))["n"]
    if n:
        raise C.UserError(f"이 동으로 접수된 건이 {n}건 있어 지울 수 없습니다. '사용' 표시를 끄세요.")
    with C.tx(con):
        con.execute("DELETE FROM dong WHERE id=?", (did,))
        C.audit(con, "dong", did, "DELETE", {"name": d["name"]})


def detect_dong(con, address: str) -> dict:
    """주소 → 행정동 추정. 주소 위치 자료에 관할 행정동이 있으면 그것을, 아니면 동별 판별 낱말(법정동·도로명 등)로 찾는다.
    여러 동이 같은 정도로 맞으면 고르지 않고 후보만 돌려준다."""
    dongs = list_dongs(con, active_only=True)
    if not dongs or not (address or "").strip():
        return {"dong": None, "source": None, "candidates": []}
    a = _norm(address)
    g = None
    try:
        g = AD.geocode(con, address)
    except Exception:  # 주소 자료가 없거나 해석 못 함
        g = None
    gh = _norm((g or {}).get("dong_h") or "") if g and not g.get("ambiguous") else ""
    gb = _norm((g or {}).get("dong_b") or "") if g and not g.get("ambiguous") else ""
    scored = []
    for d in dongs:
        score, src = 0, None
        if gh and gh == _norm(d["name"]):
            score, src = 1000, "ADDR"
        for k in [d["name"], *d["keywords"]]:
            kk = _norm(k)
            if not kk:
                continue
            if kk in a or (gb and kk == gb) or (gh and kk == gh):
                if len(kk) > score:
                    score, src = len(kk), ("ADDR" if kk in (gb, gh) and kk not in a else "KEYWORD")
        if score:
            scored.append((score, d["name"], src))
    if not scored:
        return {"dong": None, "source": None, "candidates": []}
    scored.sort(key=lambda x: -x[0])
    cands = [n for _, n, _ in scored]
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return {"dong": None, "source": None, "candidates": cands, "ambiguous": True}
    return {"dong": scored[0][1], "source": scored[0][2], "candidates": cands}


# ------------------------------------------------------------------ 업무일·한도·달력
def _volume_items(con, items) -> float:
    from dgpc import routing  # OR-Tools 를 불러오는 무거운 모듈: 쓸 때만
    ov = F.volume_overrides(con)
    return sum(routing.volume_of(i, ov) for i in items)


def request_volume(con, rid: str) -> float:
    return _volume_items(con, C.rows(con, "SELECT name, spec, qty, fee_rate_id FROM request_item WHERE request_id=? AND removed=0", (rid,)))


def booked(con, day: str, dong: str | None = None, exclude_id: str | None = None) -> tuple[int, float]:
    """그날 수거 예정(취소 제외) 건수와 적재 추정 m³. dong 이 있으면 그 동만."""
    sql, args = "SELECT id FROM request WHERE planned_date=? AND status!='CANCELLED'", [day]
    if dong:
        sql += " AND dong=?"; args.append(dong)
    if exclude_id:
        sql += " AND id!=?"; args.append(exclude_id)
    ids = [r["id"] for r in C.rows(con, sql, args)]
    if not ids:
        return 0, 0.0
    items = C.rows(con, "SELECT name, spec, qty, fee_rate_id FROM request_item WHERE removed=0 AND request_id IN (%s)" % ",".join("?" * len(ids)), ids)
    return len(ids), round(_volume_items(con, items), 2)


def working_reason(con, day: str, dong: dict | None = None) -> str | None:
    """업무일이 아니면 그 까닭, 업무일이면 None."""
    d = _date(day)
    h = C.one(con, "SELECT label FROM holiday WHERE day=?", (day,))
    if h:
        return f"휴무일({h['label']})" if h["label"] else "휴무일"
    if d.weekday() in capacity_settings(con)["closed_weekdays"]:
        return f"{WEEKDAYS[d.weekday()]}요일 수거 안 함"
    if dong and dong.get("weekdays") and d.weekday() not in dong["weekdays"]:
        return f"{dong['name']} 수거 요일({weekdays_label(dong['weekdays'])}) 아님"
    return None


def next_working_day(con, day: str, dong: str | None = None, max_days: int = 120) -> str:
    """day 다음 업무일(휴무일·쉬는 요일·동 수거 요일 반영)."""
    dd = get_dong(con, dong) if dong else None
    d = _date(day)
    for _ in range(max_days):
        d += dt.timedelta(days=1)
        if working_reason(con, d.isoformat(), dd) is None:
            return d.isoformat()
    return (_date(day) + dt.timedelta(days=1)).isoformat()


def day_info(con, day: str, dong: str | None = None, exclude_id: str | None = None, add_m3: float = 0.0, add: int = 1) -> dict:
    cap = capacity_settings(con)
    dd = get_dong(con, dong) if dong else None
    n, m3 = booked(con, day, None, exclude_id)
    d = _date(day)
    info = {"date": day, "weekday": WEEKDAYS[d.weekday()], "reason": working_reason(con, day, dd), "booked": n, "booked_m3": m3,
            "limit": cap["daily_count"], "limit_m3": cap["daily_m3"], "dong": dd["name"] if dd else None,
            "closed": bool(C.close_info(con, day))}
    info["working"] = info["reason"] is None
    over = []
    if cap["daily_count"] and n + add > cap["daily_count"]:
        over.append(f"전체 한도 {cap['daily_count']}건 (이미 {n}건)")
    if cap["daily_m3"] and m3 + add_m3 > cap["daily_m3"] and (add or add_m3):
        over.append(f"전체 적재 한도 {cap['daily_m3']:g}m³ (이미 약 {m3:g}m³)")
    if dd:
        dn, dm3 = booked(con, day, dd["name"], exclude_id)
        info.update(dong_booked=dn, dong_booked_m3=dm3, dong_limit=dd["daily_limit"], dong_limit_m3=dd["daily_limit_m3"])
        if dd["daily_limit"] and dn + add > dd["daily_limit"]:
            over.append(f"{dd['name']} 한도 {dd['daily_limit']}건 (이미 {dn}건)")
        if dd["daily_limit_m3"] and dm3 + add_m3 > dd["daily_limit_m3"] and (add or add_m3):
            over.append(f"{dd['name']} 적재 한도 {dd['daily_limit_m3']:g}m³ (이미 약 {dm3:g}m³)")
    info["over"] = over
    info["available"] = info["working"] and not over
    return info


def calendar(con, start: str, days: int = 14, dong: str | None = None, exclude_id: str | None = None) -> list[dict]:
    """start 부터 업무일 days 개(휴무일·쉬는 요일·동 수거 요일 제외)의 예약 현황."""
    dd = get_dong(con, dong) if dong else None
    out, d = [], _date(start)
    for _ in range(days * 7 + 14):  # 수거 요일이 주 1회인 동도 days 개를 채우게
        if len(out) >= days:
            break
        if working_reason(con, d.isoformat(), dd) is None:
            out.append(day_info(con, d.isoformat(), dong, exclude_id))
        d += dt.timedelta(days=1)
    return out


def earliest_available(con, start: str, dong: str | None = None, exclude_id: str | None = None, add_m3: float = 0.0, max_days: int = 120) -> str | None:
    """start(포함)부터 업무일이면서 한도(전체·동)에 여유가 있는 가장 빠른 날."""
    dd = get_dong(con, dong) if dong else None
    d = _date(start)
    for _ in range(max_days):
        s = d.isoformat()
        if working_reason(con, s, dd) is None and not day_info(con, s, dong, exclude_id, add_m3)["over"]:
            return s
        d += dt.timedelta(days=1)
    return None


def capacity_warnings(con, day: str | None, dong: str | None = None, exclude_id: str | None = None, add_m3: float = 0.0) -> list[str]:
    """예정일 경고(막지 않음): 휴무일·쉬는 요일·동 수거 요일, 전체·동 한도 초과, 이미 마감한 날."""
    if not day:
        return []
    info = day_info(con, day, dong, exclude_id, add_m3)
    out = []
    if info["reason"]:
        out.append(f"{day}은(는) {info['reason']}입니다.")
    if info["over"]:
        out.append(f"{day} 수거 한도를 넘습니다: " + ", ".join(info["over"]) + ".")
    if info["closed"]:
        out.append(f"{day}은(는) 이미 마감한 날짜입니다.")
    return out
