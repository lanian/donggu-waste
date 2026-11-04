# PC API 변경 (v0.13) — 화면(pc/ui) 담당용

PC 서버(pc/dgpc) 0.13.0에서 바뀐 API와 화면이 해야 할 일. `tests/test_pc.py`의 `v13()`이 서버 쪽 동작을 확인한다.
`GET /api/bootstrap` 응답에 `"api": 13`, `"quick_filters"`, `"reduction_round_units"`가 추가됐다(화면이 새 서버인지 볼 수 있음).

## 1. 꼭 고쳐야 하는 것 (지금 화면이 깨짐)

| 화면 동작 | 지금 pc.js | 바꿀 것 |
| --- | --- | --- |
| 접수 수정 저장 | `post('requests/' + id, {...data(), reason})` (pc.js 867) | 본문에 **`req_ver`**(상세를 열 때 받은 `r.req_ver`)를 꼭 넣는다. 없거나 지금 버전과 다르면 **HTTP 409** `{"error": "다른 곳에서 바뀌었습니다. 다시 열어 확인하세요", "stale": true, "req_ver": 지금버전, "request_id"}`. 409면 저장하지 말고 안내 후 상세를 다시 불러온다. 성공 응답에 새 `req_ver`가 온다. |
| 수동 백업 | `post('backup', {passphrase})` 결과에서 `name·bytes·sha256` (2271) | 이제 `{"job_id", "status": "QUEUED", "kind": "backup"}`. `GET /api/jobs/{job_id}`를 0.5~1초마다 불러 `status`가 `DONE`이면 `result`(`name·bytes·sha256`), `FAILED`면 `error`를 보여 준다. 비밀번호 12자 미만은 바로 400. |
| 지금 자동 백업 | `post('backup/auto')` → `r.ok/r.file` (1848) | 작업 번호. 끝나면 `result`가 이전 응답(`ok·file·error`…)과 같다. |
| 보관 기간 지난 백업 정리 | `post('backup/retention')` → `r.deleted` (1849) | 작업 번호. `result.deleted`. |
| 개인정보 파기(지금) | `post('privacy/purge')` → `r.count` (1876) | 작업 번호. `result.count`. 진행 중 `progress`/`total`(건). |
| 사진 정리 | `post('photo-purge')` → `r.count` (2282) | 작업 번호. `result.count·bytes`. |
| 무결성 검사 | `api('integrity')` (2097) | 같은 GET이지만 이제 **빠른 검사**(저장한 검사점부터 새 행만, `access/audit`에 `incremental: true, from_id`). 처음부터 끝까지 보려면 `POST /api/integrity/full` → 작업 번호 → `result`가 이전 `integrity` 응답과 같은 모양. |

작업 응답 `GET /api/jobs/{id}`: `{id, kind, label, status: QUEUED|RUNNING|DONE|FAILED, progress, total, result, error, actor, created_at, started_at, finished_at}`.
만든 사람과 관리자만 볼 수 있다(그 밖 404). 작업 기록은 서버 메모리에만 있고 끝난 지 6시간 뒤 지워진다.

## 2. 보내면 좋은 것 (선택, 안 보내도 이전처럼 동작)

- **`client_req_id`** (요청마다 새 무작위 문자열, 예: `crypto.randomUUID()`; 다시 시도할 때는 같은 값): 아래 POST 본문에 넣으면 24시간 안 같은 값이 다시 오면 처리하지 않고 처음 응답을 그대로 돌려준다(두 번 눌러도 1건).
  `POST /api/requests`(새 접수), `POST /api/requests/{id}/payments`, `POST /api/unloads`, `POST /api/requests/{id}/contacts`, `POST /api/refunds/request`, `POST /api/close`.
  키는 사용자·경로별. 처리 중 오류(400)면 기록하지 않으므로 고쳐서 같은 값으로 다시 보내도 된다.
- **`req_ver`**: `POST /api/requests/{id}/cancel`, `POST /api/requests/{id}/reduction` 본문에 넣으면 버전이 다를 때 409(넣지 않으면 확인하지 않음).
- 409는 이 밖에 배정 부분 수정(아래 4)에서도 온다(`assign_ver`).

## 3. 응답이 늘어난 것

