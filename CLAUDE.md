# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

동구 대형폐기물 접수·수거 관리 **시제품** (PC·휴대폰 같은 버전 번호, 현재 0.13.x). 문서·주석·커밋·UI 문구는 모두 한국어다. 기능·운영 절차는 `README.md`, `docs/배포·운영.md`(CI·릴리스·PC 업데이트), `docs/운영매뉴얼.md`. 휴대폰↔PC 이벤트 규격은 `docs/phone-events-v0.10.md`, v0.13 API 변경은 `docs/api-changes-v0.13.md`.

## 명령

```bash
# PC 프로그램 (Python 3.12+, sqlcipher3 필수 — 없으면 평문으로 돌지 않고 멈춤)
python -m venv .venv && .venv/Scripts/pip install -r pc/requirements.txt   # 해시 고정본. 의존성 수정은 pc/requirements.in → uv pip compile (docs/배포·운영.md 4.3)
python pc/pc_tool.py init                 # 암호화 DB·PC 키 생성 (DB 키는 OS 키 저장소에)
python pc/pc_tool.py serve                # http://127.0.0.1:8765
python pc/pc_tool.py seed-sample-dongs    # 시험용 가상 행정동

# 시험
npm ci && npm test                        # tests/fees.test.mjs + tests/interop.mjs (Node 22, python3 호출)
python tests/test_pc.py                   # PC 업무 로직
python tests/e2e.py                       # 휴대폰 화면 (Playwright Chromium, CHROME=브라우저 경로, 기본 /usr/bin/google-chrome)
python tests/e2e_pc.py                    # PC 화면
npm run test:all                          # 버전 검사 + 위 전부

# 생성물 (원본을 바꾸면 다시 만들어 함께 커밋. 각 도구에 --check 있음)
python tools/check_generated.py           # CI가 쓰는 일괄 대조 — 아래 4개
python tools/build_fee_js.py              # data/fee_table.json + data/item_handling.json → web/fees-data.js
python tools/build_labels.py              # data/labels.json → web/labels-data.js
python tools/gen_settings_doc.py          # pc/dgpc/settings.py 등록표 → docs/settings.md
python tools/gen_routes_doc.py            # pc/dgpc/server.py ROUTES → docs/routes.md
python tools/check_version.py [--tag vX.Y.Z]
npm run vendor                            # node_modules → web/vendor 복사 (devDependencies 버전 올린 뒤)
```

- 테스트는 프레임워크 없는 단일 스크립트(`ok(cond, msg)` 누적, 첫 실패에서 중단)라 **개별 테스트 선택 옵션이 없다**. 한 부분만 보려면 해당 파일을 직접 실행한다.
- 시험은 임시 폴더를 `DG_STATE`로, `DG_DB_KEY=test-db-key-not-for-production`을 쓴다. `DG_TEST_DIR`로 작업 폴더(화면 캡처는 `shots/`)를 지정할 수 있다.
- `package.json` 스크립트·`interop.mjs`는 `python3`를 부르고, 그 Python에 `sqlcipher3`가 있어야 한다. Windows에서 `python3`는 보통 전역(WindowsApps) Python이라 실패한다 → `.venv/Scripts/python.exe`를 같은 폴더에 `python3.exe`로 복사하고 `PATH="$PWD/.venv/Scripts:$PATH"`로 실행. `npm run vendor`는 `cp`를 써서 Git Bash에서만 된다. 한글 출력이 깨지면 `PYTHONUTF8=1`.

## 구조 (큰 그림)

세 부분이 **같은 전송 규격**으로 오프라인 연동된다. 서버는 PC 한 대뿐이고 휴대폰은 PC와 네트워크로 통신하지 않는다.

```
PC(pc/) ──암호화 분할 QR(목록 묶음)──▶ 휴대폰 PWA(web/)
PC ◀──암호화 결과 파일 .dgr(업무 메일) · 사진 파일 .dgp(보안 매체)── 휴대폰
```

### PC와 휴대폰이 짝으로 가진 것 (한쪽만 바꾸면 안 됨)

| 무엇 | PC | 휴대폰 | 함께 확인 |
| --- | --- | --- | --- |
| 전송 규격(ECDH P-256→HKDF→AES-256-GCM+ECDSA, Base45 QR 조각) | `pc/dgproto.py` | `web/proto.js` | `tests/interop.mjs` |
| 품목 상태·수수료(계산→감면→부과)·수납 상태 순수 계산 | `pc/dgpc/derive.py` | `web/fees.js` | 공통 벡터 `data/derive_vectors.json` (`fees.test.mjs`, `test_pc.py`) |
| 표시 이름(접수 상태·연락·미수거 사유 등) | `core.py`의 `LABELS` | `web/labels-data.js`(생성물) | 원본 `data/labels.json` |
| 수수료표·품목별 적재 기준 | `fees.py`, `routing.py` | `web/fees-data.js`(생성물) | 원본 `data/*.json` |

### PC (`pc/`)

