# PC Collection Implementation Plan

> Execute in this existing checkout with test-driven-development and verification-before-completion. Preserve earlier uncommitted work, production state and running applications. Do not automatically commit or push.

**Goal:** 접수 상세와 담당 동 상세에서 실제 수거일·품목별 추가 수거량을 입력하고 기존 배출표·서식 집계에 연결한다. 휴대폰에는 PC 입력을 돕는 간단한 전달 요약을 제공한다.

**Confirmed design:** User approved the previously proposed PC collection entry with actual collection day and quantities, and selected "휴대폰에서 접수번호·품목·수량을 쉽게 보고, 그 내용을 PC에 직접 입력". Provide a readable mobile summary/copy helper. Existing encrypted mobile result transfer remains available.

**Architecture:** Reuse result_import/result_event with reserved internal PC source, explicit audit metadata and the existing collection trace/label allocator. No schema change. Record cumulative ITEM_RESULT plus ATTEMPT atomically, but present additional quantities for this visit. Keep original fee quantities/prices and payment state. Bind saves to request version plus current result/assignment fingerprint; use existing idempotency. A PC save increments request version and stores a PC revision barrier. Optional daylist field `pc_collection_ver` (`pcv` in compact Python/JS protocol) carries that baseline to mobile. Local replay and server imports both require the matching baseline acknowledgment on quantity/result events. Old mobile results remain in the existing conflict queue, and the original event history is retained. Same-day carryOver preserves a newer PC baseline.

**Alternatives:** Updating request.status alone would bypass quantities, labels and original forms. A separate collection table would duplicate existing result semantics and require a migration. Existing event reuse keeps one aggregation path and preserves mobile payload contracts.

**Tech Stack:** Python/SQLCipher, existing ES modules/PWA, Playwright. Preserve the existing administrative visual style and Korean terminology.

## Tasks

1. Add failing `tests/test_pc_collection.py` for actual-day partial/full/uncollected outcomes, cumulative revisits, invalid/excess/fractional input, stale result/assignment/version, cancelled/purged cases, stock shortage, rollback and idempotency. Implement `pc/dgpc/pc_collection.py`, request detail basis, deterministic same-second event ordering and old-mobile PC barrier. Reuse current label/form fixtures and actual encrypted DB.
2. Add failing HTTP coverage for ADMIN/INTAKE and scoped DONG saves, other-dong denial, LEAD/FIELD denial, changed scope/session after slow body and retry idempotency. Wire `server.py` and `dong_work.py` using existing write-lock/session boundaries.
3. Add shared `pc/ui/screens/collection_entry.js` dialog to global request and scoped dong detail. Show previous/this visit/remaining counts, actual day, reason, all remaining/zero helpers, preview and shortage result. Preserve parent drafts, errors/retry/cancel/focus, auth ownership and late responses. Add actual Chromium regression before implementation.
4. Add a concise mobile PC-assistance summary with receipt/item/count/date context and explicit finalized/draft status. A clipboard fallback must work without Clipboard API. Reading/copying does not add events or submit a result. Keep names, addresses and phone numbers out of copied notes. Add a 390px real mobile-store regression.
5. Run related collection/label/form/dong/session and mobile regressions. Request independent read-only review, fix Important findings, update generated route docs, npm scripts/CI, manuals and synchronized version to 0.14.9.
6. Build `dist/desktop/v0.14.9`. Check actual frozen EXE with private fictitious state, new entry/label/form workflows and existing migration/restart/appearance behavior, verify packaged assets/manifest, then update the existing shortcut preserving arguments/window style. Leave running old versions and business records intact.

## Validation and evidence

- Use `.venv/Scripts/python.exe`, temporary DG_STATE/DG_OUT and fictitious residents only. Watch each new regression fail for the missing behavior before implementing it.
- Run targeted unit/HTTP/Chromium checks, `tools/check_generated.py`, `tools/check_version.py`, and `git diff --check`. Keep source, mocked mobile, real encrypted HTTP and frozen EXE evidence distinct.
- Record final commands/results, artifact hash and local environment differences here after verification.

## Completed validation (2026-11-12)

- `test:collection`: all five commands passed using the local venv for Python scripts: 11 real SQLCipher unit tests, 6 actual HTTP tests with signed mobile results, the Node protocol/replay assertions, actual PC Chromium flow and real encrypted mobile-store flow at 390px.
- PC UI covered delayed detail lookup plus concurrent item editing, cancel/focus, invalid/excess quantities, required reasons, partial collection, original memo draft preservation and subsequent save, scoped revisits, 1+2 physical label allocations, and a committed POST with a lost response followed by an idempotent retry.
- Existing PC regression passed 688 assertions. All nine form commands and ten related collection/label/dong/depot regression scripts passed. Existing mobile workflow passed 25 assertions and mobile cleanup passed 14 tests. Generated docs, synchronized versions and whitespace checks passed.
- Independent read-only review found and resolved four Important issues: inconsistent replay order for reverse-arriving mobile files; same-day carryOver replacing a new PC baseline; old-app local events lacking the baseline ACK; and item edits made during the detail GET. Each was covered by a failing regression before its fix. Final review found no remaining Important issue in that scope.
- Frozen `dist/desktop/v0.14.9/Donggu-PC/Donggu-PC.exe` passed `test_pc_collection_ui.py --exe`, `test_desktop_bundle.py --exe --legacy-dongs --theme-ui` and `test_official_forms_e2e.py --exe`. This checked direct PC entry/number allocation/retry, migration and restart, appearance, encrypted state without Python on PATH, existing XLSX/ODT/ZIP output and normal shutdown.
- Packaged collection/request/dong/results/core JavaScript, both protocol/fee assets and both report templates matched current source bytes and manifest entries. ZIP: 66,686,040 bytes, SHA256 `e65b389ae57e616e5ca4123e5ff838de7ba66e81f84a371544125f279764eac0`.
- Existing `E:\Projects\waste\동구 PC 실행.lnk` now targets 0.14.9 with its empty arguments and window style 1 preserved. Existing 0.14.7 processes and prior releases were left intact. The parent `AGENTS.md` was preserved.

Evidence lives in `D:\Temp\dg-pc-collection-20261112` (logs, screenshots, fictitious forms, release and shortcut checks). Python is locally 3.14.3 and Node 24.20.0; CI specifies 3.12/22. `npm test` uses the venv's site-packages via process-local PYTHONPATH because the Windows python3 alias lacks project packages. Remote CI and `npm run test:all` were not run. Mobile hosting was not published; the new mobile source must be deployed and the app updated to expose the help screen. No production business records or live provider integrations were edited or verified.
