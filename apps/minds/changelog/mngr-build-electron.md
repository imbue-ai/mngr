Bundled-app runtime fixes for the standalone minds.app, carved out of #1317 so they can land independently of that PR's mngr_forward / e2e / spec work.

Electron runtime (`electron/`):
- `main.js`: the macOS About panel now shows the git SHA the build was cut from (appended to ToDesktop's buildId), so a shipped binary maps back to a commit. DevTools auto-open is gated behind `MINDS_OPEN_DEVTOOLS=1` (the built-in cmd+opt+I shortcut crashes under BaseWindow + WebContentsViews).
- `backend.js`: spawn `uv run --active` with `VIRTUAL_ENV` pointed at the per-user venv (`~/.minds/.venv`) instead of `<project>/.venv`, which lives inside the signed, read-only `.app` bundle and fails with "Operation not permitted" on macOS. Pass `MINDS_RESTIC_BINARY`, append `/opt/homebrew/bin` and `/usr/local/bin` to PATH (LaunchServices-started apps inherit a minimal PATH), and run the backend with `-v` so INFO-level lifecycle logs land in `minds.log`.
- `env-setup.js`: `uv sync --active` plus an explicit `--reinstall-package` for every workspace package, so an upgrade actually re-extracts our freshly-built wheels (their PEP 440 version is unchanged across releases, so uv otherwise treats them as already-installed and the user keeps running stale code).
- `paths.js`: add `getResticPath()`.
- `pyproject/pyproject.toml`: list every bundled workspace package as a direct dependency with matching `[tool.uv.sources]` overrides (uv ignores source overrides for transitive-only packages and silently pulls stale PyPI versions).

Build pipeline (`scripts/`):
- `download-binaries.js`: download a per-platform `restic` binary (pinned, checksum-verified) into `resources/restic/`.
- `ensure-binaries.js` (new): `prestart` hook that downloads only the missing bundled binaries for dev `pnpm start`.
- `build.js`: download restic, build each workspace package to a wheel and rewrite the runtime pyproject + lockfile against those wheels, bake the git SHA into `build-info.json`, and bundle latchkey via `pnpm deploy --prod` (lockfile-pinned).
- `build_test.py`: `test_workspace_package_lists_are_consistent` drift guard asserting the four copies of the workspace-package list agree; wheels-are-pure-Python / exclude-test-files acceptance checks; node-missing skip for the todesktop entitlement tests.

Packaging:
- `package.json`, `pnpm-workspace.yaml` (cross-platform `supportedArchitectures`), `pnpm-lock.yaml`, `todesktop.js` (sign the bundled restic), `.gitignore` (`electron/build-info.json`).

Runtime restic discovery:
- `desktop_client/restic_cli.py`: resolve restic from `MINDS_RESTIC_BINARY` (the bundled binary) before falling back to a PATH lookup.
- `conftest.py`: point `MINDS_RESTIC_BINARY` at the bundled binary when present so tests don't need a system-wide restic install.
