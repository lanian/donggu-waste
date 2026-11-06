"""PC 서버가 대신 묻는 외부 서비스 (v0.11, 사용자 요청으로 추가). 브라우저는 이 서버들에 직접 연결하지 않는다.

1) 행정안전부 도로명주소 API (주소정보누리집 business.juso.go.kr)
   - 검색 addrLinkApi.do: 보내는 것 = 승인키(confmKey), 검색어(keyword), currentPage, countPerPage, resultType=json
   - 좌표 addrCoordApi.do: 보내는 것 = 좌표 승인키, 고른 결과의 건물 식별 번호(admCd·rnMgtSn·udrtYn·buldMnnm·buldSlno), resultType=json
   - 검색어 안전장치(keyword_guard): 전화번호 모양 값, 접수 화면의 이름 칸 값, 이 PC에 저장된 주민 이름이 들어 있으면 보내지 않는다.
2) 카카오모빌리티 길찾기 API (apis-navi.kakaomobility.com) — 이동시간 행렬(routing.py)
   - 보내는 것 = REST 키(Authorization: KakaoAK …)와 좌표(경도,위도)뿐. 이름·주소·접수번호는 보내지 않는다.

공통
- 받는 곳 주소는 환경변수 DG_JUSO_BASE / DG_KAKAO_NAVI_BASE 로 바꿀 수 있다(자동 시험용 가짜 서버). 기본은 실제 주소.
- 기관망 프록시: urllib 기본 동작대로 HTTPS_PROXY / HTTP_PROXY / NO_PROXY 환경변수를 따른다. 이 PC 안(127.0.0.1) 주소는 프록시를 거치지 않는다.
- 요청 주소·검색어·키는 로그·오류 문구에 남기지 않는다(ExternalError 문구는 일반 문구만).
- 시간 제한 5초. 쿠키·Referer 없음.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

from dgpc import geo

TIMEOUT_S = 5
JUSO_DEFAULT = "https://business.juso.go.kr"
KAKAO_NAVI_DEFAULT = "https://apis-navi.kakaomobility.com"


def juso_base() -> str:
    return (os.environ.get("DG_JUSO_BASE") or JUSO_DEFAULT).rstrip("/")


def kakao_base() -> str:
    return (os.environ.get("DG_KAKAO_NAVI_BASE") or KAKAO_NAVI_DEFAULT).rstrip("/")


class ExternalError(Exception):
    """외부 서비스 실패. 문구에는 검색어·키·주소를 넣지 않는다. code: NETWORK·TIMEOUT·HTTP_4xx·AUTH·E0001 …"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _is_local(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def _opener(url: str):
    if _is_local(url):  # 시험용 가짜 서버(이 PC 안)는 프록시를 거치지 않는다
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()))  # 환경변수 프록시 그대로


def urlopen(req: urllib.request.Request, timeout: float):
    """실제 네트워크 호출 한 곳. 단위 시험은 이 함수를 바꿔 끼운다(monkeypatch)."""
    return _opener(req.full_url).open(req, timeout=timeout)


def http_json(url: str, *, data: dict | None = None, headers: dict | None = None, timeout: float = TIMEOUT_S) -> dict:
    h = {"Accept": "application/json", "User-Agent": "donggu-waste-pc/0.11"}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method="POST" if body is not None else "GET")
    try:
        with urlopen(req, timeout) as r:
            raw = r.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        code = e.code
        raise ExternalError("AUTH" if code in (401, 403) else f"HTTP_{code}",
                            "인증 키가 맞지 않거나 사용이 허락되지 않았습니다." if code in (401, 403) else f"외부 서버 오류({code})") from None
    except (TimeoutError, OSError) as e:  # URLError·소켓 시간 초과 포함
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            raise ExternalError("TIMEOUT", f"외부 서버가 {timeout:g}초 안에 답하지 않았습니다.") from None
        raise ExternalError("NETWORK", "외부 서버에 연결하지 못했습니다(인터넷·기관망 프록시 확인).") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ExternalError("BAD_RESPONSE", "외부 서버 응답을 읽지 못했습니다.") from None


# ------------------------------------------------------------------ 비밀 키 표시 (화면에는 가린 값만)
def mask(secret: str | None) -> str:
    s = secret or ""
    if not s:
        return ""
    return "••••" + s[-4:] if len(s) >= 12 else "••••"


KEY_RE = re.compile(r"^[A-Za-z0-9+/=_\-.]{8,200}$")


