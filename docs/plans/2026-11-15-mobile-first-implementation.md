# Mobile-first Design Implementation Plan

**Goal:** Improve mobile scanning and touch selection, and align PC controls with a clear, accessible workbench style.

**Architecture:** Apply presentation-only CSS and small home/search markup changes. Keep business logic, encrypted persistence, routing contracts and payloads unchanged. Mobile and PC have disjoint owners; accessibility review is read-only.

**Tech Stack:** Existing HTML/CSS/ES modules, Python 3.x, Playwright Chromium, temporary SQLCipher/IndexedDB fixtures. No new dependency or font asset.

## Task 1: Mobile field UI

Owner files: `web/style.css`, `web/screens/home.js`, `web/screens/solo.js`, `web/screens/stoplist.js`, `tests/test_mobile_list_filters.py`.

1. Extend the existing filter regression with a 360px/XL case requiring all state choices to be within the viewport and clickable without horizontal scrolling. Observe its failure before the layout fix.
2. Scope wrapping/segmented filter layout to home list groups, preserving 56px targets and selection semantics. Keep dong choices usable without changing filter data or order.
3. Apply neutral surface/spacing/type roles to home primary action, summary, tools, cards and controls; align SOLO summary position with PC. Preserve all alerts, unique primary action, two-column tools and fixed safe areas.
4. Add a visible search label and concise help while preserving `aria-label="목록에서 찾기"`, debounce ownership and query state. Improve critical card text with relative sizes, preserving all text-size preferences.
5. Run `python tests/test_mobile_list_filters.py`, `npm run test:mobile-list`, `npm run test:mobile-ui`, plus relevant collection/field-layout cases. Save comparable before/after screenshots and actual computed metrics to the temporary evidence folder. Do not add tests that merely assert decorative CSS values.

## Task 2: PC visual consistency

Owner file: `pc/ui/pc.css` only.

1. Use the captured actual-server 14-view baseline from `tests/test_pc_design.py --baseline --quick`.
2. Introduce screen-only semantic control/type/shape rules at the effective cascade layer. Preserve desktop density and current shell; selectively enlarge key helper/KPI text instead of increasing every label.
3. Align default/hover/selected/disabled/focus roles without overriding danger, warning, native controls, document or print styles. Add coarse-pointer touch targets and reduced-motion handling with bounded selectors.
4. Run `python tests/test_pc_design.py --quick` with before/after labels, existing PC theme/design checks and a coarse-pointer diagnostic. Check draft/filter/keyboard paths and no viewport overflow or hidden primary actions.

## Task 3: Independent verification and record

Root owns plans, package/CI wiring if needed, integration and commits. Accessibility expert owns no implementation file.

1. Freeze both implementations. Review actual diff and screenshots, then recompute body/control/state contrast across light/dark/high-contrast and keyboard/reduced-motion modes.
2. Root independently runs the meaningful new regression and appropriate existing mobile list/home/layout/field, PC design/theme and workflow checks. Inspect failures before any additional change.
3. Check JavaScript syntax, versions/generated assets, whitespace, unchanged protocol/store/backend files and root AGENTS.md.
4. Commit verified local changes and record exact commands/results and remaining physical-device limits. Do not publish or rebuild the EXE during this task.

## Implementation and verification record

- Implemented only the five presentation files and existing filter regression named above. Core text-size preferences, app/SW/PC version 0.14.18, backend, authorization, store, protocol, fees, generated assets and dependencies are unchanged.
- The 360px/XL hidden Cancelled filter failed the actual UI regression before the fix, then passed at 360/390/430px in normal/XL and PC/SOLO. All choices remain at least 56px tall and can be selected without horizontal scrolling.
- Same-clock mobile before/after captures: summary 128.5→88px (normal), 144.1→99.2px (XL); tools 28px shorter. At normal/390px the first PC card moves 1325.8→1313.6px, SOLO 1214.8→1199.1px. Larger long-address cards retain all information and can grow with the larger text. Evidence: `D:/Temp/dg-ui-design-20261115/mobile/comparison.json` and before/after directories.
- Root independently ran eight mobile scripts: list cards/filters/return, home UI, layout, phone search, collection save and automatic lock. Every script returned 0. The source-stability sentinel intentionally rejected this combined run because the implementation owner removed one redundant home-tool background override during execution. Root then reran cards/home/layout against the final stable CSS: all passed, matching final SHA256 `46d9e93d8d699e1806d8818f03f6ed677ec08283f73d40e974587dc1d9103d03`. Independent final warning/disabled checks also passed. See `root-mobile/results.json` and `root-final-css/results.json`.
- Root independently ran `test_pc_design.py --quick`, `test_pc_theme_races.py`, `test_collection_quick_ui.py`, and `test_pc_item_picker_flow.py`: all passed with stable final PC CSS. PC owner additionally verified theme14, an actual-server fine/coarse 18-case matrix, keyboard/forced-colors/reduced-motion and unchanged print metrics. Desktop regular36/compact30 and table density remain unchanged; coarse-pointer primary targets are 44px. Critical helper text is 13px at the 15px preference and scales with 17px. Evidence: `root-pc/results.json`, `pc/control-matrix.json`, `pc/final-pc-metrics.json`.
- Independent accessibility review is GO, no important remaining finding: actual PIN/IndexedDB mobile 24 variants plus six final warning/disabled themes; critical text and controls meet measured contrast requirements, focus/reduced-motion and viewport bounds passed. Default item16.005px, remaining16.995px, receipt13.9995px. The review independently exercised mobile; PC matrices were source/evidence-reviewed rather than rerun by that reviewer. See `D:/Temp/dg-ui-design-20261114/accessibility/review/final-review.json`.
- JavaScript syntax, version/generated-file consistency and `git diff --check` passed. No full `test:all`, EXE build, public deployment, physical phone/camera or native launcher test was performed for this design task.
- Review gallery: `D:/Temp/dg-ui-design-20261115/design-preview.html`. Its five before/after pairs, platform filter, image zoom and 390px layout were verified. Gallery data is fictitious. Root AGENTS.md, existing release ZIP, shortcut and production processes/state were preserved.
