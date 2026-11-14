# Pull Refresh and Mobile-to-PC Field Review Improvements

**Approved scope:** User reports pull-down refresh relocking the mobile app, requests easier PC handling of mobile field additions, and explicitly selects both new field receipts and added items. Keep the previously chosen restart PIN policy. This work updates local source; publishing is a separate release action.

## Design

1. Apply vertical overscroll containment to the mobile document root/body. Normal scrolling, horizontal navigation, maps, selection and existing dialogs remain. Explicit reload/cold restart still requires PIN; never persist an unwrapped key/PIN or bypass startup protection.
2. New field receipts already become PC requests, and field items already merge into the existing request. Keep these business paths and show a per-import review list with links to those actual requests rather than creating another request. Add an office-only read-only API scoped to the actual import because existing responses have counts only. Distinguish applied new receipt, added item, pending price and conflict/review states; exclude resident name/phone/address. Duplicate-file responses reuse the original import ID and confirmation code without another write.
3. The request screen must not mark a receipt office-confirmed while edits are unsaved. Make saving/confirmation order and errors explicit, preserve the draft on failure and avoid duplicate revisions or field checks. Pending prices remain separately visible. Seed the existing similar-item lookup with the mobile item name/spec while preserving manual selection, evidence and reviewer requirements.

## Implementation Plan and Ownership

- Root: `web/style.css`, `pc/dgpc/ops.py`, `pc/dgpc/results.py`, `pc/dgpc/server.py`, API tests, generated routes, documentation, npm/CI registration and integration. Record before/after root scroll policy and ordinary scrolling/reload/PIN behavior with an isolated browser diagnostic. The small CSS fix does not need a permanent test mirroring one property. Use the existing mobile lock/UI regressions. Native phone pull gestures remain a separate physical-device check.
- Import owner: `pc/ui/screens/results.js` plus new `tests/test_pc_field_import_ui.py`. Reproduce the missing actionable import summary; add per-import field review links for file/QR routes using the root-owned lookup API. Preserve duplicate-file codes, history, owner/role restrictions, in-flight navigation and load/error retry. Do not write backend/CSS/shared core.
- Request owner: `pc/ui/screens/request.js` plus new `tests/test_pc_field_review_ui.py`. Add RED coverage for office confirmation with dirty edits and price-lookup re-entry. Implement the narrow reliable save/confirm ordering, pending-price clarity and prefilled lookup. Reuse existing persistence/dirty/date/payload contracts; no automatic item choice or backend/schema change.
- Both agents run actual temporary-PC HTTP/browser tests with fake records, record RED/GREEN and freeze their owned files. Root integrates, checks versions/generated assets, runs relevant PC request/import/mobile draft/lock tests and requests independent read-only review.

## Verification Boundaries

Use temporary `DG_STATE`/`DG_OUT` and fictitious resident data. Preserve production state/processes, release artifacts, root AGENTS.md and release 0.14.16. Do not commit encrypted databases/keys/output. No agent commits, pushes, builds or deploys.

**Evidence:** `D:/Temp/dg-refresh-field-import-20261114`.

**Scroll behavior sources:** [Chrome guidance](https://developer.chrome.com/blog/overscroll-behavior/) and the [CSS Overscroll specification](https://drafts.csswg.org/css-overscroll/). Root containment prevents browser overscroll navigation where supported; headless testing does not prove a physical Android/Safari pull gesture.

## Implementation and Verification Completed

- Added `GET /api/imports/{id}/field-review` for ADMIN/INTAKE/LEAD. It groups the actual import's field events by actual request UUID, derives current pending prices, and returns no resident name/phone/address. FIELD/DONG are denied. Duplicate-file and import-race responses retain the original import ID/code.
- File and synthetic-camera QR imports now show actual receipt links, added items/quantities, pending prices and conflicts. History can reopen/retry the list. Existing conflict reasons survive tab changes and older responses; detached screens and replaced authentication sessions cannot apply late responses.
- Request edits must be saved before office confirmation. Memory-only operation state is partitioned by the authentication object. Pending saves/checks survive same-session reentry; failed saves preserve the submitted fields/items/reason for explicit recovery or discard. Lost responses reconcile with stored state. Recovery compares the full item payload, including fee rate, unit and line identity. Similar-item search is prefilled while manual selection/evidence/reviewer remain required.
- Registered the API/UI tests in npm and CI; updated the manual and generated route table (166 routes). Production state/processes, the existing root AGENTS.md (2707 bytes, original modification time), versions and packaged 0.14.16 artifacts were preserved.

Local environment: Node 24.20.0, Python 3.14.3 and Playwright Chromium. CI remains Node 22/Python 3.12; it was not run remotely for this branch.

| Verification | Result / evidence under the evidence directory |
| --- | --- |
| Root scroll diagnostic, before/after | PASS: vertical auto→contain; horizontal auto; root scroll 650px and inner scroll 400px; explicit reload PIN, records and lock-OFF setting preserved. `refresh-auto.json`, `refresh-contain.json` |
| `npm run test:field-import` | PASS: 3 API tests plus actual temporary-PC HTTP/browser file/QR/import and save/confirm regressions. `focused-final.log` |
| `python tests/test_pc_field_review_ui.py` after the final item-comparison fix | PASS: all 11 behavior groups, including same-display-name fee selection recovery. `request-final.log` |
| `python tests/test_pc.py` | PASS: 688 checks. New route explicitly added to the permission snapshot extension. `pc-logic-final.log` |
| `npm test` | PASS: fees, interoperability, collection protocol and fee calculator. `npm-test.log` |
| `python tests/e2e_pc.py` | PASS: 115 checks. `pc-ui-final.log` |
| `python tests/test_pc_collection_ui.py` | PASS: editing/saving, quantities, drafts, labels and lost-response retries. `collection-final.log` |
| `npm run test:mobile-ui` | PASS: home/settings and 18 layout variants. `mobile-ui.log` |
| `python tests/test_mobile_auto_lock.py` | PASS: OFF setting, startup/manual PIN, drafts and other-tab protection. `lock.log` |
| `python tests/test_review_ui.py --suite requests` | PASS: 5 request-list response/order/navigation checks. `review-requests.log` |
| JS syntax, Python compilation, version/generated files, diff whitespace | PASS; versions remain 0.14.16 |

The combined focused run and broad PC UI run preceded only the final one-line item-recovery comparison change; the complete request UI script and syntax checks were rerun against its final SHA256 `63c565f4577f1bbd1bbbe8976c4fb116f58c0af25bbf58d2985b53e468530780`.

Independent read-only review is **GO for the changed source** after resolving stale conflict responses, save/reentry ordering, authentication isolation, failure recovery and complete item restoration. RED/GREEN logs and fictitious-data screenshots are in `import/`, `request/`, `focused/` and `request-final/`.

No full `test:all`, packaged EXE build/run, physical phone pull gesture, physical webcam or production deployment was performed for this change. QR coverage uses generated frames through the real decoder/decryption/import path. This branch is a local implementation awaiting a separate release.