- `pc_tool.py`: CLI 진입점. `dgpc/server.py`: 표준 라이브러리 `ThreadingHTTPServer`로 `pc/ui/`와 `/api/*` 제공.
- **API 경로는 `server.py`의 `@endpoint(method, pattern, roles, lock=…, access=…)` 데코레이터로 `ROUTES`에 등록**한다. 역할 권한(ADMIN/LEAD/INTAKE/FIELD)·DB 잠금(GET=read, POST=write)·접속기록이 여기서 같이 정해진다. 경로를 추가·변경하면 `gen_routes_doc.py`로 `docs/routes.md`를 다시 만든다. 모든 API 요청은 `X-DG-CSRF: 1` 필요.
- **업무 모듈은 계층 구조**(아래 → 위, 위는 아래만 부름). 정확한 순서는 `dgpc/service.py` docstring이 정본:
  `settings`·`store` → `core` → `fees` → `derive` → `payments`·`devices`·`users`·`auditlog`·`addresses` → `area`·`finance` → `intake` → `results` → `privacy`·`photos` → `assign` → `close`·`reports`·`backup` → `config` → `addrsearch` → `service` → `ops` → `server`
  - `core`는 다른 업무 모듈을 부르지 않는다(위 계층 일은 `on_dirty`로 등록받아 호출).
  - `service.py`는 이제 예전 이름을 다시 내보내는 **겉면(façade)** 일 뿐이다. 새 코드는 해당 업무 모듈을 직접 import한다.
  - `ops.py`: 백그라운드 작업 실행기(작업 번호 반환 → 화면이 `GET /api/jobs/{id}`로 진행 확인)와 예약 작업.
- **설정 키는 `settings.py`의 `SETTINGS` 등록표에만** 정의한다(기본값·검사·`secret`·`expose`). 등록 안 된 키는 `get_setting/set_setting`에서 `KeyError`. 추가하면 `gen_settings_doc.py`로 `docs/settings.md` 갱신.
- `store.py`: SQLCipher DB(`pc/state/pc.db`, 또는 `DG_STATE`). 스키마 변경은 **`MIGRATIONS` 목록에 단계 추가**(`PRAGMA user_version`, 단계마다 한 트랜잭션, 시작 때 자동 실행, 실행 전 DB 사본). DB가 프로그램보다 새 버전이면 열지 않는다. `test_pc.py`의 옛 버전 DB 시험이 통과해야 한다.

### 화면 (PC `pc/ui/`, 휴대폰 `web/`) — 둘 다 빌드 없는 ES 모듈

- 같은 틀: 시작점(`pc/ui/pc.js` / `web/app.js`)이 라우터, `core.js`가 공통 도우미(h·api·render 등), `widgets.js`가 공유 부품, 화면마다 `screens/*.js`. 화면 모듈은 `core.js`·`widgets.js`에만 기댄다.
- **PC 새 화면**: `pc/ui/screens/이름.js` → `pc.js`의 `screens`·`NAV`·`GROUPS`에 등록 → `server.py`의 `UI_FILES`에 파일 추가(안 하면 404).
- **휴대폰 새 파일**: `web/sw.js`의 `FILES` 사전 저장 목록에 직접 추가(안 하면 오프라인에서 깨짐. `check_version.py`는 목록에 있는 파일이 실제로 있는지만 보고 빠진 파일은 못 잡는다). 휴대폰 상태를 통째로 바꿀 때는 `core.js`의 `setSt()` 등 설정 함수를 쓴다(다른 모듈의 `let`은 재대입 불가).
- PC 화면은 휴대폰 모듈을 `/web/...` 경로로 일부 재사용한다(예: `/web/map.js`).
- 휴대폰 앱은 GitHub Pages로 `web/` 그대로 배포. **PC 연결 방식**과 **단독 방식** 두 가지로 동작. 순수 함수(`field.js`, `fees.js`, `proto.js` 등)는 Node 테스트가 import하므로 DOM 의존 코드와 분리한다.

## 지켜야 할 불변 조건

- **버전 올리기**: `web/app.js`의 `APP_VERSION`, `web/sw.js`의 `VERSION`, `pc/dgpc/__init__.py`의 `__version__` 세 곳을 같은 값으로(`check_version.py`가 셋을 대조, CI 배포 조건). `web/`을 바꾸면 휴대폰이 새 앱을 받도록 반드시 올린다. README "현재 버전"·운영매뉴얼 제목도 맞춘다.
- 생성물(`web/fees-data.js`, `web/labels-data.js`, `docs/settings.md`, `docs/routes.md`)과 `web/vendor/`는 직접 고치지 않는다. CI가 원본과 대조한다.
- **개인정보 경계가 기능 요구사항이다**: 이름·연락처는 어떤 외부 서비스로도 나가지 않는다(지도 조각·행안부 검색어·좌표만). 결과 파일엔 원칙적으로 개인정보 없음(현장 접수·정정 요청만 예외). 서버 로그·변경 이력·접속기록에 검색어·이름·주소를 남기지 않는다. CSP(`web/index.html`)의 허용 서버를 늘리지 않는다. e2e 시험이 외부 요청과 반출 내용을 검사하므로 이를 깨면 실패한다.
- PC가 원본이다. 접수는 지우지 않고 버전으로 보존(수정 사유 필수), 현장 결과는 `event_id`로 한 번만 반영, 오래된 배정·취소된 건의 결과는 자동 반영하지 않고 충돌함으로 보낸다. 한 접수는 활성 배정이 하나뿐.
- 시험·예시에는 가상 자료만 쓴다(실제 동 이름·주민 정보 금지, 가상 동은 `sample_data.py`).

## CI

`.github/workflows/ci.yml`: 버전 검사 → Linux 단위·로직(vendor `cmp` 대조, `check_generated.py`, `npm test`, `test_pc.py`) → e2e 2종. Windows·macOS PC 시험은 참고용. main push·`v*` 태그에서 시험 통과 시 `web/`만 Pages 배포. `*.md`·`docs/`만 바뀌면 건너뜀.
