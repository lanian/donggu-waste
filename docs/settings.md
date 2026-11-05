# 설정 키 목록 (PC)

> 이 파일은 `python3 tools/gen_settings_doc.py` 가 `pc/dgpc/settings.py` 의 등록표에서 만든다. 손으로 고치지 말 것.

- 값은 암호화 DB 의 `meta` 표에 JSON 으로 저장된다. 등록되지 않은 키를 읽거나 쓰면 오류(KeyError)가 난다.
- **화면 묶음**: 표시된 키는 설정 묶음(`GET /api/bootstrap` 의 `settings`)에 같은 이름으로 들어간다.
- **비밀**: 값을 화면·로그·문서에 내지 않는다(화면에는 가린 값만).
- 새 설정 추가: `settings.py` 에 `register(...)` 한 줄 → 저장은 `config.save_settings`(또는 담당 모듈의 save_settings)에서 검사 후 `set_setting`.

## 설정

| 키 | 기본값 | 묶음 | 저장 모듈 | 화면 묶음 | 설명 |
|---|---|---|---|---|---|
| `depot` | `{"lat": 35.1462, "lon": 126.9233, "label": "차고지(기본값: 동구청 부근)"}` | 노선 | config | 예 | 차고지(출발지) 좌표와 이름. 비어 있으면 동구청 부근 기본값. |
| `osrm_url` | `""` | 노선 | config | 예 | 이 PC 안의 OSRM 주소(http://127.0.0.1:포트). 비우면 쓰지 않음. |
| `travel_method` | `null` | 노선 | config |  | 이동시간 계산 방식: ESTIMATE(직선 추정)·OSRM·KAKAO. 비어 있으면 OSRM 주소가 있을 때 OSRM. |
| `service_min_base` | `5` | 노선 | config | 예 | 한 곳 작업 기본 시간(분). |
| `service_min_per_item` | `3` | 노선 | config | 예 | 품목 하나당 더하는 작업 시간(분). |
| `unload_site` | `null` | 노선 | config | 예 | 반입지(하차 장소) 좌표·이름. 비우면 하차를 노선에 넣지 않음. |
| `end_site` | `null` | 노선 | config | 예 | 종료지 좌표·이름. 비우면 차고지로 돌아옴. |
| `unload_min` | `20` | 노선 | config | 예 | 하차 한 번에 걸리는 시간(분, 0~180). |
| `max_unloads` | `2` | 노선 | config | 예 | 하루 최대 하차 횟수(0~5). |
| `unload_hours` | `null` | 노선 | config | 예 | 반입지가 받는 시간 {start, end}(HH:MM). 비우면 따지지 않음. |
| `lunch` | `null` | 노선 | config | 예 | 쉬는(점심) 시간 창 {start, end, minutes}. 비우면 없음. |
| `base_first` | `true` | 노선 | config | 예 | 차고지에서 가까운 곳부터 도는 쪽을 먼저 고려. |
| `volume_overrides` | `{}` | 노선 | fees |  | 품목(수수료표 ID)별 부피 보정(m³). 노선 적재 계산에 쓴다. |
| `ext_juso_search_key` | (비밀) | 외부 서비스 | config |  | 외부 서비스 주소 검색 승인키. 화면에는 가린 값만 보낸다. |
| `ext_juso_coord_key` | (비밀) | 외부 서비스 | config |  | 외부 서비스 좌표 승인키. 화면에는 가린 값만 보낸다. |
| `ext_kakao_rest_key` | (비밀) | 외부 서비스 | config |  | 외부 서비스 카카오모빌리티 REST 키. 화면에는 가린 값만 보낸다. |
| `jurisdiction` | `"동구"` | 안내 | config | 예 | 관할 구 이름(주소 관할 경고·안내 글 머리). |
| `intake_notices` | `[]` | 안내 | config | 예 | 접수 시 안내: [{keywords: [품목 낱말], text: 안내 글}] (최대 30개). |
| `contact_info` | `"동구청 청소 담당 부서 (전화번호는 설정에서 입력)"` | 안내 | config | 예 | 문의처(접수증·안내 글). |
| `disposal_guide` | `"수거일 아침 7시까지 집 앞(공동주택은 관리사무소가 정한 곳)에 내놓고, 이 표지를 물건에 붙여 주세요."` | 안내 | config | 예 | 배출 안내 문구(접수증·안내 글). |
| `payment_guide` | `"부과 금액은 수거 전까지 가상계좌 또는 현장 카드로 내 주세요. 납부 방법·기한은 담당 부서에 확인하세요."` | 안내 | config | 예 | 납부 방법·기한 안내 문구. |
| `monthly_report_title` | `"대형폐기물 수거 월보"` | 안내 | config | 예 | 월보 제목. |
| `capacity` | `{}` | 수거 한도 | area |  | 수거 한도(하루 건수·부피, 동별 우선 팀). 화면 값은 area.capacity_settings 가 기본값을 채워 만든다. |
| `reduction_round_unit` | `1` | 수수료 | config |  | 감면액 끝수 처리 단위(원): 1 = 원 미만 버림, 10 = 10원 미만 버림. 감면을 적용·바꿀 때 접수에 저장(이미 감면한 접수는 바꿔도 그대로). |
| `reductions` | `null` | 수수료 | finance |  | 감면 사유 목록 [{code, label, rate, note, active}]. 비어 있으면 기본 목록(조례 확인 필요 표시). |
| `photo_retention_days` | `90` | 보관 | config | 예 | 현장 사진 보관 기간(일, 7~3650). 지나면 사진 바이트를 지운다. |
| `pii_retention_years` | `3` | 보관 | privacy |  | 개인정보 보유 기간(년). 지나면 자동 파기(가명 처리). |
| `access_log_retention_years` | `2` | 보관 | privacy |  | 접속기록 보관 기간(년, 2년 이상). |
| `backup_dir` | `""` | 백업 | config | 예 | 수동 백업 폴더(전체 경로). 비우면 state/backups. |
| `backup_auto` | `true` | 백업 | backup |  | 자동 백업 사용. |
| `backup_time` | `"18:00"` | 백업 | backup |  | 자동 백업 시각(HH:MM). |
| `backup_dest2` | `""` | 백업 | backup |  | 두 번째 보관 위치(전체 경로). 비우면 복사하지 않음. |
| `backup_keep_daily` | `14` | 백업 | backup |  | 자동 백업 일별 보관 개수(1~365). |
| `backup_keep_monthly` | `3` | 백업 | backup |  | 자동 백업 월별 보관 개수(0~120). |
| `backup_retention_days` | `90` | 백업 | backup |  | 백업 파일 보관 일수(7~3650). |
| `pilot` | `{}` | 시범 운영 | reports |  | 시범 운영 비교: 기간·기존 방식 수치(담당자가 적은 값). |

## 운영 기록 (프로그램이 남김)

| 키 | 기본값 | 묶음 | 저장 모듈 | 화면 묶음 | 설명 |
|---|---|---|---|---|---|
| `backup_pubkey` | `null` | 백업 | backup |  | 자동 백업 공개키 {pub, fpr, created_at, created_by}. 개인키는 PC에 두지 않는다. |
| `audit_chain_anchor` | `""` | 무결성 | privacy |  | 변경 이력 해시 사슬의 시작점(읽기만 함: 이전 판에서 옮긴 DB용, 없으면 빈 값). |
| `access_chain_anchor` | `null` | 무결성 | privacy |  | 접속기록 해시 사슬의 시작점 {id, hash, at}. |
| `chain_ckpt_access` | `null` | 무결성 | privacy |  | 접속기록 빠른 검사 검사점(마지막으로 확인한 행). |
| `chain_ckpt_audit` | `null` | 무결성 | privacy |  | 변경 이력 빠른 검사 검사점(마지막으로 확인한 행). |
| `last_quick_verify` | `null` | 예약 작업 | privacy |  | 무결성 빠른 검사 마지막 실행 {at, ok}. |
| `last_full_verify` | `null` | 예약 작업 | privacy |  | 무결성 전체 검사 마지막 실행 {at, ok}. |
| `last_pii_purge_run` | `null` | 예약 작업 | privacy |  | 개인정보 자동 파기 마지막 실행 {at, count, trigger}. |
| `last_photo_purge_run` | `null` | 예약 작업 | photos |  | 사진 보관 기간 정리 마지막 실행 {at, count, trigger}. |
| `last_daily_tasks` | `null` | 예약 작업 | ops |  | 하루 예약 작업 마지막 실행 {at, ...}. |
| `last_refund_sync` | `null` | 예약 작업 | ops |  | 자동 환불 건 맞추기 마지막 실행 {at, ...}. |
| `last_backup_at` | `null` | 백업 | backup |  | 마지막 백업 시각(ISO). |
| `backup_status` | `{}` | 백업 | backup |  | 마지막 자동 백업 결과(성공·파일·오류). |
| `auto_backup_day` | `null` | 백업 | backup |  | 자동 백업을 마지막으로 한 날(KST, 하루 한 번). |
| `addr_import` | `null` | 주소 | addresses |  | 주소 위치 자료(행안부 위치정보요약DB) 마지막 가져오기 {at, count, total, region, codes}. 45일 넘으면 갱신 안내. |
| `login` | `null` | 사용자 | users |  | v0.9 이전 단일 업무 비밀번호(관리자 계정으로 옮긴 뒤 지운다). |

## 설정 묶음에 계산해 더하는 값 (contribute)

| 이름 | 모듈 | 함수 |
|---|---|---|
| fee | dgpc.fees | `_settings_part` |
| reduction_round | dgpc.derive | `_settings_part` |
| pay_methods | dgpc.payments | `_settings_part` |
| capacity | dgpc.area | `_settings_part` |
| reductions | dgpc.finance | `_settings_part` |
| retention | dgpc.privacy | `_settings_part` |
| backup | dgpc.backup | `_settings_part` |
| external | dgpc.config | `_external_part` |
