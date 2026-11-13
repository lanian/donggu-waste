# Collection Label Usage Implementation Plan

> Apply the writing-plans and test-driven-development workflows in this checkout. Preserve the existing uncommitted work; do not commit or push automatically.

**Goal:** 실제 수거 1개당 배출표 1장을 접수별·전체 사용 장수로 자동 집계한다.

**Architecture:** 별도 사용 기록을 추가하지 않고, PC에 반영된 현장 이벤트에서 마지막 확정 결과까지의 누적 수거 수량을 읽는다. 재전송·재방문·정정에도 접수별 최종 누적값을 한 번만 합산한다. 숫자 배출표 원장, 수납, 휴대폰 전송 형식과 DB 스키마는 유지한다.

**Tech Stack:** Python/SQLCipher, 기존 HTTP 라우터, ES modules, Chromium/Playwright.

## Agreed behavior

- 3개 수거 → 3장, 3개 중 1개 수거 → 1장, 미수거 → 0장.
- `APPLIED` 상태의 `ITEM_RESULT`와 현장 추가 품목을 읽되, `ATTEMPT`의 확정 결과가 있어야 반영한다. 입력 중인 수량은 다음 확정까지 집계하지 않는다.
- 기존 사용분은 재배정·취소·개인정보 파기로 사라지지 않는다. 확정된 수량 정정은 다음 조회에서 반영한다. 과거에 제거된 품목도 수거 이력이 있으면 포함한다.
- 전체 기간의 접수별 누적값이다. 마지막 확정 일시는 참고용이며 일별 증감 사용량으로 표시하지 않는다.
- 번호 원장의 사용 장수와 더하지 않으며, 인쇄·접수 등록만으로 사용 장수가 생기지 않는다.
- 관리 화면은 사용 합계, 수거 접수 수, 접수번호·행정동 필터, 품목별 장수, 접수 링크와 페이지 이동을 제공한다. 이름·전화·주소는 집계 응답에 담지 않는다.

## Task 1: Regression first

Create `tests/test_label_usage.py` with temporary SQLCipher fixtures. Assert missing functionality first, then cover quantity, partial/zero, finalized snapshots, duplicate imports, conflicts, corrections, revisit, removed/field items, purge/cancellation, privacy, filters and paging. Run `.venv/Scripts/python.exe tests/test_label_usage.py` and confirm failure before implementation.

## Task 2: Read-only aggregation and API

Extend `pc/dgpc/numbered_labels.py` with `usage_page()` and its event reducer. Batch-read only necessary fields, in import/sequence order. Add `GET label-usage` with existing office view roles in `pc/dgpc/server.py`; extend route/permission tests and regenerate `docs/routes.md`.

## Task 3: PC screen

Add `pc/ui/screens/label_usage.js` using existing UI components and theme styles; embed it above the number ledger in `numbered_labels.js`. Preserve independent filters, drafts, errors, retries and stale-response guards. Add real-server browser regression plus delayed-response UI cases; retain the existing serial-ledger E2E.

## Task 4: Verify and package

Run usage logic/API/browser checks, existing reports/labels suites, PC business tests, version/generated checks, syntax and diff checks. Obtain independent code review and address findings. Update manuals and test scripts, bump the synchronized version to 0.14.6, build into a new release directory, test the packaged EXE using fictitious temporary state, and update the existing shortcut while preserving its arguments. Do not shut down the user's running app.

## Review and validation

- RED: missing aggregation function, missing HTTP route and missing screen section were observed before implementation.
- Review reproduced a late lower-sequence item arriving after its final attempt (0 instead of 3 sheets), and an older final arriving after a newer final (1 instead of 3). Both signed-HTTP regressions now pass. The reducer replays each phone/generation in sequence without replaying old events over newer snapshots; different phones/generations retain their import ordering.
- Malformed non-string `ATTEMPT.outcome` previously raised `TypeError`; its regression passes after validation.
- GREEN: usage logic 13, signed HTTP/API 5, actual SQLCipher/HTTP/Chromium UI 2. Coverage includes quantity/partial/zero, finalization, duplicate files/events, revisit/correction, late events, generation/device boundaries, field items, removed items, purge/cancel, privacy, paging, filters, errors and stale responses.
- Existing checks: PC logic 688, serial ledger 14, migrations 3, reports 12, report/ledger API 4, browser UI 30, presentation 9, and the real-server report/ledger flow (including encrypted solo-backup usage aggregation) pass.
- Fee vectors 43 and Python/WebCrypto interoperability 130 pass. On Windows, `npm test` needs the local venv site-packages in `PYTHONPATH` because `python3` resolves to the base interpreter. Version/generated checks, Python/JavaScript syntax and `git diff --check` pass.
- Local runtime: Python 3.14.3, Node 24.20.0, Chromium 1248. CI's Python 3.12/Node 22 run and production/native-tray interaction are not claimed.
- Independent read-only follow-up review found no remaining Critical/Important issues. Existing `E:/Projects/waste/AGENTS.md` was present and remains unchanged.

## Release verification

- Built `dist/desktop/v0.14.6/Donggu-PC/Donggu-PC.exe` and `Donggu-PC-windows-x64.zip`. ZIP SHA256: `5bd7a80da12ae12714a36921f826c1187f3b95e134b027896f5ae67eb58120a9`.
- `tests/test_reports_labels_bundle.py --exe dist/desktop/v0.14.6/Donggu-PC/Donggu-PC.exe` passes: the actual EXE imports an encrypted solo backup (3 collected + 1 collected = 4 sheets), duplicate reimport stays 4, and the independent serial-ledger flow still passes. Runs with temporary fictitious encrypted state, Python removed from PATH, browser asset comparisons and normal server shutdown.
- `tests/test_desktop_bundle.py --exe dist/desktop/v0.14.6/Donggu-PC/Donggu-PC.exe --legacy-dongs --theme-ui` passes: migration 8→9, default dongs/settings preservation, dong reorder/restart, fixture-only VWorld, login appearance, bundled assets, actual routing and normal child-server exit.
- Updated the existing `E:/Projects/waste/동구 PC 실행.lnk` to 0.14.6 and verified its arguments were preserved. Did not stop or modify the user's running app or production data. The user must normally exit the previous program and launch the shortcut for the new server/UI to apply. No commit, push or deployment was performed.
