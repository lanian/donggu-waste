# Mobile Field Workflow Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the four reproduced field-work defects and the reviewed quantity/short-screen usability issues.

**Approved design:** User replied "개선" to `docs/reviews/2026-11-14-mobile-field-ui-review.md`. Retain the green interface and existing business/protocol contracts. Intake must keep its first operation/IDs and form through delayed or failed writes, with real success before clearing/navigating. Quantity edits after a saved result require reconfirmation; never silently rewrite the previous ATTEMPT. Confirm a full-fill action when it would overwrite deliberately entered partial quantities. Quantity values gain a validated numeric entry sheet while ± buttons remain. Short screens scroll the large header/steps and use a persistent compact error with expandable details.

**Architecture:** Screen-local ownership guards and state-scoped intake operations; additive `resultNeedsConfirmation` on mobile derivation; existing encrypted commit/retry and event formats. Core provides optional numeric stepper entry and a compact error banner. Shared gates keep unresolved collection results visible in home/help/export without blocking emergency backup of unsaved records.

**Tech Stack:** ES modules/CSS; Python Playwright with real PIN/IndexedDB and fictitious data; existing fee/protocol assertions.

## Task 1: Intake Save and Quantity

**Owner files:** `web/screens/intake.js`, new `tests/test_mobile_intake_save.py`.

1. Write/run RED for actual quota failure (no success or clearing), delayed response/repeated save (one request/event), 150−1=149, same-ID retry, navigation/lock ownership and edit/solo paths.
2. Make save pending/failed/success states explicit and serialize one operation. Retry commits the existing mutation; it never creates another receipt, FIELD_INTAKE, version or counter increment. Freeze editable controls during the operation; preserve input and return/focus behavior.
3. Specify intake bounds 1..9999 and enable optional numeric stepper entry. Run the new tests and relevant existing draft/intake workflow checks.

## Task 2: Result Reconfirmation and Partial Protection

**Owner files:** `web/fees.js`, `web/screens/stop.js`, new `tests/test_mobile_result_confirmation.py`.

1. Write/run RED for DONE→quantity edit keeping completion/next, full-fill overwriting partial quantities without confirmation, cancellation and reconfirmed same event stream.
2. Add `resultNeedsConfirmation` from relevant events after the latest ATTEMPT, preserving recorded outcome/history and PC/protocol derivation contracts. Detail/help clearly show reconfirmation and gate next/copy until a fresh result is durably stored.
3. Confirm an intentional partial quantity overwrite with a change summary; cancel leaves values/events intact. Enable numeric quantity entry. Run existing eleven collection-save regressions and focused fee checks.

## Task 3: Shared Quantity and Short Screens

**Root files:** `web/core.js`, `web/widgets.js`, `web/style.css`, `web/screens/home.js`, `web/screens/solo.js`, `web/screens/export.js`; new `tests/test_mobile_field_layout.py` owned by a test agent.

1. Establish RED for numeric entry and unreachable buttons in 360×480/400 with large text and actual storage failure. Use synthetic viewport evidence explicitly.
2. Add optional `stepper(..., label, { editable: true })`: tap value → existing sheet with integer/min/max validation → apply/cancel. Preserve debounce/flush/lock and source-view ownership, with useful focus return. Reuse `quantityInput(value,min,max,label)` in picker controls and enforce 1..9999 bounds.
3. Use persistent save-failure summary with expandable details. In short viewports make steps/picker header scroll normally and adjust offsets, preserving large targets and reachable back/retry controls.
4. Prioritize reconfirmation in both home modes, guard ordinary result export/cleanup and keep emergency recovery export available during actual save failure. Do not change the transmitted events or reset drafts/filters.

## Task 4: Integration and Review

1. Register stable tests in npm/CI and update the manual/review outcome. Run new tests, `npm test`, existing mobile E2E/workflow/cleanup/save/help/lock/date tests appropriate to the changed contracts, version/generated checks and syntax/diff checks.
2. Inspect screenshots and request independent read-only review of saving races, stale views, result gating and short-screen access. Fix concrete findings; repeat affected checks.
3. Commit verified local changes on `mobile-ui-20261114`. Preserve root AGENTS.md, production app/state, public site and release versions; publishing remains a release action.

**Parallel ownership:** Intake and collection agents edit only their owner files/tests. Layout test agent edits only its new test. Root owns core/widgets/CSS/home/export/package/CI/docs. Agents do not commit, push, build or deploy.

**Evidence:** `D:/Temp/dg-field-fixes-20261114`.
