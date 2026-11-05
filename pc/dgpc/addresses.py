"""주소 자료(addr_point): 가져오기·좌표 찾기·주소 경고(관할·지번)·좌표 지정.
"""
from __future__ import annotations

import datetime as _dt
import re

from dgpc import geo
from dgpc.core import UserError, audit, chosung_of, get_setting, now, one, rows, set_setting, tx

def set_location(con, rid: str, lat: float, lon: float, source: str = "MANUAL"):
    if not (33 <= float(lat) <= 39 and 124 <= float(lon) <= 132):
        raise UserError("좌표가 국내 범위를 벗어났습니다.")
    with tx(con):
        con.execute("UPDATE request SET lat=?, lon=?, geo_source=?, updated_at=? WHERE id=?", (float(lat), float(lon), source, now(), rid))
        audit(con, "request", rid, "LOCATION", {"source": source})


# ------------------------------------------------------------------ 로컬 주소 → 좌표
def parse_addr_points(raw: bytes, region: str = "", columns: dict | None = None) -> dict:
    """주소 자료 파일 읽기(DB 없음 — 서버는 잠금 밖에서)."""
    try:
        return geo.read_points(raw, columns, region)
    except ValueError as e:
        raise UserError(str(e))


ADDR_STALE_DAYS = 45  # 주소 위치 자료가 이보다 오래되면 대시보드·운영 준비 점검에서 갱신 안내(행안부는 매월 갱신)


def import_addr_points(con, raw: bytes, region: str = "", columns: dict | None = None, preview: bool = False, parsed: dict | None = None) -> dict:
    res = parsed or parse_addr_points(raw, region, columns)
    pts = res["points"]
    if not pts:
        raise UserError("가져올 주소 좌표가 없습니다. 지역 이름(시군구) 거르기나 파일 형식을 확인하세요.")
    info = {"columns": {k: v for k, v in res["columns"].items()}, "delimiter": res["delimiter"], "count": len(pts),
            "sample": [{"sigungu": p[0], "road": p[1], "main": p[2], "sub": p[3], "lat": p[4], "lon": p[5], "dong_h": p[6], "dong_b": p[7], "bname": p[8],
                        "jibun": (f"{p[9]}" + (f"-{p[10]}" if p[10] else "")) if p[9] else "", "underground": p[11], "road_cd": p[12]} for p in pts[:5]],
            "has_dong": any(p[6] or p[7] for p in pts[:200]), "has_bname": any(p[8] for p in pts[:2000]), "has_jibun": any(p[9] for p in pts[:2000]),
            "has_codes": any(p[12] for p in pts[:2000])}
    if preview:
        return info
    # 같은 건물에 출입구가 여럿이면 출입구일련번호가 가장 작은 것(없으면 파일 앞쪽) 하나를 대표로 남긴다 — 정렬은 안정 정렬
    pts = sorted(pts, key=lambda p: p[14] if p[14] is not None else 10 ** 12)
    with tx(con):
        # 같은 주소가 이미 있으면: 좌표는 출입구일련번호가 더 작은(또는 처음 알게 된) 출입구일 때만 바꾸고, 비어 있던 동·건물명·지번·식별 번호를 채운다
        con.executemany("""INSERT INTO addr_point (sigungu, road, main, sub, lat, lon, dong_h, dong_b, bname, jibun_main, jibun_sub, underground, road_cd, bjd_cd, entr_sn, bname_cho)
            VALUES (?,?,?,?,?,?,NULLIF(?,''),NULLIF(?,''),NULLIF(?,''),?,?,?,NULLIF(?,''),NULLIF(?,''),?,NULLIF(?,''))
            ON CONFLICT(road, main, sub, sigungu, underground) DO UPDATE SET
            lat=CASE WHEN excluded.entr_sn IS NOT NULL AND (addr_point.entr_sn IS NULL OR excluded.entr_sn < addr_point.entr_sn) THEN excluded.lat ELSE addr_point.lat END,
            lon=CASE WHEN excluded.entr_sn IS NOT NULL AND (addr_point.entr_sn IS NULL OR excluded.entr_sn < addr_point.entr_sn) THEN excluded.lon ELSE addr_point.lon END,
            entr_sn=CASE WHEN excluded.entr_sn IS NOT NULL AND (addr_point.entr_sn IS NULL OR excluded.entr_sn < addr_point.entr_sn) THEN excluded.entr_sn ELSE addr_point.entr_sn END,
            dong_h=COALESCE(addr_point.dong_h, excluded.dong_h), dong_b=COALESCE(addr_point.dong_b, excluded.dong_b),
            bname=COALESCE(addr_point.bname, excluded.bname), bname_cho=COALESCE(addr_point.bname_cho, excluded.bname_cho),
            jibun_main=COALESCE(addr_point.jibun_main, excluded.jibun_main), jibun_sub=COALESCE(addr_point.jibun_sub, excluded.jibun_sub),
            road_cd=COALESCE(excluded.road_cd, addr_point.road_cd), bjd_cd=COALESCE(excluded.bjd_cd, addr_point.bjd_cd)""",
                        [(*p, chosung_of(re.sub(r"\s+", "", p[8] or ""))) for p in pts])
        audit(con, "address", "import", "IMPORT", {"count": len(pts), "region": region})
        info["total"] = one(con, "SELECT COUNT(*) AS n FROM addr_point")["n"]
        set_setting(con, "addr_import", {"at": now(), "count": len(pts), "total": info["total"], "region": region, "codes": info["has_codes"]})
    return info


