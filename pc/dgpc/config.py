"""설정 화면: 설정 묶음(settings) 만들기·저장(save_settings)·외부 서비스 키·연결 시험.

어떤 설정이 있는지·기본값·검사는 settings.py 의 등록표(SETTINGS)가 정한다.
"""
from __future__ import annotations

import re
from pathlib import Path

from dgpc import external as X, backup as _bk, privacy as _pv, area, finance, settings as SR
from dgpc import derive as _derive, fees as _fees, payments as _payments  # noqa: F401 — 설정 묶음에 값을 더하는 모듈(contribute)을 불러 둔다
from dgpc.settings import CONTACT_PLACEHOLDER, EXTERNAL_KEYS, PAYMENT_GUIDE_DEFAULT  # noqa: F401 — 예전 이름(S.CONTACT_PLACEHOLDER 등)
from dgpc.core import UserError, audit, get_setting, set_setting, tx
from dgpc.derive import ROUND_UNITS

def settings(con) -> dict:
    """화면이 받는 설정 묶음: 등록표에서 expose=True 인 키(저장 값 또는 기본값) + 업무 모듈이 contribute() 로 더하는 계산 값."""
    out = {k: get_setting(con, k) for k, s in SR.SETTINGS.items() if s.expose}
    for _name, fn in SR.CONTRIBUTORS:
        out.update(fn(con))
    return out


@SR.contribute("external")
def _external_part(con) -> dict:
    return {"external": external_settings(con)}


def save_settings(con, data: dict):
    with tx(con):
        if "capacity" in data:
            area.save_capacity(con, data["capacity"] or {})
        if "intake_notices" in data:
            notes = []
            for n in data["intake_notices"] or []:
                kws = n.get("keywords") or []
                if isinstance(kws, str):
                    kws = re.split(r"[,\n]+", kws)
                kws = [k.strip()[:30] for k in kws if k and k.strip()][:30]
                text = (n.get("text") or "").strip()[:300]
                if not text:
                    continue
                if not kws:
                    raise UserError("접수 시 안내마다 품목 낱말을 하나 이상 적어 주세요.")
                notes.append({"keywords": kws, "text": text})
            set_setting(con, "intake_notices", notes[:30])
        audit(con, "settings", "pc", "SAVE", {"keys": ",".join(sorted(data))[:120]})
        if "reduction_round_unit" in data:
            try:
                u = int(data["reduction_round_unit"])
            except (TypeError, ValueError):
                u = 0
            if u not in ROUND_UNITS:
                raise UserError("감면액 끝수 처리 단위는 1원 또는 10원입니다.")
            set_setting(con, "reduction_round_unit", u)
        for k, lim in (("contact_info", 200), ("disposal_guide", 400), ("monthly_report_title", 60), ("payment_guide", 300)):
            if k in data:
                v = (data[k] or "").strip()[:lim]
                if k == "monthly_report_title" and not v:
                    raise UserError("월보 제목을 적어 주세요.")
                set_setting(con, k, v)
        if "reductions" in data:
            finance.save_reductions(con, data["reductions"])
        _pv.save_settings(con, data)
        _bk.save_settings(con, data)
        if "depot" in data:
            d = data["depot"]
            set_setting(con, "depot", {"lat": float(d["lat"]), "lon": float(d["lon"]), "label": str(d.get("label", "차고지"))})
        if "external" in data:
            _save_external(con, data["external"] or {})
        if "osrm_url" in data:
            u = (data["osrm_url"] or "").strip()
            if u and not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?/?$", u):
                raise UserError("OSRM 주소는 이 PC 안(http://127.0.0.1:포트)만 쓸 수 있습니다.")
            set_setting(con, "osrm_url", u)
        for k in ("service_min_base", "service_min_per_item"):
            if k in data:
                set_setting(con, k, max(0, int(data[k])))
        for key, default_label, what in (("unload_site", "반입지", "반입지"), ("end_site", "종료지", "종료지")):
            if key not in data:
                continue
            u = data[key] or {}
            if u.get("lat") in (None, "") or u.get("lon") in (None, ""):
                set_setting(con, key, None)
            else:
                lat, lon = float(u["lat"]), float(u["lon"])
                if not (33 <= lat <= 39 and 124 <= lon <= 132):
                    raise UserError(f"{what} 좌표가 국내 범위를 벗어났습니다.")
                set_setting(con, key, {"lat": lat, "lon": lon, "label": str(u.get("label") or default_label)[:40]})
        for k, lo, hi in (("unload_min", 0, 180), ("max_unloads", 0, 5)):
            if k in data:
                set_setting(con, k, min(hi, max(lo, int(data[k]))))
        if "base_first" in data:
            set_setting(con, "base_first", bool(data["base_first"]))
        if "unload_hours" in data:  # 반입지가 받는 시간 (비우면 따지지 않음)
            u = data["unload_hours"] or {}
            a, b = (u.get("start") or "").strip(), (u.get("end") or "").strip()
            if not a and not b:
                set_setting(con, "unload_hours", None)
            else:
                if not (re.fullmatch(r"\d\d:\d\d", a) and re.fullmatch(r"\d\d:\d\d", b) and a < b):
                    raise UserError("반입지 받는 시간은 시작·끝을 HH:MM으로, 시작이 끝보다 앞서게 적어 주세요.")
                set_setting(con, "unload_hours", {"start": a, "end": b})
        if "lunch" in data:  # 쉬는(점심) 시간: start~end 사이 어딘가에 minutes 분
            u = data["lunch"] or {}
            a, b = (u.get("start") or "").strip(), (u.get("end") or "").strip()
            m = int(u.get("minutes") or 0) if str(u.get("minutes") or "").strip().isdigit() else 0
            if not a and not b:
                set_setting(con, "lunch", None)
            else:
                if not (re.fullmatch(r"\d\d:\d\d", a) and re.fullmatch(r"\d\d:\d\d", b) and a < b):
                    raise UserError("쉬는 시간 창은 시작·끝을 HH:MM으로, 시작이 끝보다 앞서게 적어 주세요.")
                span = (int(b[:2]) * 60 + int(b[3:])) - (int(a[:2]) * 60 + int(a[3:]))
                if not 10 <= m <= span:
                    raise UserError("쉬는 시간(분)은 10분 이상, 시간 창 길이 이하로 적어 주세요.")
                set_setting(con, "lunch", {"start": a, "end": b, "minutes": m})
        if "jurisdiction" in data:
            set_setting(con, "jurisdiction", (data["jurisdiction"] or "").strip()[:20])
        if "photo_retention_days" in data:
            n = int(data["photo_retention_days"])
            if not 7 <= n <= 3650:
                raise UserError("사진 보관 기간은 7일~3650일 사이로 정해 주세요.")
            set_setting(con, "photo_retention_days", n)
        if "backup_dir" in data:
            b = (data["backup_dir"] or "").strip()
            if b and not Path(b).is_absolute():
                raise UserError("백업 폴더는 이 PC의 전체 경로로 적어 주세요 (예: D:\\동구백업).")
            set_setting(con, "backup_dir", b)


