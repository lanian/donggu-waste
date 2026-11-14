# Mobile Field Workflow Gaps Implementation Plan

**Goal:** Add evidenced missing mobile field actions following the user's request to check absent mobile functions and implement them, while continuing the personal-phone worker change.

**Architecture:** Preserve offline encrypted state, existing event schemas/IDs/quantity/date semantics and PC handoff. Add local read-only search/photo inspection and repeat-safe persistence controls around existing field actions. No new provider, backend, dependency or release.

Prepared add/unload events are appended synchronously before awaiting persistence. The existing final `core.saving` snapshot therefore protects the whole operation during lock settlement. Their long-running operation promise must not be registered in `stepFlush`: the router also awaits that hook before mounting a different screen, so such registration blocks navigation while an acknowledgement is delayed. Use view/route ownership to isolate completion, keeping ordinary navigation available.

## Confirmed Findings and Chosen Changes

1. `stoplist.js` excludes phone from shared search text used by PC-mode home, SOLO home and `find.js`. Add full-number, formatted-number and last-digit matching, including multiword AND/initial-consonant behavior. Exclude phone on privacy-purged records. Update hints; retain all date/status/search-return rules and keep queries on-device.
2. `stop.js` only displays cropped 88px photo thumbnails. Add an in-app large whole-photo view with phase/item/time and close/back handling, using existing encrypted bytes. No public URL/new window/share. Catch failed/late reads; close/revoke on navigation or lock and restore focus only to the current originating control.
3. `add.js` ignores `addEvent()`'s `false` save result, reports success and clears the basket. Add an explicit failed/pending/saved operation and retry UI. Reuse prepared row/event identities; retries persist existing state instead of appending again. Preserve merges, quantities, drafts, current-state/route ownership and lock/write settlement.
4. `stop.js` field memo and `unload.js` also ignore failed writes before clearing/navigating. Give these explicit failure/retry and double-click protection, retaining the existing event identity. Preserve typed input, other drafts and late-response navigation boundaries. Keep unload draft through its map-location selection.
5. `unload.js` checks only nonempty strings: negative numbers and both zero are recorded. Require finite nonnegative numbers and at least one positive volume/weight; retain invalid inputs/error and append no event. Preserve the current event work-date contract.

## Ownership and Execution

- Personal-worker owner finishes/freeze `core.js`, `settings.js`, `stoplist.js`, `test_mobile_personal_worker.py` from its existing plan. Root takes `stoplist.js` ownership only after that freeze for phone search and owns `find.js`, new `test_mobile_phone_search.py`.
- Photo/memo owner: `web/screens/stop.js`, `web/style.css`, new `tests/test_mobile_photo_viewer.py`, new `tests/test_mobile_memo_save.py`. Preserve the existing thumbnail error/lock guards and previous root overscroll fix. No core/store/widgets writes.
- Add owner: `web/screens/add.js`, new `tests/test_mobile_add_save.py`. Unload was initially implemented by the add owner, then explicitly frozen/transferred to the completed personal-worker owner for `web/screens/unload.js` and new `tests/test_mobile_unload_save.py` final regression/implementation ownership. The shared test helpers `writes`, `release`, `disk_state` retain their interface. Use existing commit/addEvent/addCrewEvent/stepFlush contracts; no agent core/store/widgets writes.
- Root owns npm/CI registration, existing tests, manual, plans, integration and local commits. After the personal-worker owner freezes core, root forwards `addCrewEvent`'s existing `commit()` result to its caller (matching `addEvent`), so unload can check actual persistence without depending on a mutable global promise. Event content and existing awaited calls remain compatible. No agent commits, pushes, builds or deploys. Work from `mobile-personal-worker-20261114`; preserve production state/processes, root AGENTS.md and existing releases.
- Each owner reproduces the problem with a real PIN/IndexedDB Chromium regression before implementation, verifies GREEN, performs syntax/diff checks and freezes named files. Independent read-only review follows; resolve concrete blockers before final integration.

## Verification

Use fake names/phone numbers/requests and temporary browser/PC state. `DG_TEST_DIR/shots` is the artifact convention; evidence root `D:/Temp/dg-mobile-gaps-20261114`.

- New scripts: personal worker, phone search, photo viewer, field memo save, field add save and unload save.
- Relevant existing checks: mobile home/layout/auto-lock, date visibility, photo preview, intake save, result confirmation, cleanup, full mobile `e2e.py` and `npm test` interoperability.
- Negative paths: encrypted write false/retry, duplicate click, partially prepared multi-row operation, merge choice, navigate/reenter/lock, out-of-order reads, missing photo and invalid unload numbers. Assert real encrypted state/event IDs/quantities, not only UI messages.
- Distinguish local synthetic browser/protocol results from actual phones, physical camera, native EXE and deployment. No version bump or deployment in this task.

## Completed Implementation

