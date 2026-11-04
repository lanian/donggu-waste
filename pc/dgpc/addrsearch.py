"""주소 검색: PC 주소 자료 검색 + 행정안전부 도로명주소 API(검색어 거름·접속기록은 건수만).
"""
from __future__ import annotations

import re

from dgpc import external as X, privacy as _pv, area, geo
from dgpc.addresses import _addr_std
from dgpc.config import secret
from dgpc.core import is_chosung, rows

def _known_names(con) -> list[str]:
    return [r["name"] for r in rows(con, "SELECT DISTINCT name FROM request WHERE pii_purged_at IS NULL AND name IS NOT NULL AND length(name) >= 2")]


def _dong_for_juso(con, it: dict) -> str | None:
    for n in (it.get("hemdNm"), it.get("emdNm")):
        if n and area.get_dong(con, n):
            return n
    addr = f'{it.get("road_part1") or it.get("road") or ""} {it.get("emdNm") or ""} {it.get("hemdNm") or ""}'
    return area.detect_dong(con, addr).get("dong")


def juso_search(con, q: str, name: str = "") -> dict:
    """PC가 대신 묻는 행정안전부 도로명주소 검색. 보내는 것은 검색어와 승인키뿐.
    검색어가 전화번호·신청인 이름·저장된 주민 이름을 담고 있으면 보내지 않는다. 접속기록에는 검색어 없이 건수만 남긴다."""
    key = secret(con, "juso_search_key")
    if not key:
        return {"configured": False, "items": []}
    kw = X.clean_keyword(q)
    if len(kw) < 2:
        return {"configured": True, "items": []}
    why = X.keyword_guard(kw, name, _known_names(con))
    if why:
        return {"configured": True, "items": [], "blocked": why}
    try:
        res = X.juso_search(key, kw)
    except X.ExternalError as e:
        _pv.record_access(con, "SEARCH", "", 0, "행안부 주소 검색 실패 (검색어 기록 안 함)")
        return {"configured": True, "items": [], "error": str(e), "code": e.code}
    items = []
    for it in res["items"]:
        addr = it["road_part1"] + (f' ({", ".join(x for x in (it["emdNm"], it["bdNm"]) if x)})' if it["emdNm"] or it["bdNm"] else "")
        items.append({"kind": "행안부", "address": addr, "label": addr + (f' · 지번 {it["jibun"]}' if it["jibun"] else ""), "zip": it["zip"],
                      "dong": _dong_for_juso(con, it), "emdNm": it["emdNm"], "hemdNm": it["hemdNm"], "bname": it["bdNm"], "ref": it["ref"],
                      "lat": None, "lon": None})
    _pv.record_access(con, "SEARCH", "", len(items), "행안부 주소 검색 (검색어 기록 안 함)")
    return {"configured": True, "items": items, "total": res["total"], "coord": bool(secret(con, "juso_coord_key"))}


def juso_coord(con, ref: dict) -> dict:
    """고른 행안부 검색 결과의 출입구 좌표(WGS84). 보내는 것은 좌표 승인키와 건물 식별 번호뿐."""
    key = secret(con, "juso_coord_key")
    if not key:
        return {"none": True, "reason": "좌표 승인키가 없어 위치는 지도에서 지정하세요."}
    try:
        lat, lon = X.juso_coord(key, ref)
    except X.ExternalError as e:
        return {"none": True, "reason": str(e), "code": e.code}
    return {"lat": lat, "lon": lon}


def addr_search(con, q: str, limit: int = 12) -> list[dict]:
    """주소 찾기 제안: 도로명(도로명+번호), 지번(법정동+번지), 건물명·아파트명(초성 가능). 이 PC의 주소 자료만 쓴다."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    found: list[tuple[str, dict]] = []
    seen = set()
    def add(kind, cs):
        for c in cs:
            k = (c["road"], c["main"], c["sub"], c["sigungu"])
            if k not in seen and len(found) < limit:
                seen.add(k); found.append((kind, c))
    pa = geo.parse_address(q)
    if pa:
        add("도로명", rows(con, "SELECT * FROM addr_point WHERE road=? AND main=? ORDER BY (sub=?) DESC, sub LIMIT ?", (pa["road"], pa["main"], pa["sub"], limit)))
    m = re.search(r"([가-힣0-9]+?(?:동|가|리))\s*(산\s*)?(\d+)(?:\s*-\s*(\d+))?\s*$", q)
    if m and not pa:
        args = [f"%{m.group(1)}%", int(m.group(3))]
        sql = "SELECT * FROM addr_point WHERE dong_b LIKE ? AND jibun_main=?"
        if m.group(4):
            sql += " AND jibun_sub=?"; args.append(int(m.group(4)))
        add("지번", rows(con, sql + " ORDER BY jibun_sub LIMIT ?", args + [limit]))
    qn = re.sub(r"\s+", "", q)
    if is_chosung(qn):
        add("건물명", rows(con, "SELECT * FROM addr_point WHERE bname_cho LIKE ? ORDER BY length(bname), bname LIMIT ?", (f"%{qn}%", limit)))
    else:
        add("건물명", rows(con, "SELECT * FROM addr_point WHERE REPLACE(bname,' ','') LIKE ? ORDER BY length(bname), bname LIMIT ?", (f"%{qn}%", limit)))
        if re.fullmatch(r"[가-힣A-Za-z0-9·.]+(대로|로|길)", qn):
            add("도로명", rows(con, "SELECT * FROM addr_point WHERE road=? ORDER BY main, sub LIMIT ?", (qn, limit)))
    out = []
    for kind, c in found:
        addr = _addr_std(c)
        dong = c.get("dong_h") if c.get("dong_h") and area.get_dong(con, c["dong_h"]) else area.detect_dong(con, addr).get("dong")
        out.append({"kind": kind, "address": addr, "label": addr + (f' · 지번 {c["dong_b"] or ""} {c["jibun_main"]}{"-" + str(c["jibun_sub"]) if c["jibun_sub"] else ""}' if kind == "지번" else ""),
                    "lat": c["lat"], "lon": c["lon"], "dong": dong, "bname": c.get("bname") or ""})
    # 도로명 앞부분만 적었을 때(예: '필문', '동계천로'): 맞는 도로를 알려 주고 번호를 이어 적게 한다(고르면 검색어만 채움)
    if len(out) < limit and not pa and not is_chosung(qn) and not re.search(r"\d", qn) and len(qn) >= 2:
        for x in rows(con, """SELECT road, MIN(main) AS lo, MAX(main) AS hi, COUNT(*) AS n FROM addr_point WHERE road LIKE ?
                GROUP BY road ORDER BY length(road), road LIMIT ?""", (qn + "%", min(6, limit - len(out)))):
            out.append({"kind": "도로", "hint": True, "road": x["road"], "address": x["road"],
                        "label": f'{x["road"]} — 건물번호 {x["lo"]}~{x["hi"]} 중 하나를 이어서 적으세요 ({x["n"]}곳)'})
    return out
