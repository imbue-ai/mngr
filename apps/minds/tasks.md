# Minds Desktop App - Installation UX Tasks

## Code Signing (blocking for public distribution)

**Status:** Not configured

**Problem:** The ToDesktop build produces an ad-hoc signed app (`Signature=adhoc`, `TeamIdentifier=not set`). macOS quarantines it and shows "minds.app is damaged and can't be opened."

**Workaround for testing:** Remove the quarantine flag after downloading:
```bash
xattr -cr /Applications/minds.app
```

**To fix properly:**
1. Obtain an Apple Developer certificate (requires Apple Developer Program membership, $99/year)
2. Upload the certificate to ToDesktop dashboard: https://app.todesktop.com/apps/26032588hqdzk > Certificates
3. ToDesktop will then sign and notarize builds automatically
4. Check if Imbue already has an Apple Developer account -- ask Josh

**References:**
- ToDesktop signing docs: https://www.todesktop.com/electron/docs/introduction/signing-application
- Apple Developer Program: https://developer.apple.com/programs/

## Cross-platform git binary bundling

**Status:** Broken for Windows/Linux

**Problem:** `scripts/build.js` copies the local macOS git binary (`which git`) into `resources/`. This macOS binary gets bundled into all platform builds, so Windows and Linux builds get a macOS git that can't run.

**Options:**
1. Move `build.js` into a `todesktop:beforeBuild` hook so it runs on each platform's build server
2. Download platform-specific portable git (like `build.js` already does for uv)
3. Use the `dugite` npm package (pre-built git binaries, used by GitHub Desktop)

## Backend startup failure (blocking -- app cannot start)

**Status:** Root cause identified

**Problem:** The packaged app runs `uv run --project <pyproject-dir> minds forward`, but `uv sync` fails because the Python packages aren't published to PyPI.

The standalone `electron/pyproject/pyproject.toml` declares:
```
dependencies = [
    "minds>=0.1.0",
    "imbue-mngr-claude>=0.2.0",
    "imbue-mngr-modal>=0.2.0",
]
```

The `[tool.uv.sources]` section (local monorepo paths) is correctly stripped by `build.js` for the packaged build. But without it, uv tries to fetch these packages from PyPI, where they don't exist. Result: `uv sync` fails, no venv is created, `minds` command not found, backend exits with code 2.

**Log evidence:** `~/.minds/logs/minds.log` shows:
```
error: Failed to spawn: `minds`
  Caused by: No such file or directory (os error 2)
```

**To fix:** Publish `minds`, `imbue-mngr-claude`, and `imbue-mngr-modal` to PyPI (or a private index). Alternatively, bundle the wheels directly in the Electron app resources and point uv at them with `--find-links`.

**Fix implemented:** Modified `build.js` to bundle all workspace Python packages into `resources/packages/` and rewrite `[tool.uv.sources]` paths. Tested locally -- `uv sync` succeeds and `minds --help` runs. Needs to be deployed via a new ToDesktop build to verify end-to-end.

## Electron frontend auth polling loop (non-blocking but noisy)

**Status:** Identified

**Problem:** The Electron frontend polls `GET /auth/api/status` in a tight loop (multiple times per second). This is a SuperTokens auth endpoint that only exists when `SUPERTOKENS_CONNECTION_URI` is configured. Without SuperTokens, the endpoint returns 404 on every request, flooding the log.

The one-time-code authentication (`/login?one_time_code=...`) works fine -- the session cookie is set. But the frontend doesn't know SuperTokens is disabled and keeps polling.

**Symptoms:**
- "Discovering agents..." stuck on screen
- Clicking "Login" gives `{"detail":"Not Found"}`
- Log shows hundreds of `GET /auth/api/status 404` entries

**Likely cause:** The Electron chrome/frontend expects SuperTokens auth flow. When running locally without SuperTokens, it falls into a polling loop checking auth status that never resolves.

**To investigate:** Ask Josh if the Electron app is expected to work without SuperTokens, or if SuperTokens is a required dependency for the packaged app.
