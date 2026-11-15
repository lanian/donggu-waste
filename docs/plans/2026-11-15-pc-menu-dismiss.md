# PC menu outside-dismiss implementation plan

**Goal:** Close the narrow-window PC business menu when the user clicks outside it, retaining the clicked control's normal action and input state. Wide-screen fixed/rail navigation remains available.

**Design:** Use a window click capture listener after the click target is established; avoid pointerdown dismissal because closing the drawer changes the header's height. Update only the navigation flag/class/expanded attribute through one helper, without rendering the workspace. No backdrop or form/navigation interception is added. Ignore open application dialogs; Escape also respects handled keys and IME. Restore focus only when it would otherwise stay inside the hidden navigation. Crossing to the desktop breakpoint clears temporary open state while preserving the user's rail preference.

**Files:** `pc/ui/pc.js`; focused real-server Playwright regression `tests/test_pc_menu_dismiss.py`; register that test in npm/CI. No backend, data schema, native launcher, mobile PWA or production installation changes. Use fictitious accounts and temporary state/free ports.

## Steps

1. RED: Open the menu at <=1000px, click a work field and assert closed state, `aria-expanded=false`, original focus/value/node identity and no unintended save. Check same-active navigation, Escape, dialogs, resize/rail and repeated render handlers.
2. Implement a shared open/close helper, replacing the separate toggle/link/Escape mutations. Outside click and desktop breakpoint changes call the same helper. Keep listener registration outside render.
3. GREEN: Run the focused test and existing PC theme/design behavior regressions appropriate to the changed shell. Check 1000/1001px, large text/dark theme, keyboard focus and no console errors or new business writes from dismissal.
4. Independently review the narrow patch, record results, commit on a feature branch. No deployment or current22 shortcut/package change in this task.