- `GET /api/requests`: `quick`에 **`OVERDUE`**(예정일 지남·최종 결과 없음), **`NODATE`**(예정일 없음) 추가, `counts`에도 두 키. 거르기·건수·쪽 나눔은 이제 SQL(동작 같음). 초성 검색은 저장한 초성 열로.
  접수 행에 요약 열 `fee_final, levied, paid_total, pay_status, active_crew_id, chosung`이 함께 온다(목록 표에 바로 써도 됨).
- `GET /api/close` (`day_summary`): **`crews_no_result`** `[{crew_id, crew, sent_at, work_date}]` — 그날 목록을 보냈는데 마지막으로 보낸 뒤 그 휴대폰 결과 파일이 없는 팀. `POST /api/close` 응답에 **`warnings`**(문장 목록). 마감은 막지 않는다 → 마감 확인창에 경고로 보여 주기.
- 결과 가져오기(`POST /api/import/result`) 응답: `seq_reused`(휴대폰 복원으로 순번이 겹친 기록 수, 반영됨·경고만), `import_id`. 사진 가져오기 응답에 `import_id`.
- 충돌함의 "같은 팀 옛 갱신본" 결과는 이제 **반영**된다(`conflict_reason`에 "이전 갱신본(…) — 같은 팀 배정 유지로 반영" 메모). 충돌은 다른 팀으로 옮겼거나 뺀 경우만.
- 결과 기록에 `warning` 열(순번 재사용 등). 상세의 `events`에는 아직 넣지 않음.
- `GET /api/bootstrap` → `settings`에 `payment_guide`(납부 안내 문구, 접수증·안내 문구용), `reduction_round_unit`(1 또는 10), `reduction_round_units`([1, 10]). `POST /api/settings`로 저장(`payment_guide` 300자, `reduction_round_unit` 1·10만). 끝수 단위는 감면을 적용·바꿀 때 접수 열 `reduction_round_unit`에 저장되고(감면 해제 시 비움), 금액 계산(상세·수납 상태·요약 열·접수증·통계·CSV·휴대폰 목록)은 모두 접수에 저장된 값을 쓴다 — 설정을 바꿔도 이미 감면한 접수는 그대로. 수납 상태 응답에 `reduction_round_unit`(그 접수에 쓴 단위). 휴대폰 목록 묶음에는 10원일 때만 선택 키 `"ru": 10`(phone-events 8.5).
- 단독 휴대폰 옮기기(`POST /api/import/solo?preview=1`): `invalid`(틀린 접수 수), `errors: [{request_id(앞 8자), receipt_no, error}]` → 미리보기 표에 행별로. 실제 옮기기 응답에도 `invalid`, `renamed`(접수번호 S 표시로 바꾼 수).
- 접수번호가 겹치는 단독 접수: `…-S0001`, 그것도 있으면 `…-S0001-2`, `-3`….
- 수신 코드 입력(`POST /api/bundles/{id}/receipt`): 휴대폰 화면의 앞 글자 `R-`·띄어쓰기·하이픈을 붙여 넣어도 받는다. 오류 문구는 "수신 코드가 맞지 않습니다".
- 수납·환불 입력: 금액이 숫자가 아니면(예: "만원") 400 업무 오류(이전 500). `"12,000원"`은 받음. 없는 날짜(`2026-13-45`)는 400.

## 4. 새 API

