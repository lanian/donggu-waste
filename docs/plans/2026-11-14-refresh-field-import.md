# Pull Refresh and Mobile-to-PC Field Review Improvements

**Approved scope:** User reports pull-down refresh relocking the mobile app, requests easier PC handling of mobile field additions, and explicitly selects both new field receipts and added items. Keep the previously chosen restart PIN policy. This work updates local source; publishing is a separate release action.

## Design

1. Apply vertical overscroll containment to the mobile document root/body. Normal scrolling, horizontal navigation, maps, selection and existing dialogs remain. Explicit reload/cold restart still requires PIN; never persist an unwrapped key/PIN or bypass startup protection.
2. New field receipts already become PC requests, and field items already merge into the existing request. Keep these business paths and show a per-import review list with links to those actual requests rather than creating another request. Distinguish applied new receipt, added item, pending price and conflict/review states using existing authorized API data.
3. The request screen must not mark a receipt office-confirmed while edits are unsaved. Make saving/confirmation order and errors explicit, preserve the draft on failure and avoid duplicate revisions or field checks. Pending prices remain separately visible. Seed the existing similar-item lookup with the mobile item name/spec while preserving manual selection, evidence and reviewer requirements.

## Implementation Plan and Ownership

- Root: `web/style.css`, documentation, npm/CI registration and integration. Record before/after root scroll policy and ordinary scrolling/reload/PIN behavior with an isolated browser diagnostic. The small CSS fix does not need a permanent test mirroring one property. Use the existing mobile lock/UI regressions. Native phone pull gestures remain a separate physical-device check.
- Import owner: `pc/ui/screens/results.js` plus new `tests/test_pc_field_import_ui.py`. Reproduce the missing actionable import summary; add per-import field review links for file/QR routes using existing APIs. Preserve duplicate-file codes, history, owner/role restrictions, in-flight navigation and load/error retry. Do not write backend/CSS/shared core.
- Request owner: `pc/ui/screens/request.js` plus new `tests/test_pc_field_review_ui.py`. Add RED coverage for office confirmation with dirty edits and price-lookup re-entry. Implement the narrow reliable save/confirm ordering, pending-price clarity and prefilled lookup. Reuse existing persistence/dirty/date/payload contracts; no automatic item choice or backend/schema change.
- Both agents run actual temporary-PC HTTP/browser tests with fake records, record RED/GREEN and freeze their owned files. Root integrates, checks versions/generated assets, runs relevant PC request/import/mobile draft/lock tests and requests independent read-only review.

## Verification Boundaries

Use temporary `DG_STATE`/`DG_OUT` and fictitious resident data. Preserve production state/processes, release artifacts, root AGENTS.md and release 0.14.16. Do not commit encrypted databases/keys/output. No agent commits, pushes, builds or deploys.

**Evidence:** `D:/Temp/dg-refresh-field-import-20261114`.

**Scroll behavior sources:** [Chrome guidance](https://developer.chrome.com/blog/overscroll-behavior/) and the [CSS Overscroll specification](https://drafts.csswg.org/css-overscroll/). Root containment prevents browser overscroll navigation where supported; headless testing does not prove a physical Android/Safari pull gesture.
