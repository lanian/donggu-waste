"""주소 자료(addr_point): 가져오기·좌표 찾기·주소 경고(관할·지번)·좌표 지정.
"""
from __future__ import annotations

import re

from dgpc import geo
from dgpc.core import UserError, audit, chosung_of, get_setting, now, one, rows, tx

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


def import_addr_points(con, raw: bytes, region: str = "", columns: dict | None = None, preview: bool = False, parsed: dict | None = None) -> dict:
    res = parsed or parse_addr_points(raw, region, columns)
    pts = res["points"]
    if not pts:
        raise UserError("가져올 주소 좌표가 없습니다. 지역 이름(시군구) 거르기나 파일 형식을 확인하세요.")
    info = {"columns": {k: v for k, v in res["columns"].items()}, "delimiter": res["delimiter"], "count": len(pts),
            "sample": [{"sigungu": p[0], "road": p[1], "main": p[2], "sub": p[3], "lat": p[4], "lon": p[5], "dong_h": p[6], "dong_b": p[7], "bname": p[8],
                        "jibun": (f"{p[9]}" + (f"-{p[10]}" if p[10] else "")) if p[9] else ""} for p in pts[:5]],
            "has_dong": any(p[6] or p[7] for p in pts[:200]), "has_bname": any(p[8] for p in pts[:2000]), "has_jibun": any(p[9] for p in pts[:2000])}
    if preview:
        return info
    with tx(con):
        # 같은 주소가 이미 있으면 좌표는 그대로 두고, 비어 있던 행정동·법정동만 채운다
        con.executemany("""INSERT INTO addr_point (sigungu, road, main, sub, lat, lon, dong_h, dong_b, bname, jibun_main, jibun_sub, bname_cho)
            VALUES (?,?,?,?,?,?,NULLIF(?,''),NULLIF(?,''),NULLIF(?,''),?,?,NULLIF(?,''))
            ON CONFLICT(road, main, sub, sigungu) DO UPDATE SET dong_h=COALESCE(addr_point.dong_h, excluded.dong_h), dong_b=COALESCE(addr_point.dong_b, excluded.dong_b),
            bname=COALESCE(addr_point.bname, excluded.bname), bname_cho=COALESCE(addr_point.bname_cho, excluded.bname_cho),
            jibun_main=COALESCE(addr_point.jibun_main, excluded.jibun_main), jibun_sub=COALESCE(addr_point.jibun_sub, excluded.jibun_sub)""",
                        [(*p, chosung_of(re.sub(r"\s+", "", p[8] or ""))) for p in pts])
        audit(con, "address", "import", "IMPORT", {"count": len(pts), "region": region})
    info["total"] = one(con, "SELECT COUNT(*) AS n FROM addr_point")["n"]
    return info


def addr_stats(con) -> dict:
    return {"count": one(con, "SELECT COUNT(*) AS n FROM addr_point")["n"],
            "sigungu": [r["sigungu"] for r in rows(con, "SELECT DISTINCT sigungu FROM addr_point ORDER BY sigungu LIMIT 20")]}


def geocode(con, address: str) -> dict | None:
    """주소 문자열 → 건물 출입구 좌표. 외부로 아무것도 보내지 않는다."""
    pa = geo.parse_address(address)
    if not pa:
        return None
    cands = rows(con, "SELECT * FROM addr_point WHERE road=? AND main=? AND sub=?", (pa["road"], pa["main"], pa["sub"]))
    exact = True
    if not cands and pa["sub"]:
        cands, exact = rows(con, "SELECT * FROM addr_point WHERE road=? AND main=? ORDER BY sub", (pa["road"], pa["main"])), False
    if not cands:
        return None
    if len(cands) > 1 and pa["sigungu_hint"]:
        narrowed = [c for c in cands if pa["sigungu_hint"] in (c["sigungu"] or "")]
        cands = narrowed or cands
    if len({c["sigungu"] for c in cands}) > 1:
        return {"ambiguous": True, "options": [c["sigungu"] for c in cands]}
    c = cands[0]
    label = f'{c["road"]} {c["main"]}' + (f'-{c["sub"]}' if c["sub"] else "")
    return {"lat": c["lat"], "lon": c["lon"], "label": label, "exact": exact, "sigungu": c["sigungu"], "dong_h": c.get("dong_h") or "", "dong_b": c.get("dong_b") or ""}


def _addr_std(c: dict) -> str:
    """주소 자료 한 줄 → 표준 문자열 '시군구 도로명 본번-부번 (법정동, 건물명)'."""
    base = f'{c["sigungu"] + " " if c["sigungu"] else ""}{c["road"]} {c["main"]}' + (f'-{c["sub"]}' if c["sub"] else "")
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
