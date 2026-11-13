# Assignment and Route Workflow Implementation Plan

> Apply brainstorming, writing-plans, test-driven-development, frontend-design and verification-before-completion in the existing checkout. Preserve earlier work, AGENTS.md, production data and running apps. No automatic commit/push or mobile publication.

**Goal:** 배정 대상 확인, 노선 수동 조정, 자동 계산과 확정 과정을 연결한다.

**Confirmed:** User selected all three proposed areas: full workflow, easier order/team changes, and travel/time/capacity calculation. Retain each dong's garage and fixed crew. Use the preceding popup policy: routine work inline; short consequential confirmations remain.

**Design:** Keep automatic recommendation, with explicit fast/precise calculation budgets. Add order-preserving recalculation after manual edits. Mark changed times as uncalculated and require successful review before confirmation. Preserve edits/reason on errors, support undo, readable receipt/address search, and show a final inline date/team/visit/unassigned summary. Keep existing confirmed-assignment edits distinct and load all unassigned candidate pages. Show team-specific depots, capacity, estimated drive/work minutes and an explicitly labelled straight-line distance total including unloading/return; do not describe it as road distance.

**Alternatives:** Full automatic recalculation alone discards a deliberate manual order; keeping manual edits without recalculation shows stale times. Provide both automatic recommendation and fixed-order review using the same existing OR-Tools constraints. Preserve capacity, appointments, breaks, unloading and per-crew starts/returns.

**Architecture:** Add optional `fixed_plan` to `routing.plan` and the existing POST `route`; no new schema or phone protocol. Fixed-order runs must retain every selected stop and its crew/order, may insert unloading/breaks, and return infeasible instead of silently dropping stops. Bind new global route proofs to request versions, crew profiles/settings and assignment revisions; recheck after calculation and before confirmation. Older optional proof formats remain compatible. Reject a manual fixed-crew violation. Frontend keeps an immutable displayed draft until a matching successful response arrives.

## Tasks

1. Add RED real-OR-Tools tests for fixed crew/order, same-place ordering, no silent dropping, capacity/unload/break/time-window constraints, invalid IDs/duplicates and distance/time summaries. Implement minimal optional routing support; run legacy depot routing regressions.
2. Add RED actual HTTP tests for new route review, modified requests/settings/assignment conflicts and permissions. Extend existing route preparation/proof checks and include required-crew metadata. Keep CPU/network work outside the DB lock.
3. Add RED browser workflows for move/undo, stale ETA hiding, reason retention, review error/retry, date/crew/owner freshness, workflow summary and candidate pagination. Implement the existing UI style with searchable route/candidate lists and accessible arrow-button focus restoration.
4. Run appropriate routing/assignment/core/dong tests, related E2E, syntax/version/generated/whitespace checks. Obtain independent read-only review and resolve Important findings. Inspect fictitious responsive screenshots.
5. Synchronize release version to 0.14.12, update manuals/scripts, build the PC package and verify new route workflow plus prior collection/date/legacy workflows in frozen temporary state. Update the workspace shortcut after verification; preserve production app and previous releases.

## Evidence

Use `D:/Temp/dg-dispatch-workflow-20261113` for logs/screenshots. Tests use fictitious residents and temporary encrypted state. Record source, real solver, frozen EXE and live external-service evidence separately.

## Completed validation (2026-11-13)

- Real OR-Tools: 11 route-review tests and 6 legacy depot routing tests passed. RED/GREEN covered fixed order/crew, same-place ordering, mandatory selected stops, capacity failure, unloading insertion/window, impossible appointments, and individually feasible colocated visits remaining separate. Added drive/work/elapsed minutes and explicitly straight-line distance including return.
- HTTP: 16 route-review/depot tests passed, including 502 daily candidates; request/settings/assignment changes; normal signed phone ITEM_RESULT/LOCATION_FIX imports without req_ver changes; and postpone then refresh the remaining order. Proofs contain only request-state digests. Read-only fixed review verifies the original signed scope and takes a fresh snapshot; the new snapshot is checked after solve and strictly at confirmation.
- Existing PC logic: 688 assertions passed on final routing behavior. PC E2E: 115 assertions passed, including direct assignment editing, QR transfer, imports, reporting/finance flows and permissions. Follow-up async/browser regressions: 20 tests passed. Synthetic aborted sockets and intentional wrong-key/401 checks emitted their expected diagnostics; no success claim relies on them being live failures.
- Source and frozen workflow test passed: searchable route/candidate lists, team/order changes, undo, stale-time hiding, retained reason/order on review and mode-switch failures, postpone/rebase, and held confirmation-response changes. RED reproduced an actual unreviewed commit before revision/snapshot protection. The Seoul midnight date regression now uses moveDay and starts from the next calendar date.
- Inspected fictitious 1440px/980px screenshots. Removed native replaceChildren null text, corrected toolbar layout and cleared obsolete errors. No horizontal overflow at 980px. Current-assignment editing shows the adjustment stage.
- Independent read-only review rechecked the Important findings; none remained in its final narrow check. Version/precache, JavaScript/Python syntax, generated assets/docs and whitespace checks passed. CI/npm route checks were registered; remote CI and npm test:all were not run.
- Built `dist/desktop/v0.14.12/Donggu-PC/Donggu-PC.exe`. Four frozen workflows passed: new dispatch workflow; prior collection/idempotency/label allocation; date filters; and legacy DB/default dongs/order/restart/theme/login/routing without Python on PATH. Map tests used an empty basemap and VWorld used a fictitious provider; this is not a live road-provider or native tray/manual-launch test.
- ZIP: 66,699,940 bytes; SHA256 `f2f6fd46e8fead2d3b6198dfe1c3d1830449ef8daee2115c89202ff89dd42fb5`. Workspace shortcut now points to 0.14.12, preserving arguments/window style with a backup in the evidence folder. Existing releases, AGENTS.md and production app/state were preserved. Mobile hosting was not published.
