# PC 서버 API 경로 (역할 × 경로)

> 이 파일은 `python3 tools/gen_routes_doc.py` 가 `pc/dgpc/server.py` 의 경로 표(`@endpoint`)에서 만든다. 손으로 고치지 말 것.

- 주소 앞에 `/api/` 가 붙는다. 모든 API 요청에는 `X-DG-CSRF: 1` 머리글이 있어야 한다.
- **잠금**: `read` = 조회 전용 연결(잠금 없음, 쓰기 필요 시 잠금 잡고 다시) · `write` = DB 잠금 + 쓰기 연결 · `none` = 처리 함수가 스스로(잠금 밖 검증·계산, 반영만 잠금).
- **임시 비번**: ✓ 인 경로만 임시 비밀번호 상태(비밀번호 변경 전)에서 쓸 수 있다.
- **접속기록**: 경로에서 남기는 접속기록 동작(건수·메모만). 내보내기·인쇄·배차표·주소 검색은 업무 함수 안에서 남긴다.
- 경로 표에 없는 주소: 관리자, 또는 같은 방법·같은 첫 마디의 경로를 쓸 수 있는 역할은 404, 그 밖은 403.
- 새 경로 추가: `server.py` 의 `Handler` 안에 `@endpoint(방법, 경로, 역할 묶음, lock=…, access=…)` 처리 함수 하나 → 이 문서를 다시 만든다.
  역할 묶음: `ALL`=ADMIN·FIELD·INTAKE·LEAD, `VIEW`=ADMIN·INTAKE·LEAD, `WORK`=ADMIN·INTAKE, `CREW`=ADMIN·FIELD·INTAKE, `REVIEW`=ADMIN·LEAD, `ADMIN`=ADMIN.

## 로그인 전

| 방법 | 경로 | 설명 |
|---|---|---|
| GET | `state` | 설정 필요 여부·로그인 여부 |
| POST | `setup` | 첫 관리자 만들기(사용자가 없을 때만) |
| POST | `login` | 로그인(아이디별·PC별 실패 잠금) |
| POST | `logout` | 로그아웃(세션만 있으면) |

## GET