# ------------------------------------------------------------------ 외부 서비스 설정 (v0.11): 행안부 주소 API 승인키·카카오모빌리티 키
# 키는 암호화 DB(meta)에만 두고, 화면(브라우저)에는 저장 뒤 가린 값만 보낸다.
TRAVEL_METHODS = ("ESTIMATE", "OSRM", "KAKAO")


def secret(con, name: str) -> str:
    return get_setting(con, "ext_" + name) or ""


def travel_method(con) -> str:
    m = get_setting(con, "travel_method")
    if m in TRAVEL_METHODS:
        return m
    return "OSRM" if get_setting(con, "osrm_url") else "ESTIMATE"  # 이전 설정 그대로(OSRM 주소가 있으면 OSRM)


def external_settings(con) -> dict:
    return {"keys": {k: {"set": bool(secret(con, k)), "mask": X.mask(secret(con, k)), "label": lab} for k, lab in EXTERNAL_KEYS.items()},
            "travel_method": travel_method(con)}


def _save_external(con, d: dict):
    for k, lab in EXTERNAL_KEYS.items():
        if k not in (d.get("keys") or {}):
            continue
        v = ((d.get("keys") or {})[k] or "").strip()
        if v and not X.KEY_RE.match(v):
            raise UserError(f"{lab} 형식이 올바르지 않습니다 (영문·숫자 8자 이상).")
        set_setting(con, "ext_" + k, v)
        audit(con, "settings", "external", "KEY_SET" if v else "KEY_CLEAR", {"key": k})  # 키 값은 남기지 않는다
    if "travel_method" in d:
        m = d["travel_method"]
        if m not in TRAVEL_METHODS:
            raise UserError("이동시간 계산 방식을 골라 주세요.")
        if m == "KAKAO" and not secret(con, "kakao_rest_key"):
            raise UserError("카카오모빌리티 REST 키를 먼저 저장하세요.")
        set_setting(con, "travel_method", m)


def external_test(con, service: str) -> dict:
    """설정 화면 [연결 시험]: 개인정보 없는 고정 값으로 한 번 묻는다."""
    try:
        if service == "juso":
            key = secret(con, "juso_search_key")
            if not key:
                raise UserError("주소 검색 승인키를 먼저 저장하세요.")
            r = X.juso_search(key, "동구청", per=1)
            msg = f"주소 검색 정상 (시험 검색 '동구청' {r['total']}건)"
            if secret(con, "juso_coord_key") and r["items"]:
                X.juso_coord(secret(con, "juso_coord_key"), r["items"][0]["ref"])
                msg += " · 좌표 정상"
            return {"ok": True, "message": msg}
        if service == "kakao":
            key = secret(con, "kakao_rest_key")
            if not key:
                raise UserError("카카오모빌리티 REST 키를 먼저 저장하세요.")
            d = get_setting(con, "depot")
            o = (float(d["lat"]), float(d["lon"]))
            sec = X.kakao_direction(key, o, (o[0] + 0.005, o[1] + 0.005))
            return {"ok": sec is not None, "message": f"카카오모빌리티 정상 (차고지 부근 시험 경로 {sec}초)" if sec is not None else "응답은 왔으나 경로가 없습니다."}
    except X.ExternalError as e:
        return {"ok": False, "message": str(e), "code": e.code}
    raise UserError("알 수 없는 서비스입니다.")
