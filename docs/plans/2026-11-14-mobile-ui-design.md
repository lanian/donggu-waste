# Mobile Button and UI Design

**Approved:** User requested mobile button/UI improvements and replied "구현" to the proposed home, collection and settings design.

## Direction

Keep the existing green field-work interface and Korean terminology. Retain the single "지금 할 일" primary action; group frequently used home tools into two columns so the visit list is easier to reach. Preserve all existing navigation handlers and warning cards.

On collection details, distinguish the primary result/next action from phone and navigation tools. Label the result-section jump honestly, preserve real save/retry semantics and prior/cumulative quantity rules, and keep large quantity controls readable at small widths and large text sizes.

Put screen, lock and worker settings near the top. Improve selected, disabled, saving and error visibility, including dark-theme error contrast. Keep startup PIN, auto-lock preference, business events, encryption and PC input-help guards unchanged.

## Responsive and Accessibility Checks

Check 360, 390 and 430 CSS-pixel widths with normal and largest text, light/dark/high-contrast themes, keyboard focus and safe-area insets. Controls must remain reachable without horizontal page overflow; fixed bars and errors must not cover essential navigation or result content. Use existing local icons/fonts and CSS variables; add no external dependency.

## Boundaries

Mobile source/UI and focused browser regressions only. Preserve `AGENTS.md`, production state, running desktop processes, API/protocol/payload contracts and existing release versions. Implement and verify locally; publishing is a separate release action.