| 경로 | 관리자 | 접수 | 수거팀 | 팀장 | 잠금 | 임시 비번 | 접속기록 | 설명 |
|---|---|---|---|---|---|---|---|---|
| `bootstrap` | ✓ | ✓ | ✓ | ✓ | read | ✓ |  | 화면 시작 자료: 설정·팀·기기·오늘·나·수수료표·동·코드 목록. |
| `calendar` | ✓ | ✓ |  | ✓ | read |  |  | 수거 가능일 달력(수용량·휴무일). |
| `dong-detect` | ✓ | ✓ |  | ✓ | read |  |  | 주소로 행정동 찾기 + 주소 경고. |
| `dongs` | ✓ | ✓ |  | ✓ | read |  |  | 행정동 목록. |
| `holidays` | ✓ | ✓ |  | ✓ | read |  |  | 휴무일 목록. |
| `requests/import-template` | ✓ | ✓ | ✓ | ✓ | read |  |  | 접수 일괄 등록 CSV 서식. |
| `audit.csv` | ✓ |  |  | ✓ | read |  |  | 변경 이력 CSV. |
| `stats` | ✓ | ✓ |  | ✓ | read |  |  | 통계. |
| `pilot` | ✓ | ✓ |  | ✓ | read |  |  | 시범 운영 비교 설정·보고. |
| `pilot.csv` | ✓ | ✓ |  | ✓ | read |  |  | 시범 운영 비교 CSV. |
| `stats.csv` | ✓ | ✓ |  | ✓ | read |  |  | 통계 CSV(종류별). |
| `geocode` | ✓ | ✓ |  | ✓ | read |  |  | 주소 자료로 좌표 찾기. |
| `addr-search` | ✓ | ✓ |  | ✓ | read |  |  | PC 주소 자료 검색. |
| `juso-search` | ✓ | ✓ |  |  | read |  |  | 행안부 주소 검색(서버가 대신 물음). 검색어는 로그·접속기록에 남기지 않는다. |
| `juso-coord` | ✓ | ✓ |  |  | read |  |  | 행안부 검색 결과의 출입구 좌표. |
| `privacy` | ✓ |  |  | ✓ | read |  |  | 개인정보 파기 미리보기·이력·보유기간·백업 상태. |
| `access` | ✓ |  |  | ✓ | read |  |  | 접속기록 조회. |
| `access.csv` | ✓ |  |  | ✓ | read |  |  | 접속기록 CSV. |
| `access/status` | ✓ |  |  | ✓ | read |  |  | 접속기록 월 점검 상태. |
| `integrity` | ✓ |  |  | ✓ | read |  |  | 무결성 빠른 검사(검사점부터 새 행만). 전체 검사는 POST integrity/full (백그라운드 작업). |
| `jobs/{id}` | ✓ | ✓ | ✓ | ✓ | read |  |  | 백그라운드 작업 상태(본인 작업만, 관리자는 전부). |
| `imports/recent` | ✓ | ✓ | ✓ | ✓ | read |  |  | 최근 가져오기(수거팀은 자기 팀 휴대폰만). |
| `pending` | ✓ | ✓ |  | ✓ | read |  |  | 처리할 일(대시보드). |
| `readiness` | ✓ | ✓ |  | ✓ | read |  |  | 운영 준비 점검. |
| `ops-status` | ✓ | ✓ |  | ✓ | read |  |  | 예약 작업 마지막 실행. |
| `diag` | ✓ |  |  |  | read |  |  | 진단(DB 크기·표 건수·예약 작업). |
| `assign/current` | ✓ | ✓ |  | ✓ | read |  |  | 그날 확정 배정. |
| `requests/{id}/notice` | ✓ | ✓ | ✓ | ✓ | read |  |  | 주민 안내 글(이름·연락처·주소 없음). |
| `backup-status` | ✓ |  |  | ✓ | read |  |  | 자동 백업 상태. |
| `revenue` | ✓ | ✓ |  | ✓ | read |  |  | 세입 일계표·월계표. |
| `revenue.csv` | ✓ | ✓ |  | ✓ | read |  |  | 세입 CSV. |
| `recon-template` | ✓ | ✓ |  | ✓ | read |  |  | 대사(납부 자료) CSV 서식. |
| `recon-history` | ✓ | ✓ |  | ✓ | read |  |  | 대사 가져오기 이력. |
| `refunds` | ✓ | ✓ |  | ✓ | read |  |  | 환불 목록. |
| `addr-stats` | ✓ |  |  | ✓ | read |  |  | 주소 자료 건수. |
| `fee-table` | ✓ | ✓ | ✓ | ✓ | read |  |  | 운영 수수료표 상태·검토 행·개정 목록. |
| `me` | ✓ | ✓ | ✓ | ✓ | read | ✓ |  | 나(사용자·역할·화면 설정). |
| `users` | ✓ |  |  | ✓ | read |  |  | 사용자 목록. |
| `backups` | ✓ |  |  |  | read |  |  | 백업 파일 목록. |
| `photo-purge` | ✓ |  |  |  | read |  |  | 사진 정리 대상 미리보기. |
| `audit` | ✓ |  |  | ✓ | read |  |  | 변경 이력 조회. |
| `requests` | ✓ | ✓ | ✓ | ✓ | read |  | LIST | 접수 검색(수거팀 계정: 자기 팀에 배정된 접수만). |
| `requests/{id}` | ✓ | ✓ | ✓ | ✓ | read |  | VIEW | 접수 상세. |
| `duplicates` | ✓ | ✓ |  | ✓ | read |  | SEARCH | 중복 후보(전화·주소). |
| `batches` | ✓ | ✓ | ✓ | ✓ | read |  | LIST | 그날 배정 목록(수거팀은 자기 팀만). |
| `conflicts` | ✓ | ✓ |  | ✓ | read |  | LIST | 충돌함(현장 접수·정정 요청 원문 = 개인정보 포함). |
| `field-intakes` | ✓ | ✓ |  | ✓ | read |  | SEARCH | 현장 접수 확인 목록. |
| `close` | ✓ | ✓ | ✓ | ✓ | read |  | LIST | 마감 점검 요약(수거팀은 자기 팀 범위). |
| `registration-qr` | ✓ |  |  |  | read |  |  | 기기 등록 QR 이미지. |
| `unloads` | ✓ | ✓ | ✓ | ✓ | read |  |  | 그날 하차 기록(수거팀은 자기 팀만). |
| `volumes` | ✓ | ✓ |  | ✓ | read |  |  | 자주 쓴 품목의 부피·무게 추정과 보정값. |
| `load-check` | ✓ | ✓ |  | ✓ | read |  |  | 적재 점검(기간). |
| `fee-usage` | ✓ | ✓ | ✓ | ✓ | read |  |  | 자주 쓴 품목. |
| `qrtests` | ✓ | ✓ | ✓ | ✓ | read |  |  | QR 실측 목록. |
| `photo/{id}` | ✓ | ✓ | ✓ | ✓ | read |  | PHOTO | 현장 사진(수거팀은 자기 팀 접수만). |

## POST

