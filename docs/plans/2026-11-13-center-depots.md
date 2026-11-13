# Administrative Center Depot Implementation Plan

> Use brainstorming, writing-plans, test-driven-development, frontend-design and verification-before-completion in the current checkout. Preserve AGENTS.md, existing user work, production app/state and prior releases. No automatic commit/push/publication.

**Goal:** 동별 차고지를 해당 행정복지센터 위치로 쉽게 설정하고, 동마다 한 개의 기존 담당팀과 연결한다.

**Confirmed:** User states that each dong's garage is its administrative welfare center and currently has one separate collection team per dong. Preserve existing team assignments/custom depots; do not invent vehicle capacities or create duplicate teams.

**Design:** Ship an offline reference of all 13 official center addresses and published map markers. Add a center-location preset in each depot editor and an administrator-only one-click operation that fills missing depots for active official dongs with a uniquely assigned active crew. Keep custom depots, inactive/unmapped/shared-team configurations and unrelated dongs untouched, reporting why rows were skipped. Existing row Save semantics and route start/return rules continue. Team connection is explicit where no saved mapping exists.

**Data evidence:** On 2026-11-13 downloaded all 13 current official direction pages at `https://www.donggu.kr/menu.es?mid=a10605010300` through `a10617010300`. The pages publish road addresses and embedded marker data. Converted the published marker coordinates to WGS84 using the same official page's loaded map SDK (no VWorld API or private API key). Preserve source URL and checked date per record. Dongmyeong uses 동계천로 172; Hakdong uses the listed temporary location 남문로753번길 20. Evidence under `D:/Temp/dg-center-depots-20261113`.

**Alternatives:** Runtime geocoding would require service credentials/network and could select an old center. Unconditional replacement would overwrite intentional garage settings. Use verified offline presets plus guarded fill-missing application. No schema/phone protocol change.

## Tasks

1. Add RED service tests for all 13 records, ready-row application, custom/no-team/inactive/shared preservation, non-depot field preservation and idempotent retry. Add JSON/reference service and read-only center metadata on dong records.
2. Add RED real HTTP authorization/result/route-start tests. Add administrator endpoint with atomic fill-missing updates and audit. Include public reference JSON in desktop assets and regenerate route docs.
3. Add browser regression for preset/cancel/save, one-click fill-missing, existing row drafts/order, skip feedback, route origins/returns and unknown-response retry. Preserve settings async/owner/generation contracts.
4. Run related depot/default/order/settings/route checks; obtain independent review; inspect screenshots. Update version to 0.14.13 and manuals/check scripts, build and verify frozen EXE plus prior dispatch/legacy flow. Update shortcut after verification.

## Validation and review

- Service: all 13 references, unique active team application, custom/no-team/inactive/shared preservation, transaction rollback, idempotent fill and read-only metadata covered by 6 tests. HTTP: administrator permissions, real route depot/team and recorded same-request retry covered by 3 tests.
- Browser: preset/cancel/local apply/save, other row drafts, uncertain POST result across navigation, held successful response across equivalent settings hashes, failed GET recovery without another POST, orphan drafts after external deletion, and existing depot/team preservation. RED confirmed for unknown outcome and stale cross-hash rendering, then corrected with session-scoped operation state, read reconciliation and a scoped forced render of an active dong card.
- Independent review found the uncertain-response stale-save and orphan-draft issues; follow-up identified the cross-hash renderer skip. Preserve authentication-owner guards, current row refresh generations and unrelated screen drafts. The endpoint uses the existing transactional idempotency ledger; retries keep the same request ID.
- New checks are included in `test:pc`, `test:e2e`, `test:dongs`, `test:depots` and CI. Version sources synchronized to 0.14.13; generated route/settings/assets and precache checks pass.
- Final targeted backend checks: default dongs 10, order 7, center service 6, center HTTP 3, depot service 6, routing depots 6 and depot HTTP 29 (67 total). Existing depot browser, settings authentication/draft races and order browser (15 assertions) pass after the change. Center UI source regressions pass; screenshot inspected. This is targeted validation, not a new full-suite run.
- Final read-only review reports no remaining Important issue. Frozen 0.14.13 passes the center UI regression, existing dispatch workflow and legacy DB/order/restart/theme suite in isolated temporary state directories. Packaged center JSON and settings.js match source bytes. This verifies the frozen server/browser workflows, not native tray interactions or production team mappings.
- Release: `dist/desktop/v0.14.13/Donggu-PC/Donggu-PC.exe`; ZIP 66,705,823 bytes, SHA256 `26d80a007e308afe5645c260ec3d9afeee7b086243f00c7901e45986fa6fa89f`. `E:/Projects/waste/동구 PC 실행.lnk` now targets 0.14.13 with empty arguments and original window style. Prior shortcut backed up under `D:/Temp/dg-center-depots-20261113/shortcut-before-v0.14.13.lnk`.
- AGENTS.md remains unchanged (2707 bytes, original 2026-11-04 timestamp). Production app/state and prior releases were preserved; no commit, push, mobile publication or automatic application to production depot settings.