| 방법·경로 | 권한 | 내용 |
| --- | --- | --- |
| `GET /api/jobs/{id}` | 만든 사람·관리자 | 백그라운드 작업 진행·결과 (위 1) |
| `POST /api/integrity/full` | 관리자 | 전체 무결성 검사 작업 |
| `POST /api/refunds/sync` | 관리자 | 환불 전체 점검 작업(자동 환불 건 맞추기). 평소에는 쓰기마다 그 접수만, 밤마다 전체 |
| `GET /api/imports/recent?limit=30` | 접수·관리자·팀장, 수거팀(자기 팀만) | 최근 결과·사진 가져오기: `[{id, kind: RESULT|PHOTO|SOLO, channel, device_id, device_label, crew, imported_at, counts, release_code(결과 반영 코드 D-), photo_code(사진 회수 코드 P-, 사진만), phone_confirmed, phone_confirmed_by: MANUAL|DAYLIST|null}]`. 가져오기 화면을 다시 열어도 코드를 다시 보여 주기 |
| `POST /api/imports/{id}/confirm` | 접수·수거팀·관리자 | "휴대폰에 코드 입력함" 표시. 휴대폰이 다음 목록(받은 순번 rel 포함)을 수신 확인해도 자동으로 확인됨(DAYLIST) |
| `GET /api/pending` | 접수·팀장·관리자 | 대시보드 처리할 일 `{today, total, items: [{key, label, count, oldest, link}]}`. key: `close_approve`(마감 확인 대기), `refund_approve`(환불 결재 대기), `unclosed_days`(마감 안 한 지난 날짜 90일), `overdue`, `nodate`, `no_result_crews`(+`crews`), `conflicts`. `link`는 화면 키 제안(`requests?quick=OVERDUE` 등) |
| `GET /api/readiness` | 접수·팀장·관리자 | 운영 준비 점검 `{ok, done, total, items: [{key, label, ok, fix, note}]}`. key: crews, devices, depot, unload_site, dongs, address, backup_key, fee, contact, admin_password. `fix`는 고칠 화면 키(`settings/crews`, `settings/devices`, `settings/route`, `settings/dongs`, `settings/address`, `settings/backup`, `fee`, `settings/notice`, `users`) |
| `GET /api/ops-status` | 접수·팀장·관리자 | 예약 작업 상태 `{items: [{key, label, last_at, age_days, stale}], warnings: [...]}` — 개인정보 자동 파기·사진 정리·하루 예약 작업·빠른/전체 무결성 검사·백업. 2일(전체 검사 8일) 넘으면 `stale` → 대시보드 경고 띠 |
| `GET /api/diag` | 관리자만 | 진단: `db_bytes, wal_bytes, photos{count,bytes}, free_bytes, auto_vacuum, schema_version, tables{표: 건수}, last{backup, verify_quick, verify_full, pii_purge, photo_purge, daily_tasks, refund_sync}, pre_migration{count, bytes, newest, oldest, keep, days}, scheduler_tick, sessions, jobs_queued` |
| `GET /api/assign/current?date=` | 접수·팀장·관리자 | 그날 확정(ACTIVE) 배정 `[{batch_id, crew_id, crew_name, assign_ver, created_at, sent_at, stops: [{seq, eta, request_id, receipt_no, status, dong, lat, lon, unload_after?}]}]` |
| `POST /api/assign/edit` | 접수·관리자 | 다시 계산하지 않는 부분 수정. 본문 `{date, op, assign_ver: {팀ID: 화면이 본 갱신본}, reason?}` + op별: `insert {crew_id, request_id, position?(1부터, 없으면 끝), eta?}`, `remove {request_id}`, `reorder {crew_id, order: [request_id…] (같은 접수 모두)}`, `move {request_id, to_crew_id, position?}`. 고친 팀마다 새 갱신본(assign_ver+1) → 응답 `{batches: [{batch_id, crew_id, assign_ver, count}], resend_needed: [팀ID]}` → 그 팀 목록을 다시 보내야 함. `assign_ver`가 지금과 다르면 409 `{crew_id, assign_ver}` |
| `POST /api/conflicts/bulk` | 접수·관리자 | 충돌 한꺼번에: `{action: APPLY|REJECT, reason, request_id | import_id}` → `{resolved, failed: [{event_id, kind, error}]}` |
| `GET /api/requests/{id}/notice` | 조회 권한(수거팀은 자기 팀) | 주민 안내 글(**text/plain**): 접수번호·부과번호·배출일·부과 금액·납부 확인·납부 방법·기한(설정 `payment_guide`)·배출 안내·문의처. 이름·연락처·주소 없음. [안내 문구 복사] 단추용 |

접수증(`POST /api/requests/{id}/receipt-print`)은 서버 HTML이라 화면 변경 없음 — **납부 안내** 칸(설정 `payment_guide`)이 들어간다.

## 5. 동작이 바뀐 것 (화면은 그대로 둬도 됨)

