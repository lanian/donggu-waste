# 실행 간편화와 서버 준비 판정 개선

**목표:** 실행앱에서 시작·업무 열기·종료를 구분하고 실제 PC 웹 로딩 실패를 복구한다.

## 현재 환경의 확정 원인

- 8765 포트는 기존 CLI Python 서버가 사용하고 새0.13.4 실행앱은 이를 재사용한다.
- `/`와 `/pc.js`, `/core.js`는 새 디스크 파일을200으로 보내지만 새 `/start.js`는404다. 장기 실행 서버의 옛 `UI_FILES`와 갱신한 디스크 파일이 섞였다.
- 현재 `app_ready()`는 API bool2개와 HTML title만 확인해 이를 준비 완료로 표시한다.
- 기존 CLI는 실행앱이 소유한 자식이 아니어서 앱 종료가 서버 종료를 뜻하지 않는다. 무관한 프로세스 강제 종료나 같은 DB의 서버 중복 실행으로 우회하지 않는다.

## 구현과 검증 순서

1. root: 실제 브라우저에서 `/start.js`404를 재현하는 회귀를 추가하고 실패 확인. 독립 초기 안내를 기존 허용 진입점 `/pc.js`로 통합하고 core를 동적으로 읽는다. mutable boot는 namespace live binding으로 보존한다. 기존 서버를 재시작하지 않고 HTML 새로고침만으로 로그인 복구를 확인한다.
2. runtime 담당: 준비 판정에 HTML이 참조하는 동일 출처 스크립트와 필수 업무 파일의 실제200·JS MIME·비어 있지 않은 본문을 확인한다. 불량 기존 서버와 무관 포트 충돌을 구분한다. RED→GREEN, 기존 프로세스/키/종료34개 회귀를 실행한다.
3. Tk 담당: 기본 창에는 상태·주 동작·종료를 두고 자료 폴더/바로가기는 설정·상세에 둔다. 종료 뒤 주 동작은 업무 시작, 준비 뒤는 업무 화면 열기다. 외부 서버는 종료 불가를 직접 표시한다. 실제 Tk 상태/키보드/확대/종료 대기 회귀로 검증한다.
4. root: 시작·날짜·초안 회귀, 최종 EXE의 실제 SQLCipher·업무 자산·노선·정상 종료·DB 보존과 브라우저를 검증한다. 버전0.13.5를 별도 폴더에 빌드하고 기존 자료·배포본·AGENTS.md를 보존한다.

현재 사용자 서버에는 읽기 전용 state/정적 파일 확인만 한다. 로그인·업무 변경·실제 자료 조회는 하지 않는다. 모든 변경 회귀와 새 서버 시험은 임시 자료·별도 포트다. 커밋·원격 배포는 하지 않는다.

## 구현·검증 기록

- 현재8765의 CLI 서버가 새 HTML·pc.js를200으로 보내고 start.js만404로 보내는 것을 HTTP와 실제 Chromium에서 확인했다. 이전 API/title 준비 판정은 이 상태를 준비 완료로 오인했다.
- 기존 허용 pc.js에 초기 watchdog과 동적 core/라우터를 통합했다. 변경되는 boot는 core.boot live binding으로 읽는다. 기존 서버를 재시작하지 않고 현재8765에서 로그인 표시·404없음·초기 카드 제거를 확인했다. 새 app_ready의 실제 자산 확인도 True다. 사용자 로그인이나 업무 변경은 하지 않았다.
- 옛 start.js 경로를404로 고정한 임시 실제 서버 회귀를 RED→GREEN으로 확인했다. 빠른 초기 화면 이동에서 이전 render 완료가 watchdog을 끄는 결함도 독립 검토로 찾아6개 RED→GREEN으로 수정했다.
- 준비 판정은 실제 script src와 pc.js/core.js의200·JS MIME·본문을 확인한다. 외부 출처/redirect는 따라가지 않는다. 기존 서버 자산 실패는 일반 포트 충돌과 구분해 안내한다.
- 기본 창은640×440(최소560×400), 상태·주 동작·종료를 유지하고 자료 위치·바로가기는 설정·상세에 접었다. 오류 복구 버튼은 고정 노출하고 내용이 맞으면 스크롤바를 숨긴다. 외부 서버 사용 중에는 자료 폴더 변경·종료를 잠근다.
- 외부 서버는 단일 백그라운드 조회, 완료 뒤3초 간격, 연속2회 실패로 연결 끊김을 표시한다. controller/operation epoch/view/status/busy/closing 가드로 늦은 조회 결과를 버린다.
- 마지막 자료 범위 검토에서 연결 끊김 복구가 다른 선택 폴더에 새 DB를 만들 수 있음을 P2로 판정했다. 외부 서버 복구는 읽기 전용 재연결로 제한하며 새 서버 시작·키 요청·초기화를 하지 않는다.

