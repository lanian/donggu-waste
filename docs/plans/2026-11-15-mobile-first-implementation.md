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