- Phone searches accept the stored form, normalized digits, usual hyphen formatting (including 02 numbers), last digits and existing multiword/initial-consonant queries. PC/SOLO home and the all-date finder share the behavior. Purged phone data is excluded, filters survive detail return, and searches do not write business data or send queries outside the device.
- Saved photos open inside the app in whole-image fit mode, with magnified inspection, phase/item/time and retry on failed/missing reads. Closing, back, navigation, state replacement and PIN lock clean up the viewer/Blob URL. Original ciphertext, photo metadata and events stay unchanged.
- Field additions prepare every row before the first persistence wait. Failed saves retain the basket and retry the same event/line identities. Merge choice, 9999 cap, rapid repeated clicks, delayed ACKs, actual route mounts/reentry and lock settlement are protected. No operation promise is placed in `stepFlush`.
- Memos retain failed input and retry the prepared event. New typing/cursor/focus, another request, reentry, changed state and pending-ciphertext recovery after lock preserve their ownership.
- Unload saves retain a state-owned draft through actual map selection/cancel and reuse the prepared event on retry. Validation rejects negative/nonfinite/all-zero and native `badInput` such as `1e`/`-`, while allowing positive weight/volume with blank/zero in the other field. Work date, time, unit, worker, sequence and generation stay unchanged. The history heading is now accurate: `하차 내역`.
- Six new browser scripts are registered in npm/CI. Existing mobile worker selectors and the operator manual were updated. No new application asset or dependency was introduced; versions remain 0.14.16.

## Final Verification Results

All changed application files were frozen before the final full E2E log was created; their modification times were checked against that start time. Root independently verified the final source with Windows, Node 24.20.0, Python 3.14.3 and Playwright Chromium. CI remains Node 22/Python 3.12 and was not remotely triggered.

| Command/check | Result and evidence under `D:/Temp/dg-mobile-gaps-20261114` |
| --- | --- |
| `npm run test:worker` | PASS; `worker-final.log` |
| `npm run test:mobile-gaps` | PASS: phone search, photo viewer, memo save, field-add save and unload save; `gaps-final.log` |
| `python tests/e2e.py` | PASS: 186 checks, synthetic camera/QR → actual temporary-PC import included; `e2e-final.log`, `e2e-final/shots/` |
| `npm run test:mobile-ui` | PASS: PC/SOLO home and 18 size/theme/layout variants; `layout-final.log` |
| `python tests/test_mobile_photo_preview.py` | PASS: 3 previous encrypted preview/error/lock paths; `test_mobile_photo_preview-final.log` |
| `python tests/test_mobile_intake_save.py` | PASS: quantity, draft, real write failure/retry, repeated input, delayed response/navigation/lock; `test_mobile_intake_save-final.log` |
| `python tests/test_mobile_result_confirmation.py` | PASS: confirmation and PC-input-help guards; `test_mobile_result_confirmation-final.log` |
| `python tests/test_mobile_auto_lock.py` | PASS: PIN/auto-lock settings and drafts; `test_mobile_auto_lock-final.log` |
| `python tests/test_date_visibility_mobile.py` | PASS: dates/status/returning search conditions; `date.log` |
| `python tests/test_mobile_cleanup.py` | PASS: 14 checks; `cleanup.log` |
| `npm test` | PASS: fees, Python/WebCrypto interoperability, collection protocol, fee calculator; `npm-test.log` |
| External `probe_short_add.py` | PASS: 360×480 very-large-text real storage failure, unoccluded ≥48px retry control and same-event recovery; `short-add.log`, `short-add-error.png` |
| JS syntax, Python compilation, version/generated files, whitespace | PASS |

Independent read-only review is **GO for the changed source**, with no remaining Critical/Important issues after the delayed-navigation and native-number-input boundaries were fixed. Reviewers did not run tests/builds; the execution evidence above is root verification. RED/GREEN and representative screenshots are retained under `add-unload/`, `photo-memo/`, `unload-final/`, `new-final/`, and the personal-worker evidence directory.

Final critical source SHA256:

- `add.js`: `98efecaba1793c21314b78cec5f74d9f7f21b62ad12c706039bee65768ee3366`
- `unload.js`: `f635bea0ff04e57389333159249063f0cdbb7259bfacc16e30a12dc4fac6adae`
- `stop.js`: `65373d73715c2f36019046c686e325e7ce51753147fe4488d70d2e2385072d06`
- `core.js`: `192cccd51f0c4eea1922ef8577b1b5bfe580e0050fc986bd6f9f1e40ba1d3514`

No full `test:all`, actual phone/physical camera, native EXE build/run, live service or production deployment was performed. Map draft coverage uses the real MapLibre canvas with a blank fixture style, not a production route/provider integration. Production state/processes, packaged releases and the existing root AGENTS.md were preserved. This is a verified local change awaiting a separate release.
