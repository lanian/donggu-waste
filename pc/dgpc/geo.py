"""로컬 주소 → 좌표 (외부 서비스 없이 이 PC 안에서만).

자료: 행정안전부 도로명주소 '위치정보요약DB'(주소정보누리집·공공데이터포털에서 내려받는 텍스트 파일).
- 파일마다 열 순서·구분자·인코딩이 조금씩 다를 수 있어, 첫 줄 머리글이 있으면 이름으로, 없으면 값 모양으로 열을 찾는다.
  (도로명 = '로/길'로 끝나는 열, 건물본번·부번 = 그 뒤 숫자 열, 좌표 = UTM-K 범위의 숫자 두 열 또는 위도·경도)
- 찾은 열 배치와 표본 변환 결과를 먼저 보여 주고(미리보기), 담당자가 확인한 뒤 가져온다. --columns 로 직접 지정할 수도 있다.
- 좌표는 UTM-K(GRS80, EPSG:5179)를 WGS84 위도·경도로 바꿔 저장한다. 건물 출입구 좌표라 배출 위치와 다를 수 있으므로
  담당자가 지도에서 확인·보정한다.
"""
from __future__ import annotations

import csv
import io
import math
import re

# ------------------------------------------------------------------ UTM-K (EPSG:5179) ↔ WGS84
_A = 6378137.0
_F = 1 / 298.257222101
_E2 = _F * (2 - _F)
_EP2 = _E2 / (1 - _E2)
_LAT0, _LON0, _K0, _FE, _FN = math.radians(38.0), math.radians(127.5), 0.9996, 1_000_000.0, 2_000_000.0


def _m(phi: float) -> float:
    e2, e4, e6 = _E2, _E2 ** 2, _E2 ** 3
    return _A * ((1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * phi - (3 * e2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * phi)
                 + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * phi) - (35 * e6 / 3072) * math.sin(6 * phi))


_M0 = _m(_LAT0)


def wgs84_to_utmk(lat: float, lon: float) -> tuple[float, float]:
    phi, lam = math.radians(lat), math.radians(lon)
    n = _A / math.sqrt(1 - _E2 * math.sin(phi) ** 2)
    t, c = math.tan(phi) ** 2, _EP2 * math.cos(phi) ** 2
    a = (lam - _LON0) * math.cos(phi)
    x = _FE + _K0 * n * (a + (1 - t + c) * a ** 3 / 6 + (5 - 18 * t + t * t + 72 * c - 58 * _EP2) * a ** 5 / 120)
    y = _FN + _K0 * (_m(phi) - _M0 + n * math.tan(phi) * (a * a / 2 + (5 - t + 9 * c + 4 * c * c) * a ** 4 / 24
                                                          + (61 - 58 * t + t * t + 600 * c - 330 * _EP2) * a ** 6 / 720))
    return x, y


def utmk_to_wgs84(x: float, y: float) -> tuple[float, float]:
    m = _M0 + (y - _FN) / _K0
    mu = m / (_A * (1 - _E2 / 4 - 3 * _E2 ** 2 / 64 - 5 * _E2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))
    phi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu) + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu) + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    n1 = _A / math.sqrt(1 - _E2 * math.sin(phi1) ** 2)
    t1, c1 = math.tan(phi1) ** 2, _EP2 * math.cos(phi1) ** 2
    r1 = _A * (1 - _E2) / (1 - _E2 * math.sin(phi1) ** 2) ** 1.5
    d = (x - _FE) / (n1 * _K0)
    lat = phi1 - (n1 * math.tan(phi1) / r1) * (d * d / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * _EP2) * d ** 4 / 24
                                                + (61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * _EP2 - 3 * c1 * c1) * d ** 6 / 720)
    lon = _LON0 + (d - (1 + 2 * t1 + c1) * d ** 3 / 6 + (5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * _EP2 + 24 * t1 * t1) * d ** 5 / 120) / math.cos(phi1)
    return math.degrees(lat), math.degrees(lon)


# ------------------------------------------------------------------ 주소 문자열 해석
_ROAD_RE = re.compile(r"([가-힣A-Za-z0-9·.]+?(?:대로|로|길))\s*(?:(\d+)\s*번?\s*길\s*)?(지하\s*)?(\d+)(?:\s*-\s*(\d+))?")