def addr_stats(con) -> dict:
    """저장된 주소 위치 자료: 건수·시군구·식별 번호가 있는 건수·마지막으로 가져온 때(ADDR_STALE_DAYS 넘으면 stale)."""
    n = one(con, "SELECT COUNT(*) AS n, COUNT(road_cd) AS c FROM addr_point")
    last = get_setting(con, "addr_import") or {}
    age = _age_days(last.get("at"))
    return {"count": n["n"], "with_codes": n["c"], "imported_at": last.get("at"), "age_days": round(age, 1) if age is not None else None,
            "stale": bool(n["n"]) and (age is None or age > ADDR_STALE_DAYS), "stale_days": ADDR_STALE_DAYS,
            "sigungu": [r["sigungu"] for r in rows(con, "SELECT DISTINCT sigungu FROM addr_point ORDER BY sigungu LIMIT 20")]}


def _age_days(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return (_dt.datetime.now(_dt.timezone.utc) - t).total_seconds() / 86400


# ------------------------------------------------------------------ 행안부 검색 결과 → 이 PC 주소 위치 자료 (좌표 API 없이)
def _int(v) -> int | None:
    v = str(v if v is not None else "").strip()
    return int(v) if v.isdigit() else None


def match_juso(con, ref: dict) -> dict | None:
    """행안부 도로명주소 검색 결과(ref) → 이 PC 주소 위치 자료의 한 곳. 외부로 아무것도 보내지 않는다.
    1) 도로명코드(rnMgtSn 12자리)·지하여부·건물본번·부번 — 정확히 같은 건물
    2) 도로명(rn, 공백 없앰)·지하여부·건물본번·부번 — 관할 시군구 안에서만(식별 번호가 없는 예전 자료용)
    3) 지번: 법정동코드(admCd)·지번본번·부번, 없으면 법정동 이름(emdNm)·번지 — 관할 시군구 안, 산 번지는 빼고
    여러 곳이면 지상 먼저, 출입구일련번호·시군구·도로명 순으로 첫 곳. 돌려주는 값: {lat, lon, how, label, dong_h, dong_b}"""
    ref = ref or {}
    main, sub = _int(ref.get("buldMnnm")), _int(ref.get("buldSlno")) or 0
    ug = 1 if str(ref.get("udrtYn") or "0").strip() == "1" else 0
    adm = str(ref.get("admCd") or "").strip()
    adm = adm if re.fullmatch(r"\d{10}", adm) else ""
    rn_cd = str(ref.get("rnMgtSn") or "").strip()
    jm, js = _int(ref.get("lnbrMnnm")), _int(ref.get("lnbrSlno")) or 0
    san = str(ref.get("mtYn") or "0").strip() == "1"
    order = "ORDER BY (underground=?) DESC, entr_sn IS NULL, entr_sn, sigungu, road LIMIT 1"
    # 1) 식별 번호로 정확히: 도로명코드·지하여부·본번·부번
    if main and re.fullmatch(r"\d{12}", rn_cd):
        c = one(con, f"SELECT * FROM addr_point WHERE road_cd=? AND underground=? AND main=? AND sub=? {order}", (rn_cd, ug, main, sub, ug))
        if c:
            return _match_out(c, "CODE")
    # 2)·3) 이름으로 찾기: 관할 시군구(설정의 관할 구) 주소만. 식별 번호가 있는 자료는 시군구 코드(법정동코드 앞 5자리)도 맞아야 한다
    area = (get_setting(con, "jurisdiction") or "").strip()
    sgg = str(ref.get("sggNm") or "").strip()
    named = bool(area or sgg) and not (area and sgg and area not in sgg.split())
    sg_like = f"%{area or sgg}%"
    same_sgg, sgg_args = ("(bjd_cd IS NULL OR substr(bjd_cd, 1, 5)=?)", (adm[:5],)) if adm else ("1", ())
    rn = geo.norm_road(ref.get("rn") or "")
    if named and main and rn:
        # 지하여부도 같아야 한다: 지하 주소를 지상 건물(또는 그 반대) 좌표로 채우지 않게(없으면 좌표 API·지도 지정으로 넘어감)
        c = one(con, f"SELECT * FROM addr_point WHERE road=? AND main=? AND sub=? AND underground=? AND sigungu LIKE ? AND {same_sgg} {order}",
                (rn, main, sub, ug, sg_like, *sgg_args, ug))
        if c:
            return _match_out(c, "ROAD")
    if jm and not san:
        c = one(con, f"SELECT * FROM addr_point WHERE bjd_cd=? AND jibun_main=? AND jibun_sub=? {order}", (adm, jm, js, 0)) if adm else None
        emd = (ref.get("emdNm") or "").strip()
        if not c and named and emd:
            c = one(con, f"SELECT * FROM addr_point WHERE dong_b=? AND jibun_main=? AND jibun_sub=? AND sigungu LIKE ? AND {same_sgg} {order}",
                    (emd, jm, js, sg_like, *sgg_args, 0))
        if c:
            return _match_out(c, "JIBUN")
    return None


def _match_out(c: dict, how: str) -> dict:
    return {"lat": c["lat"], "lon": c["lon"], "how": how, "label": _addr_std(c), "dong_h": c.get("dong_h") or "", "dong_b": c.get("dong_b") or ""}


def geocode(con, address: str) -> dict | None:
    """주소 문자열 → 건물 출입구 좌표. 외부로 아무것도 보내지 않는다."""
    pa = geo.parse_address(address)
    if not pa:
        return None
    ug = pa.get("underground", 0)
    cands = rows(con, "SELECT * FROM addr_point WHERE road=? AND main=? AND sub=? ORDER BY (underground=?) DESC, entr_sn IS NULL, entr_sn",
                 (pa["road"], pa["main"], pa["sub"], ug))
    exact = True
    if not cands and pa["sub"]:
        cands, exact = rows(con, "SELECT * FROM addr_point WHERE road=? AND main=? ORDER BY sub, (underground=?) DESC", (pa["road"], pa["main"], ug)), False
    if not cands:
        return None
    if len(cands) > 1 and pa["sigungu_hint"]:
        narrowed = [c for c in cands if pa["sigungu_hint"] in (c["sigungu"] or "")]
        cands = narrowed or cands
    if len({c["sigungu"] for c in cands}) > 1:  # 같은 시군구의 지상·지하는 모호하지 않음(지하여부가 맞는 것 먼저)
        return {"ambiguous": True, "options": [c["sigungu"] for c in cands]}
    c = cands[0]
    label = f'{c["road"]} {c["main"]}' + (f'-{c["sub"]}' if c["sub"] else "")
    return {"lat": c["lat"], "lon": c["lon"], "label": label, "exact": exact, "sigungu": c["sigungu"], "dong_h": c.get("dong_h") or "", "dong_b": c.get("dong_b") or ""}


def _addr_std(c: dict) -> str:
    """주소 자료 한 줄 → 표준 문자열 '시군구 도로명 본번-부번 (법정동, 건물명)'."""
    base = f'{c["sigungu"] + " " if c["sigungu"] else ""}{c["road"]} {"지하 " if c.get("underground") else ""}{c["main"]}' + (f'-{c["sub"]}' if c["sub"] else "")
    ref = ", ".join(x for x in (c.get("dong_b") or "", c.get("bname") or "") if x)
    return base + (f" ({ref})" if ref else "")


def geocode_missing(con, date: str = "") -> dict:
    """위치 없는 접수를 주소 자료로 한꺼번에 찾는다(찾은 것은 출처 '주소 자료'로 표시, 지도에서 확인 필요)."""
    sql = "SELECT id, address FROM request WHERE lat IS NULL AND status NOT IN ('DONE','CANCELLED')" + (" AND planned_date=?" if date else "")
    found, missing = 0, 0
    for r in rows(con, sql, (date,) if date else ()):
        g = geocode(con, r["address"])
        if g and not g.get("ambiguous"):
            set_location(con, r["id"], g["lat"], g["lon"], source="ADDR")
            found += 1
        else:
            missing += 1
    return {"found": found, "missing": missing}



# ------------------------------------------------------------------ 관할 확인 (동별·구별 관할 불일치 경고, 막지는 않음)
_ROAD_RE = re.compile(r"[가-힣A-Za-z0-9·.]+(?:대로|로|길)\s*\d+(?![\d가])")  # '충장로1가 12'는 지번(법정동)
_JIBUN_RE = re.compile(r"[가-힣]+[0-9]*(?:동|리|가)\s*(?:산\s*)?\d")


def address_warnings(con, address: str) -> list[str]:
    """배출 주소 확인(막지 않음): 관할 밖 + 건물번호(번지) 없음·도로명/지번 형식 아님·주소칸의 전화번호."""
    a = re.sub(r"\(.*?\)", " ", address or "").strip()
    out = []
    if not a:
        return out
    if re.search(r"01[016789][-\s]?\d{3,4}[-\s]?\d{4}", address or ""):
        out.append("주소칸에 전화번호가 들어 있습니다. 연락처 칸으로 옮기세요.")
    if not re.search(r"\d", a):
        out.append("건물번호(번지)가 없습니다. 예: 예시로 12, 대인동 34-5")
    elif not (_ROAD_RE.search(a) or _JIBUN_RE.search(a)):
        out.append("도로명(…로·…길 + 건물번호) 또는 지번(…동 + 번지) 형식인지 확인하세요.")
    return jurisdiction_warnings(con, address) + out


def jurisdiction_warnings(con, address: str) -> list[str]:
    area = (get_setting(con, "jurisdiction") or "").strip()
    if not area:
        return []
    a = re.sub(r"\(.*?\)", " ", address or "")
    head = re.split(r"[가-힣A-Za-z0-9·.]+(?:대로|로|길)\s*\d", a)[0]  # 도로명 앞부분(시·구·동)만 본다
    units = re.findall(r"([가-힣]+(?:구|군))(?=\s|$)", head)
    if units and area not in units:
        return [f"주소의 '{units[0]}'이(가) 관할({area})과 다릅니다. 관할 구역을 확인하세요."]
    return []
