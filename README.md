# 동구 대형폐기물 접수·수거 시제품

전화 접수 → PC에서 당일 목록을 **암호화 분할 QR**로 표시 → 휴대폰(Android·아이폰 공통 PWA)이 카메라로 받아 **오프라인**으로 현장 기록 → **암호화 결과 파일**을 업무 메일로 PC에 회수하는 흐름을 검증하는 시제품입니다.

> **시제품입니다. 가상 자료로만 사용하세요.** 수수료표는 확인된 일부 행만 들어 있으며(원문 대조 전), 운영용이 아닙니다.

## 두 가지 사용 방식

처음 실행할 때 고릅니다.

| 방식 | 원본 위치 | 흐름 |
| --- | --- | --- |
| **PC와 연결 (권장)** | PC | PC가 암호화 QR로 목록 전달 → 휴대폰 현장 기록 → 암호화 결과 파일로 회수 |
| **이 휴대폰만 사용** | 휴대폰 | 휴대폰에서 전화 접수 등록(중복 경고, 수정 사유·이력, 취소 사유) → 날짜별 목록·방문 순서 → 현장 기록 → **암호화 백업 파일**로 보관·다른 휴대폰에 복원 |

단독 방식은 원본이 휴대폰에만 있으므로 분실·고장·브라우저 데이터 삭제 시 원본까지 사라집니다. 앱은 마지막 백업 후 24시간이 지나면 경고합니다. 여러 휴대폰이 같은 접수를 나눠 처리할 수는 없습니다.

## 원칙

- 서버에 업무 자료를 두지 않습니다. GitHub Pages에는 **앱 코드만** 올라갑니다.
- 휴대폰 자료는 IndexedDB에 레코드별 AES-GCM으로 암호화하고, 데이터 키는 PIN(PBKDF2-SHA256 60만 회)으로 감쌉니다.
- PC↔휴대폰은 네트워크로 직접 연결하지 않습니다. 목록은 화면 QR, 결과는 암호화 파일로만 오갑니다.
- 앱 CSP가 외부 통신을 **바탕 지도 서버(tiles.openfreemap.org) 하나**로 제한합니다. 외부 스크립트·분석·오류 수집 코드가 없습니다.
- 지도에는 바탕 지도 조각만 요청합니다. 이름·연락처·주소·핀 좌표는 보내지 않고, 주소 검색·길찾기 API를 쓰지 않습니다. 조각은 줌 14(약 2km 단위)까지만 요청해 집 단위 위치가 지도 서버에 드러나지 않게 합니다. 설정에서 지도를 끌 수 있습니다.

## 구성

| 경로 | 내용 |
| --- | --- |
| `pc/dgproto.py` | 전송 규격(봉투 암호화·서명, 조각, Base45) PC 구현 |
| `pc/pc_tool.py` | PC 명령줄 도구: 키 생성, 기기 등록, 목록 QR 화면 생성, 수신 확인, 결과 가져오기 |
| `pc/sample_data.py` | 가상 접수 자료, 시제품용 수수료 행 |
| `web/` | 휴대폰 PWA (GitHub Pages로 배포되는 부분) |
| `web/proto.js` | 같은 전송 규격의 WebCrypto 구현 |
| `web/store.js` | PIN 잠금과 암호화 저장소 |
| `web/fees.js` | 품목 검색, 수수료·잔량 계산 |
| `web/map.js` | 온라인 지도(MapLibre + OpenFreeMap), 허용 주소·줌 제한 |
| `web/backup.js` | 단독 모드 암호화 백업·복원 |
| `tests/interop.mjs` | Python↔WebCrypto 상호 운용 테스트 (CI에서 실행) |
| `tests/e2e.py` | 헤드리스 Chrome 가짜 카메라로 QR 영상을 읽는 종단 테스트 |

## 사용 순서

### 1. PC 준비 (1회)

