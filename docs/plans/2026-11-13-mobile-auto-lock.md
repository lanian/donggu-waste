# Mobile Auto Lock Implementation Plan

**Goal:** 모바일 자동 잠금을 켜고 끄며 기존 PIN·암호화·업무 자료를 보존한다.

**Approved:** User selected "자동 잠금만 끄기 — 앱 재실행 시 PIN 유지". Toggle only idle and background-return auto locking. Keep manual lock, startup PIN/biometrics, other-tab locking, hidden-screen veil, camera cleanup and pending-write settlement. User was informed that the verified update will be deployed to the existing mobile site.

**Design:** Store a nonsecret, device-specific `auto-lock` preference separately from encrypted business state, like existing UI preferences. Missing/invalid values default to enabled. A write must commit successfully before runtime changes; failed writes retain the previous effective setting. Guard against storage reset and concurrent changes. Reset activity timing when switching so enabling after a long inactive period does not immediately lock. Preserve the selected 5/10/15 minute interval; disable interval controls while off or saving.

The existing `commit()` converts failure to false and preserves pending encrypted state. Keeping this device preference out of business state avoids restoring an unsuccessful toggle through pending record recovery and avoids changing intake/events/photos. It does not store a PIN or unwrapped key. Backups retain business data; automatic lock policy remains a local device preference.

## Steps

1. [x] Add RED actual mobile browser tests: default/legacy enabled; toggle persisted; idle/background off; veil retained; manual/reload PIN preserved; reenable timing; save failure/retry; pending toggle across settings navigation preserving other inputs; alternate-tab protection. Use real PIN/IndexedDB and synthetic time/visibility only to avoid waiting minutes.
2. [x] Add guarded preference accessors in `web/store.js`, runtime setter/status and two condition checks in `web/core.js`; add an accessible switch in `web/screens/settings.js`. Preserve other settings and the encrypted state contract.
3. [x] Run focused regression plus current mobile workflow, cleanup and calculator/offline flows. Obtain read-only review, inspect 390px settings screenshots and failure messages.
4. [ ] Register new check, update guide and synchronize 0.14.15 versions. Build/verify the corresponding desktop artifact if shared release version requires it. Commit/push the verified update and wait for required CI/Pages; verify actual published files, switch, persistence and startup PIN. Keep production app/state and AGENTS.md unchanged.

**Evidence:** `D:/Temp/dg-auto-lock-20261113`.

## Validation

- Missing switch RED confirmed before implementation. Actual PIN/IndexedDB test passes default ON, persisted OFF, idle/background skipping, veil, manual/restart PIN, save failure, delayed acknowledgement across navigation with another settings draft, fresh reenable timing, alternate-tab lock, invalid preference fallback and store wipe during an old toggle.
- Settings save uses a raw device preference and only changes effective policy after durable success; no business-state `commit()` or pending business ciphertext is changed. Independent review reports no Important issue. 390px control screenshot inspected.
- Final integration passes all 186 existing mobile E2E checks, the eleven new collection-save cases and the automatic-lock regression against the frozen collection source. Coverage includes virtual WebAuthn/PRF, pending photos and pending quantity before locking; logs are `e2e-final.log`, `collection-final.log` and `lock-final.log` in the evidence directory.
- Calculator source/real service-worker offline and npm fee/interoperability/protocol checks pass. Version/precache/generated-file checks pass for 0.14.15. Desktop 0.14.15 build succeeds; ZIP SHA256 `60dd9aaec69b62122aaaf422785290a96204102a6f83177ff3f3e2894f04200b`.
- First CI run exposed a fixture race after storage wipe: the hash reset renders PIN before the following full reload, destroying a raw-read evaluation. The test now registers a real `load` event before wiping, then retains PIN/read assertions. The focused test and independent review pass; product code is unchanged.
