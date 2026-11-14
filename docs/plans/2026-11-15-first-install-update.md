# First-install explicit update correction

During the authorized 0.14.19 design deployment, all 56 CI jobs and Pages succeeded. Live upgrade verification then exposed a pre-existing update edge case. Source 0.14.19 is `c2e95c0c7b3b1a7fe0a414e7c23ec45b2528767e`.

## Root cause

`web/core.js` snapshots `hadController` at module evaluation. On the initial document of a fresh installation it is false. Ignoring the first automatic `clients.claim()` is intentional, but a later explicit Update in that same document also fails the condition and never calls `reloadWhenSafe()`. The update bar remains at the saving message even though writes have settled.

Independent real-SW diagnostics with actual 18/19 Git assets confirmed: first-install document receives the controller changes with all write queues clear but performs no reload; a document fully reloaded under the old controller performs one automatic reload and reaches the new PIN/version. Evidence: `D:/Temp/dg-release-01419-20261115/live/update-diagnostic/`. The failed public attempt was closed by its harness and is not claimed as a successful continuous upgrade.

## Minimal correction and ownership

- Update owner: `web/core.js`, new `tests/test_mobile_update.py` only.
- Root: release versions/manual/operations notes, npm/CI registration, integration, release artifacts and evidence.
- Track an explicit update request only after unlock, `settleWrites()` and existing pending-write guards pass. On controller change, permit the existing safe-reload path for either an initially controlled document or a successful explicit request.
- Keep first installation's automatic claim from reloading an input document. Keep lock, pending photos/quantity/save errors, encrypted storage and event contracts unchanged.

## Verification and release

1. Add real service-worker fixture regression: fresh-document automatic claim preserves input; later explicit update reloads to PIN/new fixture version; controlled-document update works; lock/pending-photo guards reject update; records/preferences persist.
2. Observe RED against existing source, implement the minimum flag/condition change, then GREEN. Fixture version transitions are not public-release proof.
3. Release correction as 0.14.20 rather than changing the already published 19 source under the same cache version. Preserve the successful 19 CI/package evidence and mark it superseded.
4. Run focused update/mobile/lock checks, source review, a fresh versioned desktop build and full CI/Pages gates. Verify public 19→20 through a real old client if possible, new cached code and offline PIN. Clearly separate initial failed continuous-18 evidence, synthetic reproduction and later public checks.

No production data/process or existing release files are modified. The user's deployment authorization covers this necessary update correction; no new business feature is included.