- **조회(GET)는 DB 쓰기 잠금 없이** 조회 전용 연결로 처리한다(접속기록·변경 이력은 잠금 안에서 따로 씀). 노선 계산·외부 호출(행안부·카카오)·결과/사진/단독 파일 검증·주소 자료 읽기·로그인 비밀번호 확인도 잠금 밖 → 긴 작업 중에도 다른 화면이 멈추지 않는다.
- 세션: 30분 무조작 만료 + **로그인 뒤 10시간 절대 만료**(조작해도). 401 `{"login": true}`이면 로그인 화면으로(지금과 같음).
- LAN 모드: 한 PC(IP)에서 10분에 20번 넘게 로그인 실패 → 그 PC 10분 잠금(400, "여러 번 틀렸습니다. N초 뒤…"). 이 PC에서만 여는 기본 방식(모두 127.0.0.1)에는 위치별 제한이 없고 계정별 잠금만. 로그인 실패 변경 이력은 분마다 모아 한 줄(`LOGIN_FAIL {count, minute}`).
- 백업 검사(`backups/verify`, `backups/verify-auto`)는 정한 이름 형식(`donggu-backup|auto-YYYYMMDD-HHMMSS[-n].dgbak|dgabk`)만, 백업 폴더 밖 경로 거부.
- 두 번째 백업 위치 변경은 변경 이력에 경로가 남는다(`backup / DEST2`).
- 1초 넘는 요청은 서버 로그에 WARNING(경로 틀·ms).
- 완료일(`completed_at`) = 휴대폰이 최종 결과를 남긴 시각(미래 10분 넘으면 PC 시각). 완료일 기준 통계가 이 값을 쓴다.
- 환불 자동 건(신청 전)은 수납·취소·감면·결과 반영 등 쓰기와 **같은 트랜잭션**에서 그 접수만 맞춘다. `GET /api/refunds`는 더 이상 전체 점검을 하지 않는다(밤마다 + 관리자 `POST /api/refunds/sync`).
- 개인정보 자동 파기·사진 보관 기간 정리는 서버 예약 작업이 하루 한 번(기본 02:30 이후 첫 순간, `DG_DAILY_AT`) 500건씩 한다. 파기 대장은 묶음마다 한 줄. 마감 기록 요약(`day_close_log.summary_json`) 속 그 묶음 접수의 이름·연락처·주소도 **같은 묶음 트랜잭션에서** 지운다(중간에 멈춰도 파기된 접수 이름이 요약에 남지 않음). 또 파기를 돌릴 때마다(수동·마감 확인·예약 작업, 대상 0건이어도) 이미 파기된 접수(`pii_purged_at`)의 이름 등이 남은 요약을 id·접수번호로 찾아 지운다(예전 버전에서 멈춘 경우 복구) — 결과에 `repaired_close_logs`(고친 마감 기록 수), 변경 이력 `privacy / close-summary PURGE_PII`.
- 휴대폰에 보내는 목록에 `rel`(세대 없는 이 기기 기록 1..seq를 빠짐없이 받음, 회수한 사진 ID, 선택 `g` = 기록 세대별 받은 번호) — 휴대폰이 코드를 넣지 않아도 정리(dgproto). 세대(`gen`)는 휴대폰이 백업에서 복원할 때마다 바뀌어, 복원 뒤 다시 쓰인 번호를 PC가 받은 것으로 알리지 않는다. 결과 파일: 기록마다 선택 `gen`, 맨 위 선택 `gens` = `[[세대, 첫 번호]…]`. 자세히는 `docs/phone-events-v0.10.md` 10.7.

## 6. e2e_pc.py 실행 결과 (서버만 바꾼 상태, 2026-11-04)

`python3 tests/e2e_pc.py` → 10개 통과 뒤 **"수정 이력" 단계에서 멈춤**: 접수 수정 저장이 `req_ver` 없이 가서 409 → "주민 요청: 위치 보완" 이력이 안 보임. 위 1의 수정(접수 수정에 `req_ver`)이 먼저 필요하다.
그 뒤 단계는 이 실행에서 확인되지 않았다. 코드로 본 예상 깨짐: 수동 백업·자동 백업·백업 정리·개인정보 파기·사진 정리 단추(작업 번호 응답, 위 1), 무결성 검사(빠른 검사로 바뀜 — 화면 표시는 그대로 되지만 전체 검사 단추 추가 필요).

## 7. 화면에 넣으면 좋은 것 (서버 준비됨)

- 대시보드: `GET /api/pending` 처리할 일 상자, `GET /api/ops-status` 경고 띠, `GET /api/readiness` 운영 준비 점검표(관리자).
- 가져오기 화면: `GET /api/imports/recent` 목록(코드를 `D-XXXX XXXX` / `P-XXXX XXXX`로, 이름은 **결과 반영 코드**·**사진 회수 코드**), [휴대폰에 입력함] 단추.
- 접수 목록 빠른 조회 단추 "지난 예정일 미처리(OVERDUE)"·"예정일 없음(NODATE)".
- 마감: `crews_no_result` 경고.
- 배정 화면: 확정 배정에서 넣기·빼기·순서·팀 옮기기(`assign/edit`), 409 처리.
- 충돌함: 접수별·가져오기별 [모두 반영]/[모두 거부] (`conflicts/bulk`).
- 접수 상세: [안내 문구] (`requests/{id}/notice`, text/plain).
- 설정: 납부 안내 문구(`payment_guide`), 감면액 끝수 단위(1원·10원), 관리자 [환불 전체 점검]·[전체 무결성 검사].
- 진단(관리자): `GET /api/diag`.

