# PC menu outside-dismiss implementation plan

**Goal:** Close the narrow-window PC business menu when the user clicks outside it, retaining the clicked control's normal action and input state. Wide-screen fixed/rail navigation remains available.

**Design:** Capture outside-click eligibility against the initial menu/dialog state, then close in window bubbling after target handlers finish. This preserves the original layout while MapLibre calculates the selected point; a real-map regression checks both the target's original menu state and its coordinates. Avoid pointerdown dismissal for the same layout reason. Update only the navigation flag/class/expanded attribute through one helper, without rendering the workspace. No backdrop or form/navigation interception is added. Ignore already open application dialogs; Escape also respects handled keys and IME. Restore focus only when it would otherwise stay inside the hidden navigation. Crossing to the desktop breakpoint clears temporary open state while preserving the user's rail preference.

**Files:** `pc/ui/pc.js`; focused real-server Playwright regression `tests/test_pc_menu_dismiss.py`; register that test in npm/CI. No backend, data schema, native launcher, mobile PWA or production installation changes. Use fictitious accounts and temporary state/free ports.

## Steps

1. RED: Open the menu at <=1000px, click a work field and assert closed state, `aria-expanded=false`, original focus/value/node identity and no unintended save. Check same-active navigation, Escape, dialogs, resize/rail and repeated render handlers.
2. Implement a shared open/close helper, replacing the separate toggle/link/Escape mutations. Outside click and desktop breakpoint changes call the same helper. Keep listener registration outside render; preserve original map click coordinates before any drawer layout change.
3. GREEN: Run the focused test and existing PC theme/design behavior regressions appropriate to the changed shell. Check 1000/1001px, large text/dark theme, keyboard focus and no console errors or new business writes from dismissal.
4. Independently review the narrow patch, record results, commit on a feature branch. No deployment or current22 shortcut/package change in this task.

## Verification

- RED: the original outside field click left `nav-open` and expanded=true. An early-capture-close variant also fails the real MapLibre target-state ordering check. Fractional client coordinates are rounded to browser input pixels in the fixture; numerical rounding alone is not treated as a product defect.
- GREEN: `npm run test:menu` passes19 checks with real temporary SQLCipher/HTTP/Chromium, including390/900/1000px,1001px/rail, dark/font17, original map coordinates, existing/new dialogs, Escape/IME, input focus/DOM/draft and zero business writes/page errors.
- Existing `tests/test_pc_theme.py` passes14 checks; `tests/test_pc_design.py --behaviors-only --label menu` passes all4 existing workflow checks with zero browser errors. Syntax, version/generated-data and whitespace checks pass. Screenshot and result evidence is under `D:/Temp/dg-pc-menu-dismiss-20261115/verified-final19`.
- Independent final review is GO; a separate fresh run passes19/19 with zero errors/writes (`D:/Temp/dg-pc-menu-review-20261115/pc/review-menu-final19.log`). The final two-phase implementation supersedes the earlier capture-only review.
- npm and the CI matrix include the regression. The next full CI matrix has55 E2E suites (60 total jobs), so release verification must derive/check the new count rather than reuse22's historical59.
- Existing22 EXE/ZIP and outer `AGENTS.md` hashes are unchanged; the installed shortcut still targets22 with `--port 8766`. No production DB or new EXE/live-site verification is included. This feature remains on `feat/pc-menu-dismiss-20261115` pending a requested release.
