# Sticker and certificate check improvement

**Goal:** Make each mobile item's sticker/certificate state and collection decision clear, with usable number entry and safe saving.

**Architecture:** Keep `STICKER_CHECK` (`OK`/`MISSING`/`MISMATCH`, optional number, `COLLECT`/`LEAVE`) and the existing manual collection quantities. Keep Python, schema, protocol, inventory, generated labels and release version unchanged. Baseline `1ad1b9f`, local branch `feat/sticker-check-20261115`; no deployment/build in this task.

## Design

- The user selected clearer check buttons, state and collection decisions as the priority. Number entry remains a small improvement to the existing field.
- Keep the three inline choices for quick field use; add an explicit unchecked state, checked/problem/unchecked item counts, and a readable decision summary. A separate full-screen checklist adds navigation; automatic number matching needs an authoritative expected number that this screen does not have.
- Explain that checking a sticker or choosing `COLLECT` does not fill the collected quantity or save a collection outcome. Show actual additional collection quantity separately. `LEAVE` retains prior PC collection, resets this visit's additional quantity and records `NO_STICKER`; explain that effect before choosing. Later sticker corrections retain existing quantities/reasons and explain how to review them.
- For mismatch, prefill the previously entered number, retain leading zeroes/hyphens, and reject more than 30 characters rather than silently truncating. The number is a manual note, not automatic verification or stock allocation.
- Keep existing choice labels and shared `sheet()` contracts. Make check buttons at least 56px and allow wrapping. Use sticker-scoped CSS and item/spec context; verify scrollable actions at 360×400 XL, including input-focused keyboard-size conditions.
- Use existing collection write ownership/freezing/retry handling for sticker actions. Prevent parallel saves and stale decisions after navigation/lock/source/stop changes, retain cancellation/focus and existing queued quantity flushing. Pending/failed records use the existing retry with the same event IDs.
- Independent baseline reproduction found that accepted `LEAVE` could save its sticker decision but omit the quantity record when the first write settled after navigation or lock. After acceptance and owner checks, enqueue the existing two records together before awaiting storage, keeping their order and bodies. Before acceptance, stale choices still write nothing.

## Tasks and ownership

1. Mobile owner: `web/screens/stop.js` sticker functions/rows and minimum collection-wrapper integration; sticker-scoped `web/style.css`; new `tests/test_mobile_sticker_check.py`. Write RED regressions for unchecked/decision semantics, safe number editing, focus/cancel, small-screen hit targets, and serialized saving before implementation. Preserve the completed PC-input helper region.
2. Root: npm/CI registration, manual, plan and integration checks. PC expert audits the unchanged cross-system contracts. Independent reviewer verifies the final diff and state/quantity/save boundaries.
3. Verify with actual PIN/IndexedDB and fictitious data: new sticker suite, `test_mobile_collection_save.py`, `test_mobile_result_confirmation.py`, `test_mobile_field_layout.py`, existing/new PC-input helper suites, `npm test`, syntax/version/generated/whitespace checks. Add changed-behavior regressions and record RED→GREEN and representative light/dark/HC normal/XL browser captures. Do not claim physical phone, native keyboard/camera, packaged EXE or public deployment evidence.

## Preservation contracts

- `COLLECT` leaves quantities unchanged; `LEAVE` never reduces quantities below `prior_collected` and stays excluded by `fillAll` while reason is `NO_STICKER`.
- `OK` does not silently erase a previous no-sticker reason, increase quantity, mark paid or change an overall saved result. Quantity changes still require outcome reconfirmation through existing rules.
- Resident sticker/certificate checking is separate from the administrative numbered-label inventory. Existing enum values, body keys, worker and PC-version metadata, import/close summaries and retry identities stay compatible.
- Preserve outer AGENTS, previous helper commit, release artifacts/shortcut, production state and processes.

## Implementation and verification

- Added item counts and explicit unchecked/checked/problem states, larger check controls, decision/additional-collected quantities, and guidance when an old `NO_STICKER` reason remains. `OK` and `COLLECT` still leave quantities unchanged.
- Sticker actions use the existing collection owner through quantity flushing, choice, storage and focus return. Accepted `LEAVE` queues the same two event bodies in their existing order before waiting for storage. Cancelled or stale choices do not create sticker events.
- RED→GREEN covered missing status summary, 31-character silent truncation, pending double clicks, accepted-LEAVE incomplete settlement, queued-quantity/modal ownership and same-tick navigation. Final seven-case sticker suite passed. Previously accepted quantity edits survive cancelling sticker confirmation.
- Fresh root checks passed: `npm run test:stickers`, `test_mobile_collection_save.py`, `test_mobile_field_layout.py`, `npm test`, syntax/version/generated/whitespace checks and npm/CI integration (54 unique existing suites). The mobile owner passed `test_mobile_result_confirmation.py`, `test_collection_help_mobile.py` and `test_mobile_pc_input_help.py` on frozen source.
- Independent baseline/final comparisons used real PIN/IndexedDB with held write-completion callbacks: navigation and lock now preserve `LEAVE` plus its quantity result and the prior-collection floor. Baseline retained the new decision but missed the quantity change.
- Final independent review passed accepted-write navigation/lock, queued quantity/cancel/focus, 31-character rejection and leading-zero/hyphen preservation, unchanged `COLLECT` quantities/outcome, and 360×400 XL action hit/click checks. No additional blocking findings.
- Browser layout cases: 390×844 normal light, 360×844 XL dark, 390×480 XL light, 360×400 XL dark/high contrast, and opening input before reducing height to 280px (simulated keyboard space). Controls remain at least 56px; internal scrolling exposes both decisions and cancel with actual hit/click checks. Normal check controls grew 48→56px and 13→16px. The extra effect explanations increase sheet content height; this is not a claim that the sheet is shorter.

Evidence: `D:/Temp/dg-sticker-help-20261115/{root,mobile/implementation,audit}/`. Frozen source hashes: stop.js `588ddb61269a815203f2122f261d11b6d9df9d4315b7eb8b9c27b6934b516456`; CSS `e7700c7c654196913cca4218f99689f263becdfdcaba161390948f000806f96d`. The previous PC-input helper text and backend/protocol/store/generated labels are unchanged. Outer AGENTS hash remains its baseline. No deployment/build or production-state/process changes; actual phone, native keyboard, camera and packaged-EXE verification are separate.
