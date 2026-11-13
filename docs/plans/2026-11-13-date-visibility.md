# Date Visibility Implementation Plan

> Use systematic-debugging, writing-plans, test-driven-development and verification-before-completion in this existing checkout. Preserve uncommitted work, production state, existing AGENTS.md and running apps. No automatic commit, push or mobile publication.

**Goal:** PC와 휴대폰 모두 날짜 때문에 가려진 접수를 쉽게 찾고, 현재 조회 범위를 분명하게 보여 준다.

**Confirmed:** User reports difficulty finding content by date and explicitly selected both PC and mobile. Continue the proposed date-independent lookup and visible date context.

**Root causes:** PC dong search applies its selected-day bucket before text search; global PC filters combine dates, OPEN status and quick buckets without a prominent reset/context. Mobile solo mode has no all-record view for completed past requests; the search box is hidden for fewer than six records. PC-connected mobile holds only its current received list plus local field intakes, not the entire server archive.

**Design:** Add an explicit all-period bucket and yesterday/today/tomorrow controls to dong browsing, while routing and metrics keep their selected-day contracts. Add PC request date presets and a date/status reset that preserves text and dong filters, with a live filter summary. Add a separate mobile date/receipt finder over locally held records in both modes, defaulting to all dates and all statuses, with an optional planned-day filter and clear local-storage scope. Keep the normal operational home flows. Always show the existing mobile search input. Do not retain new archives or change privacy cleanup or collection payloads.

**Alternatives:** Making every operational screen default to all records would mix dates into routing and daily work. Adding a new archive or live phone-to-PC query would require different retention/network contracts. A dedicated finder with explicit scope gives access to held records without changing those behaviors.

**Tech Stack:** Existing Python/SQLCipher and ES modules/PWA. Reuse the administrative UI style, native dates, responsive cards and current search/permission helpers. No dependencies/schema changes.

## Tasks

1. Add failing actual HTTP regressions for `dong-work/requests?bucket=all`, including old/done/cancelled/no-date records, pagination/text and current dong scope. Implement only the additional bucket.
2. Add failing actual Chromium PC flow for date presets/reset/context, preserved search/dong filters and all-period dong lookup with explicit work-day controls. Implement shared small date utilities and PC screen changes.
3. Add failing actual encrypted-mobile-store flows for finding old completed, future, cancelled and no-date records with fewer than six entries, in solo and PC-connected modes. Implement the finder, routing/cache registration and home entry points. State the limited local scope and preserve events/PII cleanup.
4. Run related PC/dong/mobile/collection regressions plus generated/version/whitespace checks. Obtain independent read-only review and resolve Important defects. Update synchronized version to 0.14.10 and usage/CI/scripts.
5. Build 0.14.10, verify the actual frozen EXE date workflow, collection and old migration/restart/forms workflows with fictitious state, verify packaged assets/manifest and update the shortcut preserving arguments/style. Keep mobile hosting publication separate from source implementation and report that boundary.

## Evidence

Use `D:\Temp\dg-date-visibility-20261113` for logs/screenshots and private temporary DG_STATE/DG_OUT. Record final commands/results and release hash after verification.

## Completed validation (2026-11-13)

- New date regressions passed: 2 actual SQLCipher/HTTP tests, actual PC Chromium date/status reset and scoped lookup, and encrypted-mobile-store lookup at 390px. Latest PC layout checks passed at 980px and 390px and in the final frozen EXE.
- Read-only review found two Important issues, both reproduced RED and fixed: a received PC stop carries its date on `daylist.work_date`, and finder-origin detail navigation must preserve the search through both dock buttons and same-request auxiliary screens. Tests now use the actual received-stop shape and exercise top/dock/last-item/add-cancel return paths. Final review found no remaining Important issue in that scope.
- A missing static-helper registration and a narrow-screen date-button overflow were also caught during actual-browser checks and corrected. The date toolbar now wraps in its own row using namespaced CSS. No schema/protocol/financial or assignment-date contracts changed.
- Existing `tests/test_pc.py` passed 688 assertions. Ten related dong/mobile/collection regression scripts passed; after the navigation fixes, the existing mobile workflow (25 assertions), cleanup (14 tests) and collection-help flow passed again. Generated docs, synchronized version and whitespace checks passed. Full `npm run test:all` and remote CI were not run.
- Final `dist/desktop/v0.14.10/Donggu-PC/Donggu-PC.exe` passed `test_date_visibility_ui.py --exe`, `test_pc_collection_ui.py --exe`, `test_desktop_bundle.py --exe --legacy-dongs --theme-ui`, and `test_official_forms_e2e.py --exe`. Checks used private fictitious encrypted state, no Python on child PATH and normal shutdown. Existing migration/restart/appearance/routing/number-allocation/XLSX/ODT/ZIP behaviors passed.
- Packaged date helper, request/dong screens, CSS, collection dialog and both report templates matched source bytes and manifest entries. ZIP size: 66,689,393 bytes; SHA256 `27b9efc532f5c5f8b01e3f420eca3a38082e7391a6f427c5fd0396806c70a172`.
- Updated the existing `E:\Projects\waste\동구 PC 실행.lnk` to 0.14.10, preserving empty arguments and window style 1. Running 0.14.7 processes, prior releases, original business records and the parent AGENTS.md were preserved.

Local Python is 3.14.3 (CI specifies 3.12). Screenshots and logs are under the evidence path above. Mobile source was implemented and locally verified, but hosting was not published; the site and app require updating. PC-connected mobile search covers received/local records only and does not invent an archive of no-longer-held server records.
