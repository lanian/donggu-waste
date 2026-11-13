# Popup Workflow Implementation Plan

> Apply brainstorming, writing-plans, frontend-design, test-driven-development and verification-before-completion in the existing checkout. Preserve existing uncommitted work, AGENTS.md, production state and running apps. No automatic commit, push or publication.

**Goal:** 동별 접수와 수거 입력의 중첩 팝업을 없애고 휴대폰 PC 입력 도움을 큰 화면으로 제공한다.

**Approved design:** The preceding review recommended a dedicated dong detail view, collection input within that view, and a full-page mobile reference. The user's follow-up requests that improvement. Short selections, consequential confirmations, reauthentication, print and QR windows remain appropriate.

**Architecture:** Reuse the existing dong screen and scoped endpoints for `#/dongs/new` and `#/dongs/<id>`. Retain module-owned filters and in-memory drafts, and return to the originating list position. Replace the shared PC collection modal with a labelled expandable region, retaining input on collapse and the identical submitted payload/idempotency key after an uncertain response. Add a mobile reference route within the existing stop module and preserve the finder return path. No backend, protocol or storage changes.

**Tech Stack:** Existing browser ES modules, Python temporary-state HTTP test server, Playwright and existing administrative/mobile CSS. Reuse theme variables, fonts and typography; no dependencies.

## Tasks

1. Update `tests/test_pc_collection_ui.py` and `tests/test_collection_help_mobile.py` to require inline/full-page workflows. Add collapse/reopen, preserved parent draft, uncertain-response retry and return-context assertions. Run each test and confirm the expected failure before implementation.
2. Change `pc/ui/screens/collection_entry.js` to render a named region within its parent. Keep fresh-version/item checks, date/quantity validation, immutable uncertain requests and automatic label assignment. Style the region in `pc/ui/pc.css`.
3. Change `pc/ui/screens/dongs.js` and the `pc/ui/pc.js` argument forwarding to render dedicated detail routes. Preserve current authorization, duplicates, conflict recovery, draft clearing and async freshness. Adapt dong regression locators/router fixtures to the new navigation and test return filters/focus.
4. Change `web/screens/stop.js` and `web/app.js` to route PC input help as a full page, preserve return navigation, and keep copy fallback and read-only event behavior. Add responsive styles in `web/style.css`.
5. Run collection/dong/date/mobile regressions, source syntax, whitespace and version checks. Obtain an independent read-only code review and resolve important findings. Update user documentation and synchronized release versions, build the PC package and verify the same flows in its frozen server using fictitious temporary state. Update the launcher shortcut only after that verification.

## Verification

- `.venv/Scripts/python.exe tests/test_pc_collection_ui.py`
- `.venv/Scripts/python.exe tests/test_collection_help_mobile.py`
- `.venv/Scripts/python.exe tests/test_dong_work_ui.py`
- `.venv/Scripts/python.exe tests/test_dong_work_e2e.py`
- `.venv/Scripts/python.exe tests/test_date_visibility_ui.py`
- `.venv/Scripts/python.exe tests/test_date_visibility_mobile.py`
- Related PC collection API/protocol and mobile cleanup/workflow checks; no production database access.
- Store logs and fictitious screenshots under `D:/Temp/dg-popup-workflow-20261113` and report source, packaged and published evidence separately.

## Completed validation (2026-11-13)

- RED: previous code still opened collection/help dialogs. New inline-region and mobile-route expectations failed. Additional RED cases reproduced wrong Ctrl+Enter handling, released wake policy, detached conflict retry, and an older save response deleting a newly edited draft (blur the input before releasing the held response).
- GREEN: dong UI 61 assertions, actual-HTTP scoped dong workflow, source and frozen-EXE collection flows (collapsed inputs, parent draft/filter/focus preservation, quantity/reason validation, refresh-only retry, label allocation and uncertain-response idempotency), source/frozen date filters, encrypted-mobile help/copy/finder return/wake policy, mobile date finder and cleanup 14 tests. Collection logic 11 tests, collection HTTP 6 tests and PC/mobile collection protocol passed.
- Independent read-only review found and rechecked shortcut scope, role fallback arguments, visible refresh/conflict retries, wake policy and submitted-draft identity protection. No Important finding remained after recheck. No full-suite or production-data claim is made.
- Version consistency/precache, JavaScript syntax and whitespace checks passed. Inspected fictitious PC and mobile screenshots, including responsive dong form checks at 1440/980/390px and light/dark themes.
- Built `dist/desktop/v0.14.11/Donggu-PC/Donggu-PC.exe`. Frozen tests also passed legacy DB migration, default dongs, dong ordering/restart, theme/login, fictitious VWorld provider, routing and normal server shutdown without Python on PATH. This verifies the packaged server/UI; no tray/manual launcher interaction was performed.
- ZIP: 66,690,416 bytes; SHA256 `52efacbab729934f4e13616304e3aeb849d67f185bc504d70f0062501d41208f`. Updated the existing workspace launcher shortcut to 0.14.11, preserving arguments/window style and keeping a backup under the evidence folder.
- Existing `E:/Projects/waste/AGENTS.md` was preserved. Production app/state were not stopped or modified. Mobile hosting was not published; its 0.14.11 source must be deployed for phone users to receive the new help page.
