# PC Tray Minimize Implementation Plan

**Goal:** PC 실행창을 트레이 아이콘으로 숨기고 복원한다. 사용자는 작업표시줄 최소화 대신 트레이 방식을 선택했다.

**Architecture:** Tk 창 표시와 Windows 트레이를 별도 `desktop_tray` 모듈에서 관리한다. 네이티브 트레이의 이벤트는 큐를 거쳐 Tk 메인 스레드에서 처리한다. 서버 소유권·시작·정상 종료는 기존 `desktop.py` 콜백을 사용한다.

**Tech Stack:** Python Tk/ttk, ctypes, Windows Shell_NotifyIconW. 추가 의존성 없음.

## 동작

- 하단 ‘최소화’ 버튼과 제목 표시줄의 최소화는 트레이 등록에 성공한 뒤 창을 숨긴다.
- 트레이 아이콘 클릭 또는 ‘실행창 열기’ 메뉴는 원래 창과 입력 내용을 복원한다.
- ‘실행 앱 종료’ 메뉴는 기존 창 닫기 경로를 호출한다. 소유 서버만 정상 종료한다.
- 비밀번호 등 모달 입력 중에는 입력 창을 숨기지 않는다. Explorer 재시작 시 아이콘을 재등록하고, 실패하면 창을 복원한다.
- 창을 다시 열거나 종료하면 트레이 아이콘과 네이티브 핸들을 정리한다.

## 구현 순서

1. `tests/test_desktop_tray.py`에 숨김·복원·종료·오류·모달·정리 회귀를 먼저 추가하고 미구현 실패를 확인한다.
2. `pc/desktop_tray.py`에 표시 제어와 Windows 알림 영역 어댑터를 구현한다. Windows API 서명과 포인터 크기를 명시한다.
3. `pc/desktop.py`, `pc/desktop_view.py`에 기존 종료 경로와 최소화 버튼을 연결한다. `tools/preview_desktop.py`와 실제 Tk fixture에도 콜백을 연결한다.
4. 트레이 회귀, 기존 desktop UI·수명·소유권·경로 검사, 버전·diff 검사를 실행한다. UI 경계는 100/150/200%와 최소 560×400에서 검증한다.
5. 기존 배포 폴더를 보존하며 `dist/desktop/v0.14.1-tray/`에 EXE를 빌드한다. 임시 자료로 실제 트레이·복원·서버 유지·정상 종료를 검증한 뒤 프로젝트 실행 바로가기를 새 EXE에 연결한다.

## 참고

- https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-shell_notifyiconw
- https://learn.microsoft.com/en-us/windows/win32/api/shellapi/ns-shellapi-notifyicondataw
- https://learn.microsoft.com/en-us/windows/win32/shell/notification-area

## 검증 결과

- 트레이 회귀 12개 통과. 실제 Windows 알림 영역 등록·재생성·제거, Win32 `SW_RESTORE` 복원, 창 입력 보존, 모달 입력 중 최소화 방지, 등록 실패와 종료 시 정리를 확인했다.
- 최소화 뒤 비밀번호 입력 요청 시 창을 복원한다. 직전 트레이 종료 요청이 입력 창의 중첩 이벤트 루프에서 처리되는 경합은 수정 전 실패, 수정 후 통과했다. 종료 요청은 열린 `simpledialog.Dialog`를 취소하고 기존 정상 종료 큐를 이어간다.
- 기존 Tk UI 48개 통과. 100/150/200% 배율, 최소 560×400, 복구·초점·기존 서버 재사용과 종료 동작을 확인했다. 200% 오류 화면의 정보 영역은 기존 최소 40px를 유지한다.
- 자산 경로 14개, 실행 앱·서버 소유권 56개, 실제 진행 작업 완료 대기·정상 종료 수명 검사 통과. Python 구문·버전 일치·`git diff --check` 통과. 새 트레이 검사는 npm과 CI에 연결했으며, Explorer가 없는 CI에서는 실제 알림 영역 검사만 건너뛴다.
- 최종 EXE에서 Python 없는 PATH·공백 경로·정적 자산·SQLCipher·실제 노선 계산·정상 종료를 확인했다. 별도 임시 GUI 실행에서도 API와 진입 자산을 확인했으며 시험용 부모와 자식만 정리했다.
- Computer Use로 실제 EXE의 화면 캡처를 두 번 시도했으나 `window capture timed out: timed out waiting on channel`로 실패했다. 실제 EXE의 트레이 버튼·메뉴 클릭과 렌더링 픽셀 검증은 완료하지 못했다. 위 Tk/네이티브 API 시험 및 EXE 서버 시험과 구분한다.
- 로컬 환경은 Python 3.14.3·Tk 8.6·PyInstaller 6.22.3이다. Python 3.12 CI와 전체 업무/E2E 검사는 이번 작업에서 실행하지 않았다. 별도 코드 검토에서 지적된 종료 경합을 수정한 뒤 남은 Critical/Important 항목은 없었다.
- 배포본: `dist/desktop/v0.14.1-tray/Donggu-PC/Donggu-PC.exe`와 `Donggu-PC-windows-x64.zip`. ZIP SHA256: `c565e678aef69bc4c747d80b7f33dc28d639229b0fdb6a59feb74c02bcab7b92`.
- `E:\Projects\waste\동구 PC 실행.lnk`를 새 EXE로 연결하고 기존 실행 인자를 보존했다. 이전 배포본·업무 DB·부모 `AGENTS.md`는 보존했다. 커밋·원격 배포는 하지 않았다.
