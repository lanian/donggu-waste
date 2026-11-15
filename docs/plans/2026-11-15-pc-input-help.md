# PC input help improvement

**Goal:** Make the personal phone's read-only PC input reference easier to scan and compare with PC manual collection entry.

**Architecture:** Keep the existing manual workflow, protocols, storage, PC save payloads and date/quantity ledger logic. Improve helper presentation, clipboard operation ownership, return navigation and PC read-only previews. Baseline: `b98cdde`, version 0.14.20. This task does not deploy or rebuild.

## Audits and design

Three specialists reviewed mobile layout, PC quantity semantics and state/privacy/accessibility. Evidence: `D:/Temp/dg-pc-input-help-20261115/{mobile,pc,audit}/`.

- Four-item helper: copy action is below the viewport (y1288.8 at390 normal,1562.5 at360XL); number/date context disappears on scrolling. Compact item comparisons and a top identity/result/copy region improve manual use.
- Phone additions are measured from its received-list prior baseline; PC adds a new input to the database's current prior. Entering the same phone result twice can add stock/quantity twice if capacity remains. Compare phone baseline/target cumulative against current PC values; this is a manual matching risk, not an idempotent-request defect.
- Revisit phone lists omit completed items, so phone and PC row numbers can differ. Match name/spec/unit rather than assuming equal row indices. Mark FIELD additions requiring PC item/import confirmation.
- Multiple collection dates can currently appear as their combined added quantity under the last attempt's day. Keep that total as reference, explicitly show mixed recording dates and require per-day confirmation instead of suggesting a single actual date.
- Two clipboard requests can resolve out of order and reveal an obsolete fallback; unrelated navigation can reuse old `pcInputBack` focus/scroll data.

Considered: (1) compact comparison reference (selected), (2) one-item wizard (more navigation), (3) copy-centric workflow expansion (more controls). Select the first, with receipt-only copy and existing full-note copy near the top. No automatic transfer or PC auto-fill.

Preserve the existing policy that unconfirmed results may be copied with an explicit warning. Saved-write pending/failure and result reconfirmation continue blocking copying. Unconfirmed/cancelled/removed/multi-day values must look like reference information, not an input-ready completed result. Numeric-only quantity copy is outside this pass because it drops those qualifications.

## Task 1: Mobile helper and copying

Owner: `web/screens/stop.js` (helper functions only), `web/style.css` (pc-help scope), new `tests/test_mobile_pc_input_help.py` and existing `tests/test_collection_help_mobile.py` only if needed.

1. Add meaningful RED regressions for clear baseline/additional/target quantities, mixed units/FIELD/reasons/multi-day qualification, receipt/full-copy text and serial clipboard handling.
2. Make a compact accessible header, receipt/date/result context and item comparison cards. Keep original item order, large additional quantity, nearby prior/cumulative/remaining, visible item/spec/unit matching, and no resident name/phone/address/memo.
3. Move full-copy near the top, add receipt copy, and use one busy/owner-checked copy operation with readonly textarea fallback. Preserve existing storage/reconfirm guards and initial-unconfirmed reference-copy policy.
4. Identify dates from relevant since_seq/PC-version-filtered collection records. For mixed dates, label the values as combined reference and show per-day verification guidance; do not invent allocation to dates or change ledger semantics. Keep normal single-date behavior.
5. Run new and existing helper/save/reconfirm/layout tests; capture 360/390/430 normal/XL light/dark/HC with fictitious PIN/IndexedDB data.

## Task 2: PC manual-entry comparison

Owner: `pc/ui/screens/collection_entry.js`, `pc/ui/pc.css` (collection scope), `tests/test_collection_quick_ui.py` or another existing focused collection UI suite.

1. Add RED for mixed-unit prior summary and per-line projected cumulative display.
2. Show each input's projected cumulative next to its existing quantity controls, without adding a table column or changing save payloads/retry IDs/validation/drafts.
3. Make summaries unit-specific and remove misleading aggregate '개' labels from the saved notification. Explain phone prior/PC prior matching and already-entered results. Keep real collection date and partial-result reasons unchanged.
4. Run existing collection UI/quick-entry/idempotency/parent-draft tests with temporary encrypted state.

## Task 3: Return ownership and integration

Root owns `web/app.js`, return regression, plans and npm/CI integration. On direct helper→same detail return, restore focus/position only for the original source/mode/request. Clear return data after unrelated navigation, state/lock changes or consumption. Preserve list-return and ordinary detail/subscreen behavior.

Independently review final source and tests, run appropriate helper/list-return/save/reconfirm and PC collection checks, verify syntax/version/generated files and unchanged backend/protocol/store, then commit locally. Preserve root AGENTS.md, deployed 20, packaged releases, shortcut and production processes/state. Physical-device and actual packaged-EXE verification remain separate from fixture/browser evidence.