def norm_road(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def parse_address(addr: str) -> dict | None:
    """'광주광역시 동구 필문대로 365, 101동' → {'road': '필문대로', 'main': 365, 'sub': 0, 'sigungu_hint': '동구'}"""
    a = re.sub(r"\(.*?\)", " ", addr or "")
    m = None
    for m in _ROAD_RE.finditer(a):
        break
    if not m:
        return None
    road = m.group(1)
    if m.group(2):  # '○○로 12번길 34' 형태
        road = f"{road}{m.group(2)}번길"
    hint = None
    hm = re.search(r"([가-힣]+[시군구])\s", a)
    if hm:
        hint = hm.group(1)
    hm2 = re.findall(r"([가-힣]+구)\b", a)
    if hm2:
        hint = hm2[0]
    return {"road": norm_road(road), "main": int(m.group(4)), "sub": int(m.group(5) or 0), "sigungu_hint": hint}


# ------------------------------------------------------------------ 위치정보요약DB 읽기
HEADER_NAMES = {
    "sigungu": ("시군구명", "시군구"), "road": ("도로명",), "main": ("건물본번", "건물번호본번", "본번"), "sub": ("건물부번", "건물번호부번", "부번"),
    "x": ("x좌표", "x", "xpos", "entx", "출입구x좌표"), "y": ("y좌표", "y", "ypos", "enty", "출입구y좌표"),
    "lat": ("위도", "lat", "latitude"), "lon": ("경도", "lon", "lng", "longitude"), "underground": ("지하여부",),
    # 선택: 관할 행정동·법정동(읍면동) 열이 있으면 접수의 행정동 자동 판별에 쓴다
    "dong_h": ("관할행정동", "행정동명", "행정동"), "dong_b": ("읍면동명", "법정동명", "법정동"),
    # 선택: 건물명(행안부 '건물명'·'시군구용건물명'), 지번(도로명주소 한글·건물 DB의 지번본번·부번) — 있으면 주소 찾기에 쓴다
    "bname": ("건물명", "시군구용건물명", "건축물대장건물명", "상세건물명"), "jibun_main": ("지번본번", "지번본번(번지)", "번지"),
    "jibun_sub": ("지번부번", "지번부번(호)", "호"), "san": ("산여부",),
}
_DONG_RE = re.compile(r"[가-힣0-9·]{1,10}(동|읍|면|가)")


def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp949", errors="replace")


def _sniff_delim(sample: str) -> str:
    first = sample.splitlines()[0] if sample else ""
    return max(("|", "\t", ","), key=first.count)


def _num(v: str):
    try:
        return float(v.strip())
    except (ValueError, AttributeError):
        return None


def detect_columns(rows: list[list[str]]) -> dict:
    """머리글 또는 값 모양으로 열 위치를 찾는다."""
    head = [c.strip().lower().replace(" ", "") for c in rows[0]]
    cols: dict = {}
    for key, names in HEADER_NAMES.items():
        for i, h in enumerate(head):
            if h in names:
                cols[key] = i
                break
    if "road" in cols and (("x" in cols and "y" in cols) or ("lat" in cols and "lon" in cols)) and "main" in cols:
        cols["header"] = True
        return cols
    body = rows[:200]
    width = min(len(r) for r in body)
    def share(pred, i):
        return sum(1 for r in body if pred(r[i])) / len(body)
    road_i = max(range(width), key=lambda i: share(lambda v: bool(re.fullmatch(r"[가-힣A-Za-z0-9·.]+(대로|로|길)", v.strip())), i))
    if share(lambda v: bool(re.fullmatch(r"[가-힣A-Za-z0-9·.]+(대로|로|길)", v.strip())), road_i) < 0.6:
        raise ValueError("도로명 열을 찾지 못했습니다. --columns 로 열 번호를 지정해 주세요.")
    cols = {"road": road_i, "header": False}
    ints = [i for i in range(road_i + 1, min(width, road_i + 5)) if share(lambda v: v.strip().isdigit(), i) > 0.95]
    # 도로명 바로 뒤: (지하여부 0/1) → 본번 → 부번
    if ints and share(lambda v: v.strip() in ("0", "1"), ints[0]) > 0.99 and len(ints) >= 3:
        cols["underground"], ints = ints[0], ints[1:]
    if len(ints) < 2:
        raise ValueError("건물본번·부번 열을 찾지 못했습니다. --columns 로 지정해 주세요.")
    cols["main"], cols["sub"] = ints[0], ints[1]
    xs = [i for i in range(width) if share(lambda v: (_num(v) or 0) > 700_000 and (_num(v) or 0) < 1_400_000, i) > 0.9]
    ys = [i for i in range(width) if share(lambda v: (_num(v) or 0) > 1_400_000 and (_num(v) or 0) < 2_300_000, i) > 0.9]
    lats = [i for i in range(width) if share(lambda v: 33 < (_num(v) or 0) < 39, i) > 0.9]
    lons = [i for i in range(width) if share(lambda v: 124 < (_num(v) or 0) < 132, i) > 0.9]
    if xs and ys:
        cols["x"], cols["y"] = xs[-1], ys[-1]
    elif lats and lons:
        cols["lat"], cols["lon"] = lats[-1], lons[-1]
    else:
        raise ValueError("좌표 열(UTM-K X·Y 또는 위도·경도)을 찾지 못했습니다. --columns 로 지정해 주세요.")
    # 건물명: 위치정보요약DB는 건물부번 바로 뒤가 건물명, 그 뒤가 우편번호(5자리)
    z = cols["sub"] + 2
    if z < width and share(lambda v: bool(re.fullmatch(r"\d{5}", v.strip())), z) > 0.9 and share(lambda v: bool(re.fullmatch(r"[\d.]+", v.strip())), z - 1) < 0.5:
        cols["bname"] = z - 1
    # 시군구명: '구/군/시'로 끝나는 짧은 열
    sg = [i for i in range(road_i) if share(lambda v: bool(re.fullmatch(r"[가-힣]{1,6}(구|군|시)(\s[가-힣]+구)?", v.strip())), i) > 0.9]
    if sg:
        cols["sigungu"] = sg[-1]
    # 읍면동명(도로명 앞)·관할행정동(도로명 뒤): '동·읍·면·가'로 끝나는 짧은 열 (없어도 됨)
    used = {v for k, v in cols.items() if k != "header"}
    dongs = [i for i in range(width) if i not in used and share(lambda v: bool(_DONG_RE.fullmatch(v.strip())), i) > 0.9]
    before, after = [i for i in dongs if i < road_i], [i for i in dongs if i > road_i]
    if before:
        cols["dong_b"] = before[-1]
    if after:
        cols["dong_h"] = after[-1]
    return cols


def parse_columns_arg(s: str) -> dict:
    """'road=7,main=9,sub=10,x=16,y=17,sigungu=4' (0부터 센 열 번호)"""
    out = {"header": False}
    for part in (s or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = int(v)
    return out


def read_points(raw: bytes, columns: dict | None = None, region: str = ""):
    """(시군구, 도로명, 본번, 부번, 위도, 경도, 관할행정동, 법정동, 건물명, 지번본번, 지번부번) 를 하나씩 돌려준다.
    region 이 있으면 시군구명에 그 글자가 든 것만. 없는 열은 빈 문자열(지번은 None)."""
    text = decode(raw)
    delim = _sniff_delim(text[:5000])
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows = [r for r in reader if r and any(c.strip() for c in r)]
    if not rows:
        return {"columns": {}, "delimiter": delim, "points": []}
    cols = columns or detect_columns(rows)
    body = rows[1:] if cols.get("header") else rows
    pts = []
    for r in body:
        try:
            if cols.get("underground") is not None and r[cols["underground"]].strip() == "1":
                pass  # 지하 건물도 같은 번호로 저장(동일 키면 지상 우선)
            sg = r[cols["sigungu"]].strip() if cols.get("sigungu") is not None else ""
            if region and region not in sg:
                continue
            road = norm_road(r[cols["road"]])
            main = int(float(r[cols["main"]] or 0))
            sub = int(float(r[cols["sub"]] or 0)) if cols.get("sub") is not None and r[cols["sub"]].strip() else 0
            if "x" in cols:
                x, y = float(r[cols["x"]]), float(r[cols["y"]])
                lat, lon = utmk_to_wgs84(x, y)
            else:
                lat, lon = float(r[cols["lat"]]), float(r[cols["lon"]])
            if not (33 < lat < 39 and 124 < lon < 132) or not road or main <= 0:
                continue
            dh = r[cols["dong_h"]].strip() if cols.get("dong_h") is not None else ""
            db = r[cols["dong_b"]].strip() if cols.get("dong_b") is not None else ""
            bn = r[cols["bname"]].strip()[:60] if cols.get("bname") is not None else ""
            jm = js = None
            if cols.get("jibun_main") is not None and r[cols["jibun_main"]].strip().isdigit():
                jm = int(r[cols["jibun_main"]])
                js = int(r[cols["jibun_sub"]]) if cols.get("jibun_sub") is not None and r[cols["jibun_sub"]].strip().isdigit() else 0
            pts.append((sg, road, main, sub, round(lat, 7), round(lon, 7), dh, db, bn, jm, js))
        except (ValueError, IndexError):
            continue
    return {"columns": cols, "delimiter": delim, "points": pts}
