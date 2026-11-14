# Mobile-first visual design

User request: form a team of current UI-trend specialists and improve the design. Selected scope: mobile first, with shared visual rules carried into PC. Baseline: `eae5c41`, application 0.14.18.

## Team and findings

- Mobile field UX: 24 fictitious-data Chromium combinations. At 360px/XL, SOLO's Cancelled filter extends 30.22px outside its container. Primary action, tools, summary and filters compete through similar pill shapes and surface strength. The long search placeholder also obscures supported search modes.
- PC workflow UX: 14 actual temporary SQLCipher/browser views, no baseline layout observations or page errors. Keep the existing shell and data density. Selected help/KPI labels are only 11–12px at the default 15px preference; common controls use inconsistent visual treatments.
- Accessibility/design system: current key text and semantic colors already meet AA contrast. Improve hierarchy and reading size before changing the palette. Preserve high contrast, keyboard focus, reduced motion and existing text-size preferences.

Evidence: `D:/Temp/dg-ui-design-20261114/{mobile,pc,accessibility}/`. Static-component contrast measurements are distinct from actual-screen/browser evidence.

## Considered directions

| Direction | Benefit | Trade-off |
| --- | --- | --- |
| Clear public-service workbench (selected) | Readable semantic text, distinct primary action, calm grouped surfaces and visible filters | Moderate visual change within current workflows |
| Strong expressive shapes and colored panels | Stronger visual identity and emphasis | More visual weight in already dense field/office screens |
| Existing layout with isolated touch-ups | Smallest diff | Leaves the competing pill/surface hierarchy largely intact |

## Selected design

- Keep green/yellow/red/blue status meanings and each platform's existing brand. Use neutral surface levels, a 4px spacing rhythm, restrained borders and role-specific corner radii.
- Mobile: one clear primary action; quieter two-column tools; compact progress summary in the same position in PC/SOLO home; visible state filters without sideways hunting, including XL. Keep every existing alert and safety badge.
- Give list search a visible label and short placeholder/help. Preserve its accessible name and search behavior. Raise critical item/quantity/address text using relative sizes so all four text-size choices still work; metadata stays secondary.
- PC: keep the current shell, table density, local Pretendard GOV and 36px regular/30px compact mouse controls. Clarify control state and selected/primary roles, selectively raise key helper labels, and support 44px targets only for coarse pointers. Preserve compact-layout constraints and print output.
- Keep content surfaces opaque and motion restrained. Avoid new fonts, libraries, network dependencies or a new theme selector.

These are project-specific applications of current [Material 3](https://m3.material.io/), [Android content structure](https://developer.android.com/design/ui/mobile/guides/layout-and-content/content-structure), [Fluent layout](https://fluent2.microsoft.design/layout), [Fluent color](https://fluent2.microsoft.design/color), [KRDS typography](https://www.krds.go.kr/html/site/style/style_03.html) and [Apple materials](https://developer.apple.com/design/human-interface-guidelines/materials). The UI/UX Pro Max script/data pointers have no installed target; its readable skill guidance and official sources supply the design review instead.

## Boundaries

Preserve drafts, filters, list return/focus, Korean labels, personal worker, lock policy, retry/cancel/error handling, date/quantity/event/protocol/store contracts and PC authorization. This is a local design implementation, with no version bump, deployment, package/shortcut changes or production data/process access. Preserve the existing root AGENTS.md.