## 8. 남은 것·제한 (서버)

- 사진을 별도 암호화 DB 파일(photos.db)로 옮기는 일(16)은 위험이 커서 하지 않았다. 대신: 새 DB는 `auto_vacuum=INCREMENTAL`, 파기·사진 정리 뒤 `PRAGMA incremental_vacuum`, 백업 해시는 조각으로 읽어 계산. 기존 DB를 INCREMENTAL로 바꾸려면 서버를 끄고 `python pc/pc_tool.py vacuum`(큰 DB는 몇 분).
- 이전 단계(PRAGMA user_version): 1 = v0.12 이하 구조, 2 = v0.13 요약 열·색인, 3 = 접수 열 `reduction_round_unit`(감면액 끝수 단위, 기존 감면 접수는 그때 설정값으로 채움), 4 = 휴대폰 기록 세대(`result_event.gen`, 표 `device_gen`·`result_seq`; 기존 결과 기록의 받은 번호를 세대 `''`로 채움). 기존 DB를 처음 열 때 `state/pre-migration/pc-v{이전}-to-v{새}-….db`로 파일을 복사해 둔다(같은 DB 키로 암호화된 그대로). 사본은 매일 예약 작업이 정리한다(가장 새 1개는 늘 남김, 나머지는 새것부터 3개 안·30일 이내만; 지우면 변경 이력 `backup / pre-migration PURGE`). DB가 프로그램보다 새 버전이면 열지 않는다.
- 접수 일괄 등록(CSV)·대사 파일의 **실제 반영**은 아직 잠금 안에서 읽고 넣는다(미리보기는 잠금 밖). 2,000·5,000행 제한이라 짧다.
- 마감 확인(`close/approve`) 뒤 자동 파기·자동 백업은 이전처럼 그 요청 안에서 돈다(예약 작업과 별개).

## 9. 화면(pc/ui) 반영 결과 (2026-11-04)

- 서버 한 줄 변경: `GET /api/conflicts` 행에 `import_id`·`warning`을 더했다(가져오기별 [모두 반영/거부], 순번 재사용 경고 표시). `tests/test_pc.py` v13에 확인 1개 추가(525개).
- 접수 수정·취소·감면에 `req_ver`를 보내고 409면 "다른 곳에서 바뀌었습니다" + [다시 열기] → 다시 연 화면에 내가 고친 칸만 보여 주고 [내 값 다시 넣기].
- 작업 번호 응답(수동·자동 백업, 백업 정리, 개인정보 파기, 사진 정리, 전체 무결성 검사, 환불 전체 점검)은 오른쪽 아래 진행 띠(`.jobs`)에서 `GET /api/jobs/{id}`를 불러 끝나면 결과를 알린다. 접속기록 화면은 [빠른 무결성 검사]·[전체 무결성 검사](관리자).
- `client_req_id`: 새 접수·수납·하차·통화 기록·환불 신청·마감에 보낸다(키별로 화면 밖에 두고 성공하면 지움, 실패 뒤 다시 보내면 같은 값). 모든 작업 단추는 누른 동안 꺼진다(두 번 누름·Ctrl+Enter).
- 새로 쓰는 API: `pending`(오늘 현황 '밀린 일'·메뉴 숫자), `readiness`(관리자 오늘 현황 맨 위, 모두 ✓이면 숨김), `ops-status`(경고 띠·진단), `diag`(사용자·이력 화면 진단), `assign/current`·`assign/edit`(배정 화면 [현재 배정 불러오기]), `conflicts/bulk`, `imports/recent`·`imports/{id}/confirm`(결과 가져오기 '최근 가져오기'), `requests/{id}/notice`([안내 문구 복사]).
- 화면 주소에 조건을 붙일 수 있다: `#/requests?quick=OVERDUE|NODATE|FEE…`, `#/close?date=YYYY-MM-DD`, `#/refunds?status=REQUESTED`, `#/results?focus=conflicts` (한 번 반영하고 주소에서 지움).
