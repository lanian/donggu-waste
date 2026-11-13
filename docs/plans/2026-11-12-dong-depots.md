# Dong Depots Implementation Plan

> Execute in this existing checkout with writing-plans, test-driven-development and verification-before-completion. Preserve earlier uncommitted changes. No automatic commit/push, production state edits, or interruption of the user's app.

**Goal:** 각 동에 정해진 담당팀과 별도 차고지를 등록하고 실제 출발·복귀 시간과 노선 지도에 적용한다.

**Confirmed:** 동마다 차고지가 다르고 담당 수거팀이 정해져 있다. 동별 차고지 설정과 담당팀을 연결한다. 미설정 동은 기존 공통 차고지·선호 팀 동작을 유지한다.

**Architecture:** DB migration 11 adds nullable `dong.depot_json`. The existing dong save API accepts optional `depot` and preserves it when omitted. A shared route-context helper resolves each selected crew's depot and fixed crew restrictions for configured dongs. Extend the existing single OR-Tools solve to use vehicle-specific start/end nodes. Both global and scoped calculations use the same context. Scoped assignment proofs bind depot settings so old calculations cannot be confirmed after changes.

**Tech Stack:** Python/SQLCipher, OR-Tools 9.15.6755, existing ES modules/Tk launcher, Playwright. Preserve the application's existing administrative UI style.

## Design decisions

- Prefer dong settings plus one multi-depot optimization over separate per-dong solves. Separate solves could allocate the same physical crew twice and lose shared shift/capacity constraints. A crew-only depot field would not express the requested dong ownership.
- A configured dong needs a valid default crew. Its requests must stay with that crew. If that crew is not selected or active, list those requests as unassigned with a clear reason; do not silently give them to another dong's team.
- One crew cannot simultaneously start from two distinct dong depots. Reject conflicting active settings and detect any remaining ambiguity at calculation time. Equal coordinates can be shared.
- Preserve grouping for legacy requests; never group requests with different fixed crews into one stop. Keep capacity, weight, unloading, appointments, breaks and mandatory stops.
- Without an explicit end site, return to each team's own start depot. An explicit existing end site remains authoritative. Do not invent actual garage coordinates.
- Settings use a per-row depot dialog with name, coordinates and existing local-address/map helpers. Apply to that row, then use its Save button. Preserve row drafts, dong order, cancel/error/retry/focus and authentication boundaries.
- Show each calculated route's depot in global/scoped previews and plot from the route's saved result rather than a changing global bootstrap value. Keep phone payload and existing assignment contracts compatible.

## Tasks

1. Add failing `tests/test_dong_depots.py` for optional point validation/preservation, fixed-team context, conflicting depots, legacy fallback, 10→11 preservation and failed-step rollback. Implement `store.py`, `area.py`, `route_depots.py` minimally.
2. Add failing `tests/test_routing_depots.py` for separate starts/returns, fixed-crew grouping, unavailable crews, unloading and explicit end. Extend `routing.plan` with optional `crew_depots` and `crew_required`, retaining existing defaults.
3. Wire `server.route_plan` and `dong_work.prepare_route/calculate` to the shared context. Bind scoped route proofs to current depot/crew settings and check them before response and assignment. Add actual HTTP regressions for both paths and permissions.
4. Add the depot dialog to `settings.js`, depot summaries in `dongs.js`, and per-route start pins/lines in `dispatch.js`. Add real Chromium regressions for save/cancel/invalid input/draft/order preservation, fallback, route display and late responses.
5. Run related and existing PC/dong/routing/appearance/form regressions; update schema expectations only where they represent the current version. Update usage docs/CI/scripts and version declarations to 0.14.8. Obtain independent read-only review and fix important findings.
6. Build `dist/desktop/v0.14.8`, verify the actual EXE in fictitious encrypted state with no Python on PATH, verify new depot behavior and old forms, update the shortcut preserving arguments. Preserve all current ZIPs and latest/previous application folders. Do not claim live garage or provider validation.

## Validation commands

- `$env:PYTHONPATH='pc;tests'; .venv\Scripts\python.exe -m unittest test_dong_depots test_routing_depots -v`
- `.venv\Scripts\python.exe tests/test_pc.py`
- `.venv\Scripts\python.exe tools/check_generated.py`, `tools/check_version.py`; `git diff --check`
- Source and actual EXE browser workflows use temporary `DG_STATE`/`DG_OUT`, fictitious residents, Chromium specified by `CHROME`, and mocked external providers only where needed.

## Completed validation (2026-11-12)

- `tests/test_pc.py`: 688 existing PC assertions passed. Depot scripts passed 6 configuration/migration tests, 6 actual OR-Tools tests and 29 HTTP tests (including inherited session/scope cases), plus the source Chromium save/map and delayed-response flows.
- Existing dong regressions passed: 15 actual HTTP/order UI checks, 59 original-module UI assertions and the actual HTTP dong workflow. Session/review regression modules passed 19 tests. All nine `test:forms` commands passed.
- Important review findings were reproduced and fixed: fixed-team grouping respects that vehicle's weight limit; logged-out route requests return 401; row saves retain other rows' drafts, reopened same-row edits and explicit baseline reversions; older metadata responses cannot overwrite newer saves. Independent read-only review found no remaining Important issue in these fixes.
- Final frozen EXE passed all three workflows: `test_dong_depots_ui.py --exe`, `test_desktop_bundle.py --exe --legacy-dongs --theme-ui`, and `test_official_forms_e2e.py --exe`. These checked per-team starts/returns, migration to schema 11, restart/order/appearance preservation, encrypted state, operation without Python on PATH, normal shutdown and existing automatic label/XLSX/ODT/ZIP behavior.
- Version consistency, generated-document checks and `git diff --check` passed. Packaged settings/dispatch/dong/core JavaScript and both report templates matched source bytes and the release manifest.
- Release: `dist/desktop/v0.14.8/Donggu-PC/Donggu-PC.exe`; ZIP size 66,676,421 bytes; SHA256 `4c634b5b3f412aaad1e0b85b0644b4146695daf7eba56d87a85b043c9cb2a40c`.
- Updated `E:\Projects\waste\동구 PC 실행.lnk` to 0.14.8, preserving empty arguments and window style 1. Existing 0.14.7 processes were left running. Previous releases and the existing parent `AGENTS.md` were preserved.

Evidence: `D:\Temp\dg-depots-20261112` contains logs, screenshots, release/shortcut checks and fictitious generated forms. Local Python is 3.14.3 (CI specifies 3.12); this was a targeted local run, not `npm run test:all` or remote CI. MapLibre itself ran with an empty mocked basemap; VWorld checks used a fictitious provider. Actual garage locations and live external providers were not entered or verified, and no production business records were edited.