| 검증 | 결과·범위 |
| --- | --- |
| `tests/test_pc_startup.py` | 최종 exit0. legacy 진입·빠른 이동6·lazy·조회35·지연 안내/수동 복구 |
| `tests/test_workflow.py` | 최종 exit0. 시작3·역할/날짜/초안6·PC13·휴대폰25, 총47 |
| `tests/e2e_pc.py` |114개 exit0. pc.js 통합 후 실행, 이후 초기 watchdog 빠른 이동 분기는 위6개와 최종 배포본에서 별도 검증 |
| `tests/test_desktop_app.py` |49개 exit0. 실제 source/pythonw·소유권·자산9·외부 상태 조회/종료6 포함 |
| `tests/test_desktop_lifecycle.py` | 실제 SQLCipher·API·작업 완료 대기·미완성 연결 제한·정상 종료·DB 보존 통과 |
| `tests/test_desktop_paths.py` |14개 exit0. 공개 자산·개인 자료 거부·버전별 출력 경로 포함 |
| 정적 검사 | 버전3곳0.13.5·precache·생성물4개·JS 문법·diff 검사 통과 |

로그: `%TEMP%/dg-0135-startup-final.log`, `dg-0135-workflow-final.log`, `dg-0135-pc-e2e.log`, `dg-0135-desktop-runtime.log`, `dg-0135-desktop-paths.log`. 이번 작업에서 전체 backend669/server28 suite를 다시 실행하지 않았으며 앞선 검증과 구분한다.

Windows Computer Use는 현재 브라우저 URL을 확인하지 못해 자동 조작을 중단했다. 기존 브라우저 창의 자동 새로고침이나 로그인은 하지 않았으며 실제 로컬 서버의 별도 Chromium에서 읽기 전용 로그인 화면 표시를 검증했다. Tk 화면 캡처는 임시 View fixture이며 실제 사용자 DB/패키지 GUI 버튼 전체 검증과 구분한다.

## 최종 배포본

- 실행: `dist/desktop/v0.13.5/Donggu-PC/Donggu-PC.exe` (폴더 전체 유지)
- 전달: `dist/desktop/v0.13.5/Donggu-PC-windows-x64.zip` — 66,428,361 bytes
- ZIP SHA256: `078e266786de3d261e00e94c945c150062d1d4c628ef0670e44e47689fd1a4d9`
- 로컬 간편 실행: `E:/Projects/waste/동구 PC 실행.lnk` → 위 EXE. 실행 인자 없이 저장된 자료 선택을 그대로 사용하며 COM readback으로 경로를 확인했다. 사용자 바탕화면은 변경하지 않았다.
- 실제 최종 EXE 시험: `tests/test_desktop_bundle.py` exit0. Python 없는 PATH·공백 경로·windowed PE·정적 파일·SQLCipher DB·계정/접수·실제 OR-Tools 노선·정상 종료·포트 해제·DB 보존 통과.
- 실제 최종 EXE 브라우저: 임시 harness에서 legacy·초기 빠른 이동6·lazy·지연 안내를 재실행해 exit0. 배포 pc.js는 소스 바이트와 같았다. 서버 준비 뒤 로그인 표본0.162초/5모듈이며 실제 사용 PC 전체 시작 시간 보장은 아니다. 로그 `%TEMP%/dg-0135-frozen-web-final.log`.
- 실제 Tk46개에는 상태/기본·최소 창/100·150·200%/키보드/종료 대기/연속 조회·늦은 응답/재연결 전용5개를 포함한다. P2 수정 후 전체 재검증 중 다중 fixture의 Tk GC가 worker에서 실행돼 Tcl 오류가 난 시도가 있었다. 테스트의 threaded 묶음에서 자동 GC를 잠시 끄고 시작/종료에 main-thread collect하도록 고쳤으며 제품 로직은 바꾸지 않았다. 실패 로그는 `%TEMP%/dg-0135-ui-fixture-gc-failure.log`에 보존했다.
- 화면: `shots/desktop-v0.13.5.png`, `shots/desktop-connection-v0.13.5.png` — 최종 소스의 실제 Tk View fixture 픽셀.

최종 재빌드는 `%TEMP%/dg-0135-final-rebuild.log` exit0이며 예비 빌드와 해시를 구분한다. 기존0.13.3/0.13.4 배포본, AGENTS.md, 미커밋 변경과 사용자 자료를 보존했다. 기존 CLI 서버의 소유권을 가져오거나 종료하지 않았으며, 새 실행앱의 종료 버튼은 자신이 시작한 서버에만 동작한다.

root 최종 Tk 재실행도46개·exit0(`%TEMP%/dg-0135-ui-verified.log`)을 확인했다. 초기 fixture GC 실패를 전체 통과로 섞어 보고하지 않았다. 최종 bundle·frozen browser·Tk fixture 프로세스는 모두 종료했고 원래 CLI 서버는 유지했다.
