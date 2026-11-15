# Desktop operations and LAN connection checks

**Goal:** Add operational diagnostics and secure same-network access configuration/testing to the PC launcher. The user selected other PCs on the same institution/Wi-Fi network. Baseline `5fe6a12`; development branch `feat/desktop-operations-20261115`, version21 until a requested release.

**Architecture:** Reuse existing LAN HTTPS/CIDR/Host/CSRF/authentication rules in `dgpc.server`. Preserve local-only default and explicit `--port 8766` used by this installation. No current production settings/server/state changes, firewall/router automation or public Internet hosting. Keep new work in source and an isolated preview package; preserve deployed21 and its shortcut.

Considered: read-only diagnostics plus manual CLI LAN configuration; integrated secure LAN profiles with peer checker (selected); managed remote hosting (outside the selected scope).

## Product behavior

1. Keep the main server status and primary work button. In details, show actual mode/address/port, copy buttons, separate diagnostic status/time, selected data/log folder and disk capacity. Existing-server reuse must say its active configuration/data folder is not established by this launcher.
2. Add connection settings: local-only or same-network HTTPS, port, bind IPv4 address, advertised DNS/IP name, allowed CIDRs, certificate/key file paths. Retain state folder and unknown existing configuration fields. CLI options override saved settings. Private key contents, DB keys and login credentials are never saved in configuration/report/arguments.
3. Apply changed connection settings only when stopped/error, not owning or reusing a server and not busy. Validate first, save atomically, then close the old launcher normally and relaunch after releasing its port mutex. Never mutate `controller.port` under an old mutex. Recreate optional shortcuts with the selected profile; preserve existing explicit ports.
4. LAN must retain TLS>=1.2, normal client certificate/hostname verification, allowed-client networks, exact Host checking and Secure/HttpOnly/SameSite cookies. No verification bypass, unrestricted CIDR, automatic certificate trust installation or plaintext external listener. Check TLS files/key pairing/validity before DB unlock/start. `0.0.0.0` is a bind address, never a displayed client URL. Institution-trusted certificate/name/network setup remains required for live use.
5. A local probe proves only this device's connection to a server. Always keep other-device reachability unconfirmed in the launcher. Provide a separate **connection checker** mode/window that another PC can run using the same package; it performs read-only HTTPS/identity/version/entry-assets checks without opening DB, keyring, business server or launcher mutex. Display the observing-device scope, with TLS/access/identity/assets failures distinguished. No authentication or resident data in the checker.
6. Diagnostics run in daemon workers with controller/epoch ownership, bounded network requests and separate result/error text; late results cannot overwrite a changed server/profile or closed view. Capture selected-folder accessibility and volume free bytes using `shutil.disk_usage`, not SQLite freelist bytes. Do not open the DB or read raw logs for reports.
7. Reports use an explicit metadata allowlist: app version, platform/architecture, configured mode/port, safe URLs, timestamps, fixed result codes and volume capacity. Exclude raw logs, environment, private paths, key/certificate contents, passwords/cookies and resident data. Preview/save are user actions; opening the log folder opens only the selected local folder. Browser-open False/errors produce a clear copyable URL and retry guidance.

## Interfaces and ownership

### Runtime owner

Own `pc/desktop_connection.py`, profile-aware portions of `pc/desktop_runtime.py`, and `tests/test_desktop_connection.py`.

- `ConnectionProfile`: immutable `mode`, `port`, `host`, `hostname`, `cert`, `key`, `allow`; `from_dict`, `to_dict`, `url`, and safe advertised URL. Default local preserves existing constructor behavior.
- `load_profile()`, `resolve_profile(options)`, `save_profile(profile)`, `validate_profile(profile)`, `profile_arguments(profile)`; preserve legacy state-only JSON and CLI precedence. Publish final signatures before integration.
- `probe_connection(profile)` / `probe_url(url)` return fixed `code/message` metadata; reject unsafe URL structure, cross-origin redirects and TLS verification bypass. Keep old `_app_health` return contracts compatible. HTTPS fixture trust may be injected only in tests, not disabled.
- `DesktopController(..., connection=profile)`, worker arguments and `child_main(..., connection=profile)` propagate the full profile. Validate before credentials/DB, reuse existing `server.lan_config`/`serve`, preserve nonce/control pipe/shutdown and unknown-existing-server semantics. Preserve profile configuration on `save_state_choice`.
- RED→GREEN: local compatibility/8766 precedence, profile persistence, invalid/no TLS/all-address CIDRs, trusted/untrusted/mismatched/expired certificates, actual TLS server-child, wrong-service/version/assets, no keys/DB on checker/preflight failure, existing-server ownership and shutdown.

### UI owner

Own `pc/desktop_view.py`, `pc/desktop_check.py`, `tests/test_desktop_operations_ui.py`. Preserve existing view callbacks/signatures, geometry, focus, scroll, tray and primary actions.

- Optional callbacks: `connection_settings`, `diagnose`, `copy_url`, `open_logs`, `save_report`, `connection_checker` (use `.get` defaults for old fixtures).
- `view.set_connection(info)` accepts `mode_label`, `url`, `peer_url`, `note`; `view.set_diagnostics(text, running=False)` and `view.set_diagnostic_actions(...)` update only operational controls/results.
- `edit_connection_profile(root, values, validate)` returns a validated profile dict or None. Validate callback returns None or a user-facing error string. Save button explicitly says save/relaunch. Inputs retain drafts on validation failure; settings cannot change a running server.
- `connection_check_main(initial_url='')` is an independent Tk checker; root routes `--check-connection` here before state/profile/mutex/controller/key imports. Reuse runtime probe helpers, worker/epoch guards, copy/save sanitized results. Explain that a same-device result does not prove another device can connect. No server launch or DB/key access.
- RED→GREEN and real Tk fixture checks: old callbacks/state transitions, new status/controls, 680×480 and560×400/scaling, keyboard/focus, checker errors/retry/close-late results, forbidden side effects.

### Root integration

Own `pc/desktop.py`, `pc/desktop_diagnostics.py`, focused diagnostics/integration tests, package/checker entry helper in `tools/build_desktop.py`, npm/CI and manuals. Parse checker mode before opening state/key/mutex. Resolve profile and preserve old mock Controller/View fixtures where feasible. Capture restart request, release old mutex in normal cleanup, then spawn with explicit profile/state. Do not restart on existing/busy/owned state.

Use immutable diagnostic snapshots and existing operation epoch in queue messages. Only accepted current results populate reports. Add a packaged `PC 접속 점검.cmd` that launches the same EXE's checker-only mode, with no server/data initialization. No system setting changes. Record a safe report and guide for checking from the other PC.

## Verification and completion

Run new profile/TLS/diagnostics/UI tests plus existing desktop paths/lifecycle/app/tray/UI, startup/launcher and version/generated checks. Extend CI for new suites and reuse Windows package checks. Independently review source/security after freeze. Build under `dist/desktop/preview-ops-20261115` with fake state/free ports and verify manifest/privacy/packaged checker/server behavior. Do not overwrite21 or retarget the production shortcut. Distinguish native/source fixtures, packaged server and actual other-device LAN evidence; the last requires real certificate/network/device configuration and must remain unconfirmed until performed.
