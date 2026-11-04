"""설정 등록표: PC가 meta 표에 두는 값(설정·운영 상태)을 한 곳에 적는다.

- 모든 get_setting/set_setting 은 여기 등록된 키만 받는다(오타·없는 키는 KeyError).
- 기본값은 여기 한 곳에서 정한다. get_setting(con, key) 처럼 기본값을 주지 않으면 이 기본값을 쓴다.
- validator 는 저장 직전 형식 검사(프로그램 오류 막기). 화면에 보일 자세한 안내(범위·형식)는 저장하는 업무 함수(config.save_settings 등)가 먼저 한다.
- secret=True 는 화면·문서·로그에 값을 내지 않는 키(외부 서비스 승인키 등).
- kind: "setting" = 설정 화면에서 바꾸는 값, "state" = 프로그램이 남기는 운영 기록(마지막 실행 시각·검사점 등).
- docs/settings.md 는 이 표에서 만든다: python3 tools/gen_settings_doc.py (손으로 고치지 않는다)

설정 묶음(config.settings, 화면이 받는 값)에는 expose=True 인 키가 그대로 들어가고, 계산해서 넣는 값(수용량·감면 목록·백업 설정 등)은
각 업무 모듈이 contribute() 로 등록한다.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable

# 문구 기본값 (설정 화면에서 바꾸기 전)
PAYMENT_GUIDE_DEFAULT = "부과 금액은 수거 전까지 가상계좌 또는 현장 카드로 내 주세요. 납부 방법·기한은 담당 부서에 확인하세요."
CONTACT_PLACEHOLDER = "동구청 청소 담당 부서 (전화번호는 설정에서 입력)"
DISPOSAL_GUIDE_DEFAULT = "수거일 아침 7시까지 집 앞(공동주택은 관리사무소가 정한 곳)에 내놓고, 이 표지를 물건에 붙여 주세요."
DEPOT_DEFAULT = {"lat": 35.1462, "lon": 126.9233, "label": "차고지(기본값: 동구청 부근)"}
# 외부 서비스 승인키(v0.11): meta 에는 "ext_" + 이름으로 둔다
EXTERNAL_KEYS = {"juso_search_key": "주소 검색 승인키", "juso_coord_key": "좌표 승인키", "kakao_rest_key": "카카오모빌리티 REST 키"}


@dataclass(frozen=True)
class Setting:
    default: Any
    doc: str
    validator: Callable[[Any], bool] | None = None
    secret: bool = False
    kind: str = "setting"            # "setting" | "state"
    expose: bool = False             # True: config.settings() 에 같은 이름으로 그대로 넣는다
    group: str = ""                  # 문서 묶음(화면 이름)
    owner: str = "config"            # 저장하는 모듈

    def get_default(self):
        return copy.deepcopy(self.default)


# ------------------------------------------------------------------ 형식 검사 도우미
def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _int_in(lo, hi):
    return lambda v: _is_int(v) and lo <= v <= hi


def _str_max(n):
    return lambda v: isinstance(v, str) and len(v) <= n


def _opt(f):
    return lambda v: v is None or f(v)


def _point(v) -> bool:
    return isinstance(v, dict) and all(isinstance(v.get(k), (int, float)) and not isinstance(v.get(k), bool) for k in ("lat", "lon"))


def _hhmm_window(v) -> bool:
    return isinstance(v, dict) and isinstance(v.get("start"), str) and isinstance(v.get("end"), str)


def _is_str(v) -> bool:
    return isinstance(v, str)


def _is_dict(v) -> bool:
    return isinstance(v, dict)


def _is_list(v) -> bool:
    return isinstance(v, list)


def _stamp(v) -> bool:  # 운영 기록: {"at": ISO 시각, ...}
    return isinstance(v, dict) and isinstance(v.get("at"), str)


SETTINGS: dict[str, Setting] = {}


def register(key: str, default: Any, doc: str, validator=None, *, secret: bool = False, kind: str = "setting", expose: bool = False,
             group: str = "", owner: str = "config") -> Setting:
    if key in SETTINGS:
        raise ValueError(f"설정 키가 두 번 등록되었습니다: {key}")
    s = Setting(default, doc, validator, secret, kind, expose, group, owner)
    SETTINGS[key] = s
    return s


def spec(key: str) -> Setting:
    try:
        return SETTINGS[key]
    except KeyError:
        raise KeyError(f"등록되지 않은 설정 키입니다: {key} (dgpc/settings.py 에 등록하세요)") from None


def check(key: str, value) -> None:
    """저장 직전 형식 검사. 맞지 않으면 ValueError(프로그램 오류 — 화면 검사가 먼저 걸러야 한다)."""
    s = spec(key)
    if s.validator is not None and not s.validator(value):
        raise ValueError(f"설정 값 형식이 올바르지 않습니다: {key}")


# ------------------------------------------------------------------ 설정 묶음에 계산 값 넣기(업무 모듈이 등록)
CONTRIBUTORS: list[tuple[str, Callable]] = []


def contribute(name: str):
    """config.settings() 에 계산한 값을 더하는 함수 등록: fn(con) -> dict. 등록 순서대로 합친다(같은 이름은 한 번만)."""
    def deco(fn):
        if not any(n == name for n, _ in CONTRIBUTORS):
            CONTRIBUTORS.append((name, fn))
        return fn
    return deco


# ================================================================== 등록표
# ---- 노선·이동시간 (설정 > 노선)
register("depot", DEPOT_DEFAULT, "차고지(출발지) 좌표와 이름. 비어 있으면 동구청 부근 기본값.", _point, expose=True, group="노선")
register("osrm_url", "", "이 PC 안의 OSRM 주소(http://127.0.0.1:포트). 비우면 쓰지 않음.", _str_max(200), expose=True, group="노선")
register("travel_method", None, "이동시간 계산 방식: ESTIMATE(직선 추정)·OSRM·KAKAO. 비어 있으면 OSRM 주소가 있을 때 OSRM.",
         _opt(lambda v: v in ("ESTIMATE", "OSRM", "KAKAO")), group="노선")
register("service_min_base", 5, "한 곳 작업 기본 시간(분).", lambda v: _is_int(v) and v >= 0, expose=True, group="노선")
register("service_min_per_item", 3, "품목 하나당 더하는 작업 시간(분).", lambda v: _is_int(v) and v >= 0, expose=True, group="노선")
register("unload_site", None, "반입지(하차 장소) 좌표·이름. 비우면 하차를 노선에 넣지 않음.", _opt(_point), expose=True, group="노선")
register("end_site", None, "종료지 좌표·이름. 비우면 차고지로 돌아옴.", _opt(_point), expose=True, group="노선")
register("unload_min", 20, "하차 한 번에 걸리는 시간(분, 0~180).", _int_in(0, 180), expose=True, group="노선")
register("max_unloads", 2, "하루 최대 하차 횟수(0~5).", _int_in(0, 5), expose=True, group="노선")
register("unload_hours", None, "반입지가 받는 시간 {start, end}(HH:MM). 비우면 따지지 않음.", _opt(_hhmm_window), expose=True, group="노선")
register("lunch", None, "쉬는(점심) 시간 창 {start, end, minutes}. 비우면 없음.", _opt(_hhmm_window), expose=True, group="노선")
register("base_first", True, "차고지에서 가까운 곳부터 도는 쪽을 먼저 고려.", lambda v: isinstance(v, bool), expose=True, group="노선")
register("volume_overrides", {}, "품목(수수료표 ID)별 부피 보정(m³). 노선 적재 계산에 쓴다.", _is_dict, group="노선", owner="fees")
for _k, _label in EXTERNAL_KEYS.items():
    register("ext_" + _k, "", f"외부 서비스 {_label}. 화면에는 가린 값만 보낸다.", _is_str, secret=True, group="외부 서비스")

# ---- 접수·안내 문구 (설정 > 안내)
register("jurisdiction", "동구", "관할 구 이름(주소 관할 경고·안내 글 머리).", _str_max(20), expose=True, group="안내")
register("intake_notices", [], "접수 시 안내: [{keywords: [품목 낱말], text: 안내 글}] (최대 30개).", _is_list, expose=True, group="안내")
register("contact_info", CONTACT_PLACEHOLDER, "문의처(접수증·안내 글).", _str_max(200), expose=True, group="안내")
register("disposal_guide", DISPOSAL_GUIDE_DEFAULT, "배출 안내 문구(접수증·안내 글).", _str_max(400), expose=True, group="안내")
register("payment_guide", PAYMENT_GUIDE_DEFAULT, "납부 방법·기한 안내 문구.", _str_max(300), expose=True, group="안내")
register("monthly_report_title", "대형폐기물 수거 월보", "월보 제목.", lambda v: isinstance(v, str) and 0 < len(v) <= 60, expose=True, group="안내")
register("capacity", {}, "수거 한도(하루 건수·부피, 동별 우선 팀). 화면 값은 area.capacity_settings 가 기본값을 채워 만든다.", _is_dict,
         group="수거 한도", owner="area")

# ---- 수수료·감면
register("reduction_round_unit", 1, "감면액 끝수 처리 단위(원): 1 = 원 미만 버림, 10 = 10원 미만 버림.", lambda v: v in (1, 10), group="수수료",
         owner="config")
register("reductions", None, "감면 사유 목록 [{code, label, rate, note, active}]. 비어 있으면 기본 목록(조례 확인 필요 표시).", _opt(_is_list),
         group="수수료", owner="finance")

# ---- 보관·개인정보
register("photo_retention_days", 90, "현장 사진 보관 기간(일, 7~3650). 지나면 사진 바이트를 지운다.", _int_in(7, 3650), expose=True, group="보관")
register("pii_retention_years", 3, "개인정보 보유 기간(년). 지나면 자동 파기(가명 처리).", lambda v: _is_int(v) and v >= 1, group="보관", owner="privacy")
register("access_log_retention_years", 2, "접속기록 보관 기간(년, 2년 이상).", lambda v: _is_int(v) and v >= 2, group="보관", owner="privacy")

# ---- 백업
register("backup_dir", "", "수동 백업 폴더(전체 경로). 비우면 state/backups.", _is_str, expose=True, group="백업")
register("backup_auto", True, "자동 백업 사용.", lambda v: isinstance(v, bool), group="백업", owner="backup")
register("backup_time", "18:00", "자동 백업 시각(HH:MM).", _str_max(5), group="백업", owner="backup")
register("backup_dest2", "", "두 번째 보관 위치(전체 경로). 비우면 복사하지 않음.", _is_str, group="백업", owner="backup")
register("backup_keep_daily", 14, "자동 백업 일별 보관 개수(1~365).", _int_in(1, 365), group="백업", owner="backup")
register("backup_keep_monthly", 3, "자동 백업 월별 보관 개수(0~120).", _int_in(0, 120), group="백업", owner="backup")
register("backup_retention_days", 90, "백업 파일 보관 일수(7~3650).", _int_in(7, 3650), group="백업", owner="backup")
register("backup_pubkey", None, "자동 백업 공개키 {pub, fpr, created_at, created_by}. 개인키는 PC에 두지 않는다.", _opt(_is_dict), kind="state",
         group="백업", owner="backup")

# ---- 시범 운영
register("pilot", {}, "시범 운영 비교: 기간·기존 방식 수치(담당자가 적은 값).", _is_dict, group="시범 운영", owner="reports")

# ---- 운영 기록(state): 프로그램이 남기는 값
register("audit_chain_anchor", "", "변경 이력 해시 사슬의 시작점(읽기만 함: 이전 판에서 옮긴 DB용, 없으면 빈 값).", _is_str, kind="state", group="무결성", owner="privacy")
register("access_chain_anchor", None, "접속기록 해시 사슬의 시작점 {id, hash, at}.", _opt(_is_dict), kind="state", group="무결성", owner="privacy")
register("chain_ckpt_access", None, "접속기록 빠른 검사 검사점(마지막으로 확인한 행).", _opt(_is_dict), kind="state", group="무결성", owner="privacy")
register("chain_ckpt_audit", None, "변경 이력 빠른 검사 검사점(마지막으로 확인한 행).", _opt(_is_dict), kind="state", group="무결성", owner="privacy")
register("last_quick_verify", None, "무결성 빠른 검사 마지막 실행 {at, ok}.", _opt(_stamp), kind="state", group="예약 작업", owner="privacy")
register("last_full_verify", None, "무결성 전체 검사 마지막 실행 {at, ok}.", _opt(_stamp), kind="state", group="예약 작업", owner="privacy")
register("last_pii_purge_run", None, "개인정보 자동 파기 마지막 실행 {at, count, trigger}.", _opt(_stamp), kind="state", group="예약 작업", owner="privacy")
register("last_photo_purge_run", None, "사진 보관 기간 정리 마지막 실행 {at, count, trigger}.", _opt(_stamp), kind="state", group="예약 작업", owner="photos")
register("last_daily_tasks", None, "하루 예약 작업 마지막 실행 {at, ...}.", _opt(_stamp), kind="state", group="예약 작업", owner="ops")
register("last_refund_sync", None, "자동 환불 건 맞추기 마지막 실행 {at, ...}.", _opt(_stamp), kind="state", group="예약 작업", owner="ops")
register("last_backup_at", None, "마지막 백업 시각(ISO).", _opt(lambda v: isinstance(v, str)), kind="state", group="백업", owner="backup")
register("backup_status", {}, "마지막 자동 백업 결과(성공·파일·오류).", _is_dict, kind="state", group="백업", owner="backup")
register("auto_backup_day", None, "자동 백업을 마지막으로 한 날(KST, 하루 한 번).", _opt(lambda v: isinstance(v, str)), kind="state", group="백업",
         owner="backup")
register("login", None, "v0.9 이전 단일 업무 비밀번호(관리자 계정으로 옮긴 뒤 지운다).", _opt(_is_dict), kind="state", group="사용자", owner="users")