# ------------------------------------------------------------------ 검색어 안전장치
_PHONE_RE = re.compile(r"(?<!\d)0\d{1,2}[-\s.]?\d{3,4}[-\s.]?\d{4}(?!\d)")
_LONG_DIGITS = re.compile(r"\d[\d\s.-]{7,}\d")  # 숫자 8자리 이상(번호 일부·주민번호 등)
# 숫자 사이에 글자 아닌 기호(/ , _ · 공백 등)가 3개까지 끼어도 이어서 8자리 이상이면 번호로 본다 (010/1234/5678 같은 우회 막기).
# 주소의 '101동 1203호'처럼 한글이 끼면 끊긴다. web/juso.js SPLIT_DIGITS 와 같은 규칙.
_SPLIT_DIGITS = re.compile(r"\d(?:[^0-9A-Za-z가-힣]{0,3}\d){7,}")


def clean_keyword(q: str) -> str:
    """주소 서버가 거부하는 특수문자를 빼고 공백을 줄여 40자로 자른다 (web/juso.js cleanKeyword와 같은 규칙)."""
    return re.sub(r"\s+", " ", re.sub(r"[%=<>\[\]'\"`;\\{}|^]", " ", q or "")).strip()[:40]


def keyword_guard(q: str, name: str = "", known_names=()) -> str | None:
    """보내면 안 되는 검색어면 이유 문구, 괜찮으면 None."""
    k = q or ""
    if _PHONE_RE.search(k) or len(re.sub(r"\D", "", k)) >= 8 and (_LONG_DIGITS.search(k) or _SPLIT_DIGITS.search(k)):
        return "검색어에 전화번호 같은 숫자가 있어 외부 주소 검색에 보내지 않았습니다. 주소 낱말만 적어 주세요."
    ks = re.sub(r"\s+", "", k)
    n = re.sub(r"\s+", "", name or "")
    if len(n) >= 2 and n in ks:
        return "검색어에 신청인 이름이 들어 있어 외부 주소 검색에 보내지 않았습니다. 주소 낱말만 적어 주세요."
    for kn in known_names:
        kn = re.sub(r"\s+", "", kn or "")
        if (len(kn) >= 3 and kn in ks) or (len(kn) == 2 and kn == ks):
            return "검색어가 접수된 주민 이름과 같거나 이름을 포함해 외부 주소 검색에 보내지 않았습니다. 주소 낱말만 적어 주세요."
    return None


# ------------------------------------------------------------------ 행정안전부 도로명주소 API
JUSO_ERR = {
    "E0001": "승인키가 맞지 않습니다. 설정에서 승인키를 확인하세요.", "E0014": "승인키 사용 기간이 끝났습니다. 새 승인키를 넣으세요.",
    "E0006": "주소를 조금 더 자세히 적어 주세요 (예: 도로명 + 건물번호).", "E0008": "두 글자 이상 적어 주세요.",
    "E0013": "특수문자는 빼고 적어 주세요.", "E0015": "검색 결과가 너무 많습니다. 더 자세히 적어 주세요.",
}


def _juso_results(url: str) -> dict:
    j = http_json(url)
    res = (j or {}).get("results") or {}
    common = res.get("common") or {}
    code = str(common.get("errorCode", ""))
    if code != "0":
        raise ExternalError(code or "UNKNOWN", JUSO_ERR.get(code) or "주소 검색에 실패했습니다.")
    return res


def juso_search(key: str, keyword: str, per: int = 10) -> dict:
    """검색 → {"total", "items": [{road, road_part1, jibun, bdNm, zip, siNm, sggNm, emdNm, hemdNm, ref: {admCd…}}]}"""
    p = urllib.parse.urlencode({"confmKey": key, "currentPage": "1", "countPerPage": str(per), "keyword": clean_keyword(keyword), "resultType": "json"})
    res = _juso_results(f"{juso_base()}/addrlink/addrLinkApi.do?{p}")
    items = []
    for x in res.get("juso") or []:
        ref = {k: str(x.get(k) or "") for k in ("admCd", "rnMgtSn", "udrtYn", "buldMnnm", "buldSlno")}
        ref["udrtYn"] = ref["udrtYn"] or "0"
        ref["buldSlno"] = ref["buldSlno"] or "0"
        # 이 PC 주소 위치 자료에서 찾을 때만 쓰는 값(도로명·시군구·법정동·지번). 좌표 API에는 위 다섯 값만 보낸다(valid_ref)
        ref.update({k: str(x.get(k) or "")[:40] for k in REF_LOCAL})
        items.append({"road": x.get("roadAddr") or "", "road_part1": x.get("roadAddrPart1") or x.get("roadAddr") or "", "jibun": x.get("jibunAddr") or "",
                      "bdNm": x.get("bdNm") or "", "zip": x.get("zipNo") or "", "siNm": x.get("siNm") or "", "sggNm": x.get("sggNm") or "",
                      "emdNm": x.get("emdNm") or "", "hemdNm": x.get("hemdNm") or "", "ref": ref})
    try:
        total = int((res.get("common") or {}).get("totalCount") or len(items))
    except ValueError:
        total = len(items)
    return {"total": total, "items": items}


