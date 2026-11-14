# Mobile list readability

User priority: make the mobile list easy to scan at a glance, in both PC-connected and SOLO modes.

## Design

- Show receipt number, resident, address, actual derived items, remaining quantities grouped by unit, and one clear result state on each card. Keep handling warnings, contact information, dates, privacy rules, and route order.
- A quantity entry alone must not look like a saved collection result. Use one presentation-only completion rule in cards, summary, home filters, and date search.
- Provide a list jump and a visible result count without collapsing operational alerts or existing tools.
- Preserve list position and focus after detail/related-screen navigation. Keep return data in memory and invalidate it across unrelated navigation, lock, state/mode replacement, and received-list changes.
- Normalize SOLO dong conditions before filtering; allow reordering only in an unfiltered date list.

## Implementation and verification

1. Add failing real-PIN/IndexedDB browser regressions for cards, filters, and return navigation using fictitious data.
2. Improve shared cards/search/CSS; apply consistent completion/filtering to home, SOLO, and finder. Implement guarded return navigation in the router.
3. Check 360/390/430px, large text, light/dark/contrast, missing receipt numbers, mixed units, purged/cancelled/removed records, and field-added items.
4. Run new suites and relevant existing home/layout/search/date/result-confirmation/mobile E2E suites; independently review changed source and test evidence.
5. Record verified local changes. No version bump, EXE build, deployment, production state access, or AGENTS.md modification is included.

## Verification record

- Final `npm run test:mobile-list`: all three suites passed after the last source changes. Covers actual PIN/encrypted IndexedDB, fictitious records, PC/SOLO, receipt/derived items/mixed units, saved result classification, immediate search after date-tab change, filters/reorder, and guarded scroll/focus return.
- Card layout: 24 Chromium combinations (360/390/430px, normal/XL text, light/dark); screenshots inspected. The jump heading remains below the sticky header.
- Existing `tests/e2e.py`: 186 checks passed. This run started before the final legacy-completed quantity-row, search ownership, and completed-CALLBACK filter adjustments; the final three new suites verify those adjustments.
- Existing mobile layout, automatic lock, result confirmation, field layout, photo viewer, memo/add/unload/intake save, photo preview, and workflow suites: all 11 passed.
- `npm test`, version consistency, generated-file consistency, changed JavaScript syntax, and `git diff --check`: passed. Version stays 0.14.17.
- Independent read-only review: GO after rechecking completed-CALLBACK filtering and sticky-header jump visibility. No important remaining issue found.
- Evidence: `D:/Temp/dg-mobile-list-20261114/` (`list-final2/`, `regressions/`, `mobile-e2e/`, `e2e.log`). Automated Chromium with fictitious data; physical Android/iOS devices and public deployment were not tested.