```bash
cd pc
pip install -r requirements.txt
python pc_tool.py init        # PC 키와 로컬 DB 생성 (pc/state/)
python pc_tool.py reg-qr      # pc/out/register.html → 브라우저로 열기
```

### 2. 휴대폰 준비 (1회)

1. Pages 주소를 열고 **홈 화면에 추가**합니다 (아이폰: 공유 → 홈 화면에 추가 / Android: 메뉴 → 앱 설치).
2. 홈 화면 아이콘으로 실행 → PIN 설정(숫자 6자리 이상).
3. [기기 등록] → PC의 등록 QR을 비춤 → 화면의 **기기 코드**를 PC에 등록:

```bash
python pc_tool.py register-device "DGD1.xxxxxxxx…"
```

4. PC와 휴대폰에 나온 **6자리 지문**이 같은지 확인합니다.

### 3. 매일

```bash
python pc_tool.py make-bundle --device <기기번호 앞8자> --count 20
# pc/out/bundle_xxxxxxxx.html 을 열면 QR이 순환 표시됩니다
```

- 휴대폰 [오늘 목록 받기] → 화면을 비춤 → 다 받으면 **수신 확인 코드**가 나옵니다.

```bash
python pc_tool.py confirm-receipt <묶음 앞8자> <수신 확인 코드>
```

- 현장: 전화하기, 연락 결과, 현장 확인·수거 수량, 품목 검색·추가, 사진, 수거 결과.
- [결과 보내기] → 결과 파일(.dgr)을 업무 메일로 보냄 → PC에서:

```bash
python pc_tool.py import-result DG_R_xxxxxxxx_1-11.dgr
```

- PC가 출력한 **보관 종료 확인 코드**를 휴대폰에 입력하면 "반영 확인됨"이 됩니다.

## 테스트

```bash
npm ci && npm test            # 상호 운용 21개 (암호화·서명·조각·변조·중복·QR 인식)
python3 tests/e2e.py          # 브라우저 종단 41개: PC 연결·단독·지도, 모든 요청의 개인정보 반출 검사 (Chrome + playwright 필요)
```

## GitHub Pages 배포

`main` 브랜치에 push하면 `.github/workflows/pages.yml`이 테스트 후 `web/`만 배포합니다.
저장소 **Settings → Pages → Source: GitHub Actions** 로 한 번 설정해야 합니다.

배포 통제: 저장소 계정 2단계 인증, 보호된 `main` 브랜치, 검토 후 병합. 새 버전은 휴대폰에서 [업데이트]를 눌러야 적용됩니다.

## 시제품 범위 밖 (설계서 기준 이후 단계)

- PC 업무 화면(접수 등록·배정·노선), OSRM·OR-Tools 노선 계산
- 사진 원본의 보안 USB 회수 (지금은 휴대폰에 암호화 보관, 결과 파일에는 해시만)
- 운영체제 키 저장소·SQLCipher 적용 (지금은 PC 키를 `pc/state/` PEM 파일로 보관)
- 공식 수수료표 전체 (원문 입력·2인 대조 필요)

## 알려진 위험

- 호스팅 계정이 탈취되면 악성 업데이트가 가능합니다. PIN 감싸기로 기존 기록 열람은 막지만 완전 차단은 아닙니다.
- 사용자가 브라우저의 사이트 데이터를 지우면 보내지 않은 기록이 사라집니다. 결과를 하루 2회 이상 보내세요.
- 웹 암호화 키는 하드웨어 보안 영역에 묶이지 않습니다.

## 라이선스

`web/vendor/jsQR.js` — [jsQR](https://github.com/cozmo/jsQR) 1.4.0, Apache-2.0.
`web/vendor/maplibre/` — [MapLibre GL JS](https://github.com/maplibre/maplibre-gl-js) 6.16.0, BSD-3-Clause.
바탕 지도 — [OpenFreeMap](https://openfreemap.org), © OpenMapTiles, © OpenStreetMap 기여자 (지도 화면에 출처 표시).