REF_RE = {"admCd": r"\d{10}", "rnMgtSn": r"\d{12}", "udrtYn": r"[01]", "buldMnnm": r"\d{1,5}", "buldSlno": r"\d{1,5}"}
REF_LOCAL = ("rn", "sggNm", "emdNm", "lnbrMnnm", "lnbrSlno", "mtYn")  # 검색 결과의 도로명·시군구·법정동·지번본번·부번·산여부


def valid_ref(ref: dict) -> dict:
    out = {}
    for k, pat in REF_RE.items():
        v = str((ref or {}).get(k) if (ref or {}).get(k) is not None else "").strip()
        if not re.fullmatch(pat, v):
            raise ExternalError("BAD_REF", "고른 주소의 건물 식별 번호가 올바르지 않습니다.")
        out[k] = v
    return out


def juso_coord(key: str, ref: dict) -> tuple[float, float]:
    """좌표제공 API → (위도, 경도). UTM-K 출입구 좌표를 geo.utmk_to_wgs84로 바꾼다."""
    p = urllib.parse.urlencode({"confmKey": key, **valid_ref(ref), "resultType": "json"})
    res = _juso_results(f"{juso_base()}/addrlink/addrCoordApi.do?{p}")
    j = (res.get("juso") or [None])[0]
    try:
        x, y = float(j["entX"]), float(j["entY"])
    except (TypeError, KeyError, ValueError):
        raise ExternalError("NO_COORD", "이 주소의 좌표가 없습니다. 지도에서 위치를 지정하세요.") from None
    lat, lon = geo.utmk_to_wgs84(x, y)
    if not (33 < lat < 39 and 124 < lon < 132):
        raise ExternalError("NO_COORD", "좌표가 국내 범위를 벗어났습니다. 지도에서 위치를 지정하세요.")
    return round(lat, 7), round(lon, 7)


# ------------------------------------------------------------------ 카카오모빌리티 길찾기
KAKAO_MAX_DEST = 30        # 다중 목적지 1회 최대 목적지 수
KAKAO_RADIUS_M = 10_000    # 다중 목적지 반경 한도(m)


def _kakao_headers(key: str) -> dict:
    return {"Authorization": f"KakaoAK {key}"}


def kakao_destinations(key: str, origin: tuple[float, float], dests: list[tuple[str, float, float]], radius_m: int = KAKAO_RADIUS_M,
                       timeout: float = TIMEOUT_S) -> dict[str, int]:
    """한 출발지 → 여러 목적지(최대 30곳, 반경 10km 안) 자동차 소요 시간(초). dests: [(키, 위도, 경도)].
    돌려주는 값: {키: 초} — 실패한 목적지(경로 없음·반경 밖)는 빠진다."""
    if not dests:
        return {}
    if len(dests) > KAKAO_MAX_DEST:
        raise ValueError("목적지는 30곳까지")
    body = {"origin": {"x": f"{origin[1]:.5f}", "y": f"{origin[0]:.5f}"},
            "destinations": [{"x": f"{lon:.5f}", "y": f"{lat:.5f}", "key": str(k)} for k, lat, lon in dests],
            "radius": int(min(radius_m, KAKAO_RADIUS_M)), "priority": "TIME"}
    j = http_json(f"{kakao_base()}/v1/destinations/directions", data=body, headers=_kakao_headers(key), timeout=timeout)
    out = {}
    for r in (j or {}).get("routes") or []:
        try:
            if int(r.get("result_code", -1)) == 0 and r.get("summary") and r["summary"].get("duration") is not None:
                out[str(r.get("key"))] = int(r["summary"]["duration"])
        except (TypeError, ValueError):
            continue
    return out


def kakao_direction(key: str, origin: tuple[float, float], dest: tuple[float, float], timeout: float = TIMEOUT_S) -> int | None:
    """출발지 → 목적지 1곳 자동차 소요 시간(초). 경로가 없으면 None."""
    p = urllib.parse.urlencode({"origin": f"{origin[1]:.5f},{origin[0]:.5f}", "destination": f"{dest[1]:.5f},{dest[0]:.5f}", "priority": "TIME"})
    j = http_json(f"{kakao_base()}/v1/directions?{p}", headers=_kakao_headers(key), timeout=timeout)
    for r in (j or {}).get("routes") or []:
        try:
            if int(r.get("result_code", -1)) == 0 and r.get("summary"):
                return int(r["summary"]["duration"])
        except (TypeError, ValueError):
            pass
    return None
