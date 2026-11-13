"""VWorld Geocoder 2.0 참고 조회. 결과를 DB/로그/멱등 캐시에 저장하지 않는다."""
from __future__ import annotations

import math
import os
import re
import urllib.parse

from dgpc import external as X
from dgpc.core import UserError, get_setting


def settings(con) -> dict:
    return {"enabled": get_setting(con, "vworld_enabled"), "domain": get_setting(con, "vworld_domain"),
            "mode": "REFERENCE_ONLY"}


def valid_domain(value) -> str:
    if not isinstance(value, str) or len(value) > 200 or re.search(r"[\s\x00-\x1f]", value):
        raise UserError("VWorld 등록 서비스 URL을 확인하세요.")
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
        valid = (parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise UserError("VWorld 등록 서비스 URL은 http/https 주소로 적고 계정·검색 조건은 넣지 마세요.")
    return value


def config_token(con) -> tuple:
    return (get_setting(con, "vworld_enabled"), get_setting(con, "ext_vworld_api_key"), get_setting(con, "vworld_domain"))


def prepare(con, data) -> dict | None:
    enabled, key, domain = config_token(con)
    if not enabled or not key:
        return None
    if not isinstance(data, dict):
        raise UserError("조회할 건물 주소를 확인하세요.")
    address, kind = data.get("address"), data.get("type", "ROAD")
    name = data.get("name", "")
    if (not isinstance(address, str) or len(address) > 250 or kind not in ("ROAD", "PARCEL")
            or not isinstance(name, str) or len(name) > 100 or re.search(r"[\x00-\x1f]", address)):
        raise UserError("조회할 건물 주소와 주소 유형을 확인하세요.")
    from dgpc.addrsearch import _known_names
    address = re.sub(r"\s+", " ", address).strip()
    why = X.keyword_guard(address, name, _known_names(con))
    if why:
        raise UserError(why)
    # 상세 동호수·괄호 속 메모는 외부로 보내지 않고 건물 주소에서 끊는다.
    end = r"\s+(?:지하\s*)?\d+(?:-\d+)?" if kind == "ROAD" else r"\s+(?:산\s*)?\d+(?:-\d+)?"
    suffix = r"(?:대로|로|길)" if kind == "ROAD" else r"(?:동|가|리)"
    aliases = r"서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주"
    region = (r"(?:(?:[가-힣]{1,20}(?:특별자치시|특별자치도|특별시|광역시|도)|" + aliases + r")\s+)?"
              r"(?:[가-힣]{1,20}(?:시|군)\s+)?(?:[가-힣]{1,20}구\s+)?(?:[가-힣0-9]{1,20}(?:읍|면)\s+)?")
    # 주소 앞/중간의 상세 동호수·비주소 낱말을 넓은 접두부로 함께 보내지 않는다.
    match = re.match(r"^(" + region + r"[가-힣A-Za-z0-9·.-]+" + suffix + end + r")(?=\s|,|\(|$)", address)
    if not match:
        raise UserError("도로명과 건물번호 또는 법정동과 지번까지 입력하세요.")
    building = match.group(1)
    base = (os.environ.get("DG_VWORLD_BASE") or "https://api.vworld.kr").rstrip("/")
    if not base.startswith("https://") and not X._is_local(base):
        raise UserError("VWorld 연결 주소는 HTTPS를 사용해야 합니다.")
    return {"key": key, "domain": domain, "address": building, "type": kind, "base": base,
            "token": (enabled, key, domain)}


def unavailable(code="NOT_CONFIGURED", message="관리자가 VWorld 키를 등록하고 참고 조회를 켜야 사용할 수 있습니다.") -> dict:
    return {"none": True, "reference_only": True, "code": code, "message": message}


def locate(prepared: dict) -> dict:
    params = {"service": "address", "version": "2.0", "request": "getCoord", "format": "json", "errorFormat": "json",
              "crs": "EPSG:4326", "type": prepared["type"], "address": prepared["address"], "refine": "false",
              "simple": "false", "key": prepared["key"]}
    if prepared["domain"]:
        params["domain"] = prepared["domain"]
    try:
        raw = X.http_json(prepared["base"] + "/req/address?" + urllib.parse.urlencode(params))
    except X.ExternalError as error:
        return unavailable(error.code, str(error))
    try:
        response = raw["response"]
        status = response["status"]
        if status == "NOT_FOUND":
            return unavailable("NOT_FOUND", "VWorld에서 위치를 찾지 못했습니다. 주소와 유형을 확인하세요.")
        if status == "ERROR":
            # 공급자 text/code는 검색어·키를 되돌려 줄 수 있어 그대로 전달하지 않는다.
            return unavailable("PROVIDER_ERROR", "VWorld 조회가 거부되었습니다. 인증키·등록 서비스 URL·사용 한도를 확인하세요.")
        if status != "OK":
            raise ValueError
        result = response["result"]
        crs = result.get("crs")
        if crs is not None and str(crs).upper() != "EPSG:4326":
            raise ValueError
        point = result["point"]
        if isinstance(point["x"], bool) or isinstance(point["y"], bool):
            raise ValueError
        lon, lat = float(point["x"]), float(point["y"])
        if not (math.isfinite(lon) and math.isfinite(lat) and 124 < lon < 132 and 33 < lat < 39):
            raise ValueError
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError):
        return unavailable("BAD_RESPONSE", "VWorld 위치 응답을 확인할 수 없습니다. 지도에서 위치를 확인하세요.")
    return {"reference_only": True, "source": "VWORLD", "label": prepared["address"],
            "lat": round(lat, 7), "lon": round(lon, 7)}
