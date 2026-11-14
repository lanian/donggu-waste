# Mobile Field Workflow Gaps Implementation Plan

**Goal:** Add evidenced missing mobile field actions following the user's request to check absent mobile functions and implement them, while continuing the personal-phone worker change.

**Architecture:** Preserve offline encrypted state, existing event schemas/IDs/quantity/date semantics and PC handoff. Add local read-only search/photo inspection and repeat-safe persistence controls around existing field actions. No new provider, backend, dependency or release.

## Confirmed Findings and Chosen Changes

1. `stoplist.js` excludes phone from shared search text used by PC-mode home, SOLO home and `find.js`. Add full-number, formatted-number and last-digit matching, including multiword AND/initial-consonant behavior. Exclude phone on privacy-purged records. Update hints; retain all date/status/search-return rules and keep queries on-device.
2. `stop.js` only displays cropped 88px photo thumbnails. Add an in-app large whole-photo view with phase/item/time and close/back handling, using existing encrypted bytes. No public URL/new window/share. Catch failed/late reads; close/revoke on navigation or lock and restore focus only to the current originating control.
3. `add.js` ignores `addEvent()`'s `false` save result, reports success and clears the basket. Add an explicit failed/pending/saved operation and retry UI. Reuse prepared row/event identities; retries persist existing state instead of appending again. Preserve merges, quantities, drafts, current-state/route ownership and lock/write settlement.
4. `stop.js` field memo and `unload.js` also ignore failed writes before clearing/navigating. Give these explicit failure/retry and double-click protection, retaining the existing event identity. Preserve typed input, other drafts and late-response navigation boundaries. Keep unload draft through its map-location selection.
5. `unload.js` checks only nonempty strings: negative numbers and both zero are recorded. Require finite nonnegative numbers and at least one positive volume/weight; retain invalid inputs/error and append no event. Preserve the current event work-date contract.

## Ownership and Execution

- Personal-worker owner finishes/freeze `core.js`, `settings.js`, `stoplist.js`, `test_mobile_personal_worker.py` from its existing plan. Root takes `stoplist.js` ownership only after that freeze for phone search and owns `find.js`, new `test_mobile_phone_search.py`.
- Photo/memo owner: `web/screens/stop.js`, `web/style.css`, new `tests/test_mobile_photo_viewer.py`, new `tests/test_mobile_memo_save.py`. Preserve the existing thumbnail error/lock guards and previous root overscroll fix. No core/store/widgets writes.
- Add/unload owner: `web/screens/add.js`, `web/screens/unload.js`, new `tests/test_mobile_add_save.py`, new `tests/test_mobile_unload_save.py`. Use existing commit/addEvent/addCrewEvent/stepFlush contracts; no core/store/widgets writes.
- Root owns npm/CI registration, existing tests, manual, plans, integration and local commits. No agent commits, pushes, builds or deploys. Work from `mobile-personal-worker-20261114`; preserve production state/processes, root AGENTS.md and existing releases.
- Each owner reproduces the problem with a real PIN/IndexedDB Chromium regression before implementation, verifies GREEN, performs syntax/diff checks and freezes named files. Independent read-only review follows; resolve concrete blockers before final integration.

## Verification

Use fake names/phone numbers/requests and temporary browser/PC state. `DG_TEST_DIR/shots` is the artifact convention; evidence root `D:/Temp/dg-mobile-gaps-20261114`.

- New scripts: personal worker, phone search, photo viewer, field memo save, field add save and unload save.
- Relevant existing checks: mobile home/layout/auto-lock, date visibility, photo preview, intake save, result confirmation, cleanup, full mobile `e2e.py` and `npm test` interoperability.
- Negative paths: encrypted write false/retry, duplicate click, partially prepared multi-row operation, merge choice, navigate/reenter/lock, out-of-order reads, missing photo and invalid unload numbers. Assert real encrypted state/event IDs/quantities, not only UI messages.
- Distinguish local synthetic browser/protocol results from actual phones, physical camera, native EXE and deployment. No version bump or deployment in this task.
