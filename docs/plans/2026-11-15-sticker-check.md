# Sticker and certificate check improvement

**Goal:** Make each mobile item's sticker/certificate state and collection decision clear, with usable number entry and safe saving.

**Architecture:** Keep `STICKER_CHECK` (`OK`/`MISSING`/`MISMATCH`, optional number, `COLLECT`/`LEAVE`) and the existing manual collection quantities. Keep Python, schema, protocol, inventory, generated labels and release version unchanged. Baseline `1ad1b9f`, local branch `feat/sticker-check-20261115`; no deployment/build in this task.

## Design

- Keep the three inline choices for quick field use; add an explicit unchecked state, checked/problem/unchecked item counts, and a readable decision summary. A separate full-screen checklist adds navigation; automatic number matching needs an authoritative expected number that this screen does not have.
- Explain that checking a sticker or choosing `COLLECT` does not fill the collected quantity or save a collection outcome. Show actual additional collection quantity separately. `LEAVE` retains prior PC collection, resets this visit's additional quantity and records `NO_STICKER`; explain that effect before choosing. Later sticker corrections retain existing quantities/reasons and explain how to review them.
- For mismatch, prefill the previously entered number, retain leading zeroes/hyphens, and reject more than 30 characters rather than silently truncating. The number is a manual note, not automatic verification or stock allocation.
- Keep existing choice labels and shared `sheet()` contracts. Make check buttons at least 56px and allow wrapping. Use sticker-scoped CSS and item/spec context; verify scrollable actions at 360×400 XL, including input-focused keyboard-size conditions.
- Use existing collection write ownership/freezing/retry handling for sticker actions. Prevent parallel saves and stale decisions after navigation/lock/source/stop changes, retain cancellation/focus and existing queued quantity flushing. Pending/failed records use the existing retry with the same event IDs.

## Tasks and ownership

1. Mobile owner: `web/screens/stop.js` sticker functions/rows and minimum collection-wrapper integration; sticker-scoped `web/style.css`; new `tests/test_mobile_sticker_check.py`. Write RED regressions for unchecked/decision semantics, safe number editing, focus/cancel, small-screen hit targets, and serialized saving before implementation. Preserve the completed PC-input helper region.
2. Root: npm/CI registration, manual, plan and integration checks. PC expert audits the unchanged cross-system contracts. Independent reviewer verifies the final diff and state/quantity/save boundaries.
3. Verify with actual PIN/IndexedDB and fictitious data: new sticker suite, `test_mobile_collection_save.py`, `test_mobile_result_confirmation.py`, `test_mobile_field_layout.py`, existing/new PC-input helper suites, `npm test`, syntax/version/generated/whitespace checks. Add changed-behavior regressions and record RED→GREEN and representative light/dark/HC normal/XL browser captures. Do not claim physical phone, native keyboard/camera, packaged EXE or public deployment evidence.

## Preservation contracts

- `COLLECT` leaves quantities unchanged; `LEAVE` never reduces quantities below `prior_collected` and stays excluded by `fillAll` while reason is `NO_STICKER`.
- `OK` does not silently erase a previous no-sticker reason, increase quantity, mark paid or change an overall saved result. Quantity changes still require outcome reconfirmation through existing rules.
- Resident sticker/certificate checking is separate from the administrative numbered-label inventory. Existing enum values, body keys, worker and PC-version metadata, import/close summaries and retry identities stay compatible.
- Preserve outer AGENTS, previous helper commit, release artifacts/shortcut, production state and processes.
