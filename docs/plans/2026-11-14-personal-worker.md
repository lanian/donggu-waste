# Personal Phone Worker Implementation Plan

**Goal:** Replace the shared-phone worker roster/switcher with one personal worker name, following the user's explicit clarification that each worker uses a personal phone.

**Architecture:** Keep `state.worker` and the existing event `body.worker` contract. A shared reader prefers the configured name and falls back to the first valid legacy roster name, including immediately after backup restore. Saving one name writes the legacy-compatible singleton `workers` array; clearing writes both empty. Preserve historical event names, PC crew/device bindings and PC historical multi-worker summaries.

**Tech stack:** Existing browser ES modules, encrypted IndexedDB, standalone Python/Playwright tests; no new dependencies.

## Design and Boundaries

- Chosen: one name field in Settings → 작업자, automatically attributed to subsequent events. Keep existing field styling and touch sizes. Home shows a compact current-name label and Settings link rather than worker-switch buttons.
- Alternatives considered: remove worker attribution entirely (loses useful field history), or derive it from the PC crew (crew and individual are different). Neither fits the user's personal-phone workflow.
- Preserve the selected legacy name even if it is not the first roster entry. Fall back only if it is absent/blank; handle restored state without requiring `loadState()` to run again. No destructive bulk state migration.
- Worker saving reports the actual encrypted save result. Preserve the typed name and error on failure and retry through the existing commit queue/pending-encrypted-state mechanism. Do not roll back only part of that mechanism or claim success on `commit() === false`.
- Save completion updates only its own connected controls; it must not erase another settings draft or a reentered screen. No automatic changes to historical `events[].body.worker`.
- Work locally from clean commit `8243fbc` on `mobile-personal-worker-20261114`. Do not publish, bump versions, build/replace an EXE or change production state/processes. Preserve root AGENTS.md.

## Tasks and Ownership

1. Implementation owner: `web/core.js`, `web/screens/settings.js`, `web/screens/stoplist.js`, new `tests/test_mobile_personal_worker.py` only. Add a real PIN/IndexedDB/browser RED regression first. Cover legacy selection/fallback, one field/no switcher, normal and crew events, name change/history preservation, explicit clear, reload, failed/retried save, delayed response/navigation and backup restore.
2. Implement the narrow shared current-name reader, single-field worker controls and read-only home display. Keep event schema, encryption/storage APIs, PIN and backend unchanged. Verify the new regression GREEN, syntax and owned diff; freeze owned files.
3. Root: adapt worker selectors/expectations in `tests/e2e.py`, `tests/test_mobile_home_ui.py`, `tests/test_mobile_auto_lock.py`; register the new browser test in package/CI; update `docs/운영매뉴얼.md`. Do not weaken attribution assertions or remove existing draft/lock checks.
4. Root: run new and relevant existing mobile browser/cleanup/interoperability checks, inspect representative 360px/large-text screens and request independent read-only review. Fix concrete findings, record evidence and commit the verified local change.

## Verification

- New: `python tests/test_mobile_personal_worker.py` using fake names/requests and temporary browser data.
- Existing: `npm run test:mobile-ui`, `python tests/test_mobile_auto_lock.py`, `python tests/test_mobile_cleanup.py`, `python tests/e2e.py`, `npm test`.
- Cheap checks: `node --check` edited JS, Python compilation, `tools/check_version.py`, `tools/check_generated.py`, `git diff --check`.
- Evidence root: `D:/Temp/dg-personal-worker-20261114`. Use `DG_TEST_DIR/shots` for browser screenshots, never commit state/keys or resident data.
- Distinguish synthetic Chromium/PIN/IndexedDB and protocol checks from actual phone, packaged EXE and production deployment evidence.

## Completed

The single name field, read-only current-name display, legacy selected-name/backup fallback, explicit clear and future-event attribution are implemented. Name saving checks the actual encrypted write result and updates only its connected controls, preserving unrelated or reentered settings drafts. Historical event names and the existing PC/device/crew protocol remain unchanged.

Root independently ran `npm run test:worker` against the final source (PASS, `D:/Temp/dg-mobile-gaps-20261114/worker-final.log`), plus the mobile home/layout/automatic-lock, backup/cleanup and full 186-check mobile flow. The final core also forwards the existing `addCrewEvent` persistence result for the separately planned unload recovery; event content is unchanged.

Representative 360/430px very-large-text and settings/home screenshots are under `D:/Temp/dg-personal-worker-20261114/green/shots`. Independent read-only source review found no remaining Critical/Important issues. Physical phones and release deployment remain outside this local implementation. The user's follow-up mobile-function audit and additions are recorded in [the workflow gaps plan](2026-11-14-mobile-workflow-gaps.md).