| 경로 | 관리자 | 접수 | 수거팀 | 팀장 | 잠금 | 임시 비번 | 접속기록 | 설명 |
|---|---|---|---|---|---|---|---|---|
| `import/result` | ✓ | ✓ | ✓ |  | none |  |  | 휴대폰 결과 파일 가져오기(수거팀은 자기 팀 휴대폰 파일만). |
| `import/photos` | ✓ | ✓ | ✓ |  | none |  |  | 휴대폰 사진 묶음 가져오기. |
| `import/solo` | ✓ | ✓ |  |  | none |  |  | 단독 휴대폰 백업 옮기기(접수를 만든다). PBKDF2 60만 회는 잠금 밖. |
| `requests/import` | ✓ | ✓ |  |  | none |  |  | 접수 CSV 일괄 등록(preview=1 은 조회 전용 연결에서 미리보기). |
| `recon` | ✓ | ✓ |  |  | none |  |  | 세외수입 납부 자료 대사(preview=1 은 미리보기). |
| `addr-import` | ✓ |  |  |  | none |  |  | 주소 자료 가져오기(큰 파일 읽기는 잠금 밖). |
| `external/test` | ✓ |  |  |  | none |  |  | 외부 서비스 연결 시험(잠금 밖). |
| `route` | ✓ | ✓ |  |  | none |  | LIST | 노선 계산(OR-Tools·카카오 길찾기는 잠금 밖). |
| `backup` | ✓ |  |  |  | none |  |  | 수동 백업(백그라운드 작업). |
| `backup/auto` | ✓ |  |  |  | none |  |  | 자동 백업 지금 실행(백그라운드 작업). |
| `backup/retention` | ✓ |  |  |  | none |  |  | 백업 보관 정리(백그라운드 작업). |
| `privacy/purge` | ✓ |  |  |  | none |  |  | 개인정보 파기(백그라운드 작업). |
| `photo-purge` | ✓ |  |  |  | none |  |  | 사진 보관 기간 정리(백그라운드 작업). |
| `refunds/sync` | ✓ |  |  |  | none |  |  | 자동 환불 건 맞추기(백그라운드 작업). |
| `integrity/full` | ✓ |  |  |  | none |  |  | 무결성 전체 검사(백그라운드 작업). |
| `requests` | ✓ | ✓ |  |  | write |  |  | 접수 등록(client_req_id 로 한 번만). |
| `requests/bulk` | ✓ | ✓ |  |  | write |  |  | 일괄 처리: 예정일 변경·취소·동 지정. |
| `requests/export` | ✓ | ✓ |  |  | write |  |  | 고른 접수 CSV(개인정보 포함은 접수 담당자·관리자만). |
| `requests/print` | ✓ | ✓ |  |  | write |  |  | 고른 접수 인쇄(잠깐 쓰는 인쇄 번호). |
| `requests/{id}/reduction` | ✓ | ✓ |  |  | write |  |  | 감면 지정·해제. |
| `requests/{id}/receipt-print` | ✓ | ✓ |  |  | write |  |  | 접수증 인쇄. |
| `requests/labels` | ✓ | ✓ |  |  | write |  |  | 배출 표지 인쇄. |
| `stats/print` | ✓ | ✓ |  | ✓ | write |  |  | 월보 인쇄. |
| `access/check` | ✓ |  |  | ✓ | write |  |  | 접속기록 월 점검 기록. |
| `refunds/request` | ✓ | ✓ |  |  | write |  |  | 환불 신청. |
| `refunds/{id}/approve` | ✓ |  |  | ✓ | write |  |  | 환불 결재(승인). |
| `refunds/{id}/reject` | ✓ |  |  | ✓ | write |  |  | 환불 결재(반려). |
| `refunds/{id}/pay` | ✓ | ✓ |  |  | write |  |  | 환불 지급 기록. |
| `refunds/{id}/void` | ✓ | ✓ |  |  | write |  |  | 환불 취소. |
| `backup-key` | ✓ |  |  |  | write |  |  | 자동 백업 키 쌍 만들기(개인키는 한 번만 내려줌). |
| `backups/verify-auto` | ✓ |  |  |  | write |  |  | 자동 백업 파일 열어 확인. |
| `requests/{id}` | ✓ | ✓ |  |  | write |  |  | 접수 수정(이전 내용은 버전으로). |
| `requests/{id}/field-check` | ✓ | ✓ |  |  | write |  |  | 현장 접수 확인. |
| `requests/{id}/cancel` | ✓ | ✓ |  |  | write |  |  | 접수 취소(사유 코드). |
| `requests/{id}/contacts` | ✓ | ✓ |  |  | write |  |  | 민원·통화 기록. |
| `requests/{id}/location` | ✓ | ✓ |  |  | write |  |  | 위치 직접 지정. |
| `assign` | ✓ | ✓ |  |  | write |  |  | 배정 확정. |
| `assign/edit` | ✓ | ✓ |  |  | write |  |  | 확정 배정 부분 수정(넣기·빼기·순서·팀 옮기기). |
| `imports/{id}/confirm` | ✓ | ✓ | ✓ |  | write |  |  | 휴대폰 반영 확인. |
| `conflicts/bulk` | ✓ | ✓ |  |  | write |  |  | 충돌 일괄 처리(접수별·가져오기별). |
| `bundles` | ✓ | ✓ |  |  | write |  |  | 휴대폰 목록 묶음 만들기. |
| `bundles/{id}/receipt` | ✓ | ✓ |  |  | write |  |  | 휴대폰 수신 확인 코드. |
| `conflicts/{id}` | ✓ | ✓ |  |  | write |  |  | 충돌 하나 처리. |
| `pilot` | ✓ | ✓ |  |  | write |  |  | 시범 운영 비교 설정 저장. |
| `close` | ✓ | ✓ |  |  | write |  |  | 마감 기록(담당자). |
| `close/approve` | ✓ |  |  | ✓ | write |  |  | 마감 확인(팀장). |
| `dongs` | ✓ |  |  |  | write |  |  | 행정동 저장. |
| `dongs/{id}/delete` | ✓ |  |  |  | write |  |  | 행정동 지우기. |
| `holidays` | ✓ |  |  |  | write |  |  | 휴무일 추가. |
| `holidays/{id}/delete` | ✓ |  |  |  | write |  |  | 휴무일 지우기. |
| `crews` | ✓ |  |  |  | write |  |  | 수거팀 저장. |
| `devices/register` | ✓ |  |  |  | write |  |  | 휴대폰 등록(지문 확인 대기). |
| `devices/{id}/confirm` | ✓ |  |  |  | write |  |  | 휴대폰 지문 확인. |
| `devices/{id}/status` | ✓ |  |  |  | write |  |  | 휴대폰 상태(분실·중지). |
| `requests/{id}/link` | ✓ | ✓ |  |  | write |  |  | 유사 품목 연결. |
| `links/{id}/void` | ✓ | ✓ |  |  | write |  |  | 유사 품목 연결 취소. |
| `requests/{id}/payments` | ✓ | ✓ |  |  | write |  |  | 수납·환불 기록. |
| `payments/{id}/void` | ✓ | ✓ |  |  | write |  |  | 수납 기록 취소. |
| `unloads` | ✓ | ✓ | ✓ |  | write |  |  | 하차 기록(수거팀은 자기 팀만). |
| `volumes` | ✓ |  |  |  | write |  |  | 품목 부피 보정 저장. |
| `qrtest` | ✓ | ✓ | ✓ |  | write |  |  | QR 실측 시험 만들기. |
| `qrtest/{id}/result` | ✓ | ✓ | ✓ |  | write |  |  | QR 실측 결과 기록. |
| `users` | ✓ |  |  |  | write |  |  | 사용자 만들기(임시 비밀번호). |
| `users/{id}/crew` | ✓ |  |  |  | write |  |  | 수거팀 계정의 팀 연결(열린 세션에도 바로 반영). |
| `users/{id}/active` | ✓ |  |  |  | write |  |  | 사용자 사용·중지(중지하면 세션 끊음). |
| `users/{id}/password` | ✓ |  |  |  | write |  |  | 임시 비밀번호 다시 정하기(세션 끊음). |
| `geocode/missing` | ✓ | ✓ |  |  | write |  |  | 좌표 없는 접수에 주소 자료로 좌표 채우기. |
| `fee-revisions` | ✓ |  |  |  | write |  |  | 수수료표 개정안 작성(관리자). |
| `fee-revisions/{id}/approve` | ✓ | ✓ |  |  | write |  |  | 수수료표 개정 승인(작성자 아닌 담당자). |
| `fee-revisions/{id}/reject` | ✓ | ✓ |  |  | write |  |  | 수수료표 개정 반려. |
| `backups/verify` | ✓ |  |  |  | write |  |  | 수동 백업 파일 열어 확인. |
| `prefs` | ✓ | ✓ | ✓ | ✓ | write | ✓ |  | 본인 화면 설정(테마·글자 크기). |
| `password` | ✓ | ✓ | ✓ | ✓ | write | ✓ |  | 본인 비밀번호 변경. |
| `settings` | ✓ |  |  |  | write |  |  | 설정 저장 → 새 설정 묶음. |

경로 129개 (로그인 전 4개 제외).
