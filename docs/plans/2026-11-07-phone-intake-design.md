# Phone Intake Design Implementation Plan

**Goal:** 신규 전화 접수의 입력 구역과 등록 동작을 명확하게 하고 기존 초안·중복 확인·주소/품목/예약·저장 계약을 보존한다.

**Architecture:** `screens/request.js`의 신규(isNew) 표시 분기와 `.intake-form` 전용 CSS만 변경한다. 기존 상세·수정 화면과 공통 위젯/서버/DB는 그대로 둔다. 기존 F.*·picker·mapBox 등 실제 DOM 노드와 `.reqform`의 input/change 버블링을 유지한다.

**Tech Stack:** 기존 ES modules, CSS, Pretendard GOV, Playwright, PyInstaller. 추가 의존성 없음.

## 설계

기존 화이트·딥네이비 방향을 이어간다. 01 신청자, 02 버릴 물건, 03 일정·우선순위, 04 배출 위치로 구역을 구분하고 라벨·입력 간격을 정리한다. 주소 찾기는 배출 주소를 채우는 검색임을 안내하고 직접 입력도 가능하다고 표시한다. 미설정 시 이유를 계속 노출하며 자세한 설정 안내만 펼쳐 읽도록 한다.

품목 검색에는 보이는 라벨을 추가하고, 확인된 단가 합계와 최종 부과·납부액을 구분한다. 하단 등록 바는 요약을 왼쪽, 단 하나의 등록 버튼을 오른쪽에 둔다. 새 form 요소·자동 저장·새 validation·복제된 등록 버튼은 추가하지 않는다. 일반 Enter, Ctrl+Enter, 가장 빠른 가능일, 예정일 없는 등록 대화상자, 중복 확인, 금액 미확정·품목 잠금을 변경하지 않는다.

## 소유와 검증

1. UI 담당: `request.js` 신규 마크업/접근성 속성/전용 클래스. 기존 수정 분기는 보존하며 공통 위젯 함수는 수정하지 않는다.
2. root: `.intake-form` 범위 CSS, 새 기본 접힘에 맞는 e2e의 주소 설정 안내 열기, 버전/빌드/문서/통합 검증.
3. 기존 `test_pc_design.py`로 밝게/어둡게·15/17px·1440/1280/980/390px의 주 동작·키보드·달력 텍스트·가로 넘침·초안을 확인한다. 기존 `e2e_pc.py`로 실제 등록·중복·주소 선택·409 복구와 후속 업무를 확인한다.
4. 새 전체 EXE에서 대표 브라우저 화면과 실제 실행·정상 종료를 임시 자료로 확인한다. 사용자 DB·계정·8765 서버는 건드리지 않고 원격 배포/커밋하지 않는다. 기존 배포본과 AGENTS.md는 보존한다.

변경 전 실제 배포 화면: `D:/Temp/dg-today-frozen-n9bk4fsd/shots/today-frozen-newrequest-1440x900-light-font15.png`. [Fluent Input 안내](https://fluent2.microsoft.design/components/web/react/core/input/usage)의 지속적인 라벨·입력 폭 원칙을 참고한다.

## 검증 결과·배포

- 표시 마크업은 신규 분기만 수정했다. 기존 수정 actionbar·데이터·중복·저장·초안·주소/품목/달력 이벤트는 보존했다. 신규 상세 위치/요청 메모는 넓은 화면에서 두 칸으로 묶고 좁은 화면에서는 한 열로 표시한다.
- 소스 디자인 48화면 검증 통과 후 독립 시각 QA에서 입력 경계선 대비를 보완했다. 최종 0.13.9 EXE에서 같은 48화면 전체(1440/1280/980/390px, 밝게/어둡게, 15/17px)를 재검증하여 레이아웃 문제 0·브라우저 오류 0, 접수 검색·선택·초안 보존 확인. 경계 대비는 실제 계산 스타일 기준 밝게 3.15:1, 어둡게 4.68:1이다. 증거 `D:/Temp/dg-intake-final-frozen-urb5zuaj`.
- 실제 `e2e_pc.py` 전체 114개 통과(exit 0). 신규 접수·중복·더블클릭·409 복구·지도/주소 선택·계정·마감·수납·환불 등 확인. 증거 `D:/Temp/dg-intake-e2e-complete-0b95776e56f843e9af1f4d45fde497a3`.
- 기존 고정 지도 클릭 좌표는 새 지도 하단 저작권 링크와 겹쳐 실패했다. 새 canvas 크기의 비율로 안쪽 두 지점을 선택하도록 fixture만 수정했고 실제 좌표 입력·저장 검사는 유지했다. 이전 오늘 현황의 숫자/단위 노드 분리는 innerText 줄바꿈과 충돌하여 `.v.text_content() == '1건'`으로 더 정확히 검사했다. 두 초기 실패 뒤 전체 E2E를 다시 실행해 통과했다.
- 최종 EXE의 콘솔 없음·Python 없는 PATH·공백 경로·공개 자산·SQLCipher·ORTools·정상 종료 확인. `test_reports_labels_bundle.frozen_server`에 request.js 자산 일치도 추가했다. 브라우저 재현 래퍼는 `D:/Temp/dg-intake-frozen-review.py`이며 기존 디자인 검증을 실제 EXE 서버로 실행한다. 외부 지도 양식만 정적 fixture이므로 실제 지도 제공 서비스 운영을 검증한 것은 아니다.
- 버전/생성물/JS 구문/Python 컴파일/diff 검사 통과. ZIP `dist/desktop/v0.13.9/Donggu-PC-windows-x64.zip`, SHA256 `1d6282481115202b4ed0a9bc3c028560613195103b3d13cf46ccaa2e0a4d0612`. 미리보기 `dist/desktop/v0.13.9/phone-intake-preview.png`.
- 기존 `동구 PC 실행.lnk`를 검증한 0.13.9로 연결했다. 실제 사용자 서버·DB·계정은 열거나 재시작하지 않았다. 이전 배포본, 부모 AGENTS.md와 다른 작업을 보존했고 커밋·원격 배포는 수행하지 않았다.
